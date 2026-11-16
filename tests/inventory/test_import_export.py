"""LAS metadata (units, CRS, classification), cropping, tiles, and the new exports."""

import csv
import json
from pathlib import Path

import numpy as np
import pytest

from forest_inventory_pipeline import inventory as inv
from forest_inventory_pipeline.crs import parse_geokeys, parse_wkt
from forest_inventory_pipeline.errors import InputDataError, StageError
from forest_inventory_pipeline.plugins import load_plugins, registry
from forest_inventory_pipeline.synthetic import make_plot
from forest_inventory_pipeline.traits.dbh.robust_fit import FitParams

laspy = pytest.importorskip("laspy")

US_FT = 1200 / 3937
WKT_SPCS_USFT = (
    'PROJCS["NAD83 / Florida East (ftUS)",GEOGCS["NAD83",DATUM["North_American_Datum_1983",'
    'SPHEROID["GRS 1980",6378137,298.257222101]],PRIMEM["Greenwich",0],'
    'UNIT["degree",0.0174532925199433]],PROJECTION["Transverse_Mercator"],'
    'PARAMETER["latitude_of_origin",24.3333333333333],PARAMETER["central_meridian",-81],'
    'PARAMETER["scale_factor",0.999941177],PARAMETER["false_easting",656166.667],'
    'PARAMETER["false_northing",0],UNIT["US survey foot",0.304800609601219],'
    'AXIS["Easting",EAST],AXIS["Northing",NORTH],AUTHORITY["EPSG","2236"]]'
)


@pytest.fixture(autouse=True)
def _plugins():
    load_plugins(entry_points=False)


def write_las(path, xyz, cls=None, wkt=None):
    header = laspy.LasHeader(point_format=6, version="1.4")
    header.scales = np.array([0.001, 0.001, 0.001])
    header.offsets = np.floor(xyz.min(axis=0))
    if wkt:
        header.vlrs.append(laspy.vlrs.known.WktCoordinateSystemVlr(wkt))
        header.global_encoding.wkt = True
    las = laspy.LasData(header)
    las.x, las.y, las.z = xyz[:, 0], xyz[:, 1], xyz[:, 2]
    if cls is not None:
        las.classification = cls
    las.write(str(path))


@pytest.fixture(scope="module")
def plot():
    return make_plot(n_trees=8, seed=4)


@pytest.fixture(scope="module")
def las_usft(tmp_path_factory, plot):
    """The demo plot as a State Plane (US survey feet) LAS, ground classified, with noise."""
    d = tmp_path_factory.mktemp("las")
    xyz_m = plot.points + [200_000.0, 400_000.0, 0.0]  # large georeferenced coordinates
    cls = np.where(plot.is_ground, 2, 1).astype(np.uint8)
    noise = np.c_[xyz_m[:50, :2], xyz_m[:50, 2] + 80.0]  # birds / multipath far above
    xyz_m = np.r_[xyz_m, noise]
    cls = np.r_[cls, np.full(50, 18, np.uint8)]
    path = d / "plot_usft.las"
    write_las(path, xyz_m / US_FT, cls, WKT_SPCS_USFT)
    return path


def test_parse_wkt_state_plane():
    info = parse_wkt(WKT_SPCS_USFT)
    assert (info.crs, info.units, info.geographic) == ("EPSG:2236", "us_ft", False)


def test_parse_wkt_geographic_and_utm():
    assert parse_wkt(
        'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563]],'
        'PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433],AUTHORITY["EPSG","4326"]]'
    ).geographic
    utm = parse_wkt(
        'PROJCRS["WGS 84 / UTM zone 32N",BASEGEOGCRS["WGS 84",DATUM["World Geodetic System 1984",'
        'ELLIPSOID["WGS 84",6378137,298.257223563,LENGTHUNIT["metre",1]]]],'
        'CS[Cartesian,2],AXIS["easting",east,LENGTHUNIT["metre",1]],ID["EPSG",32632]]'
    )
    assert (utm.crs, utm.units) == ("EPSG:32632", "m")


def test_parse_geokeys():
    info = parse_geokeys({1024: 1, 3072: 2236, 3076: 9003, 4099: 9001})
    assert (info.crs, info.units, info.z_units) == ("EPSG:2236", "us_ft", "m")
    assert parse_geokeys({1024: 2, 2048: 4326}).geographic


def test_las_units_crs_classification_noise(las_usft, plot):
    cloud = inv.load_cloud(las_usft)  # units=auto
    assert cloud.units == "us_ft" and cloud.crs == "EPSG:2236" and "Florida" in cloud.crs_wkt
    assert len(cloud.xyz) == len(plot.points)  # 50 noise points dropped
    np.testing.assert_allclose(cloud.xyz[:, 0].min(), plot.points[:, 0].min() + 200_000, atol=0.002)
    assert int((cloud.classification == 2).sum()) == int(plot.is_ground.sum())
    kept = inv.load_cloud(las_usft, keep_noise=True)
    assert len(kept.xyz) == len(plot.points) + 50


