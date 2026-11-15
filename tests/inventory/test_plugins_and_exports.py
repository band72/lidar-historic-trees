import csv
import math
import textwrap

import numpy as np
import pytest

from forest_inventory_pipeline.errors import ExportError
from forest_inventory_pipeline.plugins import ExportContext, load_plugins, registry
from forest_inventory_pipeline.records import TreeRecord

ezdxf = pytest.importorskip("ezdxf")


def records():
    ok = TreeRecord(
        tree_id=1,
        status="ok",
        x=10.0,
        y=20.0,
        ground_z=250.0,
        breast_height=1.3,
        bh_elevation=251.3,
        n_points=500,
        dbh=0.42,
        dbh_se=0.004,
        ci_low=0.41,
        ci_high=0.43,
        confidence=0.95,
        ci_method="analytic",
        n_band_points=200,
        inlier_ratio=0.8,
        rmse=0.003,
        arc_coverage=0.9,
        max_gap_deg=30,
        tilt_deg=1.0,
    )
    warn = TreeRecord(
        tree_id=2,
        status="warning",
        x=12.0,
        y=21.0,
        ground_z=250.5,
        breast_height=1.3,
        bh_elevation=251.8,
        n_points=300,
        dbh=0.30,
        dbh_se=0.01,
        ci_low=0.28,
        ci_high=0.32,
        confidence=0.95,
        ci_method="analytic",
        message="low arc coverage",
    )
    failed = TreeRecord(
        tree_id=3,
        status="failed",
        x=15.0,
        y=22.0,
        ground_z=251.0,
        breast_height=1.3,
        bh_elevation=252.3,
        n_points=40,
        message="only 3 points in band",
    )
    for r in (ok, warn):
        r.extra.update(registry.metrics["basal_area"].compute(r))
    return [ok, warn, failed]


@pytest.fixture(autouse=True)
def _plugins():
    load_plugins(entry_points=False)


def test_builtins_registered():
    assert {"csv", "dxf"} <= set(registry.exporters)
    assert {"open3d", "las", "numpy", "text", "rosbag"} <= set(registry.readers)
    assert "basal_area" in registry.metrics


def test_csv_export(tmp_path):
    path = registry.exporter("csv").export(records(), tmp_path / "t.csv", ExportContext())
    rows = list(csv.DictReader(path.open()))
    assert [r["tree_id"] for r in rows] == ["1", "2", "3"]
    assert rows[0]["bh_elevation"] == "251.3000"
    assert rows[2]["dbh"] == ""  # NaN written as empty
    assert rows[0]["basal_area_m2"] == f"{math.pi * 0.21**2:.4f}"
    header = list(rows[0])
    assert header.index("basal_area_m2") < header.index("basal_area_ci_low_m2")


def test_csv_can_exclude_failed(tmp_path):
    path = registry.exporter("csv").export(
        records(), tmp_path / "t.csv", ExportContext(), include_failed=False
    )
    assert len(list(csv.DictReader(path.open()))) == 2


@pytest.mark.parametrize("mode,expected_z", [("elevation", 251.3), ("height", 1.3), ("plan", 0.0)])
def test_dxf_circle_plane(tmp_path, mode, expected_z):
    path = registry.exporter("dxf").export(
        records(), tmp_path / "t.dxf", ExportContext(), z_mode=mode
    )
    doc = ezdxf.readfile(path)
    assert len(doc.audit().errors) == 0
    msp = doc.modelspace()
    dbh = [e for e in msp if e.dxftype() == "CIRCLE" and e.dxf.layer == "FIP_DBH"]
    warn = [e for e in msp if e.dxftype() == "CIRCLE" and e.dxf.layer == "FIP_DBH_WARN"]
    assert len(dbh) == 1 and len(warn) == 1  # failed tree not drawn
    c = dbh[0]
    assert tuple(c.dxf.center) == pytest.approx((10.0, 20.0, expected_z))
    assert c.dxf.radius == pytest.approx(0.21)
    assert tuple(c.dxf.extrusion) == (0, 0, 1)  # horizontal plane
    assert c.get_xdata("FIP")[0].value == "tree_id=1"
    ci = [e for e in msp if e.dxftype() == "CIRCLE" and e.dxf.layer == "FIP_CI"]
    assert sorted(round(e.dxf.radius, 3) for e in ci) == [0.14, 0.16, 0.205, 0.215]
    axis = [e for e in msp if e.dxftype() == "LINE"]
    assert (len(axis) == 2) == (mode == "elevation")


def test_exporter_option_validation(tmp_path):
    exp = registry.exporter("dxf")
    with pytest.raises(ExportError, match="not in"):
        exp.export(records(), tmp_path / "t.dxf", ExportContext(), z_mode="sideways")
    with pytest.raises(ExportError, match="unknown option"):
        exp.export(records(), tmp_path / "t.dxf", ExportContext(), colour="red")
    # string values from the CLI are coerced
    assert exp.resolve_options({"include_ci": "false", "text_height": "0.5"})["include_ci"] is False


def test_failed_write_leaves_no_partial_file(tmp_path, monkeypatch):
    exp = registry.exporter("csv")
    target = tmp_path / "t.csv"
    calls = {"n": 0}
    real_writerow = csv.DictWriter.writerow

    def flaky_writerow(self, row):
        calls["n"] += 1
        if calls["n"] == 2:  # header + first row written, then the disk "fails"
            raise OSError("disk full")
        return real_writerow(self, row)

    monkeypatch.setattr(csv.DictWriter, "writerow", flaky_writerow)
    with pytest.raises(OSError, match="disk full"):
        exp.export(records(), target, ExportContext())
    assert calls["n"] == 2
    assert not target.exists()
    assert not list(tmp_path.glob(".t.csv.*"))


def test_user_plugin_dir(tmp_path):
    (tmp_path / "geojson_points.py").write_text(
        textwrap.dedent("""
        import json
        from forest_inventory_pipeline.plugins import Exporter, register_exporter, atomic_write

        @register_exporter
        class GeoJson(Exporter):
            name = "test_geojson"
            extension = ".geojson"
            description = "test"
            def export(self, trees, path, context, **options):
                feats = [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [t.x, t.y]},
                          "properties": {"dbh": t.dbh}} for t in trees if t.fitted]
                with atomic_write(path) as fh:
                    json.dump({"type": "FeatureCollection", "features": feats}, fh)
                return path
    """)
    )
    (tmp_path / "broken.py").write_text("raise RuntimeError('intentionally broken')\n")
    load_plugins(extra_dirs=[tmp_path], entry_points=False)
    assert "test_geojson" in registry.exporters
    assert any("broken.py" in k for k in registry.load_errors)
    out = registry.exporter("test_geojson").export(
        records(), tmp_path / "o.geojson", ExportContext()
    )
    assert out.exists()


def test_missing_plugin_dir_is_recorded(tmp_path):
    load_plugins(extra_dirs=[tmp_path / "nope"], entry_points=False)
    assert any("nope" in k for k in registry.load_errors)


def test_basal_area_metric():
    r = records()[0]
    v = registry.metrics["basal_area"].compute(r)
    assert v["basal_area_ci_low_m2"] < v["basal_area_m2"] < v["basal_area_ci_high_m2"]
    assert np.isclose(v["basal_area_m2"], math.pi * 0.21**2)
