"""Pole and shrub checks: thresholds and actions (flag, reject, off) are settings."""

import numpy as np
import pytest

from forest_inventory_pipeline import inventory as inv
from forest_inventory_pipeline.errors import ParameterError
from forest_inventory_pipeline.structures import OverheadIndex
from forest_inventory_pipeline.traits.dbh.robust_fit import FitParams


def _pole(rng, r=0.12, top=7.0, n=40000):
    t = rng.uniform(0, 2 * np.pi, n)
    z = rng.uniform(0.0, top, n)
    return np.c_[r * np.cos(t), r * np.sin(t), z]


def _run(**fit):
    rng = np.random.default_rng(0)
    pole = _pole(rng)
    g = np.c_[rng.uniform(-6, 6, (4000, 2)), np.zeros(4000)]
    return inv.fit_trees({1: pole}, g, FitParams(**fit), overhead=OverheadIndex(pole))[0]


def test_pole_is_flagged_by_default():
    r = _run()
    assert r.status == "warning" and "bare column" in r.message


def test_pole_rejected_when_the_action_is_reject():
    r = _run(pole_action="reject")
    assert r.status == "failed" and "bare column" in r.message and not r.fitted


def test_pole_left_alone_when_the_action_is_off():
    r = _run(pole_action="off")
    assert "bare column" not in r.message


def test_pole_threshold_is_a_setting():
    assert "bare column" not in _run(pole_min_top=8.0).message  # the pole is 7 m


def test_column_check_uses_the_settings():
    fp = FitParams(short_max_top=2.0)
    assert inv.column_check(2.6, 0.0, 0.57, fp)[0] == ""  # 2.6 m is no longer "short"
    assert inv.column_check(2.6, 0.0, 0.57, FitParams())[0] == "short"
    assert inv.column_check(6.7, 0.05, 0.65, FitParams())[0] == "pole"
    assert inv.column_check(6.7, 0.25, 0.65, FitParams(flag_max_crown_share=0.3))[0] == "pole"


@pytest.mark.parametrize("bad", [dict(pole_action="delete"), dict(short_action="x"),
                                 dict(flag_max_crown_share=1.5), dict(pole_min_top=0.5)])
def test_bad_flag_settings_are_refused(bad):
    with pytest.raises(ParameterError):
        FitParams(**bad)