def test_explicit_units_override_header(las_usft, caplog):
    cloud = inv.load_cloud(las_usft, units="m")
    assert cloud.units == "m"


def test_crop_while_reading(las_usft, plot):
    xmin = (plot.points[:, 0].min() + 200_000) / US_FT
    ymin = (plot.points[:, 1].min() + 400_000) / US_FT
    box = f"{xmin},{ymin},{xmin + 10 / US_FT},{ymin + 10 / US_FT}"  # 10 m x 10 m
    cloud = inv.load_cloud(las_usft, crop=box)
    ext = cloud.xyz.max(0) - cloud.xyz.min(0)
    assert ext[0] <= 10.01 and ext[1] <= 10.01 and len(cloud.xyz) < len(plot.points) / 2
    with pytest.raises(InputDataError, match="crop"):
        inv.load_cloud(las_usft, crop="1,2,3")


def test_folder_of_tiles(tmp_path, plot):
    xyz = plot.points
    left = xyz[:, 0] < np.median(xyz[:, 0])
    write_las(tmp_path / "a.las", xyz[left])
    write_las(tmp_path / "b.las", xyz[~left])
    cloud = inv.load_cloud(tmp_path)
    assert len(cloud.xyz) == len(xyz) and len(cloud.sources) == 2


def test_tiles_with_different_crs_rejected(tmp_path, plot):
    write_las(tmp_path / "a.las", plot.points, wkt=WKT_SPCS_USFT)
    write_las(tmp_path / "b.las", plot.points, wkt=WKT_SPCS_USFT.replace('"2236"', '"2237"'))
    with pytest.raises(InputDataError, match="different coordinate systems"):
        inv.load_cloud(tmp_path)


def test_geographic_las_rejected(tmp_path, plot):
    wkt = (
        'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563]],'
        'PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433],AUTHORITY["EPSG","4326"]]'
    )
    write_las(tmp_path / "ll.las", plot.points, wkt=wkt)
    with pytest.raises(InputDataError, match="longitude/latitude"):
        inv.load_cloud(tmp_path / "ll.las")


def test_ground_from_classification(las_usft):
    cloud = inv.load_cloud(las_usft)
    g, ng = inv.segment_ground(cloud.xyz, inv.GroundParams(), cloud.classification)
    assert len(g) <= int((cloud.classification == 2).sum())
    with pytest.raises(StageError, match="no classification"):
        inv.segment_ground(cloud.xyz, inv.GroundParams(source="classification"), None)


@pytest.fixture(scope="module")
def fitted(las_usft):
    cloud = inv.load_cloud(las_usft)
    gp, sp, fp = inv.GroundParams(), inv.SegmentParams(), FitParams()
    g, ng = inv.segment_ground(cloud.xyz, gp, cloud.classification)
    pts, lab = inv.segment_trees(inv.normalize_heights(g, ng, gp), sp)
    recs = inv.fit_trees(inv.group_trees(pts, lab, 50), g, fp, gp)
    ctx = inv.context_for("plot_usft.las", fp, units="us_ft", crs=cloud.crs, crs_wkt=cloud.crs_wkt)
    return recs, ctx, cloud


def test_points_export_las_and_ply(tmp_path, fitted):
    recs, ctx, cloud = fitted
    exp = registry.exporter("points")
    out = exp.export(recs, tmp_path / "trees.laz", ctx, format="las")
    las = laspy.read(str(out))
    assert out.suffix == ".las" and len(las.points) > 1000
    assert set(np.unique(las.tree_id)) <= {r.tree_id for r in recs}
    wkts = [v.string for v in las.header.vlrs if type(v).__name__ == "WktCoordinateSystemVlr"]
    assert wkts and "2236" in wkts[0]  # the input CRS is carried into the exported cloud
    # real elevation: z (ft) - height_above_ground (ft) equals the terrain, which is well below the points
    z_m = np.asarray(las.z) * US_FT
    assert z_m.min() > cloud.xyz[:, 2].min() - 0.5
    ground_m = (np.asarray(las.z) - np.asarray(las.height_above_ground)) * US_FT
    assert np.all(np.abs(ground_m - z_m) > 0.5)  # slice starts 1 m above ground
    ply = exp.export(recs, tmp_path / "trees.laz", ctx, format="ply")
    assert ply.read_bytes().startswith(b"ply\nformat binary_little_endian")


