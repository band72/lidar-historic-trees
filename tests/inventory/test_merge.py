"""Merging georeferenced runs: UTM projection, duplicate handling, outputs and the CLI."""

import csv
import json
import math

import numpy as np
import pytest

from forest_inventory_pipeline import georef, merge

LAT0, LON0 = 30.0818, -83.0944


def test_utm_on_central_meridian_matches_the_meridian_arc():
    # WGS84 meridian arc to 45 deg: 4,984,944.378 m (scaled by k0 = 0.9996 on the meridian)
    e, n, zone, north = merge.latlon_to_utm([45.0], [9.0])
    assert zone == 32 and north
    assert e[0] == pytest.approx(500000.0, abs=1e-3)
    assert n[0] == pytest.approx(0.9996 * 4984944.378, abs=0.01)
    _, s, _, north = merge.latlon_to_utm([-45.0], [9.0])
    assert not north and s[0] == pytest.approx(10_000_000 - 0.9996 * 4984944.378, abs=0.01)


def test_utm_is_symmetric_and_keeps_local_distances():
    e, n, _, _ = merge.latlon_to_utm([LAT0, LAT0], [-81 - 0.5, -81 + 0.5], zone=17)
    assert e[0] + e[1] == pytest.approx(1_000_000, abs=1e-3) and n[0] == pytest.approx(n[1])
    # 100 m apart in ENU -> same distance on the grid, within the UTM scale factor
    lat, lon, _ = georef.enu_to_geodetic(np.array([[0.0, 0, 0], [60.0, 80.0, 0]]), LAT0, LON0, 0.0)
    e, n, zone, _ = merge.latlon_to_utm(lat, lon)
    assert zone == 17
    assert math.hypot(e[1] - e[0], n[1] - n[0]) / 100.0 == pytest.approx(1.0, abs=6e-4)


