"""
Interactive tree inventory: find trees, fit stems, DBH with confidence intervals.

Run with ``fip app`` (or ``streamlit run`` on this file).

Interaction model
-----------------
- Slow stages (loading, ground segmentation, tree segmentation) live in a form and run
  only when "Run tree extraction" is pressed; results are kept in the session.
- Stem fitting is fast, so its parameters are live sliders: every change refits all trees
  (cached per parameter set) and every chart, table and export updates immediately.
"""

from __future__ import annotations

import hashlib
import io
import tempfile
import time
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from forest_inventory_pipeline import inventory as inv
from forest_inventory_pipeline.calibration import _to_metres, calibrate
from forest_inventory_pipeline.errors import FIPError
from forest_inventory_pipeline.plugins import load_plugins, registry
from forest_inventory_pipeline.records import (
    STATUS_EXCLUDED,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_WARNING,
)
from forest_inventory_pipeline.synthetic import make_plot
from forest_inventory_pipeline.traits.dbh.robust_fit import FitParams, band_points, residuals
from forest_inventory_pipeline.units import (
    LABELS,
    from_metres,
    is_feet,
    normalize_unit,
    to_metres,
)
from forest_inventory_pipeline.utils import logger

from forest_inventory_pipeline.app.file_browser import browse, load_prefs, save_pref


def _log_dir() -> Path:
    """Per-user log folder for installed (frozen) builds; ./fip_output when run from source."""
    import os
    import sys

    if os.environ.get("FIP_HOME"):
        return Path(os.environ["FIP_HOME"]) / "logs"
    if getattr(sys, "frozen", False):
        if sys.platform == "win32":
            base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
            return base / "LiDARTreeInventory" / "logs"
        base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
        return base / "lidar-tree-inventory" / "logs"
    return Path.cwd() / "fip_output"


LOG_DIR = _log_dir()
LOG_FILE = LOG_DIR / "app.log"

# status palette (fixed) + marker shape + label: status is never carried by colour alone
STATUS_STYLE = {
    STATUS_OK: dict(color="#0ca30c", symbol="circle", label="✔ ok"),
    STATUS_WARNING: dict(color="#fab219", symbol="triangle-up", label="⚠ warning"),
    STATUS_FAILED: dict(color="#d03b3b", symbol="x", label="✖ failed"),
    STATUS_EXCLUDED: dict(color="#8a8a8a", symbol="circle-x-open", label="⊘ excluded"),
}
NOT_MEASURED = (STATUS_FAILED, STATUS_EXCLUDED)


class DisplayUnits:
    """Converts internal metres to the unit chosen for display and export."""

    def __init__(self, unit: str = "m"):
        self.unit = normalize_unit(unit)
        self.feet = is_feet(self.unit)
        self.ul = "ft" if self.feet else "m"  # length label
        self.dl = "in" if self.feet else "cm"  # diameter label
        self.al = "ft²/ac" if self.feet else "m²/ha"  # basal area per area label

    def L(self, v):
        return from_metres(v, self.unit)

    def D(self, v_m):
        return v_m / 0.0254 if self.feet else v_m * 100.0

    def BA(self, m2_per_ha: float) -> float:
        return m2_per_ha * 4.356 if self.feet else m2_per_ha  # 1 m²/ha = 4.356 ft²/acre


U = DisplayUnits("m")  # replaced in main() from the sidebar choice

SERIES_ESTIMATE = "#2a78d6"  # categorical slot 1
SERIES_TRUTH = "#eb6834"  # categorical slot 2


# --------------------------------------------------------------------------- setup


@st.cache_resource
def _init() -> bool:
    logger.configure(level="info", log_file=LOG_FILE)
    load_plugins()
    logger.info("web app started")
    return True


def _fail(stage: str, exc: Exception) -> None:
    """Show a pipeline error to the user and log it (traceback for unexpected ones)."""
    if isinstance(exc, FIPError):
        logger.error(f"{stage}: {exc}")
        st.error(f"**{stage} failed:** {exc}")
    else:
        logger.exception(f"{stage}: unexpected error")
        st.error(
            f"**{stage} failed unexpectedly:** {type(exc).__name__}: {exc}. Details are in the log tab."
        )


# --------------------------------------------------------------------------- cached stages


@st.cache_data(show_spinner=False, max_entries=4)
def load_source(
    kind: str,
    key: str,
    path: str,
    reader_opts: tuple,
    demo: tuple,
    _upload: bytes | None,
    units: str = "auto",
    crop: str = "",
):
    """Return (LoadedCloud, truth or None). ``key`` identifies the source for caching."""
    if kind == "demo":
        n_trees, seed = demo
        plot = make_plot(n_trees=n_trees, seed=seed)
        cloud = inv.LoadedCloud(
            xyz=plot.points,
            classification=None,
            units="m",
            crs=None,
            crs_wkt=None,
            sources=[f"demo plot ({n_trees} trees, seed {seed})"],
        )
        return cloud, plot.truth
    opts = dict(reader_opts)
    if kind == "upload":
        suffix = Path(path).suffix
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as fh:
            fh.write(_upload)
            tmp = fh.name
        try:
            cloud = inv.load_cloud(tmp, units=units, crop=crop or None, **opts)
            cloud.sources = [path]
            return cloud, None
        finally:
            Path(tmp).unlink(missing_ok=True)
    return inv.load_cloud(path, units=units, crop=crop or None, **opts), None


def run_segmentation(cloud, gp: inv.GroundParams, sp: inv.SegmentParams, progress) -> dict:
    t0 = time.perf_counter()
    points = cloud.xyz
    progress.progress(0.05, "ground segmentation…")
    ground, non_ground = inv.segment_ground(points, gp, cloud.classification)
    progress.progress(0.35, "height normalization…")
    normalized = inv.normalize_heights(ground, non_ground, gp)
    progress.progress(0.5, "tree segmentation (quickshift++)…")
    pts, labels = inv.segment_trees(normalized, sp)
    trees = inv.group_trees(pts, labels, sp.min_tree_points)
    if not trees:
        raise inv.StageError(
            "segment", "no tree instances found; try a smaller beta, a smaller k or a lower min_z"
        )
    progress.progress(1.0, f"found {len(trees)} candidate trees")
    run_id = hashlib.sha1(f"{time.time()}{len(points)}".encode()).hexdigest()[:10]
    return dict(
        run_id=run_id,
        ground=ground,
        trees=trees,
        seconds=time.perf_counter() - t0,
        gp=gp,
        sp=sp,
        extent=points.max(0) - points.min(0),
        units=cloud.units,
        crs=cloud.crs,
        crs_wkt=cloud.crs_wkt,
        sources=cloud.sources,
        classified=cloud.classification is not None,
    )


