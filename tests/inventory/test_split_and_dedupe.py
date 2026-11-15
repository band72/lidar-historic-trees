import numpy as np

from forest_inventory_pipeline import inventory as inv
from forest_inventory_pipeline.records import TreeRecord
from forest_inventory_pipeline.traits.dbh.robust_fit import FitParams


def stem_points(rng, cx, cy, r, n=1500):
    t = rng.uniform(0, 2 * np.pi, n)
    z = rng.uniform(0.5, 3.5, n)
    return np.c_[cx + r * np.cos(t), cy + r * np.sin(t), z] + rng.normal(0, 0.003, (n, 3))


def ground(size=20.0):
    g = np.arange(0, size, 0.5)
    gx, gy = np.meshgrid(g, g)
    return np.c_[gx.ravel(), gy.ravel(), np.full(gx.size, 100.0)]


def test_merged_cluster_is_split_into_both_stems():
    rng = np.random.default_rng(0)
    merged = np.r_[stem_points(rng, 5, 5, 0.30), stem_points(rng, 8, 5, 0.08, n=600)]
    fp = FitParams()
    trees, parents = inv.split_multi_stem({7: merged}, fp)
    assert len(trees) == 2
    assert 7 in trees and parents == {7: 7, 8: 7}  # largest keeps the id, extra gets max+1
    recs = inv.fit_trees({7: merged}, ground(), fp)
    dbh = sorted(r.dbh for r in recs if r.fitted)
    np.testing.assert_allclose(dbh, [0.16, 0.60], atol=0.01)
    assert {r.extra["cluster_id"] for r in recs} == {7}


def test_split_can_be_disabled():
    rng = np.random.default_rng(1)
    merged = np.r_[stem_points(rng, 5, 5, 0.30), stem_points(rng, 8, 5, 0.08, n=600)]
    recs = inv.fit_trees({7: merged}, ground(), FitParams(split_stems=False))
    assert len(recs) == 1


def test_single_stem_is_not_split():
    rng = np.random.default_rng(2)
    trees, parents = inv.split_multi_stem({3: stem_points(rng, 5, 5, 0.25)}, FitParams())
    assert list(trees) == [3] and parents == {}


def test_failed_split_fragment_is_dropped():
    rng = np.random.default_rng(3)
    # a stem plus a small, non-circular blob of branch points in the band
    blob = np.c_[rng.normal(7, 0.02, 200), rng.normal(5, 0.02, 200), rng.uniform(1.1, 1.5, 200)]
    recs = inv.fit_trees({1: np.r_[stem_points(rng, 5, 5, 0.2), blob]}, ground(), FitParams())
    assert all(r.status != "failed" for r in recs)
    assert any(abs(r.x - 5) < 0.02 for r in recs if r.fitted)


def _rec(tid, x, dbh, band):
    return TreeRecord(
        tree_id=tid,
        status="ok",
        x=x,
        y=0.0,
        ground_z=0,
        breast_height=1.3,
        bh_elevation=1.3,
        n_points=band,
        n_band_points=band,
        dbh=dbh,
        inlier_ratio=0.9,
        ci_low=dbh - 0.01,
        ci_high=dbh + 0.01,
    )


def test_dedupe_keeps_best_supported_estimate():
    recs = [_rec(1, 0.0, 0.40, 500), _rec(2, 0.05, 0.38, 200), _rec(3, 2.0, 0.30, 300)]
    kept = inv.dedupe_stems(recs)
    assert [r.tree_id for r in kept] == [1, 3]


def test_dedupe_keeps_touching_neighbours():
    # two 40 cm stems 45 cm apart (centres outside each other's circle) are both real
    recs = [_rec(1, 0.0, 0.40, 500), _rec(2, 0.45, 0.40, 400)]
    assert len(inv.dedupe_stems(recs)) == 2
