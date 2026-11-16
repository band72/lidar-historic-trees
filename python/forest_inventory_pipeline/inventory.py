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
from typing import Callable, Mapping, Sequence

import numpy as np
from scipy.spatial import KDTree

from . import exclusions
from .errors import FitError, InputDataError, ParameterError, StageError
from .structures import OverheadIndex
from .plugins import ExportContext, load_plugins, registry
from .preprocess.height_normalization import interpolate_ground_z
from .records import (
    STATUS_ADDED,
    STATUS_EXCLUDED,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_WARNING,
    TreeRecord,
)
from .traits.dbh.robust_fit import FitParams, fit_stem
from .units import METRES_PER_UNIT as METRES_PER_UNIT_
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
    #: where ground comes from: "auto" (the file's ASPRS ground class if present, else CSF),
    #: "csf" (always run the Cloth Simulation Filter), "classification" (require class 2)
    source: str = "auto"

    def __post_init__(self):
        _require(
            self.source in ("auto", "csf", "classification"),
            f"source must be auto, csf or classification, got {self.source!r}",
        )
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


NOISE_CLASSES = (7, 18)  # ASPRS low / high noise


@dataclass
class LoadedCloud:
    """A point cloud in metres plus what was learned about it while reading."""

    xyz: np.ndarray
    classification: np.ndarray | None
    units: str  # the unit the file(s) were in (used as the default output unit)
    crs: str | None
    crs_wkt: str | None
    sources: list[str]


def expand_inputs(paths: str | Path | Sequence[str | Path]) -> list[Path]:
    """Files as given; a folder (that is not a ROS2 bag) expands to the supported files in it."""
    load_plugins()
    items = [paths] if isinstance(paths, (str, Path)) else list(paths)
    if not items:
        raise InputDataError("no input given")
    out: list[Path] = []
    for item in items:
        p = Path(item).expanduser()
        if not p.exists():
            raise InputDataError(f"input not found: {p}")
        if p.is_dir() and not (p / "metadata.yaml").exists():
            supported = {e for r in registry.readers.values() for e in r.extensions if e != "/"}
            found = sorted(f for f in p.iterdir() if f.is_file() and f.suffix.lower() in supported)
            # .txt/.csv/.asc are generic: a folder usually also holds results tables
            # (trees.csv) or notes, which must not be read as points. Use them only
            # when the folder has no real point cloud files.
            generic = {".txt", ".csv", ".asc"}
            cloud_files = [f for f in found if f.suffix.lower() not in generic]
            if cloud_files and len(cloud_files) < len(found):
                skipped = [f.name for f in found if f.suffix.lower() in generic]
                logger.info(
                    f"{p}: ignoring text/CSV files in a folder with point clouds: {skipped}"
                )
                found = cloud_files
            if not found:
                raise InputDataError(
                    f"{p}: folder has no supported point cloud files ({sorted(supported)})"
                )
            logger.info(f"{p}: {len(found)} files")
            out += found
        else:
            out.append(p)
    return out


