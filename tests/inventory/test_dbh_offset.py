"""DBH offset: a field-calibrated correction of every DBH (+/- 2 cm), and how it is suggested."""

import numpy as np
import pytest

from forest_inventory_pipeline import inventory as inv
from forest_inventory_pipeline.calibration import calibrate
from forest_inventory_pipeline.errors import ParameterError
from forest_inventory_pipeline.records import TreeRecord
from forest_inventory_pipeline.traits.dbh import robust_fit as rf


def _stem(r=0.15, n=20000, seed=0):
    rng = np.random.default_rng(seed)
    t = rng.uniform(0, 2 * np.pi, n)
    return np.c_[r * np.cos(t), r * np.sin(t), rng.uniform(0.0, 3.0, n)]


def test_offset_shifts_dbh_and_its_interval():
    base = rf.fit_stem(_stem(), rf.FitParams())
    f = rf.fit_stem(_stem(), rf.FitParams(dbh_offset=-0.012))
    assert f.dbh == pytest.approx(base.dbh - 0.012)
    assert f.ci_low == pytest.approx(base.ci_low - 0.012)
    assert f.ci_high == pytest.approx(base.ci_high - 0.012)
    assert f.radius == pytest.approx(f.dbh / 2)
    assert any("offset" in n for n in f.notes)


@pytest.mark.parametrize("bad", [0.021, -0.025])
def test_offset_is_limited_to_two_cm(bad):
    with pytest.raises(ParameterError, match="2 cm"):
        rf.FitParams(dbh_offset=bad)


def test_offset_is_recorded_on_each_tree_only_when_used():
    g = np.c_[np.random.default_rng(1).uniform(-5, 5, (3000, 2)), np.zeros(3000)]
    r0 = inv.fit_trees({1: _stem()}, g, rf.FitParams())[0]
    r1 = inv.fit_trees({1: _stem()}, g, rf.FitParams(dbh_offset=0.008))[0]
    assert "dbh_offset_cm" not in r0.extra
    assert r1.extra["dbh_offset_cm"] == pytest.approx(0.8)
    assert r1.dbh == pytest.approx(r0.dbh + 0.008)


def _rec(tid, x, dbh, se=0.004):
    return TreeRecord(tree_id=tid, status="ok", x=x, y=0.0, ground_z=0.0, breast_height=1.3,
                      bh_elevation=1.3, n_points=100, dbh=dbh, dbh_se=se,
                      ci_low=dbh - 2 * se, ci_high=dbh + 2 * se)


def test_calibration_suggests_the_offset_that_removes_the_bias():
    rng = np.random.default_rng(0)
    tape = rng.uniform(0.15, 0.6, 25)
    scanned = tape + 0.009 + rng.normal(0, 0.004, 25)  # scans read 9 mm too large
    recs = [_rec(i, float(i), float(d)) for i, d in enumerate(scanned)]
    ref = np.c_[np.arange(25.0), np.zeros(25), tape]
    res = calibrate(recs, ref, current_model_sd=0.005)
    assert res.bias == pytest.approx(0.009, abs=0.002)
    assert res.bias_significant
    assert res.suggested_dbh_offset == pytest.approx(-0.009, abs=0.002)
    # calibrating a run that already used an offset: the suggestion is the new total
    res2 = calibrate(recs, ref, current_model_sd=0.005, current_dbh_offset=-0.004)
    assert res2.suggested_dbh_offset == pytest.approx(-0.013, abs=0.002)


def test_no_offset_suggested_for_a_bias_within_the_noise():
    rng = np.random.default_rng(1)
    tape = rng.uniform(0.15, 0.6, 8)
    scanned = tape + rng.normal(0, 0.01, 8)
    res = calibrate([_rec(i, float(i), float(d)) for i, d in enumerate(scanned)],
                    np.c_[np.arange(8.0), np.zeros(8), tape], current_model_sd=0.005)
    assert not res.bias_significant and res.suggested_dbh_offset == 0.0


