"""Georeferencing a local run with GNSS fixes: geodesy, the 2D fit, outputs and the CLI."""

import csv
import json
import math
import os
import subprocess
import sys
import xml.etree.ElementTree as ET

import numpy as np
import pytest

from forest_inventory_pipeline import georef
from forest_inventory_pipeline.errors import InputDataError

LAT0, LON0 = 30.0818, -83.0944  # the 2022-03-08 site


def test_enu_roundtrip_and_scale():
    lat = LAT0 + np.array([0.0, 1e-4, -2e-4])
    lon = LON0 + np.array([0.0, 3e-4, 1e-4])
    enu = georef.geodetic_to_enu(lat, lon, np.zeros(3), LAT0, LON0, 0.0)
    la, lo, h = georef.enu_to_geodetic(enu, LAT0, LON0, 0.0)
    assert np.allclose(la, lat, atol=1e-10) and np.allclose(lo, lon, atol=1e-10)
    assert np.allclose(h, 0.0, atol=1e-4)
    # 1e-4 deg of latitude at 30 N is 11.086 m; of longitude, 9.64 m
    assert enu[1, 1] == pytest.approx(11.086, abs=0.01)
    assert enu[1, 0] == pytest.approx(3 * 9.643, abs=0.03)


def _drive(theta_deg=-13.0, shift=(5.0, -3.0), noise=0.5, seed=0, outliers=0):
    """A curved 200 m drive in a local frame and GNSS fixes of it (2 Hz, noisy)."""
    rng = np.random.default_rng(seed)
    t = np.arange(0.0, 120.0, 0.1) + 1.6e9
    s = np.linspace(0, 1, len(t))
    xy = np.column_stack([150 * s, 40 * np.sin(3 * s)])
    th = math.radians(theta_deg)
    R = np.array([[math.cos(th), -math.sin(th)], [math.sin(th), math.cos(th)]])
    tf = t[::5]
    en = np.column_stack([np.interp(tf, t, xy[:, 0]), np.interp(tf, t, xy[:, 1])]) @ R.T + shift
    en += rng.normal(0, noise, en.shape)
    if outliers:
        en[rng.choice(len(en), outliers, replace=False)] += 25.0
    lat, lon, _ = georef.enu_to_geodetic(np.column_stack([en, np.zeros(len(en))]), LAT0, LON0, 0.0)
    fixes = georef.GnssFixes(tf, lat, lon, np.zeros(len(tf)), np.full(len(tf), noise))
    return t, xy, fixes, R, np.array(shift)


def test_fit_recovers_transform_and_rejects_outliers():
    t, xy, fixes, R, shift = _drive(outliers=6)
    a = georef.fit_alignment(t, xy, fixes)
    assert a.heading_deg == pytest.approx(-13.0, abs=0.3)
    assert a.n_used == a.n_fixes - 6  # the 25 m jumps are dropped
    assert a.rms_m == pytest.approx(0.5 * math.sqrt(2), rel=0.25)
    # a tree 10 m off the track lands where the true transform puts it (within ~GNSS noise / sqrt(n))
    tree = np.array([[70.0, 10.0]])
    want = tree @ R.T + shift
    got = georef.geodetic_to_enu(*a.to_latlon(tree), np.zeros(1), LAT0, LON0, 0.0)[:, :2]
    assert np.linalg.norm(got - want) < 0.3  # both ENU around the same origin
    sd = a.position_sd(tree)[0]
    assert 0.5 <= sd < 1.0  # dominated by the reported GNSS sd


def test_alignment_save_load(tmp_path):
    t, xy, fixes, *_ = _drive()
    a = georef.fit_alignment(t, xy, fixes)
    b = georef.Alignment.load(a.save(tmp_path / "georef.json"))
    assert b == a
    (tmp_path / "bad.json").write_text('{"format": "other"}')
    with pytest.raises(InputDataError):
        georef.Alignment.load(tmp_path / "bad.json")


def test_fit_refuses_without_overlap_or_motion():
    t, xy, fixes, *_ = _drive()
    later = georef.GnssFixes(fixes.time_s + 1000, fixes.lat, fixes.lon, fixes.alt, fixes.h_sd)
    with pytest.raises(InputDataError, match="time span"):
        georef.fit_alignment(t, xy, later)
    with pytest.raises(InputDataError, match="spans only"):
        georef.fit_alignment(t, xy * 0.01, fixes)  # the vehicle hardly moved


def _write_inputs(tmp_path, unit="m"):
    t, xy, fixes, R, shift = _drive(noise=0.3)
    with (tmp_path / "trajectory.csv").open("w") as fh:
        fh.write("time_s,x,y,z\n")
        for ti, (x, y) in zip(t, xy):
            fh.write(f"{ti:.4f},{x:.4f},{y:.4f},0\n")
    fixes.write_csv(tmp_path / "gnss.csv")
    f = 1 / 0.3048 if unit == "ft" else 1.0
    with (tmp_path / "trees.csv").open("w") as fh:
        fh.write("tree_id,status,length_unit,x,y,dbh,dbh_cm,message\n")
        fh.write(f"1,ok,{unit},{70 * f},{10 * f},{0.3 * f},30.0,\n")
        fh.write(f"2,failed,{unit},{20 * f},{0},,,too few points\n")
        fh.write(f"3,excluded,{unit},{30 * f},{0},{0.2 * f},20.0,shrub\n")
    return R, shift


