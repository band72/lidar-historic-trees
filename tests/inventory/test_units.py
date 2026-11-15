import csv
import re

import numpy as np
import pytest

from forest_inventory_pipeline import inventory as inv
from forest_inventory_pipeline.errors import ParameterError
from forest_inventory_pipeline.plugins import ExportContext, load_plugins, registry
from forest_inventory_pipeline.records import TreeRecord
from forest_inventory_pipeline.units import (
    METRES_PER_UNIT,
    from_metres,
    guess_unit_hint,
    normalize_unit,
    to_metres,
)

ezdxf = pytest.importorskip("ezdxf")


@pytest.fixture(autouse=True)
def _plugins():
    load_plugins(entry_points=False)


def rec():
    return TreeRecord(
        tree_id=1,
        status="ok",
        x=304.8,
        y=609.6,
        ground_z=30.48,
        breast_height=1.3716,
        bh_elevation=31.8516,
        n_points=100,
        dbh=0.508,
        dbh_se=0.00254,
        ci_low=0.4826,
        ci_high=0.5334,
        confidence=0.95,
        ci_method="analytic",
    )


def test_conversions():
    assert METRES_PER_UNIT["ft"] == 0.3048
    assert to_metres(10, "ft") == pytest.approx(3.048)
    assert to_metres(3937, "us_ft") == pytest.approx(1200)
    assert from_metres(to_metres(1234.5, "us_ft"), "us_ft") == pytest.approx(1234.5)
    assert normalize_unit("Feet") == "ft" and normalize_unit("US-ft") == "us_ft"
    with pytest.raises(ParameterError):
        normalize_unit("furlong")


def test_to_row_in_feet():
    row = rec().to_row("ft")
    assert row["length_unit"] == "ft"
    assert row["x"] == pytest.approx(1000) and row["y"] == pytest.approx(2000)
    assert row["breast_height"] == pytest.approx(4.5)
    assert row["dbh"] == pytest.approx(0.508 / 0.3048)
    assert row["dbh_in"] == pytest.approx(20.0)
    assert row["dbh_cm"] == pytest.approx(50.8)
    assert rec().to_row()["x"] == 304.8  # default stays metres


def test_csv_in_feet_and_auto(tmp_path):
    exp = registry.exporter("csv")
    p = exp.export([rec()], tmp_path / "ft.csv", ExportContext(units="ft"))
    row = next(csv.DictReader(p.open()))
    assert row["length_unit"] == "ft" and row["x"] == "1000.0000" and row["dbh_in"] == "20.0000"
    p = exp.export([rec()], tmp_path / "m.csv", ExportContext(units="ft"), units="m")
    assert next(csv.DictReader(p.open()))["x"] == "304.8000"  # explicit option wins


def test_dxf_in_feet(tmp_path):
    p = registry.exporter("dxf").export([rec()], tmp_path / "t.dxf", ExportContext(units="ft"))
    doc = ezdxf.readfile(p)
    assert doc.header["$INSUNITS"] == 2 and doc.header["$MEASUREMENT"] == 0
    c = next(e for e in doc.modelspace() if e.dxftype() == "CIRCLE" and e.dxf.layer == "FIP_DBH")
    assert tuple(c.dxf.center) == pytest.approx((1000, 2000, 31.8516 / 0.3048))
    assert c.dxf.radius == pytest.approx(0.254 / 0.3048)
    label = next(e for e in doc.modelspace() if e.dxftype() == "TEXT").dxf.text
    assert "20.0in" in label
    p = registry.exporter("dxf").export([rec()], tmp_path / "u.dxf", ExportContext(), units="us_ft")
    assert ezdxf.readfile(p).header["$INSUNITS"] == 21


def test_load_points_in_feet(tmp_path):
    rng = np.random.default_rng(0)
    pts_ft = np.c_[rng.uniform(0, 30, 500), rng.uniform(0, 30, 500), rng.uniform(0, 20, 500)]
    np.save(tmp_path / "ft.npy", pts_ft)
    np.testing.assert_allclose(inv.load_points(tmp_path / "ft.npy", units="ft"), pts_ft * 0.3048)
    with pytest.raises(ParameterError):
        inv.load_points(tmp_path / "ft.npy", units="yards")


def test_unit_hint():
    pts = np.c_[np.zeros(10), np.zeros(10), np.linspace(0, 100, 10)]
    assert "feet" in guess_unit_hint(pts)
    assert guess_unit_hint(pts * 0.3) is None


def test_cli_feet_roundtrip(tmp_path):
    """A cloud in feet -> CSV/DXF in feet with original coordinates and 4.5 ft breast height."""
    import subprocess
    import sys

    from forest_inventory_pipeline.synthetic import make_plot

    def fip(*args):
        # a real process (like a user's shell); CliRunner swaps stdout, which breaks the
        # editable-install rebuild hook when the C++ extension is imported
        return subprocess.run(
            [sys.executable, "-m", "forest_inventory_pipeline.fip", *args],
            capture_output=True,
            text=True,
            env={**__import__("os").environ, "COLUMNS": "200", "LOG_LEVEL": "info"},
        )

    plot = make_plot(n_trees=6, seed=1)
    np.save(tmp_path / "plot_ft.npy", plot.points / 0.3048)
    out = tmp_path / "out"
    res = fip("inventory", str(tmp_path / "plot_ft.npy"), "-o", str(out), "--units", "ft")
    assert res.returncode == 0, res.stdout + res.stderr
    rows = [r for r in csv.DictReader((out / "trees.csv").open()) if r["status"] != "failed"]
    assert rows and all(r["length_unit"] == "ft" for r in rows)
    assert all(float(r["breast_height"]) == pytest.approx(4.5) for r in rows)
    truth_ft = np.array([[t["x"], t["y"], t["dbh"]] for t in plot.truth]) / 0.3048
    for r in rows:
        d = np.hypot(truth_ft[:, 0] - float(r["x"]), truth_ft[:, 1] - float(r["y"]))
        assert d.min() < 0.5 / 0.3048  # positions are in the original feet frame
        assert float(r["dbh"]) == pytest.approx(truth_ft[d.argmin(), 2], abs=0.05)
    # calibration reads the feet CSV back, with field data in feet and inches
    with (tmp_path / "field.csv").open("w") as fh:
        fh.write("x,y,dbh\n")
        for t in plot.truth:
            fh.write(f"{t['x'] / 0.3048},{t['y'] / 0.3048},{t['dbh'] / 0.0254}\n")
    res = fip("calibrate", str(out / "trees.csv"), str(tmp_path / "field.csv"), "--dbh-unit", "in")
    assert res.returncode == 0, res.stdout + res.stderr
    out_txt = res.stdout + res.stderr
    assert "matched 6/6" in out_txt, out_txt
    bias_cm = float(re.search(r"bias ([+-][0-9.]+) cm", out_txt).group(1))
    assert abs(bias_cm) < 1.0, out_txt  # a unit mix-up would be off by ~30 %
    assert "--units ft" in out_txt, out_txt  # suggested --model-sd is given in feet
