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
End-to-end tree inventory: load -> ground segmentation -> height normalization ->
instance segmentation -> breast-height stem fit with DBH confidence intervals ->
metric plugins -> exporters.

Each stage is a plain function on numpy arrays so the web app can cache stages
independently (slow stages re-run only when their own parameters change).
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Sequence

import numpy as np
from scipy.spatial import KDTree

from .errors import FitError, InputDataError, ParameterError, StageError
from .plugins import ExportContext, load_plugins, registry
from .preprocess.height_normalization import interpolate_ground_z
from .records import STATUS_FAILED, STATUS_OK, STATUS_WARNING, TreeRecord
from .traits.dbh.robust_fit import FitParams, fit_stem
from .utils import logger

ProgressFn = Callable[[float, str], None]


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise ParameterError(msg)


@dataclass
class GroundParams:
    """Cloth Simulation Filter + IDW normalization parameters."""

    cloth_resolution: float = 0.5
    class_threshold: float = 0.5
    rigidness: int = 3
    time_step: float = 0.65
    iterations: int = 500
    slope_smooth: bool = False
    idw_k: int = 5
    idw_p: float = 2.0
    idw_rmax: float = 10.0
    #: drop CSF "ground" points this far above the local terrain (None disables)
    refine_tolerance: float | None = 0.15
    refine_cell: float = 1.0

    def __post_init__(self):
        _require(
            self.refine_tolerance is None or 0.01 <= self.refine_tolerance <= 2,
            f"refine_tolerance must be None or in [0.01, 2] m, got {self.refine_tolerance}",
        )
        _require(
            0.1 <= self.refine_cell <= 20,
            f"refine_cell must be in [0.1, 20] m, got {self.refine_cell}",
        )
        _require(
            0.05 <= self.cloth_resolution <= 10,
            f"cloth_resolution must be in [0.05, 10] m, got {self.cloth_resolution}",
        )
        _require(
            0.01 <= self.class_threshold <= 5,
            f"class_threshold must be in [0.01, 5] m, got {self.class_threshold}",
        )
        _require(self.rigidness in (1, 2, 3), f"rigidness must be 1, 2 or 3, got {self.rigidness}")
        _require(
            0.01 <= self.time_step <= 2, f"time_step must be in [0.01, 2], got {self.time_step}"
        )
        _require(
            10 <= self.iterations <= 5000,
            f"iterations must be in [10, 5000], got {self.iterations}",
        )
        _require(1 <= self.idw_k <= 64, f"idw_k must be in [1, 64], got {self.idw_k}")
        _require(0 < self.idw_p <= 10, f"idw_p must be in (0, 10], got {self.idw_p}")
        _require(self.idw_rmax > 0, f"idw_rmax must be > 0, got {self.idw_rmax}")


@dataclass
class SegmentParams:
    """Quickshift++ instance segmentation on a height slice of the normalized cloud."""

    k: int = 150
    beta: float = 0.5
    voxel_size: float | None = 0.15
    min_z: float = 1.0
    max_z: float | None = 4.0
    upsample: bool = True
    min_tree_points: int = 50

    def __post_init__(self):
        _require(2 <= self.k <= 2000, f"k must be in [2, 2000], got {self.k}")
        _require(0 < self.beta < 1, f"beta must be in (0, 1), got {self.beta}")
        _require(
            self.voxel_size is None or 0.005 <= self.voxel_size <= 2,
            f"voxel_size must be None or in [0.005, 2] m, got {self.voxel_size}",
        )
        _require(self.min_z >= 0, f"min_z must be >= 0, got {self.min_z}")
        _require(
            self.max_z is None or self.max_z > self.min_z,
            f"max_z must be > min_z, got {self.max_z} <= {self.min_z}",
        )
        _require(
            self.min_tree_points >= 3, f"min_tree_points must be >= 3, got {self.min_tree_points}"
        )


