"""Full pipeline on a synthetic plot with known truth: CSF + quickshift++ + fit + export."""

import numpy as np
import pytest

from forest_inventory_pipeline import inventory as inv
from forest_inventory_pipeline.calibration import calibrate
from forest_inventory_pipeline.synthetic import make_plot
from forest_inventory_pipeline.traits.dbh.robust_fit import FitParams


@pytest.fixture(scope="module")
def run():
    plot = make_plot(n_trees=20, seed=3)
    gp, sp, fp = inv.GroundParams(), inv.SegmentParams(), FitParams()
    ground, non_ground = inv.segment_ground(plot.points, gp)
    normalized = inv.normalize_heights(ground, non_ground, gp)
    pts, labels = inv.segment_trees(normalized, sp)
    trees = inv.group_trees(pts, labels, sp.min_tree_points)
    records = inv.fit_trees(trees, ground, fp, gp)
    return plot, records, fp


def _matched(plot, records):
    T = np.array([[t["x"], t["y"], t["dbh"], t["ground_z"]] for t in plot.truth])
    out = []
    for r in records:
        if not r.fitted:
            continue
        d = np.hypot(T[:, 0] - r.x, T[:, 1] - r.y)
        j = int(d.argmin())
        if d[j] < 0.5:
            out.append((r, T[j]))
    return out


def test_finds_most_trees(run):
    plot, records, _ = run
    assert len(_matched(plot, records)) >= 0.95 * len(plot.truth)


def test_dbh_accuracy_and_ci_coverage(run):
    plot, records, _ = run
    m = _matched(plot, records)
    err = np.array([r.dbh - t[2] for r, t in m])
    assert np.sqrt(np.mean(err**2)) < 0.01  # < 1 cm RMSE
    assert abs(err.mean()) < 0.005  # no systematic bias (ground refinement works)
    coverage = np.mean([r.ci_low <= t[2] <= r.ci_high for r, t in m])
    assert coverage >= 0.85


def test_ground_elevation_is_real_world(run):
    plot, records, fp = run
    for r, t in _matched(plot, records):
        assert r.ground_z == pytest.approx(t[3], abs=0.15)
        assert r.bh_elevation == pytest.approx(r.ground_z + fp.breast_height)


def test_exports_and_calibration(run, tmp_path):
    plot, records, fp = run
    ctx = inv.context_for("synthetic", fp)
    written = inv.export_records(records, tmp_path, ["csv", "dxf"], ctx)
    assert {p.name for p in written} == {"trees.csv", "trees.dxf", "run.json"}
    ref = np.array([[t["x"], t["y"], t["dbh"]] for t in plot.truth])
    res = calibrate(records, ref, fp.model_sd)
    assert res.n_matched >= 14
    assert 0 <= res.model_sd < 0.01


def test_band_slice_check():
    issues = inv.check_band_inside_slice(
        FitParams(breast_height=1.3, band_half_width=0.4), inv.SegmentParams(min_z=1.0)
    )
    assert issues and "min_z" in issues[0]