@st.cache_resource(show_spinner=False, max_entries=16)
def fit_all(run_id: str, fit_dict: dict, _seg: dict):
    """Cached on (segmentation run, fit parameters); results are shared, not copied, so
    callers must not mutate them. The underscore argument is not hashed."""
    fp = FitParams(**fit_dict)
    return inv.fit_trees(_seg["trees"], _seg["ground"], fp, _seg["gp"])


# --------------------------------------------------------------------------- sidebar


def sidebar_source():
    st.sidebar.header("1 · Point cloud")
    kind = st.sidebar.radio(
        "Source",
        ["demo", "path", "upload"],
        horizontal=True,
        format_func={"demo": "Demo plot", "path": "Choose file", "upload": "Upload"}.get,
        help="Demo generates a synthetic plot with known DBH so you can see how accurate the CIs are.",
    )
    path, upload, demo, reader_opts = "", None, (25, 0), {}
    if kind == "demo":
        c1, c2 = st.sidebar.columns(2)
        demo = (c1.number_input("Trees", 1, 200, 25), c2.number_input("Seed", 0, 10_000, 0))
        key = f"demo:{demo}"
    elif kind == "path":
        exts = {e for r in registry.readers.values() for e in r.extensions if e != "/"}
        with st.sidebar.container(border=True):
            path = browse("input", mode="file", extensions=exts, pref_key="last_input_dir")
        key = f"path:{path}"
        if path:
            p = Path(path).expanduser()
            if not p.exists():
                st.sidebar.error("Path does not exist.")
            else:
                key += f":{p.stat().st_mtime_ns}"
                rdr = None
                if p.is_dir() and not (p / "metadata.yaml").exists():
                    try:
                        tiles = inv.expand_inputs(p)
                        st.sidebar.caption(
                            f"Folder: {len(tiles)} point cloud files will be merged."
                        )
                    except FIPError as exc:
                        st.sidebar.error(str(exc))
                else:
                    try:
                        rdr = registry.reader_for(p)
                    except FIPError as exc:
                        st.sidebar.error(str(exc))
                if rdr is not None and rdr.name == "rosbag":
                    with st.sidebar.expander("ROS bag options", expanded=True):
                        st.caption(rdr.description)
                        reader_opts["topic"] = st.text_input(
                            "PointCloud2 topic", "", help="Empty = auto when the bag has one."
                        )
                        reader_opts["target_frame"] = st.text_input(
                            "Target frame (via /tf)",
                            "",
                            help="e.g. map or odom. Empty = scans already in a world frame.",
                        )
                        reader_opts["every_nth"] = st.number_input(
                            "Use every n-th message", 1, 10_000, 1
                        )
                        reader_opts["max_messages"] = st.number_input(
                            "Max messages (0 = all)", 0, 1_000_000, 0
                        )
                        reader_opts["max_range"] = st.number_input(
                            "Max range (m, 0 = off)", 0.0, 500.0, 0.0
                        )
                        reader_opts["voxel_size"] = st.number_input(
                            "Dedup voxel (m)", 0.0, 0.5, 0.01, format="%.3f"
                        )
                        reader_opts["allow_sensor_frame"] = st.checkbox(
                            "Scans are from a static sensor", False
                        )
    else:
        f = st.sidebar.file_uploader(
            "Point cloud file",
            type=[
                e.lstrip(".")
                for r in registry.readers.values()
                for e in r.extensions
                if e not in ("/",)
            ],
        )
        if f is not None:
            upload, path = f.getvalue(), f.name
            key = f"upload:{f.name}:{hashlib.sha1(upload).hexdigest()}"
        else:
            key = "upload:none"
    units = "m"
    if kind != "demo":
        units = st.sidebar.selectbox(
            "Point cloud units",
            ["auto", "m", "ft", "us_ft"],
            format_func=lambda u: {
                "auto": "auto (from the file header, else metres)",
                "m": "metres",
                "ft": "feet (international)",
                "us_ft": "US survey feet",
            }[u],
            help="LAS files usually declare their units; feet are converted to metres on reading.",
        )
        crop = st.sidebar.text_input(
            "Crop (optional)",
            "",
            placeholder="xmin,ymin,xmax,ymax in file coordinates",
            help="Only process this box. LAS files are cropped while reading, so large files fit in memory.",
        ).strip()
        st.session_state["crop"] = crop
        key += f":crop={crop}"
        key += f":{units}"
    return kind, key, path, upload, demo, reader_opts, units


def sidebar_units(cloud_units: str) -> DisplayUnits:
    choice = (
        st.sidebar.segmented_control(
            "Display & export units",
            ["auto", "m", "ft", "us_ft"],
            default="auto",
            format_func=lambda u: {
                "auto": "same as cloud",
                "m": "m / cm",
                "ft": "ft / in",
                "us_ft": "US ft / in",
            }[u],
            help="Lengths in tables, charts and exports. DBH is shown in cm (metric) or inches (feet).",
        )
        or "auto"
    )
    if choice != "auto":
        return DisplayUnits(choice)
    if cloud_units == "auto":  # known once the file has been read
        cloud_units = (st.session_state.get("seg") or {}).get("units", "m")
    return DisplayUnits(cloud_units)


def sidebar_segmentation():
    st.sidebar.header("2 · Find trees")
    st.sidebar.caption("Slow stages: adjust, then press **Run tree extraction**.")
    with st.sidebar.form("segmentation"):
        with st.expander("Ground (CSF)", expanded=False):
            cloth = st.slider(
                "Cloth resolution (m)",
                0.1,
                3.0,
                0.5,
                0.05,
                help="Coarser cloth bridges gaps; finer follows micro-relief.",
            )
            thresh = st.slider("Ground class threshold (m)", 0.05, 1.5, 0.5, 0.05)
            refine = st.slider(
                "Ground refinement tolerance (m)",
                0.05,
                1.0,
                0.15,
                0.01,
                help="Points this far above the local terrain are not ground (removes stem bases from the terrain model).",
            )
            rigid = st.select_slider("Rigidness", [1, 2, 3], 3, help="1 = steep terrain, 3 = flat.")
            gsource = st.selectbox(
                "Ground source",
                ["auto", "csf", "classification"],
                format_func=lambda v: {
                    "auto": "auto (file's ground class if present, else CSF)",
                    "csf": "always run CSF",
                    "classification": "file's ground class (ASPRS 2)",
                }[v],
            )
        with st.expander("Segmentation (quickshift++)", expanded=True):
            k = st.slider(
                "Neighbours k",
                10,
                400,
                150,
                10,
                help="Larger merges nearby stems; smaller splits them.",
            )
            beta = st.slider(
                "Density β", 0.05, 0.95, 0.5, 0.05, help="Higher → fewer, larger clusters."
            )
            voxel = st.slider(
                "Voxel size (m)",
                0.02,
                0.5,
                0.15,
                0.01,
                help="Downsampling for clustering only; fitting uses full resolution.",
            )
            zr = st.slider("Height slice (m)", 0.0, 15.0, (1.0, 4.0), 0.1)
            min_pts = st.slider("Min points per tree", 10, 2000, 50, 10)
        go_btn = st.form_submit_button("Run tree extraction", type="primary", width="stretch")
    gp = inv.GroundParams(
        cloth_resolution=cloth,
        class_threshold=thresh,
        rigidness=rigid,
        refine_tolerance=refine,
        source=gsource,
    )
    sp = inv.SegmentParams(
        k=k, beta=beta, voxel_size=voxel, min_z=zr[0], max_z=zr[1], min_tree_points=min_pts
    )
    return go_btn, gp, sp