def check_band_inside_slice(fit: FitParams, seg: SegmentParams) -> list[str]:
    """The DBH band must lie within the clustered height slice, or trees lose band points."""
    issues = []
    lo, hi = fit.breast_height - fit.band_half_width, fit.breast_height + fit.band_half_width
    if lo < seg.min_z:
        issues.append(
            f"DBH band starts at {lo:.2f} m but the cluster slice starts at min_z={seg.min_z} m"
        )
    if seg.max_z is not None and hi > seg.max_z:
        issues.append(
            f"DBH band ends at {hi:.2f} m but the cluster slice ends at max_z={seg.max_z} m"
        )
    return issues


# --------------------------------------------------------------------------- stages


def load_points(path: str | Path, reader: str | None = None, **reader_options) -> np.ndarray:
    path = Path(path).expanduser()
    if not path.exists():
        raise InputDataError(f"input not found: {path}")
    load_plugins()
    rdr = registry.reader_for(path, reader)
    logger.info(f"reading {path} with reader '{rdr.name}'")
    t0 = time.perf_counter()
    try:
        xyz = rdr.read(path, **reader_options)
    except InputDataError:
        raise
    except Exception as exc:
        raise InputDataError(
            f"reader '{rdr.name}' failed on {path.name}: {type(exc).__name__}: {exc}"
        ) from exc
    _validate_points(xyz, path.name)
    extent = xyz.max(axis=0) - xyz.min(axis=0)
    logger.info(
        f"loaded {len(xyz):,} points in {time.perf_counter() - t0:.1f}s; extent "
        f"{extent[0]:.1f} x {extent[1]:.1f} x {extent[2]:.1f} m"
    )
    return xyz


def _validate_points(xyz: np.ndarray, name: str) -> None:
    if not isinstance(xyz, np.ndarray) or xyz.ndim != 2 or xyz.shape[1] != 3:
        raise InputDataError(
            f"{name}: reader must return an (N, 3) array, got {getattr(xyz, 'shape', type(xyz))}"
        )
    if len(xyz) < 100:
        raise InputDataError(f"{name}: only {len(xyz)} points; a forest plot needs far more")
    if not np.all(np.isfinite(xyz)):
        raise InputDataError(f"{name}: contains NaN/inf coordinates")
    extent = xyz.max(axis=0) - xyz.min(axis=0)
    if extent[2] < 1.5:
        raise InputDataError(
            f"{name}: vertical extent is only {extent[2]:.2f} m; DBH needs stems up to at least 1.5 m"
        )
    if np.max(extent) > 5000:
        logger.warning(
            f"{name}: extent {np.max(extent):.0f} units - is the cloud in metres? Parameters assume metres."
        )


def segment_ground(points: np.ndarray, params: GroundParams) -> tuple[np.ndarray, np.ndarray]:
    """Return (ground_xyz, non_ground_xyz) using the Cloth Simulation Filter."""
    try:
        import CSF
    except ImportError as exc:
        raise StageError("ground", "cloth-simulation-filter is not installed") from exc
    t0 = time.perf_counter()
    csf = CSF.CSF()
    csf.params.bSloopSmooth = params.slope_smooth
    csf.params.cloth_resolution = params.cloth_resolution
    csf.params.rigidness = params.rigidness
    csf.params.time_step = params.time_step
    csf.params.class_threshold = params.class_threshold
    csf.params.interations = params.iterations  # sic: CSF's attribute name
    csf.setPointCloud(np.ascontiguousarray(points, dtype=np.float64))
    ground, non_ground = CSF.VecInt(), CSF.VecInt()
    csf.do_filtering(ground, non_ground, exportCloth=False)
    g, ng = np.asarray(ground, dtype=np.int64), np.asarray(non_ground, dtype=np.int64)
    if len(g) < 10:
        raise StageError(
            "ground",
            f"only {len(g)} ground points found; try a larger cloth_resolution or class_threshold",
        )
    ground_xyz, non_ground_xyz = points[g], points[ng]
    if params.refine_tolerance is not None:
        ground_xyz, lifted = refine_ground(ground_xyz, params.refine_cell, params.refine_tolerance)
        if len(lifted):
            non_ground_xyz = np.concatenate([non_ground_xyz, lifted])
            logger.info(
                f"ground refinement moved {len(lifted):,} points (stem bases, low vegetation) "
                f"more than {params.refine_tolerance} m above the terrain to non-ground"
            )
    if len(non_ground_xyz) == 0:
        raise StageError(
            "ground", "every point was classified as ground; try a smaller class_threshold"
        )
    logger.info(
        f"ground segmentation: {len(ground_xyz):,} ground / {len(non_ground_xyz):,} non-ground "
        f"in {time.perf_counter() - t0:.1f}s"
    )
    return ground_xyz, non_ground_xyz


