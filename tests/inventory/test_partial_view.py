"""Stems seen from one side: flagged, and the interval widens with the unseen arc."""

import csv

import numpy as np
import pytest

from forest_inventory_pipeline.calibration import calibrate
from forest_inventory_pipeline.errors import ParameterError
from forest_inventory_pipeline.plugins import ExportContext, load_plugins, registry
from forest_inventory_pipeline.records import TreeRecord
from forest_inventory_pipeline.traits.dbh.robust_fit import FitParams, fit_stem


def oval(rng, mean_d, ovality, rot, centre, arc, noise=0.004, n=800):
    """Points on an oval stem (ovality = (long - short) / (long + short)) seen over ``arc`` rad."""
    a, b = mean_d / 2 * (1 + ovality), mean_d / 2 * (1 - ovality)
    t = centre + rng.uniform(-arc / 2, arc / 2, n)
    x, y = a * np.cos(t), b * np.sin(t)
    xr, yr = x * np.cos(rot) - y * np.sin(rot), x * np.sin(rot) + y * np.cos(rot)
    z = rng.uniform(1.05, 1.55, n)
    return np.c_[xr + rng.normal(0, noise, n), yr + rng.normal(0, noise, n), z]


def round_stem(rng, r=0.2, arc=2 * np.pi, n=800):
    return oval(rng, 2 * r, 0.0, 0.0, 0.0, arc, n=n)


def test_default_flags_half_visible_stems():
    f = fit_stem(round_stem(np.random.default_rng(0), arc=np.pi), FitParams())
    assert 0.45 < f.arc_coverage < 0.6
    assert f.status == "warning" and any("arc coverage" in w for w in f.warnings)
    assert "round stem" in f.warnings[0] and "widened" in f.warnings[0]
    assert fit_stem(round_stem(np.random.default_rng(0)), FitParams()).status == "ok"


def test_interval_widens_as_less_is_seen():
    widths, view = [], []
    for arc in (2 * np.pi, 1.5 * np.pi, np.pi, 0.66 * np.pi):
        f = fit_stem(round_stem(np.random.default_rng(1), arc=arc), FitParams())
        widths.append(f.ci_high - f.ci_low)
        view.append(f.view_sd)
    assert view[0] < 1e-4  # seen all round: nothing added
    assert view == sorted(view) and widths == sorted(widths)  # monotonic
    assert widths[2] > 2 * widths[0]  # a half view is much less certain than a full one


def test_ovality_sd_zero_switches_it_off():
    pts = round_stem(np.random.default_rng(2), arc=np.pi)
    on = fit_stem(pts, FitParams(ovality_sd=0.03))
    off = fit_stem(pts, FitParams(ovality_sd=0.0))
    assert off.view_sd == 0.0 and on.view_sd > 0
    assert on.ci_high - on.ci_low > off.ci_high - off.ci_low
    assert on.dbh == off.dbh  # only the interval changes, never the measurement


def test_bootstrap_interval_also_widens():
    pts = round_stem(np.random.default_rng(3), arc=np.pi)
    kw = dict(ci_method="bootstrap", n_bootstrap=100, seed=1)
    on = fit_stem(pts, FitParams(ovality_sd=0.03, **kw))
    off = fit_stem(pts, FitParams(ovality_sd=0.0, **kw))
    assert on.ci_high - on.ci_low > off.ci_high - off.ci_low


@pytest.mark.parametrize("arc_deg", [270, 180, 120])
def test_interval_covers_typical_oval_stems_seen_from_one_side(arc_deg):
    """Stems with the assumed 3 % ovality, random orientation: the 95 % interval must hold."""
    rng = np.random.default_rng(arc_deg)
    n, hits = 60, 0
    for i in range(n):
        d = rng.uniform(0.2, 0.6)
        pts = oval(
            rng, d, 0.03, rng.uniform(0, 2 * np.pi), rng.uniform(0, 2 * np.pi), np.radians(arc_deg)
        )
        f = fit_stem(pts, FitParams(seed=i))
        hits += f.ci_low <= d <= f.ci_high
    assert hits / n >= 0.90, f"{hits}/{n} intervals contained the true mean diameter"


def test_param_validation():
    for bad in (-0.01, 0.5):
        with pytest.raises(ParameterError, match="ovality_sd"):
            FitParams(ovality_sd=bad)


def _rec(tid, dbh, se, view, true_err):
    return TreeRecord(
        tree_id=tid,
        status="warning",
        x=float(tid * 5),
        y=0.0,
        ground_z=0,
        breast_height=1.3,
        bh_elevation=1.3,
        n_points=100,
        dbh=dbh + true_err,
        dbh_se=se,
        view_sd=view,
        ci_low=dbh - 1.96 * se,
        ci_high=dbh + 1.96 * se,
    )


def test_calibration_is_not_inflated_by_the_partial_view_allowance():
    """The interval already includes view_sd; calibration must not read it as model error."""
    rng = np.random.default_rng(5)
    view = 0.012  # 1.2 cm of the reported uncertainty comes from the unseen part of the stems
    se = float(np.sqrt(0.002**2 + 0.005**2 + view**2))
    recs = [_rec(i, 0.4, se, view, rng.normal(0, np.hypot(0.002, view))) for i in range(400)]
    ref = np.array([[r.x, r.y, 0.4] for r in recs])
    assert calibrate(recs, ref, current_model_sd=0.005).model_sd < 0.004  # errors fully explained
    # extra model error that is NOT partial-view still shows up
    recs2 = [
        _rec(i, 0.4, se, view, rng.normal(0, np.sqrt(0.002**2 + view**2 + 0.01**2)))
        for i in range(400)
    ]
    assert calibrate(recs2, ref, current_model_sd=0.005).model_sd == pytest.approx(0.01, abs=0.002)


def test_csv_has_view_sd_in_the_output_unit(tmp_path):
    load_plugins(entry_points=False)
    r = _rec(1, 0.4, 0.01, 0.0254, 0.0)  # 2.54 cm = 1 inch
    for unit, expected in (("m", "0.0254"), ("ft", f"{0.0254 / 0.3048:.4f}")):
        out = registry.exporter("csv").export(
            [r], tmp_path / f"{unit}.csv", ExportContext(units=unit)
        )
        assert next(csv.DictReader(out.open()))["view_sd"] == expected