def sidebar_fit() -> FitParams | None:
    st.sidebar.header("3 · Fit stems (live)")
    s = st.sidebar
    if U.feet:
        bh = float(
            to_metres(
                s.slider(
                    "Breast height (ft)",
                    3.0,
                    6.5,
                    4.5,
                    0.1,
                    help="4.5 ft in the US; 1.3 m (4.27 ft) in most other countries.",
                ),
                U.unit,
            )
        )
        hw = float(
            to_metres(
                s.slider(
                    "Band half-width (ft)",
                    0.2,
                    2.0,
                    0.8,
                    0.05,
                    help="Points within breast height ± this are used.",
                ),
                U.unit,
            )
        )
    else:
        bh = s.slider(
            "Breast height (m)",
            1.0,
            2.0,
            1.3,
            0.05,
            help="1.3 m in most of the world; 1.37 m (4.5 ft) in the US.",
        )
        hw = s.slider(
            "Band half-width (m)",
            0.05,
            0.6,
            0.25,
            0.01,
            help="Points within breast height ± this are used.",
        )
    conf = s.slider("Confidence level", 0.80, 0.99, 0.95, 0.01, format="%.2f")
    method = (
        s.segmented_control(
            "CI method",
            ["analytic", "bootstrap"],
            default="analytic",
            help="Analytic: cluster-robust (by angular sector), instant. Bootstrap: resamples stem sectors; slower, makes fewer assumptions.",
        )
        or "analytic"
    )
    st.session_state.setdefault("model_sd_cm", 0.5)
    model_sd_cm = s.slider(
        "Systematic uncertainty (cm, 1 sd)",
        0.0,
        5.0,
        step=0.05,
        key="model_sd_cm",
        help="Error the point cloud can't reveal (non-circular stems, segmentation). Calibrate it in the Calibration tab.",
    )
    with s.expander("Fitting details"):
        tol = st.slider("Inlier tolerance (cm)", 0.2, 10.0, 2.0, 0.1)
        kernel = st.slider(
            "Robust kernel scale (cm)",
            0.5,
            20.0,
            3.0,
            0.5,
            help="Residuals far beyond this are down-weighted (branches, clutter).",
        )
        rmax = st.slider("Max DBH (cm)", 20, 400, 150, 5)
        tilt = st.slider("Max stem tilt (°)", 5.0, 45.0, 22.5, 0.5)
        iters = st.slider("RANSAC iterations", 100, 20000, 2000, 100)
        nboot = st.slider("Bootstrap resamples", 50, 2000, 200, 50, disabled=method != "bootstrap")
    with s.expander("Quality flags"):
        cov = st.slider(
            "Min arc coverage",
            0.0,
            1.0,
            0.6,
            0.05,
            help="Stems scanned from fewer sides than this are flagged as warnings "
            "(half of the circumference = 0.5).",
        )
        ovality = st.slider(
            "Stem non-roundness (%, 1 sd)",
            0.0,
            15.0,
            3.0,
            0.5,
            help="Assumed (long - short) / (long + short) of stem cross-sections. A stem seen "
            "from one side cannot show whether it is round, so its interval is widened by this "
            "amount, more the less of the circumference was scanned. Raise it for fluted or "
            "buttressed species.",
        )
        relw = st.slider("Max CI width (% of DBH)", 1, 100, 25, 1)
        inl = st.slider("Min inlier ratio", 0.0, 1.0, 0.3, 0.05)
    with s.expander("Trunk check"):
        trunk_check = st.toggle(
            "Reject clusters that are not trunks",
            True,
            help="Shrubs, posts, walls and sparse scatter are rejected: a trunk must have most "
            "of its 1-2.5 m points on a hollow, continuous, dense surface.",
        )
        density = st.slider(
            "Min. points per m² of trunk surface",
            0,
            2000,
            600,
            50,
            disabled=not trunk_check,
            help="Lower it for sparse scans (UAV, distant TLS: about 150). Too low lets sparse "
            "scatter through as large 'trees'.",
        )
        min_dbh = st.number_input(
            f"Minimum DBH ({U.dl}, 0 = off)",
            0.0,
            100.0,
            0.0,
            0.5,
            help="Stems thinner than this are not counted as trees (listed as failed with the "
            "reason). Many inventories use 7.5-12.7 cm (3-5 in); shrub stems are usually thinner, "
            "but so are young trees.",
        )
        min_dbh_m = min_dbh * (0.0254 if U.feet else 0.01)
    with s.expander("Partly seen stems"):
        complete = st.toggle(
            "Complete partly seen stems",
            False,
            help="For stems seen from one side at breast height (or with too few points there), "
            "measure over a taller window so that other heights, seen from other sides, "
            "complete the circle. A taper term keeps the DBH at breast height. Helps when the "
            "scanner moved; cannot help if the same side is hidden at every height.",
        )
        max_hw = st.slider(
            "Tallest window (± m)" if not U.feet else "Tallest window (± ft)",
            0.3 if not U.feet else 1.0,
            1.2 if not U.feet else 4.0,
            0.75 if not U.feet else 2.5,
            0.05 if not U.feet else 0.1,
            disabled=not complete,
            help="How far above and below breast height the window may grow.",
        )
        if U.feet:
            max_hw = float(to_metres(max_hw, U.unit))
    with s.expander("Merged trees"):
        split = st.toggle(
            "Split clusters with several stems",
            True,
            help="Segmentation sometimes merges a small tree into a neighbour's cluster. "
            "This finds separate stems in the breast-height band and fits each one.",
        )
        gap = st.slider(
            "Stem gap (cm)",
            2,
            100,
            15,
            1,
            disabled=not split,
            help="Points further apart than this (in XY, within the band) belong to different stems.",
        )
    try:
        return FitParams(
            breast_height=bh,
            band_half_width=hw,
            confidence=conf,
            ci_method=method,
            model_sd=model_sd_cm / 100,
            inlier_tolerance=tol / 100,
            kernel_scale=kernel / 100,
            max_radius=rmax / 200,
            max_tilt_deg=tilt,
            ransac_iterations=iters,
            n_bootstrap=nboot,
            min_arc_coverage=cov,
            ovality_sd=ovality / 100,
            complete_partial_stems=complete,
            reject_non_stems=trunk_check,
            min_surface_density=float(density),
            min_dbh=float(min_dbh_m),
            max_band_half_width=max(max_hw, hw),
            max_relative_ci_width=relw / 100,
            min_inlier_ratio=inl,
            split_stems=split,
            stem_gap=gap / 100,
        )
    except FIPError as exc:
        s.error(str(exc))
        return None


