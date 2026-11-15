"""Trunk check: the kinds of non-trunks found in real mobile-lidar data are rejected."""

import numpy as np
import pytest

from forest_inventory_pipeline.errors import FitError
from forest_inventory_pipeline.traits.dbh.robust_fit import FitParams, fit_stem


def trunk(rng, r=0.2, z_top=3.0, n=8000, arc=2 * np.pi, noise=0.004):
    """Hollow vertical trunk surface from 1.0 m to z_top, ~1000+ points per m^2."""
    t = rng.uniform(0, arc, n)
    z = rng.uniform(1.0, z_top, n)
    return np.c_[r * np.cos(t), r * np.sin(t), z] + rng.normal(0, noise, (n, 3))


def test_real_like_trunk_is_accepted_and_scored():
    f = fit_stem(trunk(np.random.default_rng(0)), FitParams())
    assert f.dbh == pytest.approx(0.4, abs=0.005)
    assert f.stem_dominance > 0.9 and f.stem_continuity == 1.0 and f.interior_fraction < 0.05
    assert f.surface_density > 600


def test_half_seen_trunk_is_still_a_trunk():
    f = fit_stem(trunk(np.random.default_rng(1), arc=np.pi), FitParams())
    assert f.arc_coverage == pytest.approx(0.5, abs=0.06)  # flagged, but measured


def test_low_post_or_stump_is_rejected():
    """Stops at 1.5 m: fence posts, stumps (field data: T24, T126, T245)."""
    with pytest.raises(FitError, match="not trunk-like.*continuous"):
        fit_stem(trunk(np.random.default_rng(2), r=0.06, z_top=1.5, n=3000), FitParams())


def test_filled_shrub_is_rejected():
    """Points fill the inside (field data: T248, T304)."""
    rng = np.random.default_rng(3)
    n = 20000
    rad = 0.5 * np.sqrt(rng.random(n))
    t = rng.uniform(0, 2 * np.pi, n)
    blob = np.c_[rad * np.cos(t), rad * np.sin(t), rng.uniform(1.0, 2.0, n)]
    with pytest.raises(FitError, match="not trunk-like"):
        fit_stem(blob, FitParams())


def test_sparse_scatter_fitted_with_a_big_circle_is_rejected():
    """A few hundred points spread thinly (field data: DBH 80-150 cm candidates)."""
    rng = np.random.default_rng(4)
    pts = trunk(rng, r=0.65, n=400, noise=0.01)
    with pytest.raises(FitError, match="points per m\\^2"):
        fit_stem(pts, FitParams())
    # a sparser scanner can still be used by lowering the density requirement
    assert fit_stem(pts, FitParams(min_surface_density=20)).dbh == pytest.approx(1.3, abs=0.05)


def test_trunk_with_dense_clutter_around_it_is_rejected():
    """Most points 1-2.5 m are not on the circle (bush around a pole, wall corner)."""
    rng = np.random.default_rng(5)
    pole = trunk(rng, r=0.08, n=1500)
    bush = np.c_[rng.uniform(-1.5, 1.5, (15000, 2)), rng.uniform(1.0, 2.5, 15000)]
    with pytest.raises(FitError, match="lie on the trunk surface"):
        fit_stem(np.r_[pole, bush], FitParams())


def test_check_can_be_switched_off():
    pts = trunk(np.random.default_rng(6), r=0.06, z_top=1.5, n=3000)
    f = fit_stem(pts, FitParams(reject_non_stems=False))
    assert f.dbh == pytest.approx(0.12, abs=0.01)