def refine_ground(
    ground: np.ndarray, cell: float, tolerance: float
) -> tuple[np.ndarray, np.ndarray]:
    """
    Remove points CSF labelled as ground that sit clearly above the terrain.

    CSF's ``class_threshold`` keeps everything within that distance of the cloth, which
    includes the lowest part of every stem. Those points lift the interpolated terrain
    under trees, shifting the breast-height band upward (DBH bias on tapered stems) and
    the reported ground elevation. Here each ``cell`` x ``cell`` column gets a terrain level
    from the 10th percentile of its points (true ground dominates the low end even under a
    stem), and points more than ``tolerance`` above it are returned as non-ground. Using
    each cell's own level (no neighbourhood minimum) keeps it unbiased on slopes.

    :return: (kept_ground, lifted_points)
    """
    ij = np.floor((ground[:, :2] - ground[:, :2].min(axis=0)) / cell).astype(np.int64)
    _, cell_id = np.unique(ij, axis=0, return_inverse=True)
    cell_id = cell_id.ravel()
    order = np.argsort(cell_id, kind="stable")
    sorted_ids = cell_id[order]
    _, starts = np.unique(sorted_ids, return_index=True)
    ends = np.r_[starts[1:], len(order)]
    level = np.empty(len(starts))
    z_sorted = ground[order, 2]
    for c, (s, e) in enumerate(zip(starts, ends)):
        level[c] = np.percentile(z_sorted[s:e], 10)
    keep = ground[:, 2] <= level[cell_id] + tolerance
    return ground[keep], ground[~keep]


def normalize_heights(
    ground: np.ndarray, non_ground: np.ndarray, params: GroundParams
) -> np.ndarray:
    """Non-ground points with z replaced by height above the interpolated terrain."""
    t0 = time.perf_counter()
    gz = interpolate_ground_z(
        ground, non_ground, k=params.idw_k, p=params.idw_p, rmax=params.idw_rmax
    )
    out = non_ground.copy()
    out[:, 2] -= gz
    below = int(np.sum(out[:, 2] < -0.5))
    if below > 0.01 * len(out):
        logger.warning(
            f"{below:,} points lie > 0.5 m below the terrain model; ground segmentation may be off"
        )
    logger.info(f"height normalization done in {time.perf_counter() - t0:.1f}s")
    return out


def _voxel_downsample(points: np.ndarray, voxel: float) -> np.ndarray:
    keys = np.floor(points / voxel).astype(np.int64)
    _, inverse = np.unique(keys, axis=0, return_inverse=True)
    inverse = inverse.ravel()
    counts = np.bincount(inverse)
    out = np.zeros((len(counts), 3))
    for d in range(3):
        out[:, d] = np.bincount(inverse, weights=points[:, d]) / counts
    return out