def load_cloud(
    paths: str | Path | Sequence[str | Path],
    reader: str | None = None,
    units: str = "auto",
    crop: str | Sequence[float] | None = None,
    keep_noise: bool = False,
    **reader_options,
) -> LoadedCloud:
    """
    Read one or more point clouds (e.g. LAS tiles) and return them merged, in metres.

    :param units: unit of the files' coordinates: ``auto`` (from the file header, metres if
        none is declared), ``m``, ``ft`` or ``us_ft``. An explicit unit overrides the header.
    :param crop: ``xmin,ymin,xmax,ymax`` in the files' own units; LAS files are cropped
        while reading, so only the kept points are held in memory.
    :param keep_noise: keep points classified as noise (ASPRS 7, 18); dropped by default.
    """
    from .plugins import PointCloudData
    from .plugins.builtin.readers import parse_bbox
    from .units import guess_unit_hint, normalize_unit

    requested = "auto" if units in (None, "", "auto") else normalize_unit(units)
    bbox = parse_bbox(crop)
    files = expand_inputs(paths)
    parts: list[tuple[Path, PointCloudData]] = []
    t0 = time.perf_counter()
    for path in files:
        rdr = registry.reader_for(path, reader)
        opts = dict(reader_options)
        if bbox is not None and any(o.name == "crop" for o in rdr.options):
            opts["crop"] = ",".join(str(v) for v in bbox)
        logger.info(f"reading {path} with reader '{rdr.name}'")
        try:
            data = rdr.read(path, **opts)
        except InputDataError:
            raise
        except Exception as exc:
            raise InputDataError(
                f"reader '{rdr.name}' failed on {path.name}: {type(exc).__name__}: {exc}"
            ) from exc
        if not isinstance(data, PointCloudData):
            data = PointCloudData(xyz=np.asarray(data, dtype=np.float64))
        if bbox is not None and not data.cropped:
            m = (
                (data.xyz[:, 0] >= bbox[0])
                & (data.xyz[:, 0] <= bbox[2])
                & (data.xyz[:, 1] >= bbox[1])
                & (data.xyz[:, 1] <= bbox[3])
            )
            data.xyz = data.xyz[m]
            if data.classification is not None:
                data.classification = data.classification[m]
        if len(data.xyz) == 0:
            logger.warning(f"{path.name}: no points (after crop); skipped")
            continue
        parts.append((path, data))
    if not parts:
        raise InputDataError("no points to process" + (f" inside crop box {bbox}" if bbox else ""))

    # units: explicit beats header; tiles must agree
    declared = {d.units for _, d in parts if d.units}
    silent = [pth.name for pth, d in parts if not d.units]
    if declared and silent and requested == "auto":
        # applying one file's units to a file that declares none would silently corrupt it
        raise InputDataError(
            f"{len(parts) - len(silent)} file(s) declare units ({', '.join(sorted(declared))}) but "
            f"{', '.join(silent[:3])}{'...' if len(silent) > 3 else ''} declare none; "
            "set the units explicitly (--units m|ft|us_ft) if they are all in the same unit"
        )
    if len(declared) > 1:
        raise InputDataError(
            f"input files declare different units {sorted(declared)}; process them separately"
        )
    header_unit = declared.pop() if declared else None
    if requested == "auto":
        unit = header_unit or "m"
        logger.info(
            f"units: {unit} ({'from the file header' if header_unit else 'none declared, assuming metres'})"
        )
    else:
        unit = requested
        if header_unit and header_unit != unit:
            logger.warning(
                f"units set to {unit}, but the file header says {header_unit}; using {unit}"
            )
    z_declared = {d.z_units for _, d in parts if d.z_units}
    z_unit = unit if (requested != "auto" or not z_declared) else z_declared.pop()
    if z_unit != unit:
        logger.warning(
            f"horizontal units {unit} but vertical units {z_unit} (from header); converting each"
        )

    crss = {d.crs for _, d in parts if d.crs}
    if len(crss) > 1:
        raise InputDataError(f"input files use different coordinate systems {sorted(crss)}")
    crs = crss.pop() if crss else None
    wkt = next((d.crs_wkt for _, d in parts if d.crs_wkt), None)

    xyz = np.concatenate([d.xyz for _, d in parts])
    have_cls = all(d.classification is not None for _, d in parts)
    cls = np.concatenate([d.classification for _, d in parts]) if have_cls else None
    if len(parts) > 1 and not have_cls and any(d.classification is not None for _, d in parts):
        logger.warning("only some files are classified; ignoring the classification")

    if cls is not None and not keep_noise:
        noise = np.isin(cls, NOISE_CLASSES)
        if noise.any():
            logger.info(f"dropped {int(noise.sum()):,} points classified as noise (ASPRS 7/18)")
            xyz, cls = xyz[~noise], cls[~noise]

    if unit != "m" or z_unit != "m":
        f_xy, f_z = METRES_PER_UNIT_[unit], METRES_PER_UNIT_[z_unit]
        xyz = xyz * np.array([f_xy, f_xy, f_z])
        logger.info(f"converted coordinates from {unit} to metres")
    else:
        hint = guess_unit_hint(xyz)
        if hint:
            logger.warning(f"{hint}")
    name = files[0].name if len(files) == 1 else f"{len(files)} files"
    _validate_points(xyz, name)
    extent = xyz.max(axis=0) - xyz.min(axis=0)
    logger.info(
        f"loaded {len(xyz):,} points from {name} in {time.perf_counter() - t0:.1f}s; extent "
        f"{extent[0]:.1f} x {extent[1]:.1f} x {extent[2]:.1f} m"
        + (f"; CRS {crs}" if crs else "")
        + ("; has ground classification" if cls is not None and np.any(cls == 2) else "")
    )
    return LoadedCloud(
        xyz=xyz,
        classification=cls,
        units=unit,
        crs=crs,
        crs_wkt=wkt,
        sources=[str(f) for f, _ in parts],
    )


