"""Site profiles: every one builds valid settings, and config files override them."""

import pytest

from forest_inventory_pipeline import inventory as inv
from forest_inventory_pipeline import profiles
from forest_inventory_pipeline.errors import ParameterError
from forest_inventory_pipeline.traits.dbh.robust_fit import FitParams

EXPECTED = ["Default", "Roadside / Farmstead", "Shrubs", "Isolated Trees", "Clear Forest",
            "Tall Wide Trees", "Large DBH Trees"]


def test_the_requested_profiles_exist_in_order():
    assert list(profiles.PROFILES) == EXPECTED
    assert profiles.PROFILES["Default"].sections() == {"ground": {}, "segment": {}, "fit": {}}


@pytest.mark.parametrize("name", EXPECTED)
def test_profile_builds_valid_parameters(name):
    s = profiles.get(name).sections()
    gp = inv.GroundParams(**s["ground"])
    sp = inv.SegmentParams(**s["segment"])
    for bh in (1.3, 1.3716):  # metric and US breast height
        fp = FitParams(breast_height=bh, **s["fit"])
        assert inv.check_band_inside_slice(fp, sp) == []
    assert gp and profiles.get(name).summary


def test_profiles_differ_where_their_site_needs_it():
    big = profiles.get("Large DBH Trees").fit
    assert big["max_radius"] * 2 >= 3.0 and big["stem_gap"] > FitParams().stem_gap
    assert profiles.get("Shrubs").fit["min_dbh"] > 0
    assert profiles.get("Tall Wide Trees").fit["stem_check_top"] > FitParams().stem_check_top
    # coarser segmentation merged neighbouring trees on real data: big-tree profiles keep it
    assert not profiles.get("Large DBH Trees").segment and not profiles.get("Tall Wide Trees").segment
    assert profiles.get("Roadside / Farmstead").fit["max_tilt_deg"] == 30.0


def test_lookup_is_forgiving_and_unknown_names_fail():
    assert profiles.get("large_dbh-trees").name == "Large DBH Trees"
    with pytest.raises(ParameterError, match="Clear Forest"):
        profiles.get("rainforest")


def test_config_file_overrides_the_profile():
    m = profiles.merged("Shrubs", {"fit": {"min_dbh": 0.05}, "ground": {"rigidness": 1}})
    assert m["fit"]["min_dbh"] == 0.05
    assert m["fit"]["min_top_above_breast_height"] == 1.2  # rest of the profile kept
    assert m["ground"] == {"rigidness": 1}


def test_cli_profile_is_applied_recorded_and_overridable(tmp_path):
    import json
    import os
    import subprocess
    import sys

    import numpy as np

    from forest_inventory_pipeline.synthetic import make_plot

    def fip(*args):
        return subprocess.run(
            [sys.executable, "-m", "forest_inventory_pipeline.fip", *args],
            capture_output=True, text=True, env={**os.environ, "COLUMNS": "200"},
        )

    np.save(tmp_path / "plot.npy", make_plot(n_trees=4, seed=3).points)
    (tmp_path / "c.yaml").write_text("fit:\n  min_dbh: 0.05\n")
    out = tmp_path / "out"
    res = fip("inventory", str(tmp_path / "plot.npy"), "-o", str(out), "-e", "csv",
              "--profile", "shrubs", "-c", str(tmp_path / "c.yaml"))
    assert res.returncode == 0, res.stdout + res.stderr
    params = json.loads((out / "run.json").read_text())["parameters"]
    assert params["site_profile"] == "Shrubs"
    assert params["fit"]["min_top_above_breast_height"] == 1.2  # from the profile
    assert params["fit"]["min_dbh"] == 0.05  # the config file wins
    assert params["segment"]["k"] == 150  # profiles keep the default segmentation

    bad = fip("inventory", str(tmp_path / "plot.npy"), "-o", str(tmp_path / "bad"), "--profile", "jungle")
    assert bad.returncode == 2 and "unknown profile" in bad.stdout + bad.stderr


# --- scanner profiles, evidence, saved profiles ------------------------------------------------

SCANNERS = ["Vehicle (mobile lidar)", "Backpack / handheld", "Tripod (terrestrial)", "Drone (UAV)"]


