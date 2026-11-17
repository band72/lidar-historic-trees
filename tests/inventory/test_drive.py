"""Whole-drive workflow: every segment measured on its own, georeferenced, merged into one map."""

import csv
import json
import math
import os
import subprocess
import sys

import numpy as np
import pytest

from forest_inventory_pipeline import drive, georef
from forest_inventory_pipeline.errors import InputDataError
from forest_inventory_pipeline.synthetic import make_plot
from test_import_export import write_las

LAT0, LON0 = 30.08, -83.09


def _segment(folder, name, seed, t0, east):
    """A plot in its own local frame, its trajectory, and where it is in the world (east, m)."""
    xyz = make_plot(n_trees=5, size=20.0, seed=seed).points.copy()
    xyz[:, :2] -= 10.0
    write_las(folder / f"{name}.laz", xyz)
    t = t0 + np.arange(0.0, 60.0, 0.5)
    xy = np.column_stack([np.linspace(-30, 30, len(t)), 6 * np.sin(np.linspace(0, 3, len(t)))])
    np.savetxt(folder / f"{name}_trajectory.csv", np.column_stack([t, xy]), delimiter=",",
               header="time_s,x,y", comments="")
    return t, xy + [east, 0.0]


@pytest.fixture
def drive_folder(tmp_path):
    d = tmp_path / "drive"
    d.mkdir()
    parts = [_segment(d, "segment_0-60s", 1, 0.0, 0.0), _segment(d, "segment_100-160s", 2, 100.0, 150.0)]
    t = np.concatenate([p[0] for p in parts])
    en = np.vstack([p[1] for p in parts])
    lat, lon, _ = georef.enu_to_geodetic(np.column_stack([en, np.zeros(len(en))]), LAT0, LON0, 0.0)
    np.savetxt(tmp_path / "gnss.csv", np.column_stack([t, lat, lon]), delimiter=",",
               header="time_s,lat,lon", comments="", fmt="%.9f")
    return d, tmp_path / "gnss.csv"


def test_find_segments_pairs_clouds_with_trajectories(drive_folder, tmp_path):
    d, _ = drive_folder
    segs = drive.find_segments(d)
    assert [(c.name, t.name) for c, t in segs] == [
        ("segment_0-60s.laz", "segment_0-60s_trajectory.csv"),
        ("segment_100-160s.laz", "segment_100-160s_trajectory.csv")]
    (d / "segment_0-60s_trajectory.csv").unlink()
    with pytest.raises(InputDataError, match="segment_0-60s_trajectory.csv"):
        drive.find_segments(d)
    with pytest.raises(InputDataError, match="no point cloud"):
        drive.find_segments(tmp_path)


def test_run_drive_makes_one_map(drive_folder, tmp_path):
    d, gnss = drive_folder
    seen = []
    res = drive.run_drive(d, gnss, tmp_path / "out", progress=lambda f, m: seen.append((f, m)))
    rows = list(csv.DictReader((res["merged"] / "trees_merged_wgs84.csv").open()))
    assert len(rows) == res["trees"] >= 8
    lon = np.array([float(r["lon"]) for r in rows])
    east = (lon - LON0) * 111320 * math.cos(math.radians(LAT0))
    assert (east < 40).sum() >= 4 and (east > 110).sum() >= 4  # two places 150 m apart
    assert {s["name"] for s in res["segments"]} == {"segment_0-60s", "segment_100-160s"}
    assert all(s["trees"] >= 4 and s["gnss_rms_m"] < 0.5 for s in res["segments"])
    for f in ("trees_merged.gpx", "field_sheet.csv", "trees.dxf", "merge_report.json"):
        assert (res["merged"] / f).exists()
    assert (tmp_path / "out" / "segment_0-60s" / "trees_wgs84.csv").exists()
    assert seen and seen[-1][0] == 1.0


def test_cli_drive(drive_folder, tmp_path):
    d, gnss = drive_folder
    res = subprocess.run([sys.executable, "-m", "forest_inventory_pipeline.fip", "drive", str(d),
                          "-g", str(gnss), "-o", str(tmp_path / "o")],
                         capture_output=True, text=True,
                         env={**os.environ, "COLUMNS": "220", "LOG_LEVEL": "info"})
    assert res.returncode == 0, res.stdout[-3000:] + res.stderr[-2000:]
    rep = json.loads((tmp_path / "o" / "merged" / "merge_report.json").read_text())
    assert rep["trees_out"] >= 8 and "one map" in res.stdout
