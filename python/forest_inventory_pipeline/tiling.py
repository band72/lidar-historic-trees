"""
Processing a point cloud too large for memory as a grid of overlapping square tiles.

Each tile is a *core* cell of ``size`` m plus an *overlap* buffer on every side. The stages
(ground, heights, segmentation, fit) run on the buffered tile; a tree is kept only by the
tile whose core contains its stem centre. Cores are half-open, ``[xmin, xmax) x [ymin, ymax)``,
and the outer tiles also own everything beyond their outer edges (a stem centre can be fitted
just outside the points' extent), so every location has exactly one owner: trees are neither
lost nor doubled at tile edges.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .errors import ParameterError

MIN_SIZE = 20.0
MIN_OVERLAP = 5.0
DEFAULT_OVERLAP = 10.0

Box = tuple[float, float, float, float]  # xmin, ymin, xmax, ymax (m)


@dataclass(frozen=True)
class Tile:
    row: int
    col: int
    core: Box
    buffered: Box
    last_row: bool
    last_col: bool

    @property
    def name(self) -> str:
        return f"r{self.row}c{self.col}"

    def owns(self, x: float, y: float) -> bool:
        """Whether a stem at (x, y) belongs to this tile (each location has one owner)."""
        x0, y0, x1, y1 = self.core
        in_x = (self.col == 0 or x >= x0) and (self.last_col or x < x1)
        in_y = (self.row == 0 or y >= y0) and (self.last_row or y < y1)
        return in_x and in_y


def plan_tiles(extent: Box, size: float, overlap: float = DEFAULT_OVERLAP) -> list[Tile]:
    """
    Square tiles of ``size`` m covering ``extent``, aligned to its minimum corner, row-major
    (rows along y). Each is processed over its core grown by ``overlap`` m on every side.

    :raises ParameterError: size < 20 m, overlap < 5 m, or overlap >= size / 2.
    """
    if size < MIN_SIZE:
        raise ParameterError(f"tile size must be at least {MIN_SIZE:.0f} m, got {size:g} m")
    if overlap < MIN_OVERLAP:
        raise ParameterError(f"tile overlap must be at least {MIN_OVERLAP:.0f} m, got {overlap:g} m")
    if overlap >= size / 2:
        raise ParameterError(
            f"tile overlap ({overlap:g} m) must be less than half the tile size ({size:g} m)"
        )
    xmin, ymin, xmax, ymax = (float(v) for v in extent)
    ncol = max(1, math.ceil((xmax - xmin) / size))
    nrow = max(1, math.ceil((ymax - ymin) / size))
    tiles = []
    for r in range(nrow):
        for c in range(ncol):
            x0, y0 = xmin + c * size, ymin + r * size
            core = (x0, y0, x0 + size, y0 + size)
            buffered = (x0 - overlap, y0 - overlap, x0 + size + overlap, y0 + size + overlap)
            tiles.append(Tile(r, c, core, buffered, r == nrow - 1, c == ncol - 1))
    return tiles


# --- extent and crop boxes ---------------------------------------------------------------------


def cloud_extent(paths, units: str = "auto") -> tuple[Box, str] | None:
    """
    The XY extent of the input file(s) in metres and their unit, from LAS/LAZ headers only
    (no points are read). None when any input is not LAS/LAZ: those formats have no cheap
    extent and are loaded once, then sliced.

    Units follow ``load_cloud``: an explicit ``units`` beats the header; files that declare
    different units, or some declaring and some not, are refused.
    """
    from .crs import from_las_header
    from .errors import InputDataError
    from .inventory import expand_inputs
    from .units import normalize_unit, to_metres

    files = expand_inputs(paths)
    if not files or any(p.suffix.lower() not in (".las", ".laz") for p in files):
        return None
    import laspy

    mins, maxs, declared = [], [], set()
    for p in files:
        try:
            with laspy.open(str(p)) as fh:
                h = fh.header
                mins.append(h.mins[:2])
                maxs.append(h.maxs[:2])
                declared.add(from_las_header(h).units)
        except Exception as exc:
            raise InputDataError(f"{p.name}: cannot read the LAS/LAZ header: {exc}") from exc
    if units not in (None, "", "auto"):
        unit = normalize_unit(units)
    else:
        if len(declared) > 1:
            raise InputDataError(
                f"input files declare different units ({sorted(str(u) for u in declared)}); "
                "process them separately or set the units explicitly (--units)"
            )
        unit = declared.pop() or "m"
    lo, hi = [min(m[i] for m in mins) for i in (0, 1)], [max(m[i] for m in maxs) for i in (0, 1)]
    ext = tuple(float(to_metres(v, unit)) for v in (lo[0], lo[1], hi[0], hi[1]))
    return ext, unit  # type: ignore[return-value]


def input_metadata(paths) -> dict | None:
    """
    CRS, CRS WKT and source names of LAS/LAZ input(s), from the first header (no points are
    read); None for other formats. Units come from ``cloud_extent``.
    """
    from .crs import from_las_header
    from .inventory import expand_inputs

    files = expand_inputs(paths)
    if not files or any(p.suffix.lower() not in (".las", ".laz") for p in files):
        return None
    import laspy

    with laspy.open(str(files[0])) as fh:
        info = from_las_header(fh.header)
    return dict(crs=info.crs, crs_wkt=info.wkt, sources=[str(p) for p in files])


def crop_box(tile: Tile, unit: str) -> str:
    """The tile's buffered box as ``xmin,ymin,xmax,ymax`` in ``unit``, for ``load_cloud(crop=)``."""
    from .units import from_metres

    return ",".join(f"{float(from_metres(v, unit)):.4f}" for v in tile.buffered)


