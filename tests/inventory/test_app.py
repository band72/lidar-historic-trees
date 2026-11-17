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


def test_reviewer_can_exclude_a_tree(app):
    app.sidebar.number_input[0].set_value(8)
    next(b for b in app.sidebar.button if b.label == "Run tree extraction").click().run()
    before = int(metric(app, "Trees fitted").value.split(" / ")[0])
    import re

    ms = next(m for m in app.multiselect if m.label.startswith("Exclude from the results"))
    first_tree = int(re.search(r"\d+", ms.options[0]).group())  # options are display labels
    ms.set_value([first_tree]).run()
    assert not app.exception and not app.error
    assert int(metric(app, "Trees fitted").value.split(" / ")[0]) == before - 1


def test_site_profile_sets_the_sidebar(app):
    sb = app.sidebar
    prof = next(s for s in sb.selectbox if s.label == "Site profile")
    assert prof.value == "Default" and prof.options[1:] == [
        "Roadside / Farmstead", "Shrubs", "Isolated Trees", "Clear Forest", "Tall Wide Trees",
        "Large DBH Trees",
    ]
    prof.set_value("Large DBH Trees").run()
    assert not app.exception and not app.error
    slider = lambda label: next(s for s in app.sidebar.slider if s.label.startswith(label))  # noqa: E731
    assert slider("Max DBH").value == 300
    assert slider("Stem gap").value == 30
    assert slider("Neighbours k").value == 150  # big-tree profiles keep the segmentation
    mindbh = next(n for n in app.sidebar.number_input if n.label.startswith("Minimum DBH"))
    assert mindbh.value == 20.0
    assert any("heritage" in c.value for c in app.sidebar.caption)
    # a profile setting without a slider reaches the fit (shown in the Settings tab's YAML)
    next(s for s in app.sidebar.selectbox if s.label == "Site profile").set_value("Shrubs").run()
    slider("Max stem tilt").set_value(30.0).run()
    assert any("changed from this profile" in c.value for c in app.sidebar.caption)
    app.sidebar.number_input[0].set_value(8)  # demo plot still runs under a profile
    next(b for b in app.sidebar.button if b.label == "Run tree extraction").click().run()
    assert not app.exception and not app.error
    yaml_text = next(c.value for c in app.code if c.value.startswith("# LiDAR Tree Inventory"))
    assert "min_top_above_breast_height: 1.2" in yaml_text and "site profile: Shrubs" in yaml_text
    assert "max_tilt_deg: 30.0" in yaml_text  # the reviewer's change is kept


def test_reviewer_can_keep_a_rejected_cluster_as_a_tree(app, tmp_path, monkeypatch):
    import numpy as np

    from forest_inventory_pipeline.synthetic import make_plot

    monkeypatch.setenv("FIP_HOME", str(tmp_path / "home"))
    plot = make_plot(n_trees=6, seed=6)
    rng = np.random.default_rng(0)  # a shrub-like blob the checks reject
    gx, gy = plot.points[:, 0].min() + 3.0, plot.points[:, 1].min() + 3.0
    gz = float(np.percentile(plot.points[:, 2], 5))
    pts = np.vstack([plot.points, rng.normal(0, 0.35, (6000, 3)) + [gx, gy, gz + 1.7]])
    _write_las(tmp_path / "plot.laz", pts)
    app.sidebar.radio[0].set_value("path").run()
    app.text_input(key="input_typed").set_value(str(tmp_path / "plot.laz")).run()
    next(b for b in app.sidebar.button if b.label == "Run tree extraction").click().run()
    pick = next(s for s in app.selectbox if any("failed" in str(o) for o in s.options))
    failed = next(o for o in pick.options if "failed" in str(o))
    pick.set_value(failed).run()
    next(b for b in app.button if b.label == "✚ This is a tree").click().run()
    assert not app.exception and not app.error
    pick = next(s for s in app.selectbox if any("added by reviewer" in str(o) for o in s.options))
    assert any(str(o).split(" · ")[0] == str(failed).split(" · ")[0] for o in pick.options
               if "added" in str(o))
    assert any(b.label == "Not a tree after all" for b in app.button)