def segment_trees(normalized: np.ndarray, params: SegmentParams) -> tuple[np.ndarray, np.ndarray]:
    """
    Quickshift++ instance segmentation on the height slice [min_z, max_z].

    :return: (slice_points, labels); labels are -1 for noise / unassigned.
    """
    try:
        from .cluster import project_2d_then_cluster_3d
    except ImportError as exc:
        raise StageError(
            "segment",
            f"the quickshift++ extension is not built ({exc}). Reinstall with: pip install -e .",
        ) from exc
    from .cloud import Cloud

    t0 = time.perf_counter()
    z = normalized[:, 2]
    mask = z > params.min_z
    if params.max_z is not None:
        mask &= z < params.max_z
    sliced = normalized[mask]
    if len(sliced) < params.k + 1:
        raise StageError(
            "segment",
            f"only {len(sliced)} points between {params.min_z} and {params.max_z} m (need > k={params.k})",
        )
    work = _voxel_downsample(sliced, params.voxel_size) if params.voxel_size else sliced
    if len(work) < params.k + 1:
        raise StageError(
            "segment",
            f"{len(work)} points after voxel_size={params.voxel_size} downsampling (need > k={params.k}); reduce voxel_size or k",
        )
    logger.info(
        f"clustering {len(work):,} points (slice {len(sliced):,}) with k={params.k}, beta={params.beta}"
    )
    try:
        clustered = project_2d_then_cluster_3d(
            Cloud.from_points(work), k=params.k, beta=params.beta
        )
    except Exception as exc:
        raise StageError("segment", f"quickshift++ failed: {type(exc).__name__}: {exc}") from exc
    labels_low = clustered.instance.ravel().astype(np.int64)

    if params.voxel_size and params.upsample:
        tree = KDTree(work)
        dist, idx = tree.query(sliced, k=1, distance_upper_bound=2 * params.voxel_size, workers=-1)
        labels = np.full(len(sliced), -1, dtype=np.int64)
        ok = np.isfinite(dist)
        labels[ok] = labels_low[idx[ok]]
        points_out = sliced
    else:
        labels, points_out = labels_low, work
    n_inst = len(np.unique(labels[labels >= 0]))
    logger.info(f"segmentation: {n_inst} instances in {time.perf_counter() - t0:.1f}s")
    return points_out, labels


def group_trees(points: np.ndarray, labels: np.ndarray, min_points: int) -> dict[int, np.ndarray]:
    if len(points) != len(labels):
        raise StageError("group", f"{len(points)} points but {len(labels)} labels")
    order = np.argsort(labels, kind="stable")
    sorted_labels = labels[order]
    uniq, starts = np.unique(sorted_labels, return_index=True)
    ends = np.r_[starts[1:], len(sorted_labels)]
    trees, small = {}, 0
    for lab, s, e in zip(uniq, starts, ends):
        if lab < 0:
            continue
        if e - s < min_points:
            small += 1
            continue
        trees[int(lab)] = points[order[s:e]]
    if small:
        logger.debug(f"ignored {small} instances with < {min_points} points")
    return trees


