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
    file_boxes: list[tuple] = []
    if probe is not None:
        extent, unit = probe
        file_boxes = _file_boxes(inputs, unit)
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

    results, summaries = [], []
    for t in tiles:
        t0 = time.perf_counter()
        info = dict(name=t.name, core=[round(v, 3) for v in t.core], points=0, trees=0,
                    seconds=0.0, skipped="")
        summaries.append(info)
        try:
            if whole is None:
                # only the files that reach into this tile: a file with no points in the crop
                # box makes the reader fail, which would drop the whole tile
                files = [f for f, box in file_boxes if _overlaps(box, t.buffered)]
                if not files:
                    raise InputDataError("no points to process in this tile")
                tile_cloud = inv.load_cloud(
                    files, reader=reader, units=units, crop=crop_box(t, unit),
                    keep_noise=keep_noise, **reader_options,
                )
                xyz, cls = tile_cloud.xyz, tile_cloud.classification
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
            if not _empty_tile(exc):
                # out of memory, a read error, a units/CRS conflict: never a partial result
                hint = (
                    "; the tile is too large for memory: use a smaller --tile"
                    if "MemoryError" in str(exc) else ""
                )
                raise type(exc)(*_error_args(exc, f"tile {t.name}: ", hint)) from exc
            info["skipped"] = str(exc)
        info["seconds"] = round(time.perf_counter() - t0, 1)
        if info["skipped"]:
            logger.info(f"tile {t.name}: skipped, nothing to measure ({info['skipped']})")
            continue
        results.append((t, records))
        logger.info(
            f"tile {t.name}: {info['points']:,} points, {len(records)} clusters, "
            f"{info['seconds']:.0f} s"
        )
    if not results:
        raise InputDataError(
            "no tile has anything to measure: " + "; ".join(
                f"{i['name']}: {i['skipped']}" for i in summaries[:5]
            )
        )
    kept = assemble(results)
    counts: dict[str, int] = {}
    for r in kept:
        counts[r.extra["tile"]] = counts.get(r.extra["tile"], 0) + 1
    for info in summaries:
        info["trees"] = counts.get(info["name"], 0)
    return kept, summaries


#: messages of tiles that simply hold nothing to measure (an edge of the cloud, open ground)
_EMPTY_TILE = (
    "no points to process",
    "contains no valid points",
    "a forest plot needs far more",
    "vertical extent is only",
    "every point was classified as ground",
    "ground points found",
    "points between",
    "points after voxel_size",
    "no tree instances found",
)


def _empty_tile(exc: Exception) -> bool:
    return "MemoryError" not in str(exc) and any(m in str(exc) for m in _EMPTY_TILE)


def _error_args(exc: Exception, prefix: str, suffix: str) -> tuple:
    from .errors import StageError

    if isinstance(exc, StageError):
        return (exc.stage, prefix + str(exc).split("] ", 1)[-1] + suffix)
    return (prefix + str(exc) + suffix,)


def _file_boxes(inputs, unit: str) -> list[tuple]:
    """(file, XY box in metres) of every LAS/LAZ input, from the headers."""
    import laspy

    from .inventory import expand_inputs
    from .units import to_metres

    out = []
    for p in expand_inputs(inputs):
        with laspy.open(str(p)) as fh:
            lo, hi = fh.header.mins, fh.header.maxs
        out.append((p, tuple(float(to_metres(v, unit)) for v in (lo[0], lo[1], hi[0], hi[1]))))
    return out


def _overlaps(a: Box, b: Box) -> bool:
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


#: a tile also keeps records this far outside its core (m): each tile places an edge stem a
#: little differently (its own ground model), so both could put it in the other's core
EDGE_MARGIN = 0.5


def assemble(results, margin: float = EDGE_MARGIN):
    """
    One result from the tiles' records: each tile keeps the records within ``margin`` of its
    core; records of different tiles closer than max(margin, either fitted radius) are one
    stem, of which one is kept (the one its own tile owns, then a measured one, then the
    better supported); ``dedupe_stems`` then removes overlapping stems; ids become 1..N.

    :param results: [(Tile, records of that tile)] with tile-local ids.
    """
    import math

    from . import inventory as inv

    cands = []
    for t, records in results:
        x0, y0, x1, y1 = t.core
        for r in records:
            if not (math.isfinite(r.x) and math.isfinite(r.y)):
                continue
            inside = (t.col == 0 or r.x >= x0 - margin) and (t.last_col or r.x < x1 + margin) and (
                t.row == 0 or r.y >= y0 - margin) and (t.last_row or r.y < y1 + margin)
            if not inside:
                continue
            r.extra["tile"] = t.name
            r.extra["tile_tree_id"] = int(r.tree_id)
            support = r.n_band_points * r.inlier_ratio if r.fitted else 0.0
            cands.append((not t.owns(r.x, r.y), not r.fitted, -support, len(cands), r))
    cands.sort(key=lambda c: c[:4])
    kept = []
    for *_, r in cands:
        reach = max(margin, r.dbh / 2 if r.fitted else 0.0)
        dup = any(
            k.extra["tile"] != r.extra["tile"]
            and math.hypot(k.x - r.x, k.y - r.y) < max(reach, k.dbh / 2 if k.fitted else 0.0)
            for k in kept
        )
        if not dup:
            kept.append(r)
    kept.sort(key=lambda r: (r.extra["tile"], r.extra["tile_tree_id"]))
    # global ids before dedupe (it tracks records by id, and tile ids repeat), then again after
    for i, r in enumerate(kept, start=1):
        r.tree_id = i
    kept = inv.dedupe_stems(kept)
    for i, r in enumerate(kept, start=1):
        r.tree_id = i
    return kept
