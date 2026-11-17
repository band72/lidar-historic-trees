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

from dataclasses import asdict, dataclass, field, replace
from typing import Literal

import numpy as np
from scipy import stats

from ...errors import FitError, ParameterError

N_PARAMS = 5
#: view_sd = ovality_sd * DBH * VIEW_FACTOR * (1 - arc_coverage); fitted to simulated oval stems
VIEW_FACTOR = 3.0


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
    #: added to every DBH and to both ends of its interval (m), to remove a bias measured in
    #: the field (fip calibrate suggests it: tape DBH minus scanned DBH, on average). Limited
    #: to +/- 2 cm: a larger bias points at a wrong breast height, ground or unit instead.
    dbh_offset: float = 0.0
    ci_method: Literal["analytic", "bootstrap"] = "analytic"
    n_bootstrap: int = 200
    bootstrap_block: Literal["sector", "point"] = "sector"
    n_sectors: int = 12
    #: stems whose scanned arc covers less than this fraction are flagged "warning"
    min_arc_coverage: float = 0.6
    #: assumed 1-sd non-roundness of stems, (long - short) / (long + short); widens the interval
    #: of partly seen stems. An assumption: raise it for fluted or buttressed species.
    ovality_sd: float = 0.03
    #: for stems flagged for low arc coverage (or too few band points), widen the measurement
    #: window up to ``max_band_half_width`` to see more of the circumference, fitting a taper
    #: so the DBH stays the diameter at breast height. Off by default: it changes measurements.
    complete_partial_stems: bool = False
    max_band_half_width: float = 0.75
    #: reject clusters that are not trunk-like (shrubs, posts, walls, sparse scatter); see
    #: ``trunk_features``. On real mobile-lidar data most raw clusters are not trunks.
    reject_non_stems: bool = True
    #: share of the cluster's points in the check zone that lie on the fitted trunk surface
    min_stem_dominance: float = 0.5
    #: share of 10 cm height slices in the check zone holding enough trunk-surface points
    min_stem_continuity: float = 0.8
    #: share of breast-height points inside 60 % of the radius (filled = foliage or shrub)
    max_interior_fraction: float = 0.2
    #: the trunk check looks up to this height above ground (m), or to where the crown starts
    stem_check_top: float = 2.5
    #: a 10 cm slice above breast height + 0.3 m with less than this share of points on the trunk
    #: surface is taken as the crown base, which ends the trunk check zone
    crown_slice_dominance: float = 0.35
    #: the object must reach at least this far above breast height (95th percentile of its
    #: points); low shrubs and stumps do not. Kept small on purpose: vehicle lidar often
    #: cannot see crown tops above ~3 m near the sensor, so true tree height is unreliable.
    min_top_above_breast_height: float = 1.0
    #: stems thinner than this (m) are not counted as trees (0 = off). Many inventories use
    #: 0.075-0.127 m (3-5 in); shrub stems are usually thinner.
    min_dbh: float = 0.0
    #: points per square metre of the *seen* trunk surface in the breast-height band (within
    #: inlier_tolerance). Sparse scatter fitted with a big circle falls far below; depends on
    #: the scanner and distance: lower it for sparse data (e.g. 150 for UAV or distant TLS).
    min_surface_density: float = 600.0
    #: at least this share of breast-height points must lie on the fitted surface
    min_trunk_inlier_ratio: float = 0.2
    #: below this share of the circumference seen, the visible arc is too short to fix the
    #: curvature, so the diameter is undetermined (a small post or stem on the edge of a big
    #: circle fits as well as a real big trunk): rejected. 0 = off.
    min_fit_arc_coverage: float = 0.25
    #: a stem seen over less than this share of its circumference must have at least
    #: ``min_one_sided_inlier_ratio`` of its breast-height points on the circle: its diameter rests
    #: on the curvature of one side, which foliage, structures or two stems cannot provide
    one_sided_arc: float = 0.45
    min_one_sided_inlier_ratio: float = 0.35
    #: reject stems under a roof: a post of a shed, carport or pole barn passes the trunk
    #: checks, but the material just above it lies on one smooth plane (see structures.py).
    #: Needs the overhead cloud (fit_trees(..., overhead=...)); trees score 0.08-0.09 on
    #: real data, roofs 0.28-0.60.
    reject_roof_posts: bool = True
    #: trees with DBH at least this (m) get large_tree = yes (lower-bound DBH: only if even the
    #: lower bound reaches it); 0.5 m is about 20 in
    large_tree_dbh: float = 0.5
    #: what is above a stem (needs the overhead cloud; see inventory.column_check): a bare
    #: column seen to pole_min_top m or more, or an object under short_max_top m with a DBH of
    #: short_min_dbh m or more, each with under flag_max_crown_share of the material around
    #: it. Actions: "flag" (a warning for the reviewer), "reject", or "off". A vehicle lidar
    #: cannot tell a pole from a tall trunk whose crown is above its view, so the default flags.
    #: warn when the ground under a stem is more than this (m) above or below the median of the
    #: ground within 3 m of it: breast height is then measured from the wrong level. 0 = off.
    ground_check_tolerance: float = 0.5
    pole_action: str = "flag"
    pole_min_top: float = 4.0
    short_action: str = "flag"
    short_max_top: float = 3.6
    short_min_dbh: float = 0.30
    flag_max_crown_share: float = 0.2
    roof_layer_fraction: float = 0.2
    roof_min_span: float = 0.3
    #: stop widening once this fraction of the circumference is seen (else use the best window)
    complete_target_coverage: float = 0.85
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
            0.02 <= self.band_half_width <= 1.5,
            f"band_half_width must be in [0.02, 1.5] m, got {self.band_half_width}",
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
            abs(self.dbh_offset) <= 0.02 + 1e-12,
            f"dbh_offset must be within +/- 2 cm (0.02 m), got {self.dbh_offset * 100:.1f} cm",
        )
        require(
            0 <= self.ovality_sd <= 0.3, f"ovality_sd must be in [0, 0.3], got {self.ovality_sd}"
        )
        require(0.0 <= self.min_stem_dominance <= 1.0, "min_stem_dominance must be in [0, 1]")
        require(0.0 <= self.crown_slice_dominance <= 1.0, "crown_slice_dominance must be in [0, 1]")
        require(
            0.0 <= self.min_top_above_breast_height <= 20.0,
            "min_top_above_breast_height must be in [0, 20] m",
        )
        require(0.0 <= self.min_dbh <= 2.0, f"min_dbh must be in [0, 2] m, got {self.min_dbh}")
        require(
            0.0 <= self.min_fit_arc_coverage <= 0.6,
            f"min_fit_arc_coverage must be in [0, 0.6], got {self.min_fit_arc_coverage}",
        )
        require(0.0 <= self.one_sided_arc <= 1.0, "one_sided_arc must be in [0, 1]")
        require(0.0 < self.roof_layer_fraction <= 1.0, "roof_layer_fraction must be in (0, 1]")
        require(0.0 <= self.roof_min_span <= 2.0, "roof_min_span must be in [0, 2]")
        require(0.0 < self.large_tree_dbh <= 5.0, "large_tree_dbh must be in (0, 5] m")
        for name in ("pole_action", "short_action"):
            require(
                getattr(self, name) in ("flag", "reject", "off"),
                f"{name} must be flag, reject or off, got {getattr(self, name)!r}",
            )
        require(0.0 <= self.ground_check_tolerance <= 10.0, "ground_check_tolerance must be in [0, 10] m")
        require(2.0 <= self.pole_min_top <= 40.0, "pole_min_top must be in [2, 40] m")
        require(1.5 <= self.short_max_top <= 10.0, "short_max_top must be in [1.5, 10] m")
        require(0.0 <= self.short_min_dbh <= 3.0, "short_min_dbh must be in [0, 3] m")
        require(0.0 <= self.flag_max_crown_share <= 1.0, "flag_max_crown_share must be in [0, 1]")
        require(
            0.0 <= self.min_one_sided_inlier_ratio <= 1.0,
            "min_one_sided_inlier_ratio must be in [0, 1]",
        )
        require(0.0 <= self.min_stem_continuity <= 1.0, "min_stem_continuity must be in [0, 1]")
        require(0.0 <= self.max_interior_fraction <= 1.0, "max_interior_fraction must be in [0, 1]")
        require(self.min_surface_density >= 0.0, "min_surface_density must be >= 0")
        require(
            0.0 <= self.min_trunk_inlier_ratio <= 1.0, "min_trunk_inlier_ratio must be in [0, 1]"
        )
        require(
            0.0 < self.complete_target_coverage <= 1.0,
            f"complete_target_coverage must be in (0, 1], got {self.complete_target_coverage}",
        )
        require(
            self.band_half_width <= self.max_band_half_width <= 1.5,
            "max_band_half_width must be between band_half_width and 1.5 m, "
            f"got {self.max_band_half_width}",
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
    #: informational remarks that do not make the tree a "warning" (e.g. the band was widened)
    notes: list[str] = field(default_factory=list)
    #: half-height of the measurement window that was used (m)
    band_half_width: float = 0.0
    #: trunk-likeness measures (see trunk_features)
    stem_dominance: float = float("nan")
    surface_density: float = float("nan")
    stem_continuity: float = float("nan")
    interior_fraction: float = float("nan")
    #: how the DBH was obtained: "breast-height band", "taller window (taper)" or
    #: "visible width (lower bound)" (too little of the stem seen to fix its curvature)
    dbh_method: str = "breast-height band"

    @property
    def status(self) -> str:
        return "warning" if self.warnings else "ok"


# --------------------------------------------------------------------------- geometry


def _direction(a: float, b: float) -> np.ndarray:
    w = np.array([a, b, 1.0])
    return w / np.linalg.norm(w)


def residuals(theta: np.ndarray, points: np.ndarray, z_ref: float) -> np.ndarray:
    """
    Signed point-to-surface distances (m). Positive = outside the surface.

    ``theta`` is [x0, y0, a, b, r], or [x0, y0, a, b, r, s] for a tapered stem whose radius
    changes linearly with height: r(z) = r + s * (z - z_ref). ``r`` is always the radius at
    ``z_ref`` (breast height), so DBH stays the diameter at breast height.
    """
    x0, y0, a, b, r = theta[:5]
    d = _direction(a, b)
    v = points - np.array([x0, y0, z_ref])
    along = v @ d
    perp = v - along[:, None] * d
    rad = r + theta[5] * (points[:, 2] - z_ref) if len(theta) > 5 else r
    return np.linalg.norm(perp, axis=1) - rad


def jacobian(theta: np.ndarray, points: np.ndarray, z_ref: float) -> np.ndarray:
    """Analytic d(residual)/d(theta), shape (N, len(theta))."""
    x0, y0, a, b = theta[:4]
    w = np.array([a, b, 1.0])
    w_norm = np.linalg.norm(w)
    d = w / w_norm
    v = points - np.array([x0, y0, z_ref])
    along = v @ d
    perp = v - along[:, None] * d
    rho = np.maximum(np.linalg.norm(perp, axis=1), 1e-12)
    n_hat = perp / rho[:, None]
    J = np.empty((len(points), len(theta)))
    J[:, 0] = -n_hat[:, 0]
    J[:, 1] = -n_hat[:, 1]
    J[:, 2] = -along * n_hat[:, 0] / w_norm
    J[:, 3] = -along * n_hat[:, 1] / w_norm
    J[:, 4] = -1.0
    if len(theta) > 5:
        J[:, 5] = -(points[:, 2] - z_ref)
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
                # converged when the step is below 0.01 mm (or the cost stops improving); the
                # old 0.1 micrometre limit was never reached on real, noisy scans although the
                # DBH had stopped changing (iteration-limit warnings on 71 % of field trees)
                if cost - cost_c < 1e-9 * max(cost, 1e-12) or np.linalg.norm(delta) < 1e-5:
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
    n_par = len(theta)
    if groups is None:
        B = scores.T @ scores
        corr = n / max(n - n_par, 1)
    else:
        uniq, inv = np.unique(groups, return_inverse=True)
        G = len(uniq)
        S = np.zeros((G, n_par))
        np.add.at(S, inv.ravel(), scores)
        B = S.T @ S
        corr = (G / max(G - 1, 1)) * ((n - 1) / max(n - n_par, 1))
    A_inv = np.linalg.pinv(A)
    return A_inv @ B @ A_inv * corr


def sector_ids(theta: np.ndarray, points: np.ndarray, z_ref: float, n_sectors: int) -> np.ndarray:
    ang = _angles(theta, points, z_ref)
    return np.floor((ang + np.pi) / (2 * np.pi) * n_sectors).astype(int) % n_sectors


# --------------------------------------------------------------------------- diagnostics


def _angles(theta: np.ndarray, points: np.ndarray, z_ref: float) -> np.ndarray:
    x0, y0, a, b = theta[:4]
    d = _direction(a, b)
    # orthonormal basis (u1, u2) of the plane perpendicular to the axis
    ref = np.array([1.0, 0.0, 0.0]) if abs(d[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
    u1 = np.cross(d, ref)
    u1 /= np.linalg.norm(u1)
    u2 = np.cross(d, u1)
    v = points - np.array([x0, y0, z_ref])
    return np.arctan2(v @ u2, v @ u1)


def arc_coverage(angles: np.ndarray) -> tuple[float, float]:
    """
    Fraction of the circumference that was seen, and the largest empty gap in degrees.

    A gap counts as *unseen* only if it is wider than the gaps that randomly placed points
    leave anyway. For n points spread over a full circle the widest such gap is about
    360 * (ln n + 0.58) / n degrees, so the tolerance is 360 * (ln n + 1) / n, kept between
    10 and 60 degrees. Counting occupied 10-degree slices instead would call a sparse stem
    seen all round (30 points: about 56 % of slices hit) "seen from one side".
    Every large gap is subtracted, so a stem seen from two opposite sides scores about 50 %.
    """
    n = len(angles)
    if n == 0:
        return 0.0, 360.0
    a = np.sort(np.degrees(np.asarray(angles, dtype=float)))
    gaps = np.diff(np.concatenate([a, [a[0] + 360.0]]))
    tol = float(np.clip(360.0 * (np.log(max(n, 2)) + 1.0) / n, 10.0, 60.0))
    unseen = float(gaps[gaps > tol].sum())
    return float(np.clip(1.0 - unseen / 360.0, 0.0, 1.0)), float(gaps.max())


# --------------------------------------------------------------------------- main entry


def trunk_features(
    theta: np.ndarray, points: np.ndarray, params: FitParams
) -> tuple[float, float, float]:
    """
    How trunk-like a fitted cluster is, from all its height-normalized points.

    The check zone runs from 1.0 m up to where the crown begins (the first 10 cm slice above
    breast height + 0.3 m in which fewer than ``crown_slice_dominance`` of the points lie on
    the trunk surface), or ``stem_check_top`` if no crown starts below it. A vehicle- or
    backpack-mounted lidar sees the underside of a low crown as a dense flat layer; counting
    it as "not trunk" rejected real trees with crowns from about 2 m.

    - dominance: share of points in the zone lying on the fitted surface
      (within max(5 cm, 30 % of the radius)); shrubs, walls and foliage spread far beyond it.
    - continuity: share of 10 cm slices of the zone holding at least max(5, 1 % of the zone's
      surface points) surface points, and the trunk must reach the top of the zone. Low
      shrubs, stumps and short posts stop below breast height + 0.3 m.
    - interior: share of breast-height points inside 60 % of the radius. Scanned from
      outside, a trunk is hollow; a foliage or shrub blob is filled.
    """
    r = float(theta[4])
    e = residuals(theta, points, params.breast_height)  # signed distance to the surface
    z = points[:, 2]
    near = np.abs(e) < max(0.05, 0.3 * r)
    top_cap = max(params.stem_check_top, params.breast_height + 0.5)
    min_top = params.breast_height + 0.3
    zone_top = top_cap
    for lo in np.arange(1.0, top_cap - 1e-9, 0.1):
        if lo + 0.1 <= min_top + 1e-9:
            continue
        m = (z >= lo) & (z < lo + 0.1)
        if m.sum() >= 20 and near[m].mean() < params.crown_slice_dominance:
            zone_top = float(lo)  # the crown starts here
            break
    zone = (z >= 1.0) & (z <= zone_top)
    if not zone.any():
        return 0.0, 0.0, 1.0
    dominance = float(near[zone].mean())
    surf_z = z[near & zone]
    need = max(5, int(0.01 * len(surf_z)))
    edges = np.arange(1.0, zone_top + 1e-9, 0.1)
    counts = np.histogram(surf_z, bins=edges)[0] if len(edges) > 1 else np.array([len(surf_z)])
    continuity = float(np.mean(counts >= need))
    if len(counts) and counts[-1] < need:  # the trunk must reach the top of the zone
        continuity = min(continuity, max(0.0, params.min_stem_continuity - 0.01))
    band = np.abs(z - params.breast_height) <= params.band_half_width
    interior = float(np.mean(e[band] < -0.4 * r)) if band.any() else 1.0
    return dominance, continuity, interior


def band_points(points: np.ndarray, params: FitParams) -> np.ndarray:
    z = points[:, 2]
    mask = np.abs(z - params.breast_height) <= params.band_half_width
    return points[mask]


def _fit_once(
    band: np.ndarray, params: FitParams, rng: np.random.Generator, taper: bool = False
) -> tuple[np.ndarray, bool]:
    circle, _ = ransac_circle(
        band,
        iterations=params.ransac_iterations,
        tolerance=params.inlier_tolerance,
        min_radius=params.min_radius,
        max_radius=params.max_radius,
        rng=rng,
    )
    theta0 = np.array([circle[0], circle[1], 0.0, 0.0, circle[2]] + ([0.0] if taper else []))
    theta, converged = refine_cylinder(
        band, theta0, params.breast_height, params.kernel_scale, params.lsq_iterations
    )
    return theta, converged


def _vertical_fit(
    band: np.ndarray, params: FitParams, rng: np.random.Generator
) -> np.ndarray | None:
    """
    A vertical cylinder (2D RANSAC circle, radius within limits) for a stem whose free fit
    failed on tilt or radius; None unless it sees under ``min_fit_arc_coverage`` of the
    circumference while holding at least ``min_one_sided_inlier_ratio`` of the band (otherwise
    the object really leans or is too big, and stays rejected).
    """
    if params.min_fit_arc_coverage <= 0:
        return None
    try:
        circle, _ = ransac_circle(
            band,
            iterations=params.ransac_iterations,
            tolerance=params.inlier_tolerance,
            min_radius=params.min_radius,
            max_radius=params.max_radius,
            rng=rng,
        )
    except FitError:
        return None
    theta = np.array([circle[0], circle[1], 0.0, 0.0, circle[2]])
    e = residuals(theta, band, params.breast_height)
    on = np.abs(e) < params.inlier_tolerance
    # most of the band must be on it: a small circle touching a sliver of a big trunk (or of
    # clutter) is not a narrow strip of a stem
    if on.sum() < 10 or on.mean() < params.min_one_sided_inlier_ratio:
        return None
    coverage, _ = arc_coverage(_angles(theta, band[on], params.breast_height))
    return theta if coverage < params.min_fit_arc_coverage else None


def _main_strip(points: np.ndarray, theta: np.ndarray, gap: float = 0.10) -> np.ndarray:
    """
    The largest continuous piece of ``points`` along the circle ``theta``: points sorted by
    angle are split wherever consecutive ones are more than ``gap`` (m, along the arc) apart,
    so scatter elsewhere on a big circle does not widen the strip.
    """
    if len(points) < 2:
        return points
    ang = np.arctan2(points[:, 1] - theta[1], points[:, 0] - theta[0])
    order = np.argsort(ang)
    a = ang[order]
    # rotate so the largest angular gap (the unseen side) is at the wrap-around
    d = np.diff(np.r_[a, a[0] + 2 * np.pi])
    start = (int(np.argmax(d)) + 1) % len(a)
    order = np.roll(order, -start)
    a = np.unwrap(ang[order])
    breaks = np.where(np.diff(a) * float(theta[4]) > gap)[0] + 1
    pieces = np.split(order, breaks)
    return points[max(pieces, key=len)]


VISIBLE_WIDTH = "visible width (lower bound)"
#: registration blur and range noise that can hide a strip's bulge (m)
SMEAR_ALLOWANCE = 0.03


def _visible_width_fit(
    arc: np.ndarray, theta: np.ndarray, params: FitParams, why: str, **fit_info
) -> StemFit:
    """
    A stem whose visible strip is too short to fix its curvature.

    Seen through a gap (a fence, other stems) or from far off, a vehicle lidar may see only
    60-90 degrees of a trunk. Such a strip, with a few cm of registration blur, fits a
    1 m circle as well as the real 20 cm one, so the circle's diameter and centre are
    unusable, but the strip's width is not: the chord of the visible arc can only be
    narrower than the diameter. The DBH reported is that width (a lower bound); the range
    runs from it, less 10 % for blur, up to the flattest circle that fits the strip. The
    stem is placed half the width behind the strip, so its position is off by at most
    (true DBH - width) / 2, against several decimetres for the big circle's centre.

    :param arc: (N, 3) breast-height points on the fitted circle (the visible strip).
    :param theta: the (unusable) cylinder fit, used for its side: the strip faces the scanner.
    """
    arc = _main_strip(arc, theta)
    if len(arc) < 10:
        raise FitError(f"not trunk-like: {why}, and too few points on it to measure its width")
    xy = arc[:, :2]
    mid = xy.mean(axis=0)
    _, _, vt = np.linalg.svd(xy - mid, full_matrices=False)
    along = (xy - mid) @ vt[0]
    across = (xy - mid) @ vt[1]
    width = float(np.percentile(along, 98) - np.percentile(along, 2))
    depth = float(np.percentile(across, 95) - np.percentile(across, 5))
    # A strip showing at least 60 degrees of a stem bulges by at least 13.4 % of its width
    # (1 - cos 30 deg, for D = 2 x width). Narrow strips may hide that in a few cm of blur;
    # a wide flat one is a wall, a fence or a bench, not a stem.
    if depth < 0.134 * width - SMEAR_ALLOWANCE:
        raise FitError(
            f"not trunk-like: {why}, and the visible strip is flat ({width * 100:.0f} cm wide, "
            f"{depth * 100:.0f} cm deep): a wall, fence or structure?"
        )
    width = min(max(width, 2.0 * params.min_radius), 2.0 * params.max_radius)
    # direction from the fitted circle's centre to the strip (the strip faces the scanner)
    big_r = float(theta[4])
    c_big = np.array([theta[0], theta[1]])
    u = mid - c_big
    u = u / (np.linalg.norm(u) or 1.0)
    center = c_big + u * (big_r - width / 2.0)
    # upper end: the flattest circle that fits the strip, or the diameter if only 60 degrees
    # were seen (chord = D sin 30 deg), whichever is larger
    hi = float(min(max(2.0 * big_r, 2.0 * width), 2.0 * params.max_radius))
    lo = 0.9 * width
    if params.min_dbh > 0 and hi < params.min_dbh:
        raise FitError(
            f"DBH at most {hi * 100:.1f} cm (visible width {width * 100:.1f} cm) is below the "
            f"minimum of {params.min_dbh * 100:.1f} cm"
        )
    zq = float(stats.norm.ppf(1 - (1.0 - params.confidence) / 2))
    warning = (
        f"diameter not determinable ({why}): DBH {width * 100:.1f} cm is the visible stem "
        f"width, a lower bound; the true DBH lies between about {lo * 100:.0f} and "
        f"{hi * 100:.0f} cm. The position is reliable"
    )
    return StemFit(
        center_x=float(center[0]),
        center_y=float(center[1]),
        axis=tuple(float(v) for v in _direction(theta[2], theta[3])),
        radius=width / 2.0,
        dbh=width,
        dbh_se=(hi - lo) / (2.0 * zq),
        view_sd=0.0,
        ci_low=lo,
        ci_high=hi,
        ci_method="visible width",
        confidence=params.confidence,
        n_band_points=int(fit_info["n_band"]),
        inlier_ratio=fit_info["inlier_ratio"],
        rmse=fit_info["rmse"],
        arc_coverage=fit_info["coverage"],
        max_gap_deg=fit_info["max_gap"],
        tilt_deg=fit_info["tilt"],
        converged=fit_info["converged"],
        warnings=[warning],
        band_half_width=params.band_half_width,
        stem_dominance=fit_info["dominance"],
        surface_density=fit_info["surface_density"],
        stem_continuity=fit_info["continuity"],
        interior_fraction=fit_info["interior"],
        dbh_method=VISIBLE_WIDTH,
    )


def _fit_window(
    points: np.ndarray, params: FitParams, seed_offset: int = 0, taper: bool = False
) -> StemFit:
    """
    Fit one stem in the window ``params.breast_height +- params.band_half_width``.

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
    theta, converged = _fit_once(band, params, rng, taper=taper)

    x0, y0, a, b, r = theta[:5]
    if not np.all(np.isfinite(theta)):
        raise FitError("refinement diverged (non-finite parameters)")
    if taper and abs(theta[5]) > 0.05:
        raise FitError(f"implausible taper {theta[5] * 200:.1f} cm/m of diameter")
    tilt = float(np.degrees(np.arctan(np.hypot(a, b))))
    assumed_vertical = False
    bad_tilt = tilt > params.max_tilt_deg
    bad_radius = not (params.min_radius <= r <= params.max_radius)
    if (bad_tilt or bad_radius) and not taper:
        # A narrow strip fixes neither the lean nor the curvature: the cylinder fit can trade
        # one for the other (real data: a 15 cm stem seen through a fence fitted at 55 deg).
        # If a vertical circle also sees under min_fit_arc_coverage, the lean was never
        # measurable: keep the stem with a vertical axis (the visible-width path below).
        v = _vertical_fit(band, params, np.random.default_rng([params.seed, seed_offset, 1]))
        if v is not None:
            theta, converged, tilt, assumed_vertical = v, True, 0.0, True
            x0, y0, a, b, r = theta[:5]
            bad_tilt = bad_radius = False
    if bad_tilt:
        raise FitError(f"fitted axis tilt {tilt:.1f} deg exceeds max {params.max_tilt_deg} deg")
    if bad_radius:
        raise FitError(
            f"fitted radius {r:.3f} m outside [{params.min_radius}, {params.max_radius}] m"
        )

    e = residuals(theta, band, params.breast_height)
    inlier = np.abs(e) < params.inlier_tolerance
    inlier_ratio = float(inlier.mean())
    rmse = float(np.sqrt(np.mean(e[inlier] ** 2))) if inlier.any() else float("nan")
    angles = _angles(theta, band[inlier], params.breast_height)
    coverage, max_gap = arc_coverage(angles)
    short_arc = params.reject_non_stems and coverage < params.min_fit_arc_coverage
    dominance, continuity, interior = trunk_features(theta, points, params)
    if params.reject_non_stems:
        problems = []
        if dominance < params.min_stem_dominance:
            problems.append(
                f"only {dominance:.0%} of points between 1 m and the crown lie on the trunk surface"
            )
        if continuity < params.min_stem_continuity:
            problems.append(
                f"trunk surface continuous over only {continuity:.0%} of the height between 1 m and the crown"
            )
        # on a short arc "inside" is as undetermined as the circle (blobs fail the other checks)
        if interior > params.max_interior_fraction and not short_arc:
            problems.append(
                f"{interior:.0%} of breast-height points fill the inside (foliage or shrub?)"
            )
        top = float(np.percentile(points[:, 2], 95))
        if top < params.breast_height + params.min_top_above_breast_height:
            problems.append(
                f"only reaches {top:.1f} m (needs {params.breast_height + params.min_top_above_breast_height:.1f} m; low shrub or stump?)"
            )
        if problems:
            raise FitError("not trunk-like: " + "; ".join(problems))
    if params.min_dbh > 0 and 2.0 * r < params.min_dbh and not short_arc:
        raise FitError(
            f"DBH {2.0 * r * 100:.1f} cm is below the minimum of {params.min_dbh * 100:.1f} cm"
        )

    seen_area = max(coverage, 0.05) * np.pi * 2.0 * r * 2.0 * params.band_half_width
    surface_density = float(inlier.sum()) / seen_area
    undetermined = ""
    if params.reject_non_stems:
        problems = []
        if surface_density < params.min_surface_density:
            problems.append(
                f"only {surface_density:.0f} points per m^2 on the seen trunk surface "
                f"(need {params.min_surface_density:g}; sparse scatter?)"
            )
        if inlier_ratio < params.min_trunk_inlier_ratio:
            problems.append(
                f"only {inlier_ratio:.0%} of breast-height points on the fitted surface"
            )
        if coverage < params.one_sided_arc and inlier_ratio < params.min_one_sided_inlier_ratio:
            # most breast-height points are off the arc: clutter, a structure or several stems,
            # so neither the circle nor the strip's width says anything about a trunk
            problems.append(
                f"seen from one side ({coverage:.0%}) with only {inlier_ratio:.0%} of breast-height "
                "points on the circle (foliage, a structure or several stems?)"
            )
        if problems:
            raise FitError("not trunk-like: " + "; ".join(problems))
        if coverage < params.min_fit_arc_coverage:
            undetermined = f"seen over only {coverage:.0%} of the circumference" + (
                "; lean not measurable, axis assumed vertical" if assumed_vertical else ""
            )
    if undetermined:
        # The trunk checks passed, so this is a stem, but the visible strip is too short (or
        # too cluttered) to fix the curvature: a big circle fits it as well as the real one.
        # Keep the tree and its position; give the visible width as a lower-bound DBH.
        # the strip: band points near the (unreliable) circle, with a margin wide enough that
        # the selection does not hinge on which big circle the fit found
        strip = band[np.abs(e) < max(2.5 * params.inlier_tolerance, 0.05)]
        return _visible_width_fit(
            strip, theta, params, undetermined,
            n_band=len(band), inlier_ratio=inlier_ratio, rmse=rmse, coverage=coverage,
            max_gap=max_gap, tilt=tilt, converged=converged, dominance=dominance,
            surface_density=surface_density, continuity=continuity, interior=interior,
        )

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
        band_half_width=params.band_half_width,
        stem_dominance=dominance,
        surface_density=surface_density,
        stem_continuity=continuity,
        interior_fraction=interior,
    )


def fit_stem(points: np.ndarray, params: FitParams, seed_offset: int = 0) -> StemFit:
    """Fit one stem (see :func:`_fit_stem`) and apply ``params.dbh_offset``."""
    sf = _fit_stem(points, params, seed_offset)
    off = params.dbh_offset
    if off:
        sf.dbh += off
        sf.ci_low = max(sf.ci_low + off, 0.0)
        sf.ci_high += off
        sf.radius = sf.dbh / 2.0
        sf.notes.append(
            f"DBH corrected by {off * 100:+.1f} cm (fit.dbh_offset, from field calibration)"
        )
    return sf


def _fit_stem(points: np.ndarray, params: FitParams, seed_offset: int = 0) -> StemFit:
    """
    Fit one stem and compute its DBH confidence interval.

    With ``params.complete_partial_stems``, a stem whose arc coverage at breast height is below
    ``params.min_arc_coverage`` (or that has too few points there) is refitted in a taller
    window, in 0.1 m steps up to ``max_band_half_width``, so that a scanner that saw different
    sides at different heights still yields (nearly) the whole circle. The taller fit includes
    a taper term, so the result is still the diameter at breast height. The smallest window that
    reaches ``min_arc_coverage`` is used, or the best one if it raises the coverage by at least
    10 percentage points; otherwise the standard fit is kept.

    :param points: (N, 3) height-normalized points of ONE tree instance.
    :param seed_offset: mixed into ``params.seed`` so each tree gets its own deterministic stream.
    :raises FitError: when the tree cannot be fitted (too few points, no valid circle,
        implausible geometry). Callers record the reason and continue with the next tree.
    """
    if not params.complete_partial_stems:
        return _fit_window(points, params, seed_offset)

    base, base_error = None, None
    try:
        base = _fit_window(points, params, seed_offset)
    except FitError as exc:
        base_error = exc
    if base is not None and base.arc_coverage >= params.min_arc_coverage:
        return base

    top = min(params.max_band_half_width, params.breast_height - 0.2)
    n_base = len(band_points(np.asarray(points, dtype=float), params))
    best = None
    hw = params.band_half_width
    while True:
        hw = round(hw + 0.1, 3)
        if hw > top + 1e-9:
            break
        try:
            wide = replace(params, band_half_width=hw)
        except ParameterError:
            break  # the window cannot grow further under the current settings
        if len(band_points(np.asarray(points, dtype=float), wide)) <= 1.1 * n_base:
            continue  # the extra height holds no new points (e.g. above the segmented slice)
        try:
            cand = _fit_window(points, wide, seed_offset, taper=True)
        except FitError:
            continue
        if cand.inlier_ratio < params.min_inlier_ratio or cand.dbh_method == VISIBLE_WIDTH:
            continue
        if best is None or cand.arc_coverage > best.arc_coverage:
            best = cand
        if cand.arc_coverage >= params.complete_target_coverage:
            break

    if best is not None and (base is None or best.arc_coverage >= base.arc_coverage + 0.10):
        best.dbh_method = "taller window (taper)"
        where = f"breast height +/-{best.band_half_width:.2f} m"
        if base is None:
            best.notes.append(
                f"too few usable points at breast height ({base_error}); "
                f"measured over {where} with a taper term"
            )
        else:
            best.notes.append(
                f"seen from one side at breast height (arc coverage {base.arc_coverage:.0%}); "
                f"completed to {best.arc_coverage:.0%} using {where} with a taper term"
            )
        return best
    if base is None:
        raise base_error  # type: ignore[misc]
    return base


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