# --------------------------------------------------------------------------- views


def records_frame(records) -> pd.DataFrame:
    df = pd.DataFrame([r.to_row() for r in records])
    if df.empty:
        return df
    for c in ("dbh", "ci_low", "ci_high", "dbh_se"):
        df[c + "_d"] = U.D(df[c])
    return df


def to_display(df: pd.DataFrame) -> pd.DataFrame:
    """Scale coordinates/elevations from metres to the display unit (after truth matching)."""
    df = df.copy()
    for c in ("x", "y", "ground_z", "bh_elevation", "true_ground_z"):
        if c in df:
            df[c] = U.L(df[c])
    return df


def match_truth(df: pd.DataFrame, truth: list[dict] | None) -> pd.DataFrame:
    if not truth or df.empty:
        return df
    T = np.array([[t["x"], t["y"], t["dbh"], t["ground_z"]] for t in truth])
    d = np.hypot(
        T[:, 0][None] - df["x"].to_numpy()[:, None], T[:, 1][None] - df["y"].to_numpy()[:, None]
    )
    j = d.argmin(1)
    ok = d[np.arange(len(df)), j] < 0.5
    df = df.copy()
    df["true_dbh_d"] = np.where(ok, U.D(T[j, 2]), np.nan)
    df["true_ground_z"] = np.where(ok, T[j, 3], np.nan)
    df["covered"] = (df["true_dbh_d"] >= df["ci_low_d"]) & (df["true_dbh_d"] <= df["ci_high_d"])
    return df


def summary_row(df: pd.DataFrame, extent: np.ndarray, truth):
    fitted = df[~df.status.isin(NOT_MEASURED)]
    area_ha = max(extent[0] * extent[1], 1e-9) / 10_000
    ba = float(np.sum(np.pi * (fitted.dbh / 2) ** 2))
    cols = st.columns(5 if truth else 4)
    cols[0].metric(
        "Trees fitted",
        f"{len(fitted)} / {len(df)}",
        help=f"{(df.status == STATUS_OK).sum()} ok, {(df.status == STATUS_WARNING).sum()} with warnings, {(df.status == STATUS_FAILED).sum()} failed",
    )
    cols[1].metric("Mean DBH", f"{fitted.dbh_d.mean():.1f} {U.dl}" if len(fitted) else "—")
    cols[2].metric(
        "Basal area",
        f"{U.BA(ba / area_ha):.1f} {U.al}",
        help=f"{ba:.2f} m² over the {area_ha:.2f} ha bounding box",
    )
    cols[3].metric(
        "Median CI width",
        f"±{(fitted.ci_high_d - fitted.ci_low_d).median() / 2:.2f} {U.dl}" if len(fitted) else "—",
    )
    if truth:
        m = fitted.dropna(subset=["true_dbh_d"])
        if len(m):
            rmse = float(np.sqrt(np.mean((m.dbh_d - m.true_dbh_d) ** 2)))
            cols[4].metric(
                "vs truth",
                f"RMSE {rmse:.2f} {U.dl}",
                f"CI coverage {m.covered.mean():.0%}",
                delta_color="off",
                help=f"{len(m)} of {len(truth)} true trees matched",
            )


def _status_legend_traces(fig, df, x, y, sizes=None, hover=None):
    for status, style in STATUS_STYLE.items():
        sub = df[df.status == status]
        if sub.empty:
            continue
        fig.add_trace(
            go.Scatter(
                x=sub[x],
                y=sub[y],
                mode="markers",
                name=style["label"],
                marker=dict(
                    color=style["color"],
                    symbol=style["symbol"],
                    size=sizes(sub) if sizes else 10,
                    line=dict(width=2, color="rgba(252,252,251,0.9)"),
                    sizemode="diameter",
                ),
                customdata=sub[["tree_id"]].to_numpy(),
                hovertemplate=hover(sub) if hover else None,
                text=sub["hover"] if "hover" in sub else None,
            )
        )


def plot_map(df: pd.DataFrame, truth):
    fig = go.Figure()
    d = df.copy()
    d["hover"] = [
        f"<b>Tree {r.tree_id}</b> · {STATUS_STYLE[r.status]['label']}<br>"
        + (
            f"DBH {r.dbh_d:.1f} {U.dl} [{r.ci_low_d:.1f}–{r.ci_high_d:.1f}]<br>"
            if r.status not in NOT_MEASURED
            else f"{r.message}<br>"
        )
        + f"x {r.x:.2f} · y {r.y:.2f}<br>ground {r.ground_z:.2f} {U.ul} · DBH plane {r.bh_elevation:.2f} {U.ul}"
        + (f"<br>{r.message}" if r.status == STATUS_WARNING else "")
        for r in d.itertuples()
    ]
    _status_legend_traces(
        fig,
        d,
        "x",
        "y",
        sizes=lambda s: np.clip(np.nan_to_num(s.dbh * 40, nan=8), 8, 40),
        hover=lambda s: "%{text}<extra></extra>",
    )
    if truth:
        T = pd.DataFrame(truth)
        fig.add_trace(
            go.Scatter(
                x=U.L(T.x),
                y=U.L(T.y),
                mode="markers",
                name="true stem (demo)",
                marker=dict(
                    symbol="circle-open",
                    size=np.clip(T.dbh * 40, 8, 40),
                    color=SERIES_TRUTH,
                    line=dict(width=2),
                ),
                hovertemplate=f"true DBH %{{customdata:.1f}} {U.dl}<extra></extra>",
                customdata=U.D(T.dbh),
            )
        )
    fig.update_layout(
        height=560,
        margin=dict(l=10, r=10, t=30, b=10),
        legend=dict(orientation="h", y=1.06),
        xaxis_title=f"x ({U.ul})",
        yaxis_title=f"y ({U.ul})",
        hovermode="closest",
        clickmode="event+select",
    )
    fig.update_yaxes(scaleanchor="x", scaleratio=1)
    return fig


