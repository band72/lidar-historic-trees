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


# --- saved reviews (exclusions.json) -------------------------------------------------------

from forest_inventory_pipeline import exclusions as excl  # noqa: E402
from forest_inventory_pipeline.errors import InputDataError  # noqa: E402


def test_reason_in_message_and_column():
    out = inv.apply_exclusions([rec(1), rec(2)], {2: "shrub"})
    assert out[1].message == "excluded by reviewer: shrub (was ok)"
    assert out[1].to_row()["exclusion_reason"] == "shrub"
    assert "exclusion_reason" not in out[0].extra


def test_export_writes_exclusions_file_and_roundtrips(tmp_path):
    from forest_inventory_pipeline.traits.dbh.robust_fit import FitParams

    out = inv.apply_exclusions([rec(1), rec(2), rec(3)], {2: "shrub", 3: ""})
    inv.export_records(out, tmp_path, ["csv"], inv.context_for("plot.laz", FitParams()))
    doc = json.loads((tmp_path / "exclusions.json").read_text())
    assert doc["format"] == excl.FORMAT and doc["version"] == excl.VERSION
    assert [t["tree_id"] for t in doc["trees"]] == [2, 3]
    assert doc["trees"][0] == {"tree_id": 2, "x": 2.0, "y": 0.0, "dbh": 0.3, "reason": "shrub"}
    run = json.loads((tmp_path / "run.json").read_text())
    assert "exclusions.json" in {o["file"] for o in run["outputs"]}
    loaded = excl.load(tmp_path)  # a folder means its exclusions.json
    assert [(e.tree_id, e.reason) for e in loaded] == [(2, "shrub"), (3, "")]


def test_no_exclusions_file_without_exclusions(tmp_path):
    from forest_inventory_pipeline.traits.dbh.robust_fit import FitParams

    inv.export_records([rec(1)], tmp_path, ["csv"], inv.context_for("x", FitParams()))
    assert not (tmp_path / "exclusions.json").exists()


def test_match_by_position_when_ids_change():
    # a re-run numbers the trees differently; positions carry the review over
    saved = [excl.Exclusion(tree_id=2, x=2.0, y=0.0, dbh=0.3, reason="shrub")]
    new = [rec(7), rec(9)]
    new[0].x, new[1].x = 2.1, 5.0  # tree 7 is the saved stem, 10 cm away
    matched, unmatched = excl.match(new, saved)
    assert matched == {7: "shrub"} and unmatched == []


def test_match_is_one_to_one_and_respects_tolerance():
    a, b = rec(1), rec(2)
    a.x, b.x = 0.0, 0.25
    saved = [excl.Exclusion(x=0.05, y=0.0), excl.Exclusion(x=0.1, y=0.0), excl.Exclusion(x=3, y=0)]
    matched, unmatched = excl.match([a, b], saved, tolerance=0.2)
    assert set(matched) == {1, 2}  # each tree claimed once, closest pairs first
    assert len(unmatched) == 1 and unmatched[0].x == 3


def test_failed_trees_are_never_matched_and_id_fallback():
    saved = [excl.Exclusion(tree_id=3), excl.Exclusion(tree_id=1)]
    matched, unmatched = excl.match([rec(1), rec(3, "failed", float("nan"))], saved)
    assert matched == {1: ""} and [e.tree_id for e in unmatched] == [3]


def test_load_accepts_id_list_and_run_json(tmp_path):
    (tmp_path / "ids.json").write_text("[4, 5]")
    assert [e.tree_id for e in excl.load(tmp_path / "ids.json")] == [4, 5]
    (tmp_path / "run.json").write_text(json.dumps({"parameters": {"excluded_by_reviewer": [8]}}))
    assert [e.tree_id for e in excl.load(tmp_path / "run.json")] == [8]


@pytest.mark.parametrize(
    "content, why",
    [
        ("{not json", "not valid JSON"),
        ('{"format": "something else"}', "not an exclusions file"),
        (json.dumps({"format": excl.FORMAT, "version": 99, "trees": []}), "version 99"),
        (json.dumps({"format": excl.FORMAT, "units": "ft", "trees": []}), "metres"),
        (json.dumps({"format": excl.FORMAT, "trees": [{"reason": "x"}]}), "neither tree_id"),
        (json.dumps({"format": excl.FORMAT, "trees": [{"tree_id": "abc"}]}), "bad tree_id"),
    ],
)
def test_load_rejects_bad_files(tmp_path, content, why):
    f = tmp_path / "e.json"
    f.write_text(content)
    with pytest.raises(InputDataError, match=why):
        excl.load(f)


def test_cli_reapplies_saved_review(tmp_path):
    """Run, exclude a tree, re-run with --exclusions: the same stem is excluded again."""
    import os
    import subprocess
    import sys

    import numpy as np

    from forest_inventory_pipeline.synthetic import make_plot

    def fip(*args):
        return subprocess.run(
            [sys.executable, "-m", "forest_inventory_pipeline.fip", *args],
            capture_output=True,
            text=True,
            env={**os.environ, "COLUMNS": "200", "LOG_LEVEL": "info"},
        )

    plot = make_plot(n_trees=5, seed=6)
    np.save(tmp_path / "plot.npy", plot.points)
    first = tmp_path / "first"
    res = fip("inventory", str(tmp_path / "plot.npy"), "-o", str(first), "-e", "csv")
    assert res.returncode == 0, res.stdout + res.stderr
    rows = [r for r in csv.DictReader((first / "trees.csv").open()) if r["status"] != "failed"]
    target = rows[0]
    excl.save(
        tmp_path / "review.json",
        [excl.Exclusion(x=float(target["x"]) + 0.05, y=float(target["y"]), reason="shrub")],
    )
    second = tmp_path / "second"
    res = fip(
        "inventory",
        str(tmp_path / "plot.npy"),
        "-o",
        str(second),
        "-e",
        "csv",
        "--exclusions",
        str(tmp_path / "review.json"),
    )
    assert res.returncode == 0, res.stdout + res.stderr
    after = {r["tree_id"]: r for r in csv.DictReader((second / "trees.csv").open())}
    assert after[target["tree_id"]]["status"] == "excluded"
    assert after[target["tree_id"]]["exclusion_reason"] == "shrub"
    assert sum(r["status"] == "excluded" for r in after.values()) == 1
    assert json.loads((second / "exclusions.json").read_text())["trees"][0]["reason"] == "shrub"
