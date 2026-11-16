"""
Regression test on REAL mobile-lidar data: a 40 m open shed (pole barn) from the 2022-03-08
Ouster drive (KISS-ICP registered, see docs/real_data/), cropped to a 7 m strip along it.

Its 15 posts (2.4 m apart, 14-30 cm) carry one continuous roof at 2-3.3 m and pass every
trunk check. An earlier session took them for a planted row of trees with low crowns; the
site owner confirmed they are a structure. With the overhead cloud, the roof check rejects
them; without it, they are measured, and their diameters guard the fit against changes.
"""

from pathlib import Path

import numpy as np
import pytest

from forest_inventory_pipeline import inventory as inv
from forest_inventory_pipeline.traits.dbh.robust_fit import FitParams

LAZ = Path(__file__).parents[1] / "data" / "shed_row_2022-03-08.laz"

#: (x, y, diameter cm) of 14 posts as measured with the roof check off (local KISS-ICP frame)
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
#: the 15th post: it touches an H-shaped structure (bench or frame) at breast
#: height, which once produced a bogus 129 cm "tree". Known limitation: not measured.
MERGED_WITH_STRUCTURE = (-24.35, 7.65)


@pytest.fixture(scope="module")
def stages():
    pytest.importorskip("laspy")
    cloud = inv.load_cloud(LAZ)
    gp, sp = inv.GroundParams(), inv.SegmentParams()
    ground, non_ground = inv.segment_ground(cloud.xyz, gp, cloud.classification)
    normalized = inv.normalize_heights(ground, non_ground, gp)
    pts, labels = inv.segment_trees(normalized, sp)
    return gp, ground, inv.group_trees(pts, labels, sp.min_tree_points), (normalized, non_ground[:, 2])


@pytest.fixture(scope="module")
def records(stages):
    """Roof check off: the posts are measured like stems."""
    gp, ground, trees, _ = stages
    return inv.fit_trees(trees, ground, FitParams(reject_roof_posts=False), gp)


@pytest.fixture(scope="module")
def with_roof_check(stages):
    gp, ground, trees, normalized = stages
    return inv.fit_trees(trees, ground, FitParams(), gp, overhead=inv.OverheadIndex(*normalized))


def _fitted(records):
    return [r for r in records if r.fitted]


def test_roof_check_rejects_every_post(with_roof_check):
    fitted = _fitted(with_roof_check)
    kept = [(r.tree_id, round(r.x, 2), round(r.y, 2)) for r in fitted
            if min(np.hypot(r.x - x, r.y - y) for x, y, _ in ROW) < 0.5]
    assert not kept, f"posts under the shed roof still measured as trees: {kept}"
    # the posts form a few clusters; posts split off a rejected cluster are dropped (logged),
    # so the reason is on the cluster records
    roofed = [r for r in with_roof_check if "under a roof" in r.message]
    assert len(roofed) >= 3, [r.message for r in with_roof_check if not r.fitted]


def test_every_post_is_measured_without_the_roof_check(records):
    fitted = _fitted(records)
    xy = np.array([[r.x, r.y] for r in fitted])
    missing = []
    for x, y, _ in ROW:
        d = np.hypot(xy[:, 0] - x, xy[:, 1] - y)
        if d.min() > 0.10:
            missing.append((x, y, round(float(d.min()), 2)))
    assert not missing, f"posts no longer measured (x, y, nearest m): {missing}"


def test_post_diameters_unchanged(records):
    fitted = _fitted(records)
    xy = np.array([[r.x, r.y] for r in fitted])
    changed = []
    for x, y, dbh_cm in ROW:
        r = fitted[int(np.argmin(np.hypot(xy[:, 0] - x, xy[:, 1] - y)))]
        if abs(r.dbh * 100 - dbh_cm) > 1.0:
            changed.append((x, y, dbh_cm, round(r.dbh * 100, 1)))
        assert r.ci_low <= r.dbh <= r.ci_high
    assert not changed, f"diameter moved by more than 1 cm (x, y, was, now): {changed}"


def test_no_implausible_diameters(records):
    """Structures and one-sided clutter must not come out as giant trees."""
    big = [(r.tree_id, round(r.dbh * 100)) for r in _fitted(records) if r.dbh > 0.6]
    assert not big, f"implausibly large stems among 14-30 cm posts: {big}"
    x, y = MERGED_WITH_STRUCTURE
    near = [r for r in _fitted(records) if np.hypot(r.x - x, r.y - y) < 1.0]
    assert all(r.dbh < 0.35 for r in near), [(r.tree_id, r.dbh) for r in near]