def plot_dbh_ci(df: pd.DataFrame, truth):
    d = df[~df.status.isin(NOT_MEASURED)].sort_values("dbh_d")
    d = d.assign(rank=np.arange(len(d)))
    fig = go.Figure()
    for status, style in STATUS_STYLE.items():
        sub = d[d.status == status]
        if sub.empty:
            continue
        fig.add_trace(
            go.Scatter(
                x=sub["rank"],
                y=sub.dbh_d,
                mode="markers",
                name=style["label"],
                marker=dict(color=style["color"], symbol=style["symbol"], size=9),
                error_y=dict(
                    type="data",
                    symmetric=False,
                    array=sub.ci_high_d - sub.dbh_d,
                    arrayminus=sub.dbh_d - sub.ci_low_d,
                    thickness=2,
                    width=4,
                    color=style["color"],
                ),
                customdata=np.c_[sub.tree_id, sub.ci_low_d, sub.ci_high_d],
                hovertemplate=f"Tree %{{customdata[0]}}<br>DBH %{{y:.2f}} {U.dl}<br>CI %{{customdata[1]:.2f}}–%{{customdata[2]:.2f}} {U.dl}<extra></extra>",
            )
        )
    if truth and "true_dbh_d" in d:
        fig.add_trace(
            go.Scatter(
                x=d["rank"],
                y=d.true_dbh_d,
                mode="markers",
                name="true DBH (demo)",
                marker=dict(symbol="line-ew-open", size=14, color=SERIES_TRUTH, line=dict(width=2)),
                hovertemplate=f"true %{{y:.2f}} {U.dl}<extra></extra>",
            )
        )
    fig.update_layout(
        height=360,
        margin=dict(l=10, r=10, t=30, b=10),
        legend=dict(orientation="h", y=1.1),
        xaxis_title="trees, sorted by DBH",
        yaxis_title=f"DBH ({U.dl})",
        hovermode="closest",
    )
    fig.update_xaxes(showticklabels=False)
    return fig


def plot_tree_slice(rec, fp: FitParams):
    pts = rec.points
    band = band_points(pts, fp)
    fig = go.Figure()
    if rec.fitted:
        theta = np.array(
            [rec.x, rec.y, rec.axis[0] / rec.axis[2], rec.axis[1] / rec.axis[2], rec.dbh / 2]
        )
        e = residuals(theta, band, fp.breast_height)
        inl = np.abs(e) < fp.inlier_tolerance
    else:
        inl = np.zeros(len(band), dtype=bool)
    for mask, name, color in (
        (inl, "inlier points", SERIES_ESTIMATE),
        (~inl, "outliers / clutter", "#9a9990"),
    ):
        if mask.any():
            fig.add_trace(
                go.Scattergl(
                    x=U.L(band[mask, 0]),
                    y=U.L(band[mask, 1]),
                    mode="markers",
                    name=name,
                    marker=dict(size=3, color=color, opacity=0.6),
                    hoverinfo="skip",
                )
            )
    if rec.fitted:
        t = np.linspace(0, 2 * np.pi, 200)
        for r, name, dash, width in (
            (rec.dbh / 2, f"DBH {U.D(rec.dbh):.2f} {U.dl}", "solid", 2),
            (rec.ci_low / 2, f"CI low {U.D(rec.ci_low):.2f}", "dash", 1.5),
            (rec.ci_high / 2, f"CI high {U.D(rec.ci_high):.2f}", "dash", 1.5),
        ):
            fig.add_trace(
                go.Scatter(
                    x=U.L(rec.x + r * np.cos(t)),
                    y=U.L(rec.y + r * np.sin(t)),
                    mode="lines",
                    name=name,
                    line=dict(color=STATUS_STYLE[rec.status]["color"], dash=dash, width=width),
                    hoverinfo="skip",
                )
            )
    fig.update_layout(
        height=420,
        margin=dict(l=10, r=10, t=30, b=10),
        legend=dict(orientation="h", y=1.12),
        xaxis_title=f"x ({U.ul})",
        yaxis_title=f"y ({U.ul})",
        title=dict(text=f"Breast-height band ({len(band)} points)", font=dict(size=13)),
    )
    fig.update_yaxes(scaleanchor="x", scaleratio=1)
    return fig


def plot_tree_3d(rec, fp: FitParams, max_points: int = 15000):
    pts = rec.points
    if len(pts) > max_points:
        pts = pts[np.random.default_rng(0).choice(len(pts), max_points, replace=False)]
    in_band = np.abs(pts[:, 2] - fp.breast_height) <= fp.band_half_width
    fig = go.Figure()
    fig.add_trace(
        go.Scatter3d(
            x=U.L(pts[~in_band, 0]),
            y=U.L(pts[~in_band, 1]),
            z=U.L(pts[~in_band, 2]),
            mode="markers",
            name="tree points",
            marker=dict(size=1.5, color="#9a9990"),
            hoverinfo="skip",
        )
    )
    fig.add_trace(
        go.Scatter3d(
            x=U.L(pts[in_band, 0]),
            y=U.L(pts[in_band, 1]),
            z=U.L(pts[in_band, 2]),
            mode="markers",
            name="DBH band",
            marker=dict(size=2, color=SERIES_ESTIMATE),
            hoverinfo="skip",
        )
    )
    if rec.fitted:
        t = np.linspace(0, 2 * np.pi, 60)
        ax = np.array(rec.axis)
        ref = np.array([1.0, 0, 0]) if abs(ax[0]) < 0.9 else np.array([0, 1.0, 0])
        u1 = np.cross(ax, ref)
        u1 /= np.linalg.norm(u1)
        u2 = np.cross(ax, u1)
        c0 = np.array([rec.x, rec.y, fp.breast_height])
        for h in (-fp.band_half_width, 0.0, fp.band_half_width):
            c = c0 + ax * (h / ax[2])
            ring = c[None] + rec.dbh / 2 * (np.cos(t)[:, None] * u1 + np.sin(t)[:, None] * u2)
            fig.add_trace(
                go.Scatter3d(
                    x=U.L(ring[:, 0]),
                    y=U.L(ring[:, 1]),
                    z=U.L(ring[:, 2]),
                    mode="lines",
                    showlegend=h == 0.0,
                    name="fitted cylinder",
                    line=dict(color=STATUS_STYLE[rec.status]["color"], width=5),
                    hoverinfo="skip",
                )
            )
    fig.update_layout(
        height=420,
        margin=dict(l=0, r=0, t=30, b=0),
        legend=dict(orientation="h", y=1.08),
        scene=dict(aspectmode="data", zaxis_title=f"height above ground ({U.ul})"),
    )
    return fig


