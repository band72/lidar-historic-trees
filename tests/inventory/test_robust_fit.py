import numpy as np
import pytest

from forest_inventory_pipeline.errors import FitError, ParameterError
from forest_inventory_pipeline.traits.dbh import robust_fit as rf


def stem(
    rng, r=0.2, n=800, arc=2 * np.pi, noise=0.004, lean=(0.03, -0.02), c=(3.0, 4.0), clutter=80
):
    t = rng.uniform(0, arc, n)
    z = rng.uniform(0.9, 1.8, n)
    x = c[0] + lean[0] * (z - 1.3) + r * np.cos(t) + rng.normal(0, noise, n)
    y = c[1] + lean[1] * (z - 1.3) + r * np.sin(t) + rng.normal(0, noise, n)
    pts = np.c_[x, y, z]
    junk = np.c_[
        rng.uniform(c[0] - 0.6, c[0] + 0.6, clutter),
        rng.uniform(c[1] - 0.6, c[1] + 0.6, clutter),
        rng.uniform(1.0, 1.6, clutter),
    ]
    return np.r_[pts, junk]


@pytest.mark.parametrize("method", ["analytic", "bootstrap"])
def test_full_circle_is_accurate(method):
    rng = np.random.default_rng(0)
    f = rf.fit_stem(stem(rng), rf.FitParams(ci_method=method))
    assert f.dbh == pytest.approx(0.4, abs=0.003)
    assert f.ci_low < 0.4 < f.ci_high
    assert f.center_x == pytest.approx(3.0, abs=0.005)
    assert f.arc_coverage > 0.95
    assert f.status == "ok"


def test_lean_is_recovered():
    rng = np.random.default_rng(1)
    f = rf.fit_stem(stem(rng, lean=(0.1, 0.0), clutter=0), rf.FitParams())
    assert f.tilt_deg == pytest.approx(np.degrees(np.arctan(0.1)), abs=1.0)


def test_one_sided_scan_is_flagged():
    rng = np.random.default_rng(2)
    f = rf.fit_stem(stem(rng, arc=np.pi / 2), rf.FitParams())
    assert f.arc_coverage < 0.4
    assert any("arc coverage" in w for w in f.warnings)


def test_analytic_ci_coverage_on_iid_noise():
    """With model_sd=0 and pure noise the interval should be close to nominal."""
    covered, n = 0, 60
    for seed in range(n):
        rng = np.random.default_rng(100 + seed)
        f = rf.fit_stem(
            stem(rng, n=400, clutter=0), rf.FitParams(model_sd=0.0, confidence=0.9, seed=seed)
        )
        covered += f.ci_low <= 0.4 <= f.ci_high
    assert (
        covered / n >= 0.75
    )  # cluster-robust with few sectors is slightly conservative or nominal


def test_model_sd_widens_interval():
    rng = np.random.default_rng(3)
    pts = stem(rng)
    narrow = rf.fit_stem(pts, rf.FitParams(model_sd=0.0))
    wide = rf.fit_stem(pts, rf.FitParams(model_sd=0.01))
    assert (wide.ci_high - wide.ci_low) > (narrow.ci_high - narrow.ci_low) + 0.02


def test_deterministic_for_same_seed():
    rng = np.random.default_rng(4)
    pts = stem(rng)
    a = rf.fit_stem(pts, rf.FitParams(ci_method="bootstrap", seed=7), seed_offset=3)
    b = rf.fit_stem(pts, rf.FitParams(ci_method="bootstrap", seed=7), seed_offset=3)
    assert (a.dbh, a.ci_low, a.ci_high) == (b.dbh, b.ci_low, b.ci_high)


def test_too_few_points_raises_fit_error():
    rng = np.random.default_rng(5)
    pts = stem(rng, n=20, clutter=0)
    with pytest.raises(FitError, match="breast-height band"):
        rf.fit_stem(pts, rf.FitParams())


def test_band_outside_points_raises():
    rng = np.random.default_rng(6)
    with pytest.raises(FitError):
        rf.fit_stem(stem(rng), rf.FitParams(breast_height=3.0))


def test_bad_shape_raises():
    with pytest.raises(FitError):
        rf.fit_stem(np.zeros((10, 2)), rf.FitParams())


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(confidence=1.0),
        dict(confidence=0.3),
        dict(min_radius=0.5, max_radius=0.4),
        dict(breast_height=0.2, band_half_width=0.3),
        dict(ci_method="magic"),
        dict(model_sd=-1),
    ],
)
def test_params_are_validated(kwargs):
    with pytest.raises(ParameterError):
        rf.FitParams(**kwargs)


def test_analytic_jacobian_matches_numeric():
    rng = np.random.default_rng(7)
    pts = stem(rng, clutter=0)[:200]
    theta = np.array([3.01, 3.98, 0.02, -0.01, 0.19])
    J = rf.jacobian(theta, pts, 1.3)
    eps = 1e-7
    Jn = np.empty_like(J)
    for i in range(5):
        d = np.zeros(5)
        d[i] = eps
        Jn[:, i] = (rf.residuals(theta + d, pts, 1.3) - rf.residuals(theta - d, pts, 1.3)) / (
            2 * eps
        )
    np.testing.assert_allclose(J, Jn, atol=1e-5)


def test_ransac_rejects_out_of_range_radius():
    rng = np.random.default_rng(8)
    pts = stem(rng, r=0.9, clutter=0)
    with pytest.raises(FitError):
        rf.fit_stem(pts, rf.FitParams(max_radius=0.5))