def test_scanner_profiles_exist_and_vehicle_is_the_default():
    assert list(profiles.SCANNERS) == SCANNERS
    assert profiles.DEFAULT_SCANNER == "Vehicle (mobile lidar)"
    assert profiles.SCANNERS["Vehicle (mobile lidar)"].sections()["fit"] == {}


@pytest.mark.parametrize("name", list(profiles.PROFILES))
def test_site_profiles_set_tree_criteria_not_scanner_density(name):
    assert "min_surface_density" not in profiles.get(name).fit


def test_scanner_sets_density_and_the_site_scales_it():
    big = profiles.merged("Large DBH Trees", scanner="Vehicle (mobile lidar)")
    assert big["fit"]["min_surface_density"] == pytest.approx(300.0)  # 600 x 0.5, as tested
    drone = profiles.merged("Large DBH Trees", scanner="Drone (UAV)")
    assert drone["fit"]["min_surface_density"] == pytest.approx(75.0)  # 150 x 0.5
    assert "min_surface_density" not in profiles.merged("Default")["fit"]  # defaults untouched
    # a config file still has the last word
    cfg = profiles.merged("Large DBH Trees", {"fit": {"min_surface_density": 500}}, scanner="Drone (UAV)")
    assert cfg["fit"]["min_surface_density"] == 500


def test_every_profile_states_its_evidence():
    for p in list(profiles.PROFILES.values()) + list(profiles.SCANNERS.values()):
        assert p.evidence, p.name
    for name in ("Default", "Roadside / Farmstead", "Isolated Trees", "Large DBH Trees"):
        assert profiles.get(name).evidence.startswith("Checked on real data"), name
    for name in ("Shrubs", "Clear Forest"):
        assert profiles.get(name).evidence.startswith("Untested"), name


def test_shrubs_keeps_small_trees_and_rejects_short_blobs():
    f = profiles.get("Shrubs").fit
    assert 1.3 + f["min_top_above_breast_height"] <= 2.6  # 2.7 m ornamental trees reach it
    assert f["short_action"] == "reject"


def test_clear_forest_keeps_the_default_lean_limit():
    assert "max_tilt_deg" not in profiles.get("Clear Forest").fit


def test_saved_profiles_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("FIP_HOME", str(tmp_path))
    path = profiles.save_user_profile(
        "My pines", {"fit": {"pole_action": "off", "max_tilt_deg": 25.0}, "segment": {"k": 120}},
        summary="Old pines along the drive")
    assert path.parent == tmp_path / "profiles"
    p = profiles.get("my pines")
    assert p.fit == {"pole_action": "off", "max_tilt_deg": 25.0} and p.segment == {"k": 120}
    assert p.evidence.startswith("Saved by you") and "My pines" in profiles.all_profiles()
    with pytest.raises(ParameterError, match="built-in"):
        profiles.save_user_profile("Shrubs", {"fit": {}})
    with pytest.raises(ParameterError):
        profiles.save_user_profile("bad", {"fit": {"max_tilt_deg": 400}})  # invalid values refused
    profiles.delete_user_profile("My pines")
    with pytest.raises(ParameterError):
        profiles.get("My pines")


def test_cli_scanner_and_saved_profile(tmp_path):
    import json
    import os
    import subprocess
    import sys

    import numpy as np

    from forest_inventory_pipeline.synthetic import make_plot

    env = {**os.environ, "COLUMNS": "200", "FIP_HOME": str(tmp_path / "home")}
    np.save(tmp_path / "plot.npy", make_plot(n_trees=4, seed=3).points)
    subprocess.run([sys.executable, "-c",
                    "from forest_inventory_pipeline import profiles;"
                    "profiles.save_user_profile('Mine', {'fit': {'max_tilt_deg': 25.0}})"],
                   env=env, check=True)
    res = subprocess.run([sys.executable, "-m", "forest_inventory_pipeline.fip", "inventory",
                          str(tmp_path / "plot.npy"), "-o", str(tmp_path / "o"), "-e", "csv",
                          "--profile", "Mine", "--scanner", "Drone (UAV)"],
                         capture_output=True, text=True, env=env)
    assert res.returncode == 0, res.stdout + res.stderr
    p = json.loads((tmp_path / "o" / "run.json").read_text())["parameters"]
    assert p["site_profile"] == "Mine" and p["scanner_profile"] == "Drone (UAV)"
    assert p["fit"]["max_tilt_deg"] == 25.0 and p["fit"]["min_surface_density"] == 150.0
