"""Tiled processing of large clouds: the grid, ownership, extents, and tiled vs untiled runs."""

import numpy as np
import pytest

from forest_inventory_pipeline import tiling
from forest_inventory_pipeline.errors import ParameterError


def test_grid_covers_extent():
    tiles = tiling.plan_tiles((0, 0, 250, 120), size=100, overlap=10)
    assert [(t.row, t.col) for t in tiles] == [(0, 0), (0, 1), (0, 2), (1, 0), (1, 1), (1, 2)]
    assert tiles[0].core == (0, 0, 100, 100) and tiles[0].buffered == (-10, -10, 110, 110)
    assert tiles[-1].core == (200, 100, 300, 200)
    assert tiles[4].name == "r1c1"


def test_every_point_has_exactly_one_owner():
    tiles = tiling.plan_tiles((0, 0, 250, 120), size=100, overlap=10)
    rng = np.random.default_rng(0)
    pts = np.vstack([rng.uniform([0, 0], [250, 120], (2000, 2)),
                     [[100, 50], [200, 100], [250, 120], [0, 0], [100, 100]]])  # edges, corners, max corner
    for x, y in pts:
        assert sum(t.owns(x, y) for t in tiles) == 1, (x, y)


def test_stems_just_outside_the_extent_still_have_an_owner():
    """A fitted stem centre can fall outside the points' extent (a trunk seen from one side)."""
    tiles = tiling.plan_tiles((0, 0, 250, 120), size=100, overlap=10)
    for x, y in [(-0.3, 50), (50, -0.2), (-1, -1), (300.5, 199), (120, 200.4), (400, 400)]:
        assert sum(t.owns(x, y) for t in tiles) == 1, (x, y)


@pytest.mark.parametrize("size, overlap", [(10, 2), (100, 4), (30, 15)])
def test_limits(size, overlap):
    with pytest.raises(ParameterError):
        tiling.plan_tiles((0, 0, 100, 100), size=size, overlap=overlap)