def _write_run(folder, trees, unit="ft"):
    """trees: (tree_id, east_m, north_m, dbh_m, status, dbh_method)."""
    folder.mkdir(parents=True)
    f = 1 / 0.3048 if unit == "ft" else 1.0
    lat, lon, _ = georef.enu_to_geodetic(
        np.array([[t[1], t[2], 0.0] for t in trees]), LAT0, LON0, 0.0
    )
    cols = ["tree_id", "status", "length_unit", "dbh", "dbh_cm", "ci_low", "ci_high",
            "dbh_method", "message", "lat", "lon", "position_sd_m"]
    with (folder / "trees_wgs84.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, cols)
        w.writeheader()
        for (tid, _, _, dbh, status, method), la, lo in zip(trees, lat, lon):
            w.writerow(dict(tree_id=tid, status=status, length_unit=unit, dbh=dbh * f,
                            dbh_cm=dbh * 100, ci_low=dbh * 0.9 * f, ci_high=dbh * 1.1 * f,
                            dbh_method=method, message="", lat=la, lon=lo, position_sd_m=0.9))
        w.writerow(dict(tree_id=99, status="failed", length_unit=unit, dbh="", lat=LAT0, lon=LON0))


BH = "breast-height band"


def test_tree_seen_in_two_runs_is_kept_once_with_the_better_measurement(tmp_path):
    _write_run(tmp_path / "a", [(1, 0, 0, 0.40, "warning", BH), (2, 10, 0, 0.20, "ok", BH)])
    _write_run(tmp_path / "b", [(5, 0.8, 0.6, 0.41, "ok", BH),  # tree 1 again, 1 m off
                                (6, 11.5, 0, 0.60, "ok", BH),  # near tree 2 but much bigger
                                (7, 30, 0, 0.30, "ok", BH)], unit="m")
    trees = merge.read_run(tmp_path / "a") + merge.read_run(tmp_path / "b")
    assert len(trees) == 5  # failed rows skipped; feet converted
    assert trees[0].dbh == pytest.approx(0.40, abs=1e-6)
    res = merge.merge_trees(trees)
    assert len(res.trees) == 4
    assert res.pairs == [{"kept": "b#5", "dropped": "a#1", "distance_m": 1.0, "dbh_cm": [41.0, 40.0]}]
    kept = next(t for t in res.trees if t.tree_id == 5)
    assert kept.also_seen == ["a"]


def test_same_run_trees_are_never_merged(tmp_path):
    _write_run(tmp_path / "a", [(1, 0, 0, 0.30, "ok", BH), (2, 0.5, 0, 0.30, "ok", BH)])
    assert len(merge.merge_trees(merge.read_run(tmp_path / "a")).trees) == 2


def test_cli_writes_one_map(tmp_path):
    import os
    import subprocess
    import sys

    _write_run(tmp_path / "seg1", [(1, 0, 0, 0.40, "ok", BH), (2, 10, 0, 0.25, "warning",
                                    "visible width (lower bound)")])
    _write_run(tmp_path / "seg2", [(3, 0.5, 0.2, 0.42, "ok", BH)])
    out = tmp_path / "all"
    res = subprocess.run(
        [sys.executable, "-m", "forest_inventory_pipeline.fip", "merge",
         str(tmp_path / "seg1"), str(tmp_path / "seg2"), "-o", str(out)],
        capture_output=True, text=True, env={**os.environ, "COLUMNS": "200"},
    )
    assert res.returncode == 0, res.stdout + res.stderr
    rows = list(csv.DictReader((out / "trees_merged_wgs84.csv").open()))
    assert [r["tree_id"] for r in rows] == ["1", "2"]
    assert {r["crs"] for r in rows} == {"EPSG:32617"}
    assert (out / "trees_merged.gpx").read_text().count("<wpt") == 2
    assert (out / "trees.dxf").exists()
    rep = json.loads((out / "merge_report.json").read_text())
    assert rep["trees_in"] == 3 and rep["trees_out"] == 2 and rep["duplicates_merged"] == 1

    bad = subprocess.run(
        [sys.executable, "-m", "forest_inventory_pipeline.fip", "merge", str(tmp_path), "-o", str(out)],
        capture_output=True, text=True, env={**os.environ, "COLUMNS": "200"},
    )
    assert bad.returncode == 2 and "fip georeference" in bad.stdout + bad.stderr


def test_runs_are_tied_by_shared_trees_before_merging(tmp_path):
    """Two segments' GNSS alignments disagree by 3.6 m (beyond the 3 m cap): matching on
    distance alone leaves the shared trees doubled; tying on them removes the bias."""
    rng = np.random.default_rng(3)
    xy = rng.uniform(0, 60, (8, 2))
    dbh = rng.uniform(0.15, 0.6, 8)
    a = [(i, x, y, d, "ok", BH) for i, ((x, y), d) in enumerate(zip(xy, dbh))]
    a += [(20, 100.0, 0.0, 0.3, "ok", BH)]  # seen only in run a
    jitter = rng.normal(0, 0.15, (8, 2))
    b = [(50 + i, x + 3.0 + jx, y - 2.0 + jy, d * 1.02, "ok", BH)
         for i, ((x, y), (jx, jy), d) in enumerate(zip(xy, jitter, dbh))]
    _write_run(tmp_path / "a", a)
    _write_run(tmp_path / "b", b)
    trees = merge.read_run(tmp_path / "a") + merge.read_run(tmp_path / "b")
    res = merge.merge_trees(trees, run_quality={"a": 0.5, "b": 2.7})
    assert len(res.trees) == 9 and len(res.pairs) == 8
    (off,) = res.offsets
    assert off["run"] == "b" and off["tie_trees"] == 8
    assert off["shift_east_m"] == pytest.approx(-3.0, abs=0.2)
    assert off["shift_north_m"] == pytest.approx(2.0, abs=0.2)
    assert all(t.run not in t.also_seen for t in res.trees)
    # without tying, the 3.6 m bias leaves the shared trees doubled
    trees = merge.read_run(tmp_path / "a") + merge.read_run(tmp_path / "b")
    assert len(merge.merge_trees(trees, tie=False).trees) > 9


def test_neighbouring_trees_do_not_tie_runs(tmp_path):
    """Different trees that happen to be near each other give scattered offsets: no shift."""
    rng = np.random.default_rng(4)
    _write_run(tmp_path / "a", [(i, *rng.uniform(0, 80, 2), 0.3, "ok", BH) for i in range(10)])
    _write_run(tmp_path / "b", [(i, *rng.uniform(0, 80, 2), 0.3, "ok", BH) for i in range(10)])
    res = merge.merge_trees(merge.read_run(tmp_path / "a") + merge.read_run(tmp_path / "b"))
    assert res.offsets[0]["tie_trees"] == 0


def test_log_files_are_not_runs(tmp_path):
    (tmp_path / "seg.log").write_text("not a csv")
    from forest_inventory_pipeline.errors import InputDataError

    with pytest.raises(InputDataError, match="not a trees CSV"):
        merge.read_run(tmp_path / "seg.log")
