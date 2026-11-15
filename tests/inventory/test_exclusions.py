"""Reviewer exclusions: kept in the CSV as 'excluded', left out of totals, DXF and GeoJSON."""

import csv
import json

import pytest

from forest_inventory_pipeline import inventory as inv
from forest_inventory_pipeline.records import TreeRecord

ezdxf = pytest.importorskip("ezdxf")


def rec(tid, status="ok", dbh=0.3):
    return TreeRecord(
        tree_id=tid,
        status=status,
        x=float(tid),
        y=0.0,
        ground_z=0.0,
        breast_height=1.3,
        bh_elevation=1.3,
        n_points=100,
        dbh=dbh,
        dbh_se=0.01,
        ci_low=dbh - 0.02,
        ci_high=dbh + 0.02,
        confidence=0.95,
        ci_method="analytic",
        message="low arc coverage" if status == "warning" else "",
    )


def test_apply_exclusions_marks_and_does_not_mutate():
    records = [rec(1), rec(2, "warning"), rec(3, "failed", float("nan"))]
    out = inv.apply_exclusions(records, {2, 3})
    assert [r.status for r in out] == ["ok", "excluded", "failed"]  # failed stays failed
    assert "excluded by reviewer (was warning: low arc coverage)" == out[1].message
    assert records[1].status == "warning"  # the cached input is untouched
    assert not out[1].fitted
    s = inv.summarize(out)
    assert (s["ok"], s["excluded"], s["failed"]) == (1, 1, 1)
    assert s["basal_area_m2"] == pytest.approx(3.14159 * 0.15**2, rel=1e-3)  # only tree 1 counts


def test_exports_respect_exclusions(tmp_path):
    out = inv.apply_exclusions([rec(1), rec(2)], [2])
    ctx = inv.context_for(
        "x",
        __import__(
            "forest_inventory_pipeline.traits.dbh.robust_fit", fromlist=["FitParams"]
        ).FitParams(),
        excluded_by_reviewer=[2],
    )
    inv.export_records(out, tmp_path, ["csv", "dxf", "geojson"], ctx)
    rows = {r["tree_id"]: r for r in csv.DictReader((tmp_path / "trees.csv").open())}
    assert rows["2"]["status"] == "excluded" and rows["2"]["dbh"] != ""  # measurement kept
    circles = [
        e
        for e in ezdxf.readfile(tmp_path / "trees.dxf").modelspace()
        if e.dxftype() == "CIRCLE" and e.dxf.layer.startswith("FIP_DBH")
    ]
    assert len(circles) == 1
    assert len(json.loads((tmp_path / "trees.geojson").read_text())["features"]) == 1
    assert json.loads((tmp_path / "run.json").read_text())["parameters"][
        "excluded_by_reviewer"
    ] == [2]