def load_points(
    path: str | Path, reader: str | None = None, units: str = "m", **reader_options
) -> np.ndarray:
    """Read a point cloud and return xyz in metres (see :func:`load_cloud` for options)."""
    return load_cloud(path, reader=reader, units=units, **reader_options).xyz


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
            f"{name}: extent {np.max(extent):.0f} units - is the cloud in metres? Parameters assume "
            "metres; for feet use units=ft (CLI: --units ft)."
        )


GROUND_CLASSES = (2, 8)  # ASPRS ground, model key-point (a ground subset in LAS 1.0-1.3)


def segment_ground(
    points: np.ndarray, params: GroundParams, classification: np.ndarray | None = None
) -> tuple[np.ndarray, np.ndarray]:
    """
    Return (ground_xyz, non_ground_xyz).

    Uses the file's ASPRS ground classification when ``params.source`` allows it and the
    cloud has one (at least 100 ground points), otherwise the Cloth Simulation Filter.
    The stem-base refinement runs in both cases.
    """
    use_cls = False
    if params.source != "csf" and classification is not None:
        is_ground = np.isin(classification, GROUND_CLASSES)
        n_ground = int(is_ground.sum())
        if n_ground >= 100 and n_ground < len(points):
            use_cls = True
        elif params.source == "classification":
            raise StageError(
                "ground",
                f"the file has only {n_ground} ground-classified points; use ground source csf",
            )
    elif params.source == "classification":
        raise StageError(
            "ground", "ground source is 'classification' but the input has no classification"
        )
    if use_cls:
        t0 = time.perf_counter()
        ground_xyz, non_ground_xyz = points[is_ground], points[~is_ground]
        logger.info(
            f"ground from the file's classification (ASPRS 2/8): {len(ground_xyz):,} points"
        )
        return _finish_ground(ground_xyz, non_ground_xyz, params, t0)
    return _segment_ground_csf(points, params)


def _finish_ground(ground_xyz, non_ground_xyz, params: GroundParams, t0: float):
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


def _segment_ground_csf(points: np.ndarray, params: GroundParams) -> tuple[np.ndarray, np.ndarray]:
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
    return _finish_ground(points[g], points[ng], params, t0)


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


def split_multi_stem(
    trees: dict[int, np.ndarray], fit: FitParams
) -> tuple[dict[int, np.ndarray], dict[int, int]]:
    """
    Split clusters that contain more than one stem at breast height.

    Instance segmentation can merge a small tree into a larger neighbour's cluster
    (seen at the default quickshift++ settings); fitting would then silently measure only
    the dominant stem. Here the cluster's breast-height band is clustered in XY with
    DBSCAN (``eps = fit.stem_gap``); every component with at least ``min_band_points``
    becomes its own stem, and the cluster's other points go to the nearest stem in XY.

    :return: (new trees dict, {new_id: parent_cluster_id}) - the largest stem keeps the
        original id; extra stems get ids above every existing id.
    """
    from sklearn.cluster import DBSCAN

    out: dict[int, np.ndarray] = {}
    parent: dict[int, int] = {}
    next_id = (max(trees) + 1) if trees else 0
    n_split = 0
    for tid, pts in sorted(trees.items()):
        in_band = np.abs(pts[:, 2] - fit.breast_height) <= fit.band_half_width
        band = pts[in_band]
        if len(band) < 2 * fit.min_band_points:
            out[tid] = pts
            continue
        # DBSCAN on 2 cm cells, not raw points: at 1 cm spacing every raw point has hundreds
        # of neighbours within stem_gap, and a 3 M-point field cluster took ~8 GB. Gaps of
        # stem_gap (default 15 cm) still separate stems at this resolution.
        cell = min(0.02, fit.stem_gap / 4)
        _, first, inverse = np.unique(
            np.floor(band[:, :2] / cell).astype(np.int64),
            axis=0,
            return_index=True,
            return_inverse=True,
        )
        cell_labels = DBSCAN(eps=fit.stem_gap, min_samples=2).fit_predict(band[first, :2])
        labels = cell_labels[inverse.ravel()]
        comps = [
            c for c in np.unique(labels) if c >= 0 and np.sum(labels == c) >= fit.min_band_points
        ]
        if len(comps) <= 1:
            out[tid] = pts
            continue
        comps.sort(key=lambda c: -np.sum(labels == c))
        centers = np.array([np.median(band[labels == c, :2], axis=0) for c in comps])
        # every point of the cluster goes to the nearest stem centre in XY
        owner = KDTree(centers).query(pts[:, :2], workers=-1)[1]  # nearest stem centre in XY
        for rank in range(len(comps)):
            new_id = tid if rank == 0 else next_id
            if rank:
                next_id += 1
            out[new_id] = pts[owner == rank]
            parent[new_id] = tid
        n_split += 1
        logger.debug(f"cluster {tid}: {len(comps)} stems at breast height, split")
    if n_split:
        logger.info(
            f"split {n_split} clusters containing several stems -> {len(out) - len(trees)} extra stems"
        )
    return out, parent