def export_panel(records, fp: FitParams, seg: dict, source: str):
    st.subheader("Export")
    ctx = inv.context_for(
        source,
        fp,
        units=U.unit,
        crs=seg.get("crs"),
        crs_wkt=seg.get("crs_wkt"),
        ground=seg["gp"],
        segment=seg["sp"],
        excluded_by_reviewer=sorted(excluded_ids(seg)),
    )
    cols = st.columns(max(len(registry.exporters), 1))
    for col, (name, exp) in zip(cols, sorted(registry.exporters.items())):
        with col:
            st.markdown(f"**{exp.label or name}**")
            st.caption(exp.description)
            opts = {}
            for o in exp.options:
                key = f"exp_{name}_{o.name}"
                if o.choices:
                    opts[o.name] = st.selectbox(
                        o.name,
                        list(o.choices),
                        index=list(o.choices).index(o.default),
                        key=key,
                        help=o.help,
                    )
                elif isinstance(o.default, bool):
                    opts[o.name] = st.checkbox(o.name, o.default, key=key, help=o.help)
                elif isinstance(o.default, int):
                    opts[o.name] = int(
                        st.number_input(
                            o.name,
                            int(o.min if o.min is not None else -1e9),
                            int(o.max if o.max is not None else 1e9),
                            o.default,
                            key=key,
                            help=o.help,
                        )
                    )
                elif isinstance(o.default, float):
                    opts[o.name] = float(
                        st.number_input(
                            o.name,
                            float(o.min if o.min is not None else -1e9),
                            float(o.max if o.max is not None else 1e9),
                            o.default,
                            key=key,
                            help=o.help,
                        )
                    )
                else:
                    opts[o.name] = st.text_input(o.name, str(o.default), key=key, help=o.help)
            try:
                exp.resolve_options(opts)  # validate now so errors show immediately
            except Exception as exc:
                _fail(f"{name} export", exc)
                continue
            st.download_button(
                f"Download {exp.extension}",
                _deferred_export(exp, records, ctx, opts),  # built only when clicked
                file_name=f"trees.{opts['format']}"
                if "format" in opts
                else f"trees{exp.extension}",
                mime=exp.mime,
                key=f"dl_{name}",
                type="primary",
                width="stretch",
                on_click="ignore",
            )


def _deferred_export(exp, records, ctx, opts):
    """Callable for st.download_button: runs the exporter on click, in a worker thread."""

    def build() -> bytes:
        try:
            with tempfile.TemporaryDirectory() as td:
                out = exp.export(records, Path(td) / f"trees{exp.extension}", ctx, **opts)
                return out.read_bytes()
        except Exception:
            logger.exception(f"{exp.name} export failed")
            raise

    return build


def excluded_key(seg: dict) -> str:
    return f"excluded_{seg['run_id']}"


def excluded_ids(seg: dict) -> list[int]:
    """Tree ids the reviewer excluded in this session for this run."""
    return list(st.session_state.get(excluded_key(seg), []))


def _toggle_exclusion(seg: dict, tid: int) -> None:
    key = excluded_key(seg)
    cur = list(st.session_state.get(key, []))
    st.session_state[key] = [t for t in cur if t != tid] if tid in cur else cur + [tid]


def exclusion_panel(records, seg: dict) -> None:
    """Reviewer's list of trees to leave out (shrubs, structures the checks let through)."""
    choices = [r.tree_id for r in records if r.status != STATUS_FAILED]
    by_id = {r.tree_id: r for r in records}
    st.multiselect(
        "Exclude from the results (reviewer)",
        choices,
        key=excluded_key(seg),
        format_func=lambda t: f"T{t} · {U.D(by_id[t].dbh):.0f} {U.dl}" if t in by_id else f"T{t}",
        help="Shrubs, structures or anything else you don't want counted. Excluded trees stay in "
        "the CSV with status 'excluded', are left out of totals, maps, DXF and GeoJSON, and are "
        "listed in run.json. Tip: click a tree on the map, then use 'Exclude this tree' in Tree detail.",
    )


def save_panel(records, fp: FitParams, seg: dict) -> None:
    """Write every export plus run.json to a folder on disk (the installed-app workflow)."""
    with st.expander("💾 Save results to a folder", expanded=False):
        src = seg.get("input_path") or ""
        default_name = (
            Path(src).stem if src and Path(src).is_file() else Path(src).name if src else "demo"
        ) + "_inventory"
        if src and "output_dir" not in st.session_state:
            # start the output chooser next to the input file
            parent = Path(src).parent if Path(src).is_file() else Path(src)
            st.session_state["output_dir"] = str(parent)
            st.session_state["output_typed"] = str(parent)
            st.session_state["output_sel"] = str(parent)
        c1, c2 = st.columns([3, 2])
        with c1:
            folder = browse("output", mode="dir", pref_key="last_output_dir")
        with c2:
            name = st.text_input(
                "New subfolder for this run", default_name, help="Created inside the chosen folder."
            )
            formats = st.multiselect(
                "Formats",
                sorted(registry.exporters),
                default=[f for f in ("csv", "dxf", "geojson", "points") if f in registry.exporters],
                format_func=lambda n: registry.exporters[n].label or n,
            )
            go = st.button(
                "Save results", type="primary", disabled=not (folder and formats), width="stretch"
            )
        if go:
            out = Path(folder) / name.strip() if name.strip() else Path(folder)
            ctx = inv.context_for(
                seg.get("source", ""),
                fp,
                units=U.unit,
                crs=seg.get("crs"),
                crs_wkt=seg.get("crs_wkt"),
                ground=seg["gp"],
                segment=seg["sp"],
                excluded_by_reviewer=sorted(excluded_ids(seg)),
            )
            try:
                with st.spinner(f"writing to {out} …"):
                    written = inv.export_records(records, out, formats, ctx)
                save_pref("last_output_dir", str(folder))
                st.success(f"Saved {len(written)} files to `{out}`")
                st.code("\n".join(p.name for p in written), language=None)
                logger.info(f"app: saved {len(written)} files to {out}")
            except Exception as exc:
                _fail("Saving results", exc)


