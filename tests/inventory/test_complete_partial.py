"""fit.complete_partial_stems: widen the window so other heights complete the circle."""

import csv

import numpy as np
import pytest

from forest_inventory_pipeline.errors import FitError, ParameterError
from forest_inventory_pipeline.plugins import ExportContext, load_plugins, registry
from forest_inventory_pipeline.records import TreeRecord
from forest_inventory_pipeline.traits.dbh import robust_fit as rf
from forest_inventory_pipeline.traits.dbh.robust_fit import FitParams, fit_stem


def stem(
    rng,
    d_bh,
    ovality=0.03,
    rate=120.0,
    width=120.0,
    taper=-0.005,
    n=2500,
    noise=0.004,
    z_range=(1.0, 2.6),
):
    """Oval, tapered stem seen through a sector that turns ``rate`` deg per metre of height."""
    z = rng.uniform(*z_range, n)
    phi0, rot = rng.uniform(0, 2 * np.pi, 2)
    phi = phi0 + np.radians(rate) * (z - z_range[0]) + rng.uniform(-1, 1, n) * np.radians(width) / 2
    scale = 1 + taper * (z - 1.3) / (d_bh / 2)
    a, b = d_bh / 2 * (1 + ovality), d_bh / 2 * (1 - ovality)
    x, y = a * np.cos(phi) * scale, b * np.sin(phi) * scale
    return np.c_[
        x * np.cos(rot) - y * np.sin(rot) + rng.normal(0, noise, n),
        x * np.sin(rot) + y * np.cos(rot) + rng.normal(0, noise, n),
        z,
    ]


def test_jacobian_with_taper_matches_numeric():
    rng = np.random.default_rng(0)
    pts = stem(rng, 0.4, n=300)
    theta = np.array([0.01, -0.02, 0.03, -0.01, 0.2, -0.004])
    J = rf.jacobian(theta, pts, 1.3)
    assert J.shape == (300, 6)
    num = np.empty_like(J)
    for i in range(6):
        d = np.zeros(6)
        d[i] = 1e-7
        num[:, i] = (rf.residuals(theta + d, pts, 1.3) - rf.residuals(theta - d, pts, 1.3)) / 2e-7
    np.testing.assert_allclose(J, num, atol=1e-5)


def test_taper_fit_gives_the_radius_at_breast_height_on_a_lopsided_window():
    rng = np.random.default_rng(1)
    z = rng.uniform(1.0, 2.05, 3000)  # not centred on breast height 1.3
    t = rng.uniform(0, 2 * np.pi, 3000)
    r = 0.2 - 0.005 * (z - 1.3)
    pts = np.c_[r * np.cos(t), r * np.sin(t), z] + rng.normal(0, 0.002, (3000, 3))
    theta, _ = rf.refine_cylinder(pts, np.array([0, 0, 0, 0, 0.21, 0.0]), 1.3, 0.03, 50)
    assert theta[4] == pytest.approx(0.2, abs=0.0005) and theta[5] == pytest.approx(
        -0.005, abs=0.001
    )
    straight, _ = rf.refine_cylinder(pts, np.array([0, 0, 0, 0, 0.21]), 1.3, 0.03, 50)
    # without a taper term the window centre (z = 1.5) sets the radius: 0.005 * 0.22 = 1.1 mm low
    assert abs(straight[4] - 0.2) > 0.0009 > abs(theta[4] - 0.2)


def test_off_by_default_and_identical_when_not_needed():
    pts = stem(np.random.default_rng(2), 0.4, rate=0, width=360)  # seen all round
    a = fit_stem(pts, FitParams())
    b = fit_stem(pts, FitParams(complete_partial_stems=True))
    assert (a.dbh, a.ci_low, a.ci_high, a.band_half_width) == (b.dbh, b.ci_low, b.ci_high, 0.25)
    assert not b.notes and not b.warnings


