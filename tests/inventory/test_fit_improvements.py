"""Suspect ground under a stem, parallel fitting, and peak memory in the run record."""

import json

import numpy as np

from forest_inventory_pipeline import inventory as inv
from forest_inventory_pipeline.synthetic import make_plot
from forest_inventory_pipeline.traits.dbh.robust_fit import FitParams


def _stem(r=0.15, top=4.0, n=30000, seed=0):
    rng = np.random.default_rng(seed)
    t = rng.uniform(0, 2 * np.pi, n)
    return np.c_[r * np.cos(t), r * np.sin(t), rng.uniform(0.0, top, n)]


def _ground(pit=False, seed=1):
    rng = np.random.default_rng(seed)
    g = np.c_[rng.uniform(-8, 8, (6000, 2)), rng.normal(0, 0.01, 6000)]
    if pit:  # a few bad "ground" points far below the surface, right under the stem
        g = np.vstack([g, np.c_[rng.uniform(-0.1, 0.1, (8, 2)), np.full(8, -1.5)]])
    return g


def test_ground_far_from_its_surroundings_is_flagged():
    """2022-03-08: the ground model was 1.1-1.9 m off under three trees of one segment."""
    r = inv.fit_trees({1: _stem()}, _ground(pit=True), FitParams())[0]
    assert r.fitted and r.status == "warning"
    assert "ground under this stem" in r.message and "1.5 m below" in r.message
    assert r.extra["ground_offset_m"] < -1.0


def test_normal_ground_is_not_flagged_and_the_check_can_be_switched_off():
    r = inv.fit_trees({1: _stem()}, _ground(), FitParams())[0]
    assert "ground under" not in r.message and abs(r.extra["ground_offset_m"]) < 0.1
    r = inv.fit_trees({1: _stem()}, _ground(pit=True), FitParams(ground_check_tolerance=0.0))[0]
    assert "ground under" not in r.message


def test_sloped_ground_is_not_flagged():
    g = _ground()
    g[:, 2] += 0.3 * g[:, 0]  # a 30 % slope
    r = inv.fit_trees({1: _stem()}, g, FitParams())[0]
    assert "ground under" not in r.message


def test_parallel_fitting_gives_the_same_trees():
    plot = make_plot(n_trees=12, seed=4)
    gp, sp, fp = inv.GroundParams(), inv.SegmentParams(), FitParams()
    ground, ng = inv.segment_ground(plot.points, gp)
    pts, lab = inv.segment_trees(inv.normalize_heights(ground, ng, gp), sp)
    trees = inv.group_trees(pts, lab, sp.min_tree_points)
    key = lambda recs: [(r.tree_id, r.status, round(r.dbh, 9), r.message) for r in recs]  # noqa: E731
    one = inv.fit_trees(trees, ground, fp, gp, workers=1)
    many = inv.fit_trees(trees, ground, fp, gp, workers=4)
    assert key(one) == key(many) and len(one) >= 10


def test_peak_memory_is_recorded(tmp_path):
    from typer.testing import CliRunner

    import forest_inventory_pipeline.cluster.quickshiftpp  # noqa: F401  (see test_load_guards)
    from forest_inventory_pipeline import fip

    assert inv.peak_memory_bytes() is None or inv.peak_memory_bytes() > 10**6
    np.save(tmp_path / "p.npy", make_plot(n_trees=4, seed=3).points)
    res = CliRunner().invoke(fip.cli, ["inventory", str(tmp_path / "p.npy"), "-o", str(tmp_path / "o"), "-e", "csv"])
    assert res.exit_code == 0, res.output
    p = json.loads((tmp_path / "o" / "run.json").read_text())["parameters"]
    assert p["points"] > 1000 and (p["peak_memory_gb"] is None or p["peak_memory_gb"] > 0.01)
