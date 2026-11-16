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


def crop_box(tile: Tile, unit: str) -> str:
    """The tile's buffered box as ``xmin,ymin,xmax,ymax`` in ``unit``, for ``load_cloud(crop=)``."""
    from .units import from_metres

    return ",".join(f"{float(from_metres(v, unit)):.4f}" for v in tile.buffered)
