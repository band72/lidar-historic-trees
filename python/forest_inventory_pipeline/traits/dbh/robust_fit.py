# MIT License
#
# Copyright (c) 2024 Meher Malladi, Luca Lobefaro, Tiziano Guadagnino, Cyrill Stachniss
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
"""
Breast-height stem fitting with confidence intervals (numpy only, vectorized).

Pipeline per tree (points are height-normalized, z = height above ground):

1. keep points in a band ``breast_height +- band_half_width``
2. RANSAC a horizontal circle (3-point circumcircle in XY) for a robust initial guess
3. robust Levenberg-Marquardt refinement of a cylinder with a Geman-McClure kernel
4. DBH confidence interval:
   - ``analytic``: cluster-robust sandwich covariance of the M-estimator with angular
     sectors as clusters, Student-t interval with (sectors - 1) dof. Cheap; suitable for
     live UI updates.
   - ``bootstrap``: percentile interval from resampling. ``sector`` blocks resample angular
     sectors of the stem, so non-circular stems and one-sided scans widen the interval;
     ``point`` resamples individual points (iid assumption; usually too narrow).
   Both add ``model_sd`` (systematic error, 1 sd) in quadrature. See ``calibration.py``.

   Stems seen from one side: a circle fitted to a partial arc measures the *local curvature*,
   so a stem that is not round is mis-measured, and one side cannot show whether it is round.
   The interval therefore also includes ``view_sd = ovality_sd * DBH * 3 * (1 - arc_coverage)``
   (zero for a stem seen all round). The factor 3 was fitted to simulated oval stems at
   180-270 degrees (worst case over orientations); ``ovality_sd`` is the assumed 1-sd
   non-roundness, ``(long - short) / (long + short)`` of the cross-section.

Cylinder parameterization: theta = [x0, y0, a, b, r]. The axis passes through
(x0, y0, z_ref) with direction (a, b, 1)/||(a, b, 1)||, r is the radius. Unlike a free
center + rotation parameterization there is no gauge freedom, so the normal matrix is
full rank and the covariance is well defined.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Literal

import numpy as np
from scipy import stats

from ...errors import FitError, ParameterError

N_PARAMS = 5
#: view_sd = ovality_sd * DBH * VIEW_FACTOR * (1 - arc_coverage); fitted to simulated oval stems
VIEW_FACTOR = 3.0
_ANGLE_BINS = 36


@dataclass
class FitParams:
    """All stem-fitting knobs. Validated on construction; see ``validate``."""

    breast_height: float = 1.3
    band_half_width: float = 0.25
    min_band_points: int = 30
    ransac_iterations: int = 2000
    inlier_tolerance: float = 0.02
    min_radius: float = 0.025
    max_radius: float = 0.75
    max_tilt_deg: float = 22.5
    lsq_iterations: int = 50
    kernel_scale: float = 0.03
    confidence: float = 0.95
    #: systematic DBH uncertainty (m, 1 sd) added in quadrature: non-circular stems, taper,
    #: segmentation errors. Point resampling cannot see these. Calibrate with field data.
    model_sd: float = 0.005
    ci_method: Literal["analytic", "bootstrap"] = "analytic"
    n_bootstrap: int = 200
    bootstrap_block: Literal["sector", "point"] = "sector"
    n_sectors: int = 12
    #: stems whose scanned arc covers less than this fraction are flagged "warning"
    min_arc_coverage: float = 0.6
    #: assumed 1-sd non-roundness of stems, (long - short) / (long + short); widens the interval
    #: of partly seen stems. An assumption: raise it for fluted or buttressed species.
    ovality_sd: float = 0.03
    max_relative_ci_width: float = 0.25
    min_inlier_ratio: float = 0.3
    #: split clusters that contain several stems at breast height (under-segmentation)
    split_stems: bool = True
    #: XY gap (m) that separates two stems in the breast-height band
    stem_gap: float = 0.15
    seed: int = 0

    def __post_init__(self):
        self.validate()

    def validate(self) -> None:
        def require(cond: bool, msg: str):
            if not cond:
                raise ParameterError(msg)

        require(
            0.1 <= self.breast_height <= 5.0,
            f"breast_height must be in [0.1, 5] m, got {self.breast_height}",
        )
        require(
            0.02 <= self.band_half_width <= 1.0,
            f"band_half_width must be in [0.02, 1] m, got {self.band_half_width}",
        )
        require(
            self.breast_height - self.band_half_width > 0,
            "band must stay above ground (breast_height - band_half_width > 0)",
        )
        require(
            self.min_band_points >= 10, f"min_band_points must be >= 10, got {self.min_band_points}"
        )
        require(
            10 <= self.ransac_iterations <= 1_000_000,
            f"ransac_iterations must be in [10, 1e6], got {self.ransac_iterations}",
        )
        require(
            0.001 <= self.inlier_tolerance <= 0.2,
            f"inlier_tolerance must be in [0.001, 0.2] m, got {self.inlier_tolerance}",
        )
        require(
            0 < self.min_radius < self.max_radius,
            f"need 0 < min_radius < max_radius, got {self.min_radius}, {self.max_radius}",
        )
        require(self.max_radius <= 3.0, f"max_radius must be <= 3 m, got {self.max_radius}")
        require(
            0 < self.max_tilt_deg < 60, f"max_tilt_deg must be in (0, 60), got {self.max_tilt_deg}"
        )
        require(
            1 <= self.lsq_iterations <= 1000,
            f"lsq_iterations must be in [1, 1000], got {self.lsq_iterations}",
        )
        require(
            0.001 <= self.kernel_scale <= 1.0,
            f"kernel_scale must be in [0.001, 1] m, got {self.kernel_scale}",
        )
        require(0 <= self.model_sd <= 0.2, f"model_sd must be in [0, 0.2] m, got {self.model_sd}")
        require(
            0 <= self.ovality_sd <= 0.3, f"ovality_sd must be in [0, 0.3], got {self.ovality_sd}"
        )
        require(
            0.5 <= self.confidence < 1.0, f"confidence must be in [0.5, 1), got {self.confidence}"
        )
        require(
            self.ci_method in ("analytic", "bootstrap"),
            f"ci_method must be 'analytic' or 'bootstrap', got {self.ci_method!r}",
        )
        require(
            20 <= self.n_bootstrap <= 10_000,
            f"n_bootstrap must be in [20, 10000], got {self.n_bootstrap}",
        )
        require(
            self.bootstrap_block in ("sector", "point"),
            f"bootstrap_block must be 'sector' or 'point', got {self.bootstrap_block!r}",
        )
        require(4 <= self.n_sectors <= 72, f"n_sectors must be in [4, 72], got {self.n_sectors}")
        require(
            0 <= self.min_arc_coverage <= 1,
            f"min_arc_coverage must be in [0, 1], got {self.min_arc_coverage}",
        )
        require(
            self.max_relative_ci_width > 0,
            f"max_relative_ci_width must be > 0, got {self.max_relative_ci_width}",
        )
        require(
            0 <= self.min_inlier_ratio <= 1,
            f"min_inlier_ratio must be in [0, 1], got {self.min_inlier_ratio}",
        )
        require(
            0.02 <= self.stem_gap <= 2.0, f"stem_gap must be in [0.02, 2] m, got {self.stem_gap}"
        )

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class StemFit:
    """Result of fitting one stem. Coordinates are in the height-normalized frame."""

    center_x: float
    center_y: float
    axis: tuple[float, float, float]
    radius: float
    dbh: float
    dbh_se: float
    #: extra DBH uncertainty (m, 1 sd) for the part of the circumference that was not seen
    view_sd: float
    ci_low: float
    ci_high: float
    ci_method: str
    confidence: float
    n_band_points: int
    inlier_ratio: float
    rmse: float
    arc_coverage: float
    max_gap_deg: float
    tilt_deg: float
    converged: bool
    warnings: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        return "warning" if self.warnings else "ok"


# --------------------------------------------------------------------------- geometry


def _direction(a: float, b: float) -> np.ndarray:
    w = np.array([a, b, 1.0])
    return w / np.linalg.norm(w)


def residuals(theta: np.ndarray, points: np.ndarray, z_ref: float) -> np.ndarray:
    """Signed point-to-surface distances (m). Positive = outside the cylinder."""
    x0, y0, a, b, r = theta
    d = _direction(a, b)
    v = points - np.array([x0, y0, z_ref])
    along = v @ d
    perp = v - along[:, None] * d
    return np.linalg.norm(perp, axis=1) - r


def jacobian(theta: np.ndarray, points: np.ndarray, z_ref: float) -> np.ndarray:
    """Analytic d(residual)/d(theta), shape (N, 5)."""
    x0, y0, a, b, _ = theta
    w = np.array([a, b, 1.0])
    w_norm = np.linalg.norm(w)
    d = w / w_norm
    v = points - np.array([x0, y0, z_ref])
    along = v @ d
    perp = v - along[:, None] * d
    rho = np.maximum(np.linalg.norm(perp, axis=1), 1e-12)
    n_hat = perp / rho[:, None]
    J = np.empty((len(points), N_PARAMS))
    J[:, 0] = -n_hat[:, 0]
    J[:, 1] = -n_hat[:, 1]
    J[:, 2] = -along * n_hat[:, 0] / w_norm
    J[:, 3] = -along * n_hat[:, 1] / w_norm
    J[:, 4] = -1.0
    return J


def _gm_weights(e: np.ndarray, c: float) -> np.ndarray:
    """IRLS weights of the Geman-McClure kernel."""
    c2 = c * c
    return (c2 / (c2 + e * e)) ** 2


def _gm_cost(e: np.ndarray, c: float) -> float:
    c2 = c * c
    return float(0.5 * np.sum(c2 * e * e / (c2 + e * e)))


# --------------------------------------------------------------------------- RANSAC


def ransac_circle(
    points: np.ndarray,
    iterations: int,
    tolerance: float,
    min_radius: float,
    max_radius: float,
    rng: np.random.Generator,
    chunk: int = 4096,
) -> tuple[np.ndarray, int]:
    """Best horizontal circle (cx, cy, r) by 3-point RANSAC in XY, and its inlier count."""
    xy = points[:, :2]
    n = len(xy)
    if n < 3:
        raise FitError(f"need >= 3 points for RANSAC, got {n}")
    best, best_count = None, -1
    remaining = iterations
    while remaining > 0:
        m = min(chunk, remaining)
        remaining -= m
        idx = (
            np.stack([rng.choice(n, size=3, replace=False) for _ in range(m)])
            if n < 8
            else rng.integers(0, n, size=(m, 3))
        )
        p1, p2, p3 = xy[idx[:, 0]], xy[idx[:, 1]], xy[idx[:, 2]]
        # circumcircle via perpendicular bisectors
        ax, ay = p1[:, 0], p1[:, 1]
        bx, by = p2[:, 0], p2[:, 1]
        cx_, cy_ = p3[:, 0], p3[:, 1]
        den = 2.0 * (ax * (by - cy_) + bx * (cy_ - ay) + cx_ * (ay - by))
        ok = np.abs(den) > 1e-12
        if not np.any(ok):
            continue
        a2, b2, c2 = ax**2 + ay**2, bx**2 + by**2, cx_**2 + cy_**2
        with np.errstate(divide="ignore", invalid="ignore"):
            ux = (a2 * (by - cy_) + b2 * (cy_ - ay) + c2 * (ay - by)) / den
            uy = (a2 * (cx_ - bx) + b2 * (ax - cx_) + c2 * (bx - ax)) / den
        r = np.hypot(ax - ux, ay - uy)
        ok &= np.isfinite(r) & (r >= min_radius) & (r <= max_radius)
        if not np.any(ok):
            continue
        ux, uy, r = ux[ok], uy[ok], r[ok]
        # score: (m_ok, n) distance matrix in sub-chunks to bound memory
        counts = np.empty(len(r), dtype=np.int64)
        step = max(1, 2_000_000 // max(n, 1))
        for s in range(0, len(r), step):
            dx = xy[None, :, 0] - ux[s : s + step, None]
            dy = xy[None, :, 1] - uy[s : s + step, None]
            dist = np.abs(np.hypot(dx, dy) - r[s : s + step, None])
            counts[s : s + step] = np.sum(dist < tolerance, axis=1)
        k = int(np.argmax(counts))
        if counts[k] > best_count:
            best_count = int(counts[k])
            best = np.array([ux[k], uy[k], r[k]])
    if best is None:
        raise FitError(f"RANSAC found no circle with radius in [{min_radius}, {max_radius}] m")
    return best, best_count


# --------------------------------------------------------------------------- refinement


def refine_cylinder(
    points: np.ndarray,
    theta0: np.ndarray,
    z_ref: float,
    kernel_scale: float,
    iterations: int,
) -> tuple[np.ndarray, bool]:
    """Robust Levenberg-Marquardt. Returns (theta, converged)."""
    theta = np.asarray(theta0, dtype=float).copy()
    e = residuals(theta, points, z_ref)
    cost = _gm_cost(e, kernel_scale)
    lam = 1e-3
    for _ in range(iterations):
        J = jacobian(theta, points, z_ref)
        w = _gm_weights(e, kernel_scale)
        JW = J * w[:, None]
        H = J.T @ JW
        g = JW.T @ e
        improved = False
        for _ in range(10):
            A = H + lam * np.diag(np.maximum(np.diag(H), 1e-9))
            try:
                delta = np.linalg.solve(A, -g)
            except np.linalg.LinAlgError:
                lam *= 10
                continue
            cand = theta + delta
            e_c = residuals(cand, points, z_ref)
            cost_c = _gm_cost(e_c, kernel_scale)
            if np.isfinite(cost_c) and cost_c <= cost:
                theta, e, lam = cand, e_c, max(lam / 3, 1e-9)
                improved = True
                if cost - cost_c < 1e-12 * max(cost, 1e-12) or np.linalg.norm(delta) < 1e-7:
                    return theta, True
                cost = cost_c
                break
            lam *= 10
        if not improved:
            # cannot decrease further: at a (local) minimum
            return theta, True
    return theta, False


def sandwich_covariance(
    theta: np.ndarray,
    points: np.ndarray,
    z_ref: float,
    kernel_scale: float,
    groups: np.ndarray | None = None,
) -> np.ndarray:
    """
    Robust sandwich covariance A^-1 B A^-1 of the M-estimate.

    Without ``groups`` points are treated as independent (HC1). LiDAR stem points are not:
    bark texture, non-circular cross-sections and scan-line structure make neighbouring
    residuals correlated, which makes the iid covariance far too small. With ``groups``
    (e.g. angular sector per point) scores are summed within each group first
    (cluster-robust / CR1), so systematic per-sector deviations widen the interval.
    """
    e = residuals(theta, points, z_ref)
    J = jacobian(theta, points, z_ref)
    w = _gm_weights(e, kernel_scale)
    A = J.T @ (J * w[:, None])
    scores = J * (w * e)[:, None]
    n = len(points)
    if groups is None:
        B = scores.T @ scores
        corr = n / max(n - N_PARAMS, 1)
    else:
        uniq, inv = np.unique(groups, return_inverse=True)
        G = len(uniq)
        S = np.zeros((G, N_PARAMS))
        np.add.at(S, inv.ravel(), scores)
        B = S.T @ S
        corr = (G / max(G - 1, 1)) * ((n - 1) / max(n - N_PARAMS, 1))
    A_inv = np.linalg.pinv(A)
    return A_inv @ B @ A_inv * corr


def sector_ids(theta: np.ndarray, points: np.ndarray, z_ref: float, n_sectors: int) -> np.ndarray:
    ang = _angles(theta, points, z_ref)
    return np.floor((ang + np.pi) / (2 * np.pi) * n_sectors).astype(int) % n_sectors


# --------------------------------------------------------------------------- diagnostics


def _angles(theta: np.ndarray, points: np.ndarray, z_ref: float) -> np.ndarray:
    x0, y0, a, b, _ = theta
    d = _direction(a, b)
    # orthonormal basis (u1, u2) of the plane perpendicular to the axis
    ref = np.array([1.0, 0.0, 0.0]) if abs(d[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
    u1 = np.cross(d, ref)
    u1 /= np.linalg.norm(u1)
    u2 = np.cross(d, u1)
    v = points - np.array([x0, y0, z_ref])
    return np.arctan2(v @ u2, v @ u1)


def arc_coverage(angles: np.ndarray) -> tuple[float, float]:
    """Fraction of 10-degree bins with points, and the largest empty gap in degrees."""
    if len(angles) == 0:
        return 0.0, 360.0
    bins = np.floor((angles + np.pi) / (2 * np.pi) * _ANGLE_BINS).astype(int) % _ANGLE_BINS
    occupied = np.zeros(_ANGLE_BINS, dtype=bool)
    occupied[bins] = True
    coverage = occupied.mean()
    a = np.sort(np.degrees(angles))
    gaps = np.diff(np.concatenate([a, [a[0] + 360.0]]))
    return float(coverage), float(gaps.max())


# --------------------------------------------------------------------------- main entry


def band_points(points: np.ndarray, params: FitParams) -> np.ndarray:
    z = points[:, 2]
    mask = np.abs(z - params.breast_height) <= params.band_half_width
    return points[mask]


def _fit_once(
    band: np.ndarray, params: FitParams, rng: np.random.Generator
) -> tuple[np.ndarray, bool]:
    circle, _ = ransac_circle(
        band,
        iterations=params.ransac_iterations,
        tolerance=params.inlier_tolerance,
        min_radius=params.min_radius,
        max_radius=params.max_radius,
        rng=rng,
    )
    theta0 = np.array([circle[0], circle[1], 0.0, 0.0, circle[2]])
    theta, converged = refine_cylinder(
        band, theta0, params.breast_height, params.kernel_scale, params.lsq_iterations
    )
    return theta, converged


def fit_stem(points: np.ndarray, params: FitParams, seed_offset: int = 0) -> StemFit:
    """
    Fit one stem and compute its DBH confidence interval.

    :param points: (N, 3) height-normalized points of ONE tree instance.
    :param seed_offset: mixed into ``params.seed`` so each tree gets its own deterministic stream.
    :raises FitError: when the tree cannot be fitted (too few points, no valid circle,
        implausible geometry). Callers record the reason and continue with the next tree.
    """
    points = np.asarray(points, dtype=float)
    if points.ndim != 2 or points.shape[1] != 3:
        raise FitError(f"points must be (N, 3), got {points.shape}")
    points = points[np.all(np.isfinite(points), axis=1)]
    band = band_points(points, params)
    if len(band) < params.min_band_points:
        raise FitError(
            f"only {len(band)} points in the breast-height band "
            f"[{params.breast_height - params.band_half_width:.2f}, "
            f"{params.breast_height + params.band_half_width:.2f}] m (need {params.min_band_points})"
        )

    rng = np.random.default_rng([params.seed, seed_offset])
    theta, converged = _fit_once(band, params, rng)

    x0, y0, a, b, r = theta
    if not np.all(np.isfinite(theta)):
        raise FitError("refinement diverged (non-finite parameters)")
    tilt = float(np.degrees(np.arctan(np.hypot(a, b))))
    if tilt > params.max_tilt_deg:
        raise FitError(f"fitted axis tilt {tilt:.1f} deg exceeds max {params.max_tilt_deg} deg")
    if not (params.min_radius <= r <= params.max_radius):
        raise FitError(
            f"fitted radius {r:.3f} m outside [{params.min_radius}, {params.max_radius}] m"
        )

    e = residuals(theta, band, params.breast_height)
    inlier = np.abs(e) < params.inlier_tolerance
    inlier_ratio = float(inlier.mean())
    rmse = float(np.sqrt(np.mean(e[inlier] ** 2))) if inlier.any() else float("nan")
    angles = _angles(theta, band[inlier], params.breast_height)
    coverage, max_gap = arc_coverage(angles)

    dbh = 2.0 * r
    alpha = 1.0 - params.confidence
    zq = float(stats.norm.ppf(1 - alpha / 2))
    # a stem seen from one side cannot show whether it is round (see module docstring)
    view_sd = params.ovality_sd * dbh * VIEW_FACTOR * (1.0 - min(max(coverage, 0.0), 1.0))
    sys_sd = float(np.hypot(params.model_sd, view_sd))
    if params.ci_method == "analytic":
        groups = sector_ids(theta, band, params.breast_height, params.n_sectors)
        n_groups = len(np.unique(groups))
        if n_groups < 3:
            raise FitError(
                f"only {n_groups} angular sectors contain points; CI is not identifiable"
            )
        cov = sandwich_covariance(
            theta, band, params.breast_height, params.kernel_scale, groups=groups
        )
        var_r = cov[4, 4]
        if not np.isfinite(var_r) or var_r < 0:
            raise FitError("covariance is not finite; geometry is degenerate")
        se_stat = 2.0 * float(np.sqrt(var_r))
        tq = float(stats.t.ppf(1 - alpha / 2, df=n_groups - 1))
        half = float(np.hypot(tq * se_stat, zq * sys_sd))
        se = float(np.hypot(se_stat, sys_sd))
        lo, hi = dbh - half, dbh + half
    else:
        samples = bootstrap_dbh(band, theta, params, rng)
        if len(samples) < 0.5 * params.n_bootstrap:
            raise FitError(
                f"bootstrap failed: only {len(samples)}/{params.n_bootstrap} resamples converged"
            )
        lo, hi = (float(q) for q in np.quantile(samples, [alpha / 2, 1 - alpha / 2]))
        # percentile interval must contain the point estimate
        lo, hi = min(lo, dbh), max(hi, dbh)
        sys_half = zq * sys_sd
        lo = dbh - float(np.hypot(dbh - lo, sys_half))
        hi = dbh + float(np.hypot(hi - dbh, sys_half))
        se = float(np.hypot(np.std(samples, ddof=1), sys_sd))

    warnings = []
    if coverage < params.min_arc_coverage:
        warnings.append(
            f"low arc coverage ({coverage:.0%}): stem seen from one side; DBH assumes a round stem, "
            f"interval widened by +/-{zq * view_sd * 100:.1f} cm for possible non-roundness"
        )
    if (hi - lo) / dbh > params.max_relative_ci_width:
        warnings.append(f"wide CI ({(hi - lo) / dbh:.0%} of DBH)")
    if inlier_ratio < params.min_inlier_ratio:
        warnings.append(f"low inlier ratio ({inlier_ratio:.0%}): clutter or branches in band")
    if not converged:
        warnings.append("refinement hit the iteration limit")

    return StemFit(
        center_x=float(x0),
        center_y=float(y0),
        axis=tuple(float(v) for v in _direction(a, b)),
        radius=float(r),
        dbh=float(dbh),
        dbh_se=float(se),
        view_sd=float(view_sd),
        ci_low=float(max(lo, 0.0)),
        ci_high=float(hi),
        ci_method=params.ci_method
        if params.ci_method == "analytic"
        else f"bootstrap-{params.bootstrap_block}",
        confidence=params.confidence,
        n_band_points=int(len(band)),
        inlier_ratio=inlier_ratio,
        rmse=rmse,
        arc_coverage=coverage,
        max_gap_deg=max_gap,
        tilt_deg=tilt,
        converged=converged,
        warnings=warnings,
    )


def bootstrap_dbh(
    band: np.ndarray, theta: np.ndarray, params: FitParams, rng: np.random.Generator
) -> np.ndarray:
    """DBH bootstrap samples, warm-started from ``theta`` (RANSAC is not re-run)."""
    n = len(band)
    iters = min(params.lsq_iterations, 15)
    if params.bootstrap_block == "sector":
        sector = sector_ids(theta, band, params.breast_height, params.n_sectors)
        groups = [np.flatnonzero(sector == s) for s in range(params.n_sectors)]
        groups = [g for g in groups if len(g)]
        if len(groups) < 3:
            raise FitError(f"only {len(groups)} angular sectors contain points; cannot bootstrap")
    out = []
    for _ in range(params.n_bootstrap):
        if params.bootstrap_block == "sector":
            pick = rng.integers(0, len(groups), size=len(groups))
            idx = np.concatenate([groups[i] for i in pick])
        else:
            idx = rng.integers(0, n, size=n)
        sample = band[idx]
        try:
            th, _ = refine_cylinder(sample, theta, params.breast_height, params.kernel_scale, iters)
        except np.linalg.LinAlgError:
            continue
        r = th[4]
        if np.all(np.isfinite(th)) and params.min_radius <= r <= params.max_radius:
            out.append(2.0 * r)
    return np.asarray(out)