def settings_panel(
    gp: inv.GroundParams, sp: inv.SegmentParams, fp: FitParams, seg: dict | None
) -> None:
    """Show every setting of the current run and save them as a config file for `fip -c`."""
    import yaml
    from dataclasses import asdict

    st.subheader("Settings")
    st.caption(
        "Everything that controls the extraction. Ground and segmentation come from **2 · Find trees** "
        "(applied when you press *Run tree extraction*); the fit settings from **3 · Fit stems** apply "
        "immediately. Lengths are in metres. The file works with the command line: "
        "`fip inventory INPUT -c inventory.yaml`."
    )
    used_g, used_s = (seg["gp"], seg["sp"]) if seg else (gp, sp)
    if seg and (asdict(used_g) != asdict(gp) or asdict(used_s) != asdict(sp)):
        st.warning(
            "Ground or segmentation settings changed since the last run; press **Run tree extraction** to apply them."
        )
    cfg = {"ground": asdict(gp), "segment": asdict(sp), "fit": fp.to_dict()}
    text = "# LiDAR Tree Inventory settings (metres)\n" + yaml.safe_dump(cfg, sort_keys=False)
    st.code(text, language="yaml")
    c1, c2 = st.columns(2)
    c1.download_button(
        "Download inventory.yaml",
        text,
        file_name="inventory.yaml",
        mime="text/yaml",
        width="stretch",
    )
    folder = st.session_state.get("output_sel") or load_prefs().get("last_output_dir")
    if folder and c2.button(f"Save to {folder}", width="stretch"):
        try:
            target = Path(folder) / "inventory.yaml"
            target.write_text(text, encoding="utf-8")
            st.success(f"Saved `{target}`")
        except OSError as exc:
            _fail("Saving settings", exc)


def calibration_panel(records, fp: FitParams):
    st.subheader("Calibrate the systematic uncertainty")
    st.markdown(
        "Upload field measurements (CSV with columns **x, y, dbh** in the same coordinate frame). "
        "Residuals not explained by each tree's statistical error become the *systematic uncertainty* — "
        "the CI then reaches its nominal coverage on data like yours."
    )
    c1, c2 = st.columns(2)
    f = c1.file_uploader("Reference CSV", type=["csv"], key="calib_csv")
    unit = c2.selectbox("Reference DBH unit", ["auto", "cm", "m"])
    maxd = c2.slider("Max position mismatch (m)", 0.1, 3.0, 0.5, 0.1)
    if f is None:
        return
    try:
        ref_df = pd.read_csv(io.BytesIO(f.getvalue()))
        ref_df.columns = [c.strip().lower() for c in ref_df.columns]
        missing = [c for c in ("x", "y", "dbh") if c not in ref_df]
        if missing:
            st.error(f"Reference CSV is missing column(s) {missing}; found {list(ref_df.columns)}")
            return
        ref = _to_metres(ref_df[["x", "y", "dbh"]].to_numpy(float), unit)
        res = calibrate(records, ref, fp.model_sd, maxd)
    except Exception as exc:
        _fail("calibration", exc)
        return
    m = st.columns(4)
    m[0].metric("Matched", f"{res.n_matched} / {res.n_reference}")
    m[1].metric("Bias", f"{res.bias * 100:+.2f} cm")
    m[2].metric("RMSE", f"{res.rmse * 100:.2f} cm")
    m[3].metric("Current CI coverage", f"{res.coverage:.0%}", help=f"target {fp.confidence:.0%}")
    st.info(
        f"Suggested systematic uncertainty: **{res.model_sd * 100:.2f} cm** (currently {fp.model_sd * 100:.2f} cm)."
    )
    if st.button("Apply suggested value", type="primary"):
        st.session_state["model_sd_cm"] = round(min(res.model_sd * 100, 5.0), 2)
        st.rerun()


def log_panel():
    st.caption(f"Log file: `{LOG_FILE}`")
    try:
        lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()[-300:]
    except FileNotFoundError:
        lines = ["(no log yet)"]
    level = st.segmented_control("Show", ["all", "warnings+", "errors"], default="all") or "all"
    if level == "warnings+":
        lines = [ln for ln in lines if "WARNING" in ln or "ERROR" in ln]
    elif level == "errors":
        lines = [ln for ln in lines if "ERROR" in ln]
    st.code("\n".join(lines[-200:]) or "(nothing at this level)", language="log")
    if registry.load_errors:
        st.warning(
            "Plugins that failed to load:\n\n"
            + "\n".join(f"- `{k}`: {v}" for k, v in registry.load_errors.items())
        )


# --------------------------------------------------------------------------- main