def fit_trees(
    trees: dict[int, np.ndarray],
    ground: np.ndarray,
    fit: FitParams,
    ground_params: GroundParams | None = None,
    metrics: Sequence[str] | None = None,
    progress: ProgressFn | None = None,
    keep_points: bool = True,
    overhead: OverheadIndex | None = None,
) -> list[TreeRecord]:
    """
    Fit every tree. One tree failing never aborts the run: its record gets
    ``status='failed'`` and the reason in ``message``.

    :param overhead: the height-normalized cloud above the stems (``OverheadIndex``); with
        it, posts under a roof are rejected (``fit.reject_roof_posts``).

    :param metrics: names of metric plugins to run (None = all registered).
    """
    gp = ground_params or GroundParams()
    load_plugins()
    metric_objs = [
        registry.metrics[m] for m in (metrics if metrics is not None else sorted(registry.metrics))
    ]
    ground_tree = KDTree(ground[:, :2])
    parents: dict[int, int] = {}
    if fit.split_stems:
        trees, parents = split_multi_stem(trees, fit)
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
        if sf is not None and overhead is not None and fit.reject_non_stems and fit.reject_roof_posts:
            rc = overhead.roof(sf.center_x, sf.center_y, sf.radius, fit.breast_height + 0.5, seed=tid)
            if rc is not None:
                logger.debug(
                    f"tree {tid}: roof check layer {rc.layer_fraction:.2f} span {rc.span:.2f} "
                    f"at {rc.plane_height:.1f} m"
                )
            if rc is not None and rc.is_roof(fit.roof_layer_fraction, fit.roof_min_span):
                reason = (
                    f"under a roof: {rc.layer_fraction:.0%} of the material around "
                    f"{rc.plane_height:.1f} m above it lies on one smooth plane "
                    "(a post of a shed, carport or pole barn?)"
                )
                sf = None

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
            point_ground=(
                interpolate_ground_z(
                    ground, pts, k=gp.idw_k, p=gp.idw_p, rmax=gp.idw_rmax, kdtree=ground_tree
                )
                if keep_points
                else None
            ),
        )
        rec.extra["cluster_id"] = parents.get(tid, tid)
        if sf is None and parents.get(tid, tid) != tid:
            # an extra stem split off a cluster that does not fit is a branch/clutter fragment
            logger.debug(f"split stem {tid} (cluster {parents[tid]}) dropped: {reason}")
            continue
        if sf is None:
            rec.message = reason
            logger.debug(f"tree {tid}: failed - {reason}")
        else:
            rec.status = STATUS_WARNING if sf.warnings else STATUS_OK
            rec.dbh, rec.dbh_se, rec.view_sd = sf.dbh, sf.dbh_se, sf.view_sd
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
            rec.band_half_width = sf.band_half_width
            rec.dbh_method = sf.dbh_method
            rec.extra["large_tree"] = "yes" if sf.dbh >= fit.large_tree_dbh else "no"
            if overhead is not None:
                vh = overhead.visible_height(sf.center_x, sf.center_y, sf.radius)
                rec.extra["visible_height_m"] = round(vh, 2) if np.isfinite(vh) else ""
            rec.message = "; ".join(sf.warnings + sf.notes)
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
    records = dedupe_stems(records)
    s = summarize(records)
    logger.info(
        f"fitted {s['ok']} ok, {s['warning']} with warnings, {s['failed']} failed "
        f"of {s['total']} instances in {time.perf_counter() - t0:.1f}s"
    )
    return records


