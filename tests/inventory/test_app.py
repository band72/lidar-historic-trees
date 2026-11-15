"""Headless smoke test of the Streamlit app (demo plot -> segmentation -> live refit)."""

from pathlib import Path

import pytest

testing = pytest.importorskip("streamlit.testing.v1")

APP = (
    Path(__file__).parents[2] / "python" / "forest_inventory_pipeline" / "app" / "streamlit_app.py"
)


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # the app writes fip_output/app.log under cwd
    monkeypatch.setenv("FIP_HOME", str(tmp_path / "fip_home"))  # app preferences, not ~/.config
    at = testing.AppTest.from_file(str(APP), default_timeout=300)
    at.run()
    assert not at.exception
    return at


def metric(at, label):
    return next(m for m in at.metric if m.label == label)


def test_demo_run_and_live_slider(app):
    app.sidebar.number_input[0].set_value(10)  # trees in demo plot
    app.button[0].click().run()
    assert not app.exception and not app.error
    assert metric(app, "Trees fitted").value.split(" / ")[0] != "0"
    wide = metric(app, "Median CI width").value
    conf = next(s for s in app.sidebar.slider if s.label.startswith("Confidence"))
    conf.set_value(0.80).run()
    assert not app.exception
    assert metric(app, "Median CI width").value != wide  # refit happened live
    assert len(app.tabs) == 6  # Map, Tree detail, Table & export, Calibration, Settings, Log


def test_prompt_before_first_run(app):
    assert any("Run tree extraction" in i.value for i in app.info)


def test_feet_display(app):
    app.sidebar.number_input[0].set_value(8)
    app.button[0].click().run()
    units = next(s for s in app.sidebar.button_group if s.label == "Display & export units")
    units.set_value("ft").run()
    assert not app.exception and not app.error
    assert metric(app, "Mean DBH").value.endswith(" in")
    assert metric(app, "Basal area").value.endswith("ft²/ac")
    bh = next(s for s in app.sidebar.slider if s.label.startswith("Breast height"))
    assert bh.label == "Breast height (ft)" and bh.value == 4.5


def test_complete_partial_toggle(app):
    app.sidebar.number_input[0].set_value(8)
    app.button[0].click().run()
    toggle = next(t for t in app.sidebar.toggle if t.label == "Complete partly seen stems")
    assert toggle.value is False
    toggle.set_value(True).run()
    assert not app.exception and not app.error
    assert metric(app, "Trees fitted").value.split(" / ")[0] != "0"


def _write_las(path, xyz):
    laspy = pytest.importorskip("laspy")
    import numpy as np

    header = laspy.LasHeader(point_format=6, version="1.4")
    header.scales, header.offsets = np.array([0.001] * 3), np.floor(xyz.min(axis=0))
    las = laspy.LasData(header)
    las.x, las.y, las.z = xyz[:, 0], xyz[:, 1], xyz[:, 2]
    las.write(str(path))


def test_choose_file_run_and_save(app, tmp_path, monkeypatch):
    """The installed-app workflow: choose a file on disk, run, save every result to a folder."""
    from forest_inventory_pipeline.synthetic import make_plot

    monkeypatch.setenv("FIP_HOME", str(tmp_path / "home"))  # preferences go here, not ~/.config
    data = tmp_path / "survey"
    data.mkdir()
    _write_las(data / "plot.laz", make_plot(n_trees=8, seed=2).points)
    app.sidebar.radio[0].set_value("path").run()
    app.text_input(key="input_typed").set_value(str(data / "plot.laz")).run()
    assert any("plot.laz" in s.value for s in app.sidebar.success)
    next(b for b in app.sidebar.button if b.label == "Run tree extraction").click().run()
    assert not app.exception and not app.error
    assert metric(app, "Trees fitted").value.split(" / ")[0] != "0"
    # the output chooser starts next to the input file
    assert app.session_state["output_sel"] == str(data)
    next(b for b in app.button if b.label == "Save results").click().run()
    assert not app.exception and not app.error
    out = data / "plot_inventory"
    assert {"trees.csv", "trees.dxf", "trees.geojson", "trees.laz", "run.json"} <= {
        p.name for p in out.iterdir()
    }
    # the folders are remembered for next time
    from forest_inventory_pipeline.app.file_browser import load_prefs

    prefs = load_prefs()
    assert prefs["last_input_dir"] == str(data) and prefs["last_output_dir"] == str(data)


def test_settings_tab_gives_a_cli_config(app):
    app.sidebar.number_input[0].set_value(8)
    next(b for b in app.sidebar.button if b.label == "Run tree extraction").click().run()
    yaml_text = next(
        c.value for c in app.code if c.value.startswith("# LiDAR Tree Inventory settings")
    )
    import yaml

    from forest_inventory_pipeline import inventory as inv
    from forest_inventory_pipeline.traits.dbh.robust_fit import FitParams

    cfg = yaml.safe_load(yaml_text)
    inv.GroundParams(**cfg["ground"]), inv.SegmentParams(**cfg["segment"]), FitParams(**cfg["fit"])
