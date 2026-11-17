"""Why objects were rejected: counts by reason, with the setting to try."""

import os
import subprocess
import sys

import numpy as np

from forest_inventory_pipeline import inventory as inv
from forest_inventory_pipeline.records import TreeRecord


def rec(tid, status, msg=""):
    return TreeRecord(tree_id=tid, status=status, x=0.0, y=0.0, ground_z=0.0, breast_height=1.3,
                      bh_elevation=1.3, n_points=100, message=msg)


def test_reasons_are_counted_and_point_at_a_setting():
    recs = [
        rec(1, "failed", "fitted axis tilt 30.2 deg exceeds max 22.5 deg"),
        rec(2, "failed", "fitted axis tilt 55.4 deg exceeds max 22.5 deg"),
        rec(3, "failed", "only 12 points in the breast-height band [1.05, 1.55] m (need 30)"),
        rec(4, "failed", "not trunk-like: only 40% of points between 1 m and the crown lie on "
                         "the trunk surface; only 300 points per m^2 on the seen trunk surface"),
        rec(5, "failed", "under a roof: 41% of the material around 2.4 m above it lies on one plane"),
        rec(6, "failed", "something new and unexpected"),
        rec(7, "ok"), rec(8, "warning", "low arc coverage"), rec(9, "excluded", "by reviewer"),
    ]
    rows = inv.rejection_summary(recs)
    assert (rows[0]["reason"], rows[0]["count"]) == ("lean beyond the limit", 2)  # most common first
    assert [r["count"] for r in rows] == sorted((r["count"] for r in rows), reverse=True)
    by = {r["reason"]: r for r in rows}
    assert by["lean beyond the limit"]["count"] == 2 and "Max stem tilt" in by["lean beyond the limit"]["setting"]
    assert by["point density too low"]["count"] == 1 and by["too little of the cluster on the trunk"]["count"] == 1
    assert by["under a roof (post)"]["count"] == 1
    assert by["other"]["count"] == 1  # unknown messages are not lost
    assert sum(r["count"] for r in rows) == 7  # 6 rejected objects, one with two reasons
    assert inv.rejection_summary([rec(1, "ok")]) == []


def test_cli_logs_the_rejection_summary(tmp_path):
    from forest_inventory_pipeline.synthetic import make_plot

    plot = make_plot(n_trees=5, seed=6)
    rng = np.random.default_rng(0)  # a shrub-like blob the checks reject
    gx, gy = plot.points[:, 0].min() + 3.0, plot.points[:, 1].min() + 3.0
    gz = float(np.percentile(plot.points[:, 2], 5))
    np.save(tmp_path / "p.npy", np.vstack([plot.points, rng.normal(0, 0.35, (6000, 3)) + [gx, gy, gz + 1.7]]))
    res = subprocess.run([sys.executable, "-m", "forest_inventory_pipeline.fip", "inventory",
                          str(tmp_path / "p.npy"), "-o", str(tmp_path / "o"), "-e", "csv"],
                         capture_output=True, text=True,
                         env={**os.environ, "COLUMNS": "250", "LOG_LEVEL": "info"})
    assert res.returncode == 0, res.stdout + res.stderr
    assert "rejected" in res.stdout and "why:" in res.stdout
