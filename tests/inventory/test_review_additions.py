"""A reviewer keeps a rejected cluster as a tree: status 'added', saved, reapplied, exported."""

import csv
import json

import numpy as np
import pytest

from forest_inventory_pipeline import exclusions as excl
from forest_inventory_pipeline import georef
from forest_inventory_pipeline import inventory as inv
from forest_inventory_pipeline.errors import InputDataError
from forest_inventory_pipeline.records import TreeRecord


def rec(tid, status="ok", dbh=0.3, x=None, msg=""):
    return TreeRecord(
        tree_id=tid, status=status, x=float(tid) if x is None else x, y=0.0, ground_z=0.0,
        breast_height=1.3, bh_elevation=1.3, n_points=100,
        dbh=dbh if status != "failed" else float("nan"),
        ci_low=dbh * 0.9 if status != "failed" else float("nan"),
        ci_high=dbh * 1.1 if status != "failed" else float("nan"),
        confidence=0.95, message=msg,
    )


def test_apply_additions_marks_failed_only_and_does_not_mutate():
    recs = [rec(1), rec(2, "failed", msg="fitted axis tilt 30 deg exceeds max")]
    out = inv.apply_additions(recs, {2: "leaning tree", 1: "x"})
    assert recs[1].status == "failed"  # input untouched
    assert out[0].status == "ok"  # measured trees are not 'added'
    a = out[1]
    assert a.status == "added" and a.located and not a.fitted
    assert "leaning tree" in a.message and "tilt 30" in a.message
    s = inv.summarize(out)
    assert s["added"] == 1 and s["ok"] == 1 and s["mean_dbh"] == pytest.approx(0.3)


def test_review_file_roundtrip_with_both_actions(tmp_path):
    recs = inv.apply_additions(
        inv.apply_exclusions([rec(1), rec(2), rec(3, "failed")], {1: "shrub"}), {3: "tree"}
    )
    saved = excl.excluded_in(recs)
    assert {(e.tree_id, e.action) for e in saved} == {(1, "exclude"), (3, "add")}
    p = excl.save(tmp_path / "exclusions.json", saved)
    doc = json.loads(p.read_text())
    assert doc["version"] == 2
    assert [t.get("action", "exclude") for t in doc["trees"]] == ["exclude", "add"]
    assert {(e.tree_id, e.action) for e in excl.load(p)} == {(1, "exclude"), (3, "add")}


def test_version_1_files_still_load_and_bad_actions_fail(tmp_path):
    p = tmp_path / "v1.json"
    p.write_text(json.dumps({"format": excl.FORMAT, "version": 1, "units": "m",
                             "trees": [{"tree_id": 4, "x": 4.0, "y": 0.0}]}))
    assert excl.load(p)[0].action == "exclude"
    p.write_text(json.dumps({"format": excl.FORMAT, "version": 2, "units": "m",
                             "trees": [{"tree_id": 4, "action": "promote"}]}))
    with pytest.raises(InputDataError, match="unknown action"):
        excl.load(p)


def test_additions_match_rejected_clusters_by_position():
    recs = [rec(1, x=0.0), rec(7, "failed", x=5.0), rec(8, "failed", x=20.0)]
    entries = [excl.Exclusion(x=5.4, y=0.0, action="add"), excl.Exclusion(x=0.05, y=0.0)]
    added, unmatched = excl.match(recs, entries, action="add")
    assert added == {7: ""} and not unmatched  # 0.4 m: within the doubled tolerance
    excluded, _ = excl.match(recs, entries)
    assert excluded == {1: ""}  # exclusions never touch failed clusters


def test_added_trees_reach_every_location_output(tmp_path):
    recs = inv.apply_additions([rec(1), rec(2, "failed", msg="rejected")], {2: "tree"})
    ctx = inv.context_for("test", inv.FitParams())
    inv.export_records(recs, tmp_path, ["csv", "geojson", "dxf"], ctx)
    rows = {r["tree_id"]: r for r in csv.DictReader((tmp_path / "trees.csv").open())}
    assert rows["2"]["status"] == "added" and rows["2"]["dbh"] in ("", "nan")
    gj = json.loads((tmp_path / "trees.geojson").read_text())
    assert sorted(f["id"] for f in gj["features"]) == [1, 2]
    import ezdxf

    doc = ezdxf.readfile(tmp_path / "trees.dxf")
    assert len(doc.modelspace().query('LINE[layer=="FIP_ADDED"]')) == 2  # the cross
    assert any("added" in t.dxf.text for t in doc.modelspace().query("TEXT"))
    # GPX via georeference: added trees are waypoints, named as such
    gpx_rows = [dict(r, lat="30.0", lon="-83.0", position_sd_m="1.0") for r in rows.values()]
    georef.write_outputs(gpx_rows, list(gpx_rows[0]), tmp_path / "geo")
    gpx = (tmp_path / "geo" / "trees.gpx").read_text()
    assert gpx.count("<wpt") == 2 and "T2 added" in gpx


def test_merge_keeps_added_trees_and_prefers_measured_ones(tmp_path):
    from forest_inventory_pipeline import merge

    a = merge.Tree("a", 1, 30.0, -83.0, 0.9, "added", float("nan"), float("nan"), float("nan"), "", "")
    b = merge.Tree("b", 5, 30.0, -83.0, 0.9, "ok", 0.3, 0.28, 0.32, "breast-height band", "")
    res = merge.merge_trees([a, b])
    assert len(res.trees) == 1 and res.trees[0].status == "ok"
    out = merge.write_merged(merge.merge_trees([a]), tmp_path, ["a"])
    assert any(p.name == "trees.dxf" for p in out)


def test_cli_reapplies_a_saved_addition(tmp_path):
    import os
    import subprocess
    import sys

    from forest_inventory_pipeline.synthetic import make_plot

    def fip(*args):
        return subprocess.run([sys.executable, "-m", "forest_inventory_pipeline.fip", *args],
                              capture_output=True, text=True, env={**os.environ, "COLUMNS": "200"})

    plot = make_plot(n_trees=5, seed=6)
    rng = np.random.default_rng(0)  # a shrub-like blob the checks reject
    gx, gy = plot.points[:, 0].min() + 3.0, plot.points[:, 1].min() + 3.0
    gz = float(np.percentile(plot.points[:, 2], 5))
    blob = rng.normal(0, 0.35, (6000, 3)) + [gx, gy, gz + 1.7]
    np.save(tmp_path / "plot.npy", np.vstack([plot.points, blob]))
    first = tmp_path / "first"
    assert fip("inventory", str(tmp_path / "plot.npy"), "-o", str(first), "-e", "csv").returncode == 0
    failed = [r for r in csv.DictReader((first / "trees.csv").open()) if r["status"] == "failed"]
    if not failed:
        pytest.skip("this synthetic plot has no rejected cluster")
    t = failed[0]
    excl.save(tmp_path / "review.json",
              [excl.Exclusion(x=float(t["x"]), y=float(t["y"]), action="add", reason="tree")])
    second = tmp_path / "second"
    res = fip("inventory", str(tmp_path / "plot.npy"), "-o", str(second), "-e", "csv",
              "--exclusions", str(tmp_path / "review.json"))
    assert res.returncode == 0, res.stdout + res.stderr
    after = {r["tree_id"]: r for r in csv.DictReader((second / "trees.csv").open())}
    assert after[t["tree_id"]]["status"] == "added"
    params = json.loads((second / "run.json").read_text())["parameters"]
    assert params["added_by_reviewer"] == [int(t["tree_id"])]