def main():
    icon = Path(__file__).with_name("icon.png")
    st.set_page_config(
        page_title="LiDAR Tree Inventory",
        page_icon=str(icon) if icon.exists() else "🌲",
        layout="wide",
    )
    _init()
    st.title("🌲 LiDAR tree inventory")
    st.caption(
        "Find trees in a point cloud, fit each stem at breast height, and report DBH with a confidence interval."
    )

    global U
    kind, key, path, upload, demo, reader_opts, cloud_units = sidebar_source()
    U = sidebar_units(cloud_units)
    run_btn, gp, sp = sidebar_segmentation()
    fp = sidebar_fit()

    if fp is not None:
        for issue in inv.check_band_inside_slice(fp, sp):
            st.sidebar.warning(issue)

    ready = (
        kind == "demo"
        or (kind == "path" and path and Path(path).expanduser().exists())
        or (kind == "upload" and upload)
    )
    seg = st.session_state.get("seg")
    stale = seg is not None and seg.get("source_key") != key
    if run_btn:
        if not ready:
            st.sidebar.error("Choose a point cloud first.")
        else:
            progress = st.progress(0.0, "loading point cloud…")
            try:
                cloud, truth = load_source(
                    kind,
                    key,
                    path,
                    tuple(sorted(reader_opts.items())),
                    demo,
                    upload,
                    cloud_units,
                    st.session_state.get("crop", "") if kind != "demo" else "",
                )
                seg = run_segmentation(cloud, gp, sp, progress)
                seg.update(
                    source_key=key,
                    truth=truth,
                    source="; ".join(cloud.sources),
                    n_points=len(cloud.xyz),
                    input_path=path if kind == "path" else "",
                )
                st.session_state["seg"] = seg
                stale = False
                logger.info(
                    f"segmentation run {seg['run_id']}: {len(seg['trees'])} trees in {seg['seconds']:.1f}s"
                )
            except Exception as exc:
                progress.empty()
                _fail("Segmentation", exc)
                return
            progress.empty()
            if cloud_units == "auto" and seg.get("units", "m") != U.unit:
                st.rerun()  # units came from the file header: redraw the sidebar in those units

    if seg is None:
        st.info(
            "Pick a point cloud in the sidebar and press **Run tree extraction**. "
            "The demo plot is a quick way to see the whole workflow, with known true DBH values."
        )
        return
    if stale:
        st.warning(
            "The source changed since the last run; results below are for the previous source. Press **Run tree extraction** to update."
        )
    if fp is None:
        return

    with st.spinner("fitting stems…"):
        try:
            records = fit_all(seg["run_id"], asdict(fp), seg)
        except Exception as exc:
            _fail("Fitting", exc)
            return
    records = inv.apply_exclusions(records, excluded_ids(seg))
    df = to_display(match_truth(records_frame(records), seg.get("truth")))
    if df.empty:
        st.warning("No trees to show.")
        return
    summary_row(df, seg["extent"], seg.get("truth"))

    save_panel(records, fp, seg)
    tab_map, tab_tree, tab_table, tab_cal, tab_set, tab_log = st.tabs(
        ["Map", "Tree detail", "Table & export", "Calibration", "Settings", "Log"]
    )
    with tab_map:
        event = st.plotly_chart(
            plot_map(df, seg.get("truth")),
            width="stretch",
            on_select="rerun",
            key="map",
            theme="streamlit",
        )
        try:
            picked = event.selection.points
            if picked and picked[0].get("customdata") is not None:
                st.session_state["tree_pick"] = int(picked[0]["customdata"][0])
                st.caption(
                    f"Selected tree {st.session_state['tree_pick']} — see the Tree detail tab."
                )
        except (AttributeError, KeyError, IndexError, TypeError):
            pass
        st.plotly_chart(plot_dbh_ci(df, seg.get("truth")), width="stretch", theme="streamlit")
        st.caption(
            f"Bars are {fp.confidence:.0%} confidence intervals ({fp.ci_method}). "
            f"Segmentation: {seg['n_points']:,} points, {len(seg['trees'])} candidates, {seg['seconds']:.1f} s. "
            f"Input units: {LABELS[seg.get('units', 'm')]}; CRS: {seg.get('crs') or 'unknown'}; "
            f"ground: {'file classification' if seg.get('classified') and seg['gp'].source != 'csf' else 'CSF'}."
        )

    with tab_tree:
        ids = df.tree_id.tolist()
        default = st.session_state.get("tree_pick", ids[0])
        tid = st.selectbox(
            "Tree",
            ids,
            index=ids.index(default) if default in ids else 0,
            format_func=lambda i: (
                f"{i} · {STATUS_STYLE[df.set_index('tree_id').status[i]]['label']}"
            ),
        )
        rec = next(r for r in records if r.tree_id == tid)
        if rec.status != STATUS_FAILED:
            excl = tid in excluded_ids(seg)
            st.button(
                "Include this tree again" if excl else "Exclude this tree",
                key=f"toggle_excl_{tid}",
                on_click=_toggle_exclusion,
                args=(seg, tid),
                help="Leave this tree out of the results (e.g. a shrub or a structure).",
            )
        if rec.fitted:
            st.markdown(
                f"**DBH {U.D(rec.dbh):.2f} {U.dl}**, {rec.confidence:.0%} CI "
                f"[{U.D(rec.ci_low):.2f}, {U.D(rec.ci_high):.2f}] {U.dl} · tilt {rec.tilt_deg:.1f}° · "
                f"arc coverage {rec.arc_coverage:.0%} · inliers {rec.inlier_ratio:.0%} · RMSE {rec.rmse * 1000:.1f} mm"
            )
            st.markdown(
                f"Ground {U.L(rec.ground_z):.3f} {U.ul} · DBH plane elevation "
                f"**{U.L(rec.bh_elevation):.3f} {U.ul}** · lengths in {LABELS[U.unit]}"
            )
        if rec.message:
            (st.error if rec.status == STATUS_FAILED else st.warning)(rec.message)
        c1, c2 = st.columns(2)
        c1.plotly_chart(plot_tree_slice(rec, fp), width="stretch", theme="streamlit")
        c2.plotly_chart(plot_tree_3d(rec, fp), width="stretch", theme="streamlit")

    with tab_table:
        exclusion_panel(records, seg)
        status_filter = st.multiselect(
            "Status",
            list(STATUS_STYLE),
            default=list(STATUS_STYLE),
            format_func=lambda s: STATUS_STYLE[s]["label"],
        )
        shown = df[df.status.isin(status_filter)]
        cols = [
            "tree_id",
            "status",
            "dbh_d",
            "ci_low_d",
            "ci_high_d",
            "dbh_se_d",
            "x",
            "y",
            "ground_z",
            "bh_elevation",
            "arc_coverage",
            "inlier_ratio",
            "tilt_deg",
            "n_band_points",
            "dbh_method",
            "message",
        ]
        cols += [c for c in df.columns if c.startswith("basal_area")] + [
            c for c in ("true_dbh_d", "covered") if c in df
        ]
        st.dataframe(
            shown[cols],
            hide_index=True,
            width="stretch",
            column_config={
                "dbh_d": st.column_config.NumberColumn(f"DBH ({U.dl})", format="%.2f"),
                "ci_low_d": st.column_config.NumberColumn("CI low", format="%.2f"),
                "ci_high_d": st.column_config.NumberColumn("CI high", format="%.2f"),
                "dbh_se_d": st.column_config.NumberColumn(f"SE ({U.dl})", format="%.3f"),
                "arc_coverage": st.column_config.ProgressColumn(
                    "Arc coverage", min_value=0, max_value=1, format="percent"
                ),
                "inlier_ratio": st.column_config.ProgressColumn(
                    "Inliers", min_value=0, max_value=1, format="percent"
                ),
                "bh_elevation": st.column_config.NumberColumn(
                    f"DBH elevation ({U.ul})", format="%.3f"
                ),
                "ground_z": st.column_config.NumberColumn(f"Ground ({U.ul})", format="%.3f"),
                "x": st.column_config.NumberColumn(f"x ({U.ul})", format="%.3f"),
                "y": st.column_config.NumberColumn(f"y ({U.ul})", format="%.3f"),
            },
        )
        export_panel(records, fp, seg, seg.get("source", ""))

    with tab_cal:
        calibration_panel(records, fp)
    with tab_set:
        settings_panel(gp, sp, fp, seg)
    with tab_log:
        log_panel()


main()