def test_geojson_points_and_circles(tmp_path, fitted):
    recs, ctx, _ = fitted
    exp = registry.exporter("geojson")
    doc = json.loads(exp.export(recs, tmp_path / "p.geojson", ctx).read_text())
    assert doc["crs"]["properties"]["name"] == "urn:ogc:def:crs:EPSG::2236"
    f = doc["features"][0]
    assert f["geometry"]["type"] == "Point" and f["properties"]["length_unit"] == "us_ft"
    doc = json.loads(exp.export(recs, tmp_path / "c.geojson", ctx, geometry="circle").read_text())
    ring = np.array(doc["features"][0]["geometry"]["coordinates"][0])
    centre = ring[:-1].mean(axis=0)
    radius_ft = np.hypot(*(ring[:-1] - centre).T).mean()
    rec = next(r for r in recs if r.tree_id == doc["features"][0]["id"])
    assert radius_ft == pytest.approx(rec.dbh / 2 / US_FT, rel=0.01)


def test_csv_semicolon_decimal_comma(tmp_path, fitted):
    recs, ctx, _ = fitted
    exp = registry.exporter("csv")
    p = exp.export(recs, tmp_path / "t.csv", ctx, delimiter="semicolon", decimal="comma")
    row = next(csv.DictReader(p.open(), delimiter=";"))
    assert "," in row["dbh"] and "." not in row["dbh"]
    with pytest.raises(Exception, match="semicolon or tab"):
        exp.export(recs, tmp_path / "x.csv", ctx, decimal="comma")


def test_run_metadata_and_prj(tmp_path, fitted):
    recs, ctx, _ = fitted
    written = inv.export_records(recs, tmp_path, ["csv", "geojson"], ctx)
    names = {p.name for p in written}
    assert names == {"trees.csv", "trees.geojson", "trees.prj", "run.json"}
    meta = json.loads((tmp_path / "run.json").read_text())
    assert meta["crs"] == "EPSG:2236" and meta["output_units"] == "us_ft"
    assert {o["file"] for o in meta["outputs"]} == {"trees.csv", "trees.geojson", "trees.prj"}
    assert all(len(o["sha256"]) == 64 for o in meta["outputs"])
    assert "Florida" in (tmp_path / "trees.prj").read_text()


def test_folder_ignores_results_csv(tmp_path, plot):
    write_las(tmp_path / "a.las", plot.points)
    (tmp_path / "trees.csv").write_text("tree_id,status,x,y\n1,ok,3.0,4.0\n")
    (tmp_path / "notes.txt").write_text("plot 7, surveyed in May\n")
    cloud = inv.load_cloud(tmp_path)
    assert [Path(p).name for p in cloud.sources] == ["a.las"]  # not a path.split("/"): Windows
    # a folder holding only text files still works
    only_text = tmp_path / "txt"
    only_text.mkdir()
    np.savetxt(only_text / "p.txt", plot.points[:2000])
    assert len(inv.load_cloud(only_text).xyz) == 2000


def test_partially_declared_units_are_refused(tmp_path, plot):
    write_las(tmp_path / "a.las", plot.points / US_FT, wkt=WKT_SPCS_USFT)
    write_las(tmp_path / "b.las", plot.points)  # declares nothing
    with pytest.raises(InputDataError, match="declare none"):
        inv.load_cloud(tmp_path)
    ok = inv.load_cloud(tmp_path, units="us_ft")  # explicit units resolve it
    assert ok.units == "us_ft"


# --- the three primary input formats: LAS, LAZ, PLY ---------------------------------------


def _write_ply(path, xyz, binary=True, dtype="double", colors=False):
    n = len(xyz)
    props = [("x", dtype), ("y", dtype), ("z", dtype)] + (
        [("red", "uchar"), ("green", "uchar"), ("blue", "uchar")] if colors else []
    )
    header = "ply\nformat {} 1.0\nelement vertex {}\n{}end_header\n".format(
        "binary_little_endian" if binary else "ascii",
        n,
        "".join(f"property {t} {name}\n" for name, t in props),
    )
    if binary:
        np_t = {"double": "<f8", "float": "<f4", "uchar": "u1"}
        rec = np.zeros(n, dtype=[(name, np_t[t]) for name, t in props])
        for i, c in enumerate("xyz"):
            rec[c] = xyz[:, i]
        if colors:
            rec["red"] = rec["green"] = rec["blue"] = 120
        path.write_bytes(header.encode() + rec.tobytes())
    else:
        with open(path, "w") as fh:
            fh.write(header)
            for row in xyz:
                fh.write(
                    " ".join(f"{v:.6f}" for v in row) + (" 120 120 120" if colors else "") + "\n"
                )