# --- running the stages tile by tile -------------------------------------------------------


def run_tiled(
    inputs,
    size: float,
    overlap: float,
    gp,
    sp,
    fp,
    *,
    reader: str | None = None,
    units: str = "auto",
    keep_points: bool = False,
    keep_noise: bool = False,
    reader_options: dict | None = None,
    cloud=None,
):
    """
    Run ``inventory.process_cloud`` on every buffered tile and assemble one result.

    LAS/LAZ inputs are cropped to each tile while reading (peak memory: one buffered tile);
    other formats are loaded once and sliced. A tile keeps only the records whose (x, y) it
    owns; ``dedupe_stems`` then removes a stem fitted twice across a core edge. Tiles with no
    points or no tree clusters are skipped, with the reason in their summary.

    :param cloud: an already loaded cloud (``inventory.LoadedCloud``) for formats that cannot
        be cropped while reading, so the caller and this function do not both load it.
    :return: (records with global ids 1..N and ``extra["tile"]``, ``extra["tile_tree_id"]``;
        one summary dict per tile: name, core, points, trees, seconds, skipped)
    """
    import time


    from . import inventory as inv
    from .errors import InputDataError, StageError
    from .utils import logger

    reader_options = reader_options or {}
    probe = None if (reader or cloud is not None) else cloud_extent(inputs, units)
    whole = cloud
    if probe is not None:
        extent, unit = probe
    else:
        logger.info(
            "this format cannot be cropped while reading: the whole cloud is loaded once, "
            "then processed tile by tile (ground filtering and segmentation still per tile)"
        )
        if whole is None:
            whole = inv.load_cloud(
                inputs, reader=reader, units=units, keep_noise=keep_noise, **reader_options
            )
        lo, hi = whole.xyz[:, :2].min(axis=0), whole.xyz[:, :2].max(axis=0)
        extent = (float(lo[0]), float(lo[1]), float(hi[0]), float(hi[1]))
    tiles = plan_tiles(extent, size, overlap)
    logger.info(f"{len(tiles)} tiles of {size:g} m with {overlap:g} m overlap")

    kept, summaries, errors = [], [], []
    for t in tiles:
        t0 = time.perf_counter()
        info = dict(name=t.name, core=[round(v, 3) for v in t.core], points=0, trees=0,
                    seconds=0.0, skipped="")
        summaries.append(info)
        try:
            if whole is None:
                cloud = inv.load_cloud(
                    inputs, reader=reader, units=units, crop=crop_box(t, unit),
                    keep_noise=keep_noise, **reader_options,
                )
                xyz, cls = cloud.xyz, cloud.classification
            else:
                x0, y0, x1, y1 = t.buffered
                x, y = whole.xyz[:, 0], whole.xyz[:, 1]
                m = (x >= x0) & (x <= x1) & (y >= y0) & (y <= y1)
                if not m.any():
                    raise InputDataError("no points to process in this tile")
                xyz = whole.xyz[m]
                cls = whole.classification[m] if whole.classification is not None else None
            info["points"] = int(len(xyz))
            records = inv.process_cloud(xyz, cls, gp, sp, fp, keep_points=keep_points)
        except (InputDataError, StageError) as exc:
            # a tile holding only an edge of the cloud (a strip of ground, a few points) is not
            # usable on its own; a broken file fails every tile, which is raised below
            info["skipped"] = str(exc)
            errors.append(exc)
        info["seconds"] = round(time.perf_counter() - t0, 1)
        if info["skipped"]:
            logger.info(f"tile {t.name}: skipped ({info['skipped']})")
            continue
        own = [r for r in records if t.owns(r.x, r.y)]
        for r in own:
            r.extra["tile"] = t.name
            r.extra["tile_tree_id"] = int(r.tree_id)
        kept += own
        logger.info(
            f"tile {t.name}: {info['points']:,} points, {sum(r.fitted for r in own)} trees "
            f"measured of {len(own)} clusters in its core, {info['seconds']:.0f} s"
        )

    if errors and len(errors) == len(tiles):
        raise errors[0]  # nothing usable anywhere: the input, not the tiling, is the problem
    # global ids before dedupe (it tracks records by id, and tile ids repeat), then again after
    for i, r in enumerate(kept, start=1):
        r.tree_id = i
    kept = inv.dedupe_stems(kept)
    for i, r in enumerate(kept, start=1):
        r.tree_id = i
    counts: dict[str, int] = {}
    for r in kept:
        counts[r.extra["tile"]] = counts.get(r.extra["tile"], 0) + 1
    for info in summaries:
        info["trees"] = counts.get(info["name"], 0)
    return kept, summaries
