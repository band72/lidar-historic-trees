"""Site profiles: every one builds valid settings, and config files override them."""

import pytest

from forest_inventory_pipeline import inventory as inv
from forest_inventory_pipeline import profiles
from forest_inventory_pipeline.errors import ParameterError
from forest_inventory_pipeline.traits.dbh.robust_fit import FitParams

EXPECTED = ["Default", "Shrubs", "Isolated Trees", "Clear Forest", "Tall Wide Trees", "Large DBH Trees"]


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
    assert profiles.get("Clear Forest").fit["max_tilt_deg"] < FitParams().max_tilt_deg


def test_lookup_is_forgiving_and_unknown_names_fail():
    assert profiles.get("large_dbh-trees").name == "Large DBH Trees"
    with pytest.raises(ParameterError, match="Clear Forest"):
        profiles.get("rainforest")


def test_config_file_overrides_the_profile():
    m = profiles.merged("Shrubs", {"fit": {"min_dbh": 0.05}, "ground": {"rigidness": 1}})
    assert m["fit"]["min_dbh"] == 0.05
    assert m["fit"]["min_top_above_breast_height"] == 1.5  # rest of the profile kept
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
    assert params["fit"]["min_top_above_breast_height"] == 1.5  # from the profile
    assert params["fit"]["min_dbh"] == 0.05  # the config file wins
    assert params["segment"]["k"] == 150  # profiles keep the default segmentation

    bad = fip("inventory", str(tmp_path / "plot.npy"), "-o", str(tmp_path / "bad"), "--profile", "jungle")
    assert bad.returncode == 2 and "unknown profile" in bad.stdout + bad.stderr
