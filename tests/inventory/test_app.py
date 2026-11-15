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
    assert len(app.tabs) == 5


def test_prompt_before_first_run(app):
    assert any("Run segmentation" in i.value for i in app.info)


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