@pytest.mark.parametrize("unit", ["m", "ft"])
def test_georeference_trees_and_outputs(tmp_path, unit):
    R, shift = _write_inputs(tmp_path, unit)
    t, xy = georef.read_trajectory(tmp_path / "trajectory.csv")
    a = georef.fit_alignment(t, xy, georef.read_gnss(tmp_path / "gnss.csv"))
    rows, cols = georef.georeference_trees(tmp_path / "trees.csv", a)
    assert cols[-3:] == ["lat", "lon", "position_sd_m"]
    en = georef.geodetic_to_enu(float(rows[0]["lat"]), float(rows[0]["lon"]), 0.0, LAT0, LON0, 0.0)
    assert np.linalg.norm(en[:2] - (np.array([70.0, 10.0]) @ R.T + shift)) < 0.3  # feet converted
    written = georef.write_outputs(rows, cols, tmp_path / "out")
    names = sorted(p.name for p in written)
    assert names == ["trees.gpx", "trees_wgs84.csv", "trees_wgs84.geojson"]
    assert len(list(csv.DictReader((tmp_path / "out" / "trees_wgs84.csv").open()))) == 3
    fc = json.loads((tmp_path / "out" / "trees_wgs84.geojson").read_text())
    assert [f["properties"]["tree_id"] for f in fc["features"]] == [1]  # only measured trees
    lon, lat = fc["features"][0]["geometry"]["coordinates"]  # RFC 7946 order: lon, lat
    assert lat == pytest.approx(float(rows[0]["lat"])) and -84 < lon < -83
    gpx = ET.parse(tmp_path / "out" / "trees.gpx").getroot()
    wpts = gpx.findall("{http://www.topografix.com/GPX/1/1}wpt")
    assert len(wpts) == 1 and float(wpts[0].get("lat")) == pytest.approx(lat)


def test_read_gnss_from_ros1_bag(tmp_path):
    rosbags = pytest.importorskip("rosbags")  # noqa: F841
    from rosbags.rosbag1 import Writer
    from rosbags.typesys import Stores, get_typestore

    ts = get_typestore(Stores.ROS1_NOETIC)
    NavSatFix, NavSatStatus = (
        ts.types["sensor_msgs/msg/NavSatFix"],
        ts.types["sensor_msgs/msg/NavSatStatus"],
    )
    Header, Time = ts.types["std_msgs/msg/Header"], ts.types["builtin_interfaces/msg/Time"]
    bag = tmp_path / "gnss.bag"
    with Writer(bag) as w:
        conn = w.add_connection("/gnss1/fix", NavSatFix.__msgtype__, typestore=ts)
        for i, status in enumerate([0, 0, -1, 0]):
            msg = NavSatFix(
                header=Header(seq=i, stamp=Time(sec=100 + i, nanosec=500_000_000), frame_id="gnss"),
                status=NavSatStatus(status=status, service=1),
                latitude=LAT0 + i * 1e-5,
                longitude=LON0,
                altitude=-14.0,
                position_covariance=np.array([0.81, 0, 0, 0, 0.81, 0, 0, 0, 4.0]),
                position_covariance_type=2,
            )
            w.write(conn, (100 + i) * 10**9, ts.serialize_ros1(msg, NavSatFix.__msgtype__))
    fixes = georef.read_gnss(bag)
    assert len(fixes) == 3  # the no-fix message is dropped
    assert fixes.time_s[0] == pytest.approx(100.5)
    assert fixes.h_sd[0] == pytest.approx(0.9)
    with pytest.raises(InputDataError, match="no NavSatFix"):
        georef.read_gnss(bag, ["/gnss9/fix"])


def test_cli_georeference(tmp_path):
    _write_inputs(tmp_path)
    run = lambda *a: subprocess.run(  # noqa: E731
        [sys.executable, "-m", "forest_inventory_pipeline.fip", "georeference", *a],
        capture_output=True,
        text=True,
        env={**os.environ, "COLUMNS": "200"},
    )
    res = run(
        str(tmp_path / "trees.csv"),
        "-t",
        str(tmp_path / "trajectory.csv"),
        "-g",
        str(tmp_path / "gnss.csv"),
        "-o",
        str(tmp_path / "geo"),
    )
    assert res.returncode == 0, res.stdout + res.stderr
    for name in ("trees_wgs84.csv", "trees_wgs84.geojson", "trees.gpx", "georef.json"):
        assert (tmp_path / "geo" / name).exists(), name
    # reuse the saved transform (e.g. for a re-run of the same segment)
    res = run(
        str(tmp_path / "trees.csv"),
        "-a",
        str(tmp_path / "geo" / "georef.json"),
        "-o",
        str(tmp_path / "geo2"),
    )
    assert res.returncode == 0, res.stdout + res.stderr
    a = (tmp_path / "geo" / "trees_wgs84.csv").read_text()
    assert a == (tmp_path / "geo2" / "trees_wgs84.csv").read_text()
    res = run(str(tmp_path / "trees.csv"), "-o", str(tmp_path / "geo3"))
    assert res.returncode == 2 and "--alignment" in res.stdout + res.stderr