def test_moving_view_is_completed_from_a_taller_window():
    flag_err, comp_err, completed = [], [], 0
    for i in range(40):
        rng = np.random.default_rng(100 + i)
        d = rng.uniform(0.25, 0.6)
        pts = stem(rng, d)
        base = fit_stem(pts, FitParams(seed=i))
        comp = fit_stem(pts, FitParams(seed=i, complete_partial_stems=True))
        assert base.warnings and base.band_half_width == 0.25
        if comp.band_half_width > 0.25:
            completed += 1
            assert comp.arc_coverage >= base.arc_coverage + 0.10
            assert any("completed" in n for n in comp.notes) and "taper" in comp.notes[0]
        flag_err.append(base.dbh - d)
        comp_err.append(comp.dbh - d)
    assert completed >= 35
    rmse = lambda e: float(np.sqrt(np.mean(np.square(e))))  # noqa: E731
    assert rmse(comp_err) < 0.8 * rmse(flag_err)  # clearly better, not just different
    assert abs(np.mean(comp_err)) < 0.005  # no bias from the taper handling


def test_flagged_stem_is_cleared_when_enough_is_seen():
    f = fit_stem(stem(np.random.default_rng(3), 0.4), FitParams(complete_partial_stems=True))
    assert f.arc_coverage >= 0.6 and not f.warnings and f.status == "ok"


def test_stem_with_too_few_points_at_breast_height_is_included():
    rng = np.random.default_rng(4)
    pts = stem(rng, 0.4, rate=300, width=200, n=1200)
    pts = pts[(pts[:, 2] > 1.9) | (rng.random(len(pts)) < 0.02)]  # almost nothing near 1.3 m
    with pytest.raises(FitError, match="points in the breast-height band"):
        fit_stem(pts, FitParams())
    f = fit_stem(pts, FitParams(complete_partial_stems=True, max_band_half_width=0.75))
    assert f.band_half_width > 0.25 and "too few usable points" in f.notes[0]
    assert f.dbh == pytest.approx(0.4, abs=0.04)


def test_same_side_hidden_at_every_height_cannot_be_completed():
    pts = stem(np.random.default_rng(5), 0.4, rate=0, width=130)
    base = fit_stem(pts, FitParams())
    comp = fit_stem(pts, FitParams(complete_partial_stems=True))
    assert comp.dbh == base.dbh and comp.band_half_width == 0.25
    assert comp.warnings and not comp.notes  # still flagged, nothing pretended


def test_a_stem_that_fails_both_ways_still_fails():
    with pytest.raises(FitError):
        fit_stem(
            np.c_[np.zeros(10), np.zeros(10), np.linspace(1, 1.5, 10)],
            FitParams(complete_partial_stems=True),
        )


def test_param_validation():
    with pytest.raises(ParameterError, match="max_band_half_width"):
        FitParams(band_half_width=0.5, max_band_half_width=0.3)
    with pytest.raises(ParameterError, match="complete_target_coverage"):
        FitParams(complete_target_coverage=1.5)


def test_csv_reports_the_window_used_and_the_note(tmp_path):
    load_plugins(entry_points=False)
    f = fit_stem(stem(np.random.default_rng(6), 0.4), FitParams(complete_partial_stems=True))
    rec = TreeRecord(
        tree_id=1,
        status="ok",
        x=0,
        y=0,
        ground_z=0,
        breast_height=1.3,
        bh_elevation=1.3,
        n_points=100,
        dbh=f.dbh,
        dbh_se=f.dbh_se,
        view_sd=f.view_sd,
        ci_low=f.ci_low,
        ci_high=f.ci_high,
        band_half_width=f.band_half_width,
        message="; ".join(f.warnings + f.notes),
    )
    out = registry.exporter("csv").export([rec], tmp_path / "t.csv", ExportContext())
    row = next(csv.DictReader(out.open()))
    assert float(row["band_half_width"]) == pytest.approx(f.band_half_width, abs=1e-4)
    assert float(row["band_half_width"]) > 0.25
    assert "completed" in row["message"]


def test_windows_taller_than_one_metre_work():
    """The app allows up to 1.2 m; windows above 1.0 m used to fail as 'internal error'."""
    pts = stem(np.random.default_rng(7), 0.4, rate=40, width=100, z_range=(0.2, 3.0))
    f = fit_stem(
        pts,
        FitParams(
            complete_partial_stems=True, max_band_half_width=1.2, complete_target_coverage=1.0
        ),
    )
    assert f.band_half_width > 1.0 and f.dbh_method == "taller window (taper)"


def test_dbh_method_marks_completed_stems():
    pts = stem(np.random.default_rng(8), 0.4)
    assert fit_stem(pts, FitParams()).dbh_method == "breast-height band"
    assert (
        fit_stem(pts, FitParams(complete_partial_stems=True)).dbh_method == "taller window (taper)"
    )
