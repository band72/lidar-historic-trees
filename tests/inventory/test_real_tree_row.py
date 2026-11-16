"""
Regression test on REAL mobile-lidar data: a planted row of 15 trees from the 2022-03-08
Ouster drive (KISS-ICP registered, see docs/real_data/), cropped to a 7 m strip along the row.

These trees have crowns from about 2 m. An earlier, too-strict trunk check rejected the whole
row (the crown underside looks like a flat layer to a vehicle lidar), and nothing failed: the
synthetic tests have no such trees. This test makes that loss visible.

The baseline is the pipeline's own output (no tape measurements exist yet), so DBH tolerances
guard against *changes*, not against absolute error. Replace with field DBH when available.
"""

from pathlib import Path

import numpy as np
import pytest

from forest_inventory_pipeline import inventory as inv
from forest_inventory_pipeline.traits.dbh.robust_fit import FitParams

LAZ = Path(__file__).parents[1] / "data" / "tree_row_2022-03-08.laz"

#: (x, y, DBH cm) of the 14 row trees measured at the time of writing (local KISS-ICP frame)
ROW = [
    (-55.065, 0.225, 29.3),
    (-52.699, 0.786, 27.8),
    (-50.340, 1.355, 26.4),
    (-47.981, 1.921, 24.8),
    (-45.601, 2.491, 26.3),
    (-43.252, 3.068, 22.5),
    (-40.894, 3.640, 22.8),
    (-38.534, 4.200, 19.0),
    (-36.188, 4.732, 16.5),
    (-33.821, 5.359, 13.9),
    (-31.458, 5.922, 14.1),
    (-29.096, 6.492, 13.7),
    (-26.719, 7.069, 13.8),
    (-21.985, 8.222, 14.8),
]
#: the 14th row position: its stem touches an H-shaped structure (bench or frame) at breast
#: height, which once produced a bogus 129 cm "tree". Known limitation: not measured.
MERGED_WITH_STRUCTURE = (-24.35, 7.65)


@pytest.fixture(scope="module")
def records():
    pytest.importorskip("laspy")
    cloud = inv.load_cloud(LAZ)
    gp, sp, fp = inv.GroundParams(), inv.SegmentParams(), FitParams()
    ground, non_ground = inv.segment_ground(cloud.xyz, gp, cloud.classification)
    pts, labels = inv.segment_trees(inv.normalize_heights(ground, non_ground, gp), sp)
    trees = inv.group_trees(pts, labels, sp.min_tree_points)
    return inv.fit_trees(trees, ground, fp, gp)


def _fitted(records):
    return [r for r in records if r.fitted]


def test_every_row_tree_is_found(records):
    fitted = _fitted(records)
    xy = np.array([[r.x, r.y] for r in fitted])
    missing = []
    for x, y, _ in ROW:
        d = np.hypot(xy[:, 0] - x, xy[:, 1] - y)
        if d.min() > 0.10:
            missing.append((x, y, round(float(d.min()), 2)))
    assert not missing, f"row trees no longer measured (x, y, nearest m): {missing}"


def test_row_dbh_unchanged(records):
    fitted = _fitted(records)
    xy = np.array([[r.x, r.y] for r in fitted])
    changed = []
    for x, y, dbh_cm in ROW:
        r = fitted[int(np.argmin(np.hypot(xy[:, 0] - x, xy[:, 1] - y)))]
        if abs(r.dbh * 100 - dbh_cm) > 1.0:
            changed.append((x, y, dbh_cm, round(r.dbh * 100, 1)))
        assert r.ci_low <= r.dbh <= r.ci_high
    assert not changed, f"DBH moved by more than 1 cm (x, y, was, now): {changed}"


def test_no_implausible_diameters(records):
    """Structures and one-sided clutter must not come out as giant trees."""
    big = [(r.tree_id, round(r.dbh * 100)) for r in _fitted(records) if r.dbh > 0.6]
    assert not big, f"implausibly large stems in a row of 14-30 cm trees: {big}"
    x, y = MERGED_WITH_STRUCTURE
    near = [r for r in _fitted(records) if np.hypot(r.x - x, r.y - y) < 1.0]
    assert all(r.dbh < 0.35 for r in near), [(r.tree_id, r.dbh) for r in near]