def dedupe_stems(records: list[TreeRecord]) -> list[TreeRecord]:
    """
    Drop duplicate estimates of one stem (its points were split across two clusters).

    Two real stems cannot overlap at breast height, so when one fitted centre lies inside
    another fitted circle, only the estimate with more band inliers is kept.
    """
    fitted = [r for r in records if r.fitted]
    if len(fitted) < 2:
        return records
    xy = np.array([[r.x, r.y] for r in fitted])
    radius = np.array([r.dbh / 2 for r in fitted])
    support = np.array([r.n_band_points * r.inlier_ratio for r in fitted])
    tree = KDTree(xy)
    drop: set[int] = set()
    for i in np.argsort(-support):
        if fitted[i].tree_id in drop:
            continue
        for j in tree.query_ball_point(xy[i], r=float(radius.max())):
            if j == i or fitted[j].tree_id in drop or support[j] > support[i]:
                continue
            if np.linalg.norm(xy[i] - xy[j]) < max(radius[i], radius[j]):
                drop.add(fitted[j].tree_id)
                logger.debug(
                    f"tree {fitted[j].tree_id} duplicates tree {fitted[i].tree_id} (overlapping stems); dropped"
                )
    if drop:
        logger.info(f"removed {len(drop)} duplicate stem estimates")
    return [r for r in records if r.tree_id not in drop]


def apply_exclusions(
    records: Sequence[TreeRecord],
    excluded: set[int] | Sequence[int] | dict[int, str],
) -> list[TreeRecord]:
    """
    Records with the given tree ids marked ``excluded`` (by a reviewer).

    ``excluded`` is a collection of tree ids, or ``{tree_id: reason}`` (e.g. "shrub"); a
    reason is added to the message and to the ``exclusion_reason`` column.

    Returns new records for the excluded trees and leaves the input untouched, so cached
    fits can be shared. Excluded trees stay in the CSV with their measurements and status
    ``excluded``, and are left out of totals, maps, DXF and GeoJSON.
    """
    from dataclasses import replace

    reasons = (
        {int(k): str(v or "") for k, v in excluded.items()}
        if isinstance(excluded, dict)
        else {int(i): "" for i in excluded}
    )
    out = []
    for r in records:
        if r.tree_id in reasons and r.status != STATUS_FAILED:
            reason = reasons[r.tree_id]
            note = (
                "excluded by reviewer"
                + (f": {reason}" if reason else "")
                + (f" (was {r.status}: {r.message})" if r.message else f" (was {r.status})")
            )
            extra = dict(r.extra)
            extra["exclusion_reason"] = reason
            out.append(replace(r, status=STATUS_EXCLUDED, message=note, extra=extra))
        else:
            out.append(r)
    return out


def apply_additions(
    records: Sequence[TreeRecord], added: Mapping[int, str] | Sequence[int]
) -> list[TreeRecord]:
    """
    Records with the given (failed) tree ids kept as trees by a reviewer: status ``added``,
    a position without a measured DBH. Like :func:`apply_exclusions`, returns new records and
    leaves the input untouched.
    """
    from dataclasses import replace

    reasons = (
        {int(k): str(v or "") for k, v in added.items()}
        if isinstance(added, Mapping)
        else {int(i): "" for i in added}
    )
    out = []
    for r in records:
        if r.tree_id in reasons and r.status == STATUS_FAILED:
            reason = reasons[r.tree_id]
            note = (
                "kept as a tree by reviewer"
                + (f": {reason}" if reason else "")
                + f"; DBH not measured (the checks said: {r.message})"
            )
            extra = dict(r.extra)
            extra["exclusion_reason"] = reason
            out.append(replace(r, status=STATUS_ADDED, message=note, extra=extra))
        else:
            out.append(r)
    return out