def fit_trees(
    trees: dict[int, np.ndarray],
    ground: np.ndarray,
    fit: FitParams,
    ground_params: GroundParams | None = None,
    metrics: Sequence[str] | None = None,
    progress: ProgressFn | None = None,
    keep_points: bool = True,
) -> list[TreeRecord]:
    """
    Fit every tree. One tree failing never aborts the run: its record gets
    ``status='failed'`` and the reason in ``message``.

    :param metrics: names of metric plugins to run (None = all registered).
    """
    gp = ground_params or GroundParams()
    load_plugins()
    metric_objs = [
        registry.metrics[m] for m in (metrics if metrics is not None else sorted(registry.metrics))
    ]
    ground_tree = KDTree(ground[:, :2])
    records: list[TreeRecord] = []
    t0 = time.perf_counter()
    n = len(trees)
    for i, (tid, pts) in enumerate(sorted(trees.items())):
        if progress:
            progress(i / max(n, 1), f"fitting tree {tid} ({i + 1}/{n})")
        # provisional position: centroid of band points (or all points)
        band = pts[np.abs(pts[:, 2] - fit.breast_height) <= fit.band_half_width]
        ref = band if len(band) else pts
        cx, cy = float(np.median(ref[:, 0])), float(np.median(ref[:, 1]))
        try:
            sf = fit_stem(pts, fit, seed_offset=tid)
            cx, cy = sf.center_x, sf.center_y
        except FitError as exc:
            sf = None
            reason = str(exc)
        except Exception as exc:  # unexpected numeric failure: log with traceback, keep going
            logger.exception(f"tree {tid}: unexpected error during fit")
            sf = None
            reason = f"internal error: {type(exc).__name__}: {exc}"

        gz = float(
            interpolate_ground_z(
                ground,
                np.array([[cx, cy]]),
                k=gp.idw_k,
                p=gp.idw_p,
                rmax=gp.idw_rmax,
                kdtree=ground_tree,
            )[0]
        )
        rec = TreeRecord(
            tree_id=tid,
            status=STATUS_FAILED,
            x=cx,
            y=cy,
            ground_z=gz,
            breast_height=fit.breast_height,
            bh_elevation=gz + fit.breast_height,
            n_points=int(len(pts)),
            n_band_points=int(len(band)),
            points=pts if keep_points else None,
        )
        if sf is None:
            rec.message = reason
            logger.debug(f"tree {tid}: failed - {reason}")
        else:
            rec.status = STATUS_WARNING if sf.warnings else STATUS_OK
            rec.dbh, rec.dbh_se = sf.dbh, sf.dbh_se
            rec.ci_low, rec.ci_high = sf.ci_low, sf.ci_high
            rec.confidence, rec.ci_method = sf.confidence, sf.ci_method
            rec.n_band_points, rec.inlier_ratio, rec.rmse = (
                sf.n_band_points,
                sf.inlier_ratio,
                sf.rmse,
            )
            rec.arc_coverage, rec.max_gap_deg, rec.tilt_deg = (
                sf.arc_coverage,
                sf.max_gap_deg,
                sf.tilt_deg,
            )
            rec.axis = sf.axis
            rec.message = "; ".join(sf.warnings)
            for m in metric_objs:
                try:
                    values = m.compute(rec)
                    missing = set(m.columns) - set(values)
                    if missing:
                        raise ValueError(f"did not return columns {sorted(missing)}")
                    rec.extra.update({k: values[k] for k in m.columns})
                except Exception as exc:  # a broken metric plugin must not lose the tree
                    logger.warning(
                        f"metric '{m.name}' failed on tree {tid}: {type(exc).__name__}: {exc}"
                    )
        records.append(rec)
    if progress:
        progress(1.0, "fitting done")
    s = summarize(records)
    logger.info(
        f"fitted {s['ok']} ok, {s['warning']} with warnings, {s['failed']} failed "
        f"of {s['total']} instances in {time.perf_counter() - t0:.1f}s"
    )
    return records


def summarize(records: Sequence[TreeRecord]) -> dict:
    statuses = [r.status for r in records]
    fitted = [r for r in records if r.fitted]
    dbh = np.array([r.dbh for r in fitted]) if fitted else np.array([])
    return dict(
        total=len(records),
        ok=statuses.count(STATUS_OK),
        warning=statuses.count(STATUS_WARNING),
        failed=statuses.count(STATUS_FAILED),
        mean_dbh=float(dbh.mean()) if len(dbh) else float("nan"),
        basal_area_m2=float(np.sum(np.pi * (dbh / 2) ** 2)) if len(dbh) else 0.0,
    )


def export_records(
    records: Sequence[TreeRecord],
    output_dir: Path,
    formats: Sequence[str],
    context: ExportContext,
    stem: str = "trees",
    options: dict[str, dict] | None = None,
) -> list[Path]:
    """Run each requested exporter. A failing exporter is logged and the rest still run."""
    load_plugins()
    options = options or {}
    written, errors = [], []
    for fmt in formats:
        try:
            exp = registry.exporter(fmt)
            path = Path(output_dir) / f"{stem}{exp.extension}"
            written.append(exp.export(records, path, context, **options.get(fmt, {})))
        except Exception as exc:
            logger.error(f"export '{fmt}' failed: {type(exc).__name__}: {exc}")
            errors.append(f"{fmt}: {exc}")
    if errors and not written:
        raise StageError("export", "; ".join(errors))
    return written


def context_for(source: str, fit: FitParams, **params) -> ExportContext:
    return ExportContext(
        source=source,
        breast_height=fit.breast_height,
        confidence=fit.confidence,
        parameters={
            "fit": fit.to_dict(),
            **{
                k: asdict(v) if hasattr(v, "__dataclass_fields__") else v for k, v in params.items()
            },
        },
    )