def test_a_bias_that_grows_with_dbh_is_reported():
    """A constant offset cannot fix a bias proportional to DBH: say so."""
    rng = np.random.default_rng(2)
    tape = rng.uniform(0.1, 0.8, 30)
    scanned = tape * 1.06 + rng.normal(0, 0.003, 30)  # 6 % too large
    res = calibrate([_rec(i, float(i), float(d)) for i, d in enumerate(scanned)],
                    np.c_[np.arange(30.0), np.zeros(30), tape], current_model_sd=0.005)
    assert res.bias_per_10cm == pytest.approx(0.006, abs=0.002)
    assert res.bias_grows_with_dbh


def test_suggestion_is_capped_at_the_slider_range():
    tape = np.linspace(0.2, 0.5, 20)
    recs = [_rec(i, float(i), float(d) + 0.05) for i, d in enumerate(tape)]  # 5 cm too large
    res = calibrate(recs, np.c_[np.arange(20.0), np.zeros(20), tape], current_model_sd=0.005)
    assert res.suggested_dbh_offset == pytest.approx(-0.02)
    assert res.offset_capped


def test_app_slider_and_calibration_apply(tmp_path):
    from streamlit.testing.v1 import AppTest
    from pathlib import Path

    app_path = Path(__file__).parents[2] / "python" / "forest_inventory_pipeline" / "app" / "streamlit_app.py"
    import os

    os.environ["FIP_HOME"] = str(tmp_path / "home")
    at = AppTest.from_file(str(app_path), default_timeout=300)
    at.run()
    at.sidebar.number_input[0].set_value(8)
    next(b for b in at.sidebar.button if b.label == "Run tree extraction").click().run()
    sl = next(s for s in at.sidebar.slider if s.label == "DBH offset (cm)")
    assert sl.value == 0.0 and sl.min == -2.0 and sl.max == 2.0
    sl.set_value(1.0).run()
    assert not at.exception and not at.error
    yaml_text = next(c.value for c in at.code if c.value.startswith("# LiDAR Tree Inventory"))
    assert "dbh_offset: 0.01" in yaml_text
    # units in feet: the slider stays in cm and keeps its value
    next(s for s in at.sidebar.button_group if s.label == "Display & export units").set_value("ft").run()
    assert not at.exception and next(s for s in at.sidebar.slider if s.label == "DBH offset (cm)").value == 1.0
    os.environ.pop("FIP_HOME", None)


def test_cli_offset_and_calibration_suggestion(tmp_path):
    import csv
    import os
    import subprocess
    import sys

    from forest_inventory_pipeline import georef
    from forest_inventory_pipeline.synthetic import make_plot

    plot = make_plot(n_trees=20, seed=11)
    np.save(tmp_path / "p.npy", plot.points)
    env = {**os.environ, "COLUMNS": "250", "LOG_LEVEL": "info"}

    def fip(*a):
        return subprocess.run([sys.executable, "-m", "forest_inventory_pipeline.fip", *a],
                              capture_output=True, text=True, env=env)

    assert fip("inventory", str(tmp_path / "p.npy"), "-o", str(tmp_path / "a"), "-e", "csv",
               "--dbh-offset-cm", "1.5").returncode == 0
    rows = [r for r in csv.DictReader((tmp_path / "a" / "trees.csv").open()) if r["status"] in ("ok", "warning")]
    assert rows and all(float(r["dbh_offset_cm"]) == 1.5 for r in rows)
    # field sheet with the true DBH: the 1.5 cm offset shows as bias; calibrate suggests removing it
    truth = {(round(t["x"], 1), round(t["y"], 1)): t["dbh"] for t in plot.truth}
    with (tmp_path / "sheet.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, georef.FIELD_COLUMNS)
        w.writeheader()
        for r in rows:
            x, y = float(r["x"]), float(r["y"])
            k = min(truth, key=lambda t: (t[0] - x) ** 2 + (t[1] - y) ** 2)
            w.writerow(dict(tree_id=r["tree_id"], tape_dbh_cm=f"{truth[k] * 100:.1f}", is_tree="yes"))
    res = fip("calibrate", str(tmp_path / "a" / "trees.csv"), str(tmp_path / "sheet.csv"))
    out = res.stdout + res.stderr
    assert res.returncode == 0, out
    # the run added 1.5 cm; the suggestion takes it back (the synthetic plots read ~0.1 cm small)
    import re

    m = re.search(r"suggested DBH offset ([+-][0-9.]+) cm", out)
    assert m and abs(float(m.group(1))) <= 0.3, out[-800:]