def summarize(records: Sequence[TreeRecord]) -> dict:
    statuses = [r.status for r in records]
    fitted = [r for r in records if r.fitted]
    dbh = np.array([r.dbh for r in fitted]) if fitted else np.array([])
    return dict(
        total=len(records),
        ok=statuses.count(STATUS_OK),
        warning=statuses.count(STATUS_WARNING),
        failed=statuses.count(STATUS_FAILED),
        excluded=statuses.count(STATUS_EXCLUDED),
        added=statuses.count(STATUS_ADDED),
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
    metadata: bool = True,
) -> list[Path]:
    """
    Run each requested exporter. A failing exporter is logged and the rest still run.

    With ``metadata`` also writes ``<stem>.prj`` (when the CRS WKT is known) and
    ``run.json``: inputs, units, CRS, parameters, summary, and every output's size and
    SHA-256, so results can be traced and verified later.
    """
    load_plugins()
    options = options or {}
    written, errors, outputs = [], [], []
    for fmt in formats:
        try:
            exp = registry.exporter(fmt)
            path = Path(output_dir) / f"{stem}{exp.extension}"
            out = exp.export(records, path, context, **options.get(fmt, {}))
            written.append(out)
            outputs.append(
                {"exporter": fmt, "options": options.get(fmt, {}), "file": Path(out).name}
            )
        except Exception as exc:
            logger.error(f"export '{fmt}' failed: {type(exc).__name__}: {exc}")
            errors.append(f"{fmt}: {exc}")
    if errors and not written:
        raise StageError("export", "; ".join(errors))
    if metadata:
        written += _write_metadata(records, Path(output_dir), stem, context, outputs, errors)
    return written


def _sha256(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def software_version() -> str:
    try:
        from importlib.metadata import version

        return version("forest_inventory_pipeline")
    except Exception:
        return "unknown"


def _write_metadata(
    records, output_dir: Path, stem: str, context: ExportContext, outputs, errors
) -> list[Path]:
    import datetime
    import json
    import platform

    from .plugins import atomic_write

    extra: list[Path] = []
    if context.crs_wkt:
        prj = output_dir / f"{stem}.prj"
        with atomic_write(prj, "w", encoding="utf-8") as fh:
            fh.write(context.crs_wkt)
        extra.append(prj)
        outputs.append({"exporter": "prj", "file": prj.name})
    excl = exclusions.excluded_in(records)
    if excl:
        # the review travels with the results; `fip inventory --exclusions` reapplies it
        path = exclusions.save(output_dir / exclusions.FILE_NAME, excl, source=context.source)
        extra.append(path)
        outputs.append({"exporter": "exclusions", "file": path.name})
    for o in outputs:
        f = output_dir / o["file"]
        if f.exists():
            o["bytes"] = f.stat().st_size
            o["sha256"] = _sha256(f)
    s = summarize(records)
    meta = {
        "software": {
            "name": "LiDAR Tree Inventory",
            "version": software_version(),
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "created_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "source": context.source,
        "output_units": context.units,
        "crs": context.crs,
        "breast_height_m": context.breast_height,
        "confidence": context.confidence,
        "summary": s,
        "outputs": outputs,
        "errors": errors,
        "parameters": context.parameters,
    }
    path = output_dir / "run.json"
    with atomic_write(path, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2, default=str)
    logger.info(f"run metadata written: {path}")
    return extra + [path]


def context_for(
    source: str,
    fit: FitParams,
    units: str = "m",
    crs: str | None = None,
    crs_wkt: str | None = None,
    **params,
) -> ExportContext:
    from .units import normalize_unit

    return ExportContext(
        source=source,
        units=normalize_unit(units),
        crs=crs,
        crs_wkt=crs_wkt,
        breast_height=fit.breast_height,
        confidence=fit.confidence,
        parameters={
            "fit": fit.to_dict(),
            **{
                k: asdict(v) if hasattr(v, "__dataclass_fields__") else v for k, v in params.items()
            },
        },
    )
