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