@pytest.mark.parametrize("ext", [".las", ".laz"])
@pytest.mark.parametrize("version,fmt", [("1.2", 0), ("1.2", 3), ("1.4", 6), ("1.4", 8)])
def test_las_and_laz_versions_and_point_formats(tmp_path, ext, version, fmt):
    rng = np.random.default_rng(1)
    pts = np.c_[rng.uniform(0, 30, 3000), rng.uniform(0, 30, 3000), rng.uniform(0, 12, 3000)]
    pts += [500_000.0, 5_600_000.0, 100.0]  # UTM-sized coordinates
    header = laspy.LasHeader(point_format=fmt, version=version)
    header.scales = [0.001] * 3
    header.offsets = np.floor(pts.min(axis=0))
    las = laspy.LasData(header)
    las.x, las.y, las.z = pts[:, 0], pts[:, 1], pts[:, 2]
    path = tmp_path / f"t{ext}"
    las.write(str(path), do_compress=ext == ".laz")
    got = inv.load_cloud(path)
    assert len(got.xyz) == len(pts)
    np.testing.assert_allclose(got.xyz, pts, atol=0.001)


@pytest.mark.parametrize(
    "binary,dtype,colors",
    [
        (True, "double", False),
        (True, "float", True),
        (False, "double", False),
        (False, "float", True),
    ],
)
def test_ply_variants(tmp_path, binary, dtype, colors):
    rng = np.random.default_rng(2)
    pts = np.c_[rng.uniform(0, 30, 2000), rng.uniform(0, 30, 2000), rng.uniform(0, 12, 2000)]
    _write_ply(tmp_path / "t.ply", pts, binary, dtype, colors)
    tol = 1e-5 if dtype == "float" else 1e-6
    np.testing.assert_allclose(inv.load_cloud(tmp_path / "t.ply").xyz, pts, atol=tol * 100)


def test_ply_with_large_coordinates_keeps_precision(tmp_path):
    rng = np.random.default_rng(3)
    pts = np.c_[rng.uniform(0, 30, 2000), rng.uniform(0, 30, 2000), rng.uniform(0, 12, 2000)]
    pts += [500_000.0, 5_600_000.0, 0.0]
    _write_ply(tmp_path / "utm.ply", pts, binary=True, dtype="double")
    np.testing.assert_allclose(inv.load_cloud(tmp_path / "utm.ply").xyz, pts, atol=1e-6)


def test_broken_files_give_clear_errors(tmp_path):
    (tmp_path / "bad.laz").write_bytes(b"not a laz file")
    with pytest.raises(InputDataError, match="cannot read LAS/LAZ"):
        inv.load_cloud(tmp_path / "bad.laz")
    (tmp_path / "bad.ply").write_text("ply\nformat ascii 1.0\nelement vertex 3\nend_header\n")
    with pytest.raises(InputDataError):
        inv.load_cloud(tmp_path / "bad.ply")
    (tmp_path / "empty.las").write_bytes(b"")
    with pytest.raises(InputDataError):
        inv.load_cloud(tmp_path / "empty.las")


def test_same_trees_from_las_laz_and_ply(tmp_path, plot):
    """The same plot, stored three ways, must give the same DBH values."""
    xyz = plot.points + [500_000.0, 5_600_000.0, 0.0]
    write_las(tmp_path / "p.las", xyz)
    header = laspy.LasHeader(point_format=6, version="1.4")
    header.scales, header.offsets = [0.001] * 3, np.floor(xyz.min(axis=0))
    las = laspy.LasData(header)
    las.x, las.y, las.z = xyz[:, 0], xyz[:, 1], xyz[:, 2]
    las.write(str(tmp_path / "p.laz"), do_compress=True)
    _write_ply(tmp_path / "p.ply", xyz)
    results = {}
    for name in ("p.las", "p.laz", "p.ply"):
        cloud = inv.load_cloud(tmp_path / name)
        gp, sp, fp = inv.GroundParams(), inv.SegmentParams(), FitParams()
        g, ng = inv.segment_ground(cloud.xyz, gp, cloud.classification)
        pts, lab = inv.segment_trees(inv.normalize_heights(g, ng, gp), sp)
        recs = inv.fit_trees(inv.group_trees(pts, lab, 50), g, fp, gp)
        results[name] = sorted(r.dbh for r in recs if r.fitted)
    assert len(results["p.las"]) >= 6
    # same trees, same DBH; within 2 mm because the multithreaded ground filter can sum in a
    # different order under load (seen once in a full run on a busy machine)
    for other in ("p.laz", "p.ply"):
        assert len(results[other]) == len(results["p.las"])
        assert np.allclose(results[other], results["p.las"], atol=0.002), (other, results)