def test_georeference_panel_shows_satellite_map_and_gpx(app, tmp_path):
    import math

    import numpy as np

    from forest_inventory_pipeline import georef

    app.sidebar.number_input[0].set_value(8)
    next(b for b in app.sidebar.button if b.label == "Run tree extraction").click().run()
    # a drive past the demo plot, and GNSS fixes of it under a known rotation and shift
    t = np.arange(0.0, 60.0, 0.1)
    xy = np.column_stack([np.linspace(-20, 50, len(t)), 5 * np.sin(t / 10)])
    np.savetxt(tmp_path / "traj.csv", np.column_stack([t, xy]), delimiter=",",
               header="time_s,x,y", comments="")
    th = math.radians(20)
    en = xy[::5] @ np.array([[math.cos(th), -math.sin(th)], [math.sin(th), math.cos(th)]]).T
    lat, lon, _ = georef.enu_to_geodetic(np.column_stack([en, np.zeros(len(en))]), 30.08, -83.09, 0)
    np.savetxt(tmp_path / "gnss.csv", np.column_stack([t[::5], lat, lon]), delimiter=",",
               header="time_s,lat,lon", comments="", fmt="%.9f")
    app.text_input(key="georef_traj_typed").set_value(str(tmp_path / "traj.csv")).run()
    app.text_input(key="georef_gnss_typed").set_value(str(tmp_path / "gnss.csv")).run()
    next(b for b in app.button if b.label == "Georeference").click().run()
    assert not app.exception and not app.error
    assert any(s.value.startswith("Georeferenced") for s in app.success)
    assert any(d.label.startswith("Download trees.gpx") for d in app.get("download_button"))



def test_scanner_profile_reset_and_evidence(app):
    sb = app.sidebar
    next(s for s in sb.selectbox if s.label == "Site profile").set_value("Large DBH Trees").run()
    dens = lambda: next(s for s in app.sidebar.slider if s.label.startswith("Min. points per m"))  # noqa: E731
    assert dens().value == 300  # vehicle 600 x 0.5
    next(s for s in app.sidebar.selectbox if s.label == "Scanner").set_value("Drone (UAV)").run()
    assert not app.exception and dens().value == 75  # drone 150 x 0.5
    assert any(c.value.startswith("✓ Checked on real data") for c in app.sidebar.caption)
    assert any("What this profile changes" in e.label for e in app.sidebar.expander)
    dens().set_value(500).run()
    next(b for b in app.sidebar.button if b.label == "↺ Reset").click().run()
    assert dens().value == 75
    next(s for s in app.sidebar.selectbox if s.label == "Site profile").set_value("Shrubs").run()
    assert next(s for s in app.sidebar.selectbox if s.label.startswith("Short object")).value == "reject"
    assert any(c.value.startswith("⚠ Untested") for c in app.sidebar.caption)


def test_save_current_settings_as_a_profile(app, tmp_path):
    app.sidebar.number_input[0].set_value(8)
    next(b for b in app.sidebar.button if b.label == "Run tree extraction").click().run()
    next(s for s in app.sidebar.slider if s.label.startswith("Max stem tilt")).set_value(27.5).run()
    app.text_input(key="profile_save_name").set_value("Old pines").run()
    next(b for b in app.button if b.label == "Save as profile").click().run()
    assert not app.exception and any("Saved profile" in s.value for s in app.success)
    prof = next(s for s in app.sidebar.selectbox if s.label == "Site profile")
    assert "Old pines" in prof.options
    from forest_inventory_pipeline import profiles

    assert profiles.get("Old pines").fit == {"max_tilt_deg": 27.5}


def test_rejection_summary_in_the_app(app, tmp_path, monkeypatch):
    import numpy as np

    from forest_inventory_pipeline.synthetic import make_plot

    monkeypatch.setenv("FIP_HOME", str(tmp_path / "home"))
    plot = make_plot(n_trees=6, seed=6)
    rng = np.random.default_rng(0)
    gx, gy = plot.points[:, 0].min() + 3.0, plot.points[:, 1].min() + 3.0
    gz = float(np.percentile(plot.points[:, 2], 5))
    _write_las(tmp_path / "plot.laz", np.vstack([plot.points, rng.normal(0, 0.35, (6000, 3)) + [gx, gy, gz + 1.7]]))
    app.sidebar.radio[0].set_value("path").run()
    app.text_input(key="input_typed").set_value(str(tmp_path / "plot.laz")).run()
    next(b for b in app.sidebar.button if b.label == "Run tree extraction").click().run()
    assert not app.exception
    exp = next(e for e in app.expander if e.label.startswith("Why"))
    assert "rejected" in exp.label
    cols = [list(t.value.columns) for t in app.dataframe]
    assert ["Reason", "Objects", "Setting to try"] in cols


def test_changed_settings_table_and_keyed_controls(app):
    tilt = next(s for s in app.sidebar.slider if s.label.startswith("Max stem tilt"))
    tilt.set_value(30.0).run()
    cov = next(s for s in app.sidebar.slider if s.label == "Min arc coverage")
    cov.set_value(0.5).run()
    exp = next(e for e in app.sidebar.expander if e.label.startswith("Changed from the profile"))
    text = " ".join(m.value for m in exp.markdown)
    assert "| max tilt (°) | 22.5 | 30 |" in text
    assert "| min arc coverage | 0.6 | 0.5 |" in text
    next(b for b in app.sidebar.button if b.label == "↺ Reset").click().run()
    assert next(s for s in app.sidebar.slider if s.label == "Min arc coverage").value == 0.6
    assert next(s for s in app.sidebar.slider if s.label.startswith("Ground class threshold")).value == 0.5
