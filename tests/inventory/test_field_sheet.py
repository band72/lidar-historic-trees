"""Field check: field_sheet.csv is written with the field files and read back by fip calibrate."""

import csv
import os
import subprocess
import sys

import numpy as np

from forest_inventory_pipeline import georef


def fip(*args):
    return subprocess.run(
        [sys.executable, "-m", "forest_inventory_pipeline.fip", *args],
        capture_output=True, text=True, env={**os.environ, "COLUMNS": "250"},
    )


def test_field_sheet_lists_the_mapped_trees(tmp_path):
    rows = [
        dict(tree_id="1", status="ok", dbh="0.31", dbh_cm="31.0", dbh_method="breast-height band",
             lat="30.08", lon="-83.09", position_sd_m="0.9"),
        dict(tree_id="2", status="failed", dbh="", lat="30.08", lon="-83.09", position_sd_m="0.9"),
        dict(tree_id="3", status="added", dbh="", lat="30.08", lon="-83.09", position_sd_m="0.9"),
    ]
    georef.write_outputs(rows, list(rows[0]), tmp_path)
    sheet = list(csv.DictReader((tmp_path / "field_sheet.csv").open()))
    assert [r["tree_id"] for r in sheet] == ["1", "3"]
    assert list(sheet[0]) == georef.FIELD_COLUMNS
    assert sheet[0]["predicted_dbh_cm"] == "31.0" and sheet[0]["tape_dbh_cm"] == ""
    assert sheet[1]["dbh_method"] == "added by reviewer"


def test_calibrate_from_a_filled_field_sheet(tmp_path):
    from forest_inventory_pipeline.synthetic import make_plot

    np.save(tmp_path / "plot.npy", make_plot(n_trees=10, seed=11).points)
    out = tmp_path / "run"
    assert fip("inventory", str(tmp_path / "plot.npy"), "-o", str(out), "-e", "csv").returncode == 0
    trees = [r for r in csv.DictReader((out / "trees.csv").open()) if r["status"] in ("ok", "warning")]
    assert len(trees) >= 6
    rng = np.random.default_rng(0)
    with (tmp_path / "field_sheet.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, georef.FIELD_COLUMNS)
        w.writeheader()
        for i, r in enumerate(trees):
            tape = float(r["dbh_cm"]) + rng.normal(0, 0.5)
            not_tree = i == 0
            w.writerow(dict(tree_id=r["tree_id"], tape_dbh_cm="" if not_tree else f"{tape:.1f}",
                            is_tree="no" if not_tree else "yes"))
    res = fip("calibrate", str(out / "trees.csv"), str(tmp_path / "field_sheet.csv"))
    text = res.stdout + res.stderr
    assert res.returncode == 0, text
    assert f"matched {len(trees) - 1}/{len(trees) - 1} reference trees" in text
    assert f"{len(trees)} objects visited, 1 were not trees" in text
