"""Site and scanner profiles in the app: the sidebar dropdowns, what they change, and saving your own."""

from __future__ import annotations

from dataclasses import asdict
from typing import Callable

import streamlit as st

from forest_inventory_pipeline import inventory as inv
from forest_inventory_pipeline import profiles as site_profiles
from forest_inventory_pipeline.app.styles import DisplayUnits
from forest_inventory_pipeline.errors import FIPError
from forest_inventory_pipeline.traits.dbh.robust_fit import FitParams

#: sidebar widgets a site profile sets: key -> (section, parameter, metres -> widget value)
PROFILE_WIDGETS: dict[str, tuple[str, str, Callable]] = {
    "g_cloth": ("ground", "cloth_resolution", float),
    "g_rigid": ("ground", "rigidness", int),
    "g_thresh": ("ground", "class_threshold", float),
    "g_refine": ("ground", "refine_tolerance", lambda v: 0.15 if v is None else float(v)),
    "g_source": ("ground", "source", str),
    "f_cov": ("fit", "min_arc_coverage", float),
    "f_relw": ("fit", "max_relative_ci_width", lambda v: int(round(v * 100))),
    "f_inl": ("fit", "min_inlier_ratio", float),
    "s_k": ("segment", "k", int),
    "s_beta": ("segment", "beta", float),
    "s_voxel": ("segment", "voxel_size", float),
    "s_minpts": ("segment", "min_tree_points", int),
    "f_tol": ("fit", "inlier_tolerance", lambda v: round(v * 100, 2)),
    "f_kernel": ("fit", "kernel_scale", lambda v: round(v * 100, 2)),
    "f_rmax": ("fit", "max_radius", lambda v: int(round(v * 200))),
    "f_tilt": ("fit", "max_tilt_deg", float),
    "f_ovality": ("fit", "ovality_sd", lambda v: round(v * 100, 2)),
    "f_trunk": ("fit", "reject_non_stems", bool),
    "f_roof": ("fit", "reject_roof_posts", bool),
    "f_minh": ("fit", "min_top_above_breast_height", lambda v: round(float(v), 2)),
    "f_dom": ("fit", "min_stem_dominance", lambda v: round(float(v), 2)),
    "f_interior": ("fit", "max_interior_fraction", lambda v: round(float(v), 2)),
    "f_checktop": ("fit", "stem_check_top", lambda v: round(float(v), 2)),
    "f_pole": ("fit", "pole_action", str),
    "f_short": ("fit", "short_action", str),
    "f_density": ("fit", "min_surface_density", lambda v: int(round(v))),
    "f_complete": ("fit", "complete_partial_stems", bool),
    "f_split": ("fit", "split_stems", bool),
    "f_gap": ("fit", "stem_gap", lambda v: int(round(v * 100))),
}


#: readable names of settings a profile can change (for "What this profile changes")
SETTING_LABELS = {
    "cloth_resolution": "ground cloth (m)", "rigidness": "ground rigidness",
    "k": "neighbours k", "beta": "density β", "voxel_size": "voxel size (m)",
    "min_tree_points": "min points per tree", "max_z": "height slice top (m)",
    "inlier_tolerance": "inlier tolerance (m)", "kernel_scale": "robust kernel (m)",
    "max_radius": "max DBH / 2 (m)", "max_tilt_deg": "max tilt (°)", "ovality_sd": "non-roundness",
    "reject_non_stems": "trunk check", "reject_roof_posts": "roof check",
    "min_surface_density": "points per m² of trunk", "complete_partial_stems": "complete partly seen stems",
    "split_stems": "split merged stems", "stem_gap": "stem gap (m)", "min_dbh": "minimum DBH (m)",
    "min_top_above_breast_height": "must reach above breast height (m)",
    "min_stem_dominance": "share of points on the trunk", "max_interior_fraction": "max filled inside",
    "stem_check_top": "trunk checked up to (m)", "pole_action": "bare column (pole?)",
    "short_action": "short object, large DBH (shrub?)",
    "class_threshold": "ground class threshold (m)", "refine_tolerance": "ground refinement tolerance (m)",
    "source": "ground source", "min_arc_coverage": "min arc coverage",
    "max_relative_ci_width": "max CI width (% of DBH)", "min_inlier_ratio": "min inlier ratio",
    "min_z": "height slice (m)",
}
#: the sidebar's units for the few widgets that do not show metres
WIDGET_UNIT_NOTE = {"f_tol": "cm", "f_kernel": "cm", "f_rmax": "DBH cm", "f_ovality": "%", "f_gap": "cm"}


def widget_label(key: str, units: DisplayUnits) -> str:
    if key == "s_zr":
        return "height slice (m)"
    if key == "f_min_dbh":
        return f"minimum DBH ({units.dl})"
    _, param, _ = PROFILE_WIDGETS[key]
    label = SETTING_LABELS.get(param, param)
    return label.replace("(m)", f"({WIDGET_UNIT_NOTE[key]})") if key in WIDGET_UNIT_NOTE else label


def profile_sections(name: str, scanner: str) -> dict[str, dict]:
    return site_profiles.merged(name, scanner=scanner)


def profile_widget_values(
    name: str, units: DisplayUnits, scanner: str = site_profiles.DEFAULT_SCANNER
) -> tuple[dict, dict]:
    """
    The sidebar values of site profile ``name`` with ``scanner``: ({widget key: value}, fit
    settings that have no widget). Settings the profiles leave out get the defaults.
    """
    over = profile_sections(name, scanner)
    full = {
        "ground": {**asdict(inv.GroundParams()), **over["ground"]},
        "segment": {**asdict(inv.SegmentParams()), **over["segment"]},
        "fit": {**FitParams().to_dict(), **over["fit"]},
    }
    values = {k: conv(full[sec][param]) for k, (sec, param, conv) in PROFILE_WIDGETS.items()}
    seg = full["segment"]
    values["s_zr"] = (float(seg["min_z"]), float(seg["max_z"] or 15.0))
    values["f_min_dbh"] = round(full["fit"]["min_dbh"] / (0.0254 if units.feet else 0.01), 2)
    managed = {p for (_, p, _) in PROFILE_WIDGETS.values()} | {"min_dbh"}
    extra = {k: v for k, v in over["fit"].items() if k not in managed}
    return values, extra


def _apply_profile() -> None:
    """Callback: move the sidebar to the chosen site and scanner profiles' settings."""
    name = st.session_state.get("profile", site_profiles.DEFAULT)
    scanner = st.session_state.get("scanner", site_profiles.DEFAULT_SCANNER)
    values, extra = profile_widget_values(name, st.session_state["_profile_units"], scanner)
    st.session_state.update(values)
    st.session_state["profile_extra"] = extra


def _fmt_setting(v) -> str:
    if isinstance(v, bool):
        return "on" if v else "off"
    if isinstance(v, (tuple, list)):
        return "–".join(_fmt_setting(x) for x in v)
    return f"{v:g}" if isinstance(v, (int, float)) else str(v)


def _evidence_line(p) -> str:
    mark = "✓" if p.evidence.startswith("Checked") else "◐" if p.evidence.startswith("Partly") \
        else "★" if p.evidence.startswith("Saved") else "⚠"
    return f"{mark} {p.evidence}"


def sidebar_profile(units: DisplayUnits) -> None:
    """Site and scanner profile dropdowns: set ground, segmentation and fit sliders."""
    st.session_state["_profile_units"] = units  # for the callbacks
    if "profile" not in st.session_state:  # first run: the sidebar starts at the defaults
        st.session_state["profile"] = site_profiles.DEFAULT
        st.session_state["scanner"] = site_profiles.DEFAULT_SCANNER
        values, extra = profile_widget_values(site_profiles.DEFAULT, units)
        for k, v in values.items():
            st.session_state.setdefault(k, v)
        st.session_state.setdefault("profile_extra", extra)
    names = list(site_profiles.all_profiles())
    if st.session_state["profile"] not in names:  # a saved profile was deleted
        st.session_state["profile"] = site_profiles.DEFAULT
    name = st.sidebar.selectbox(
        "Site profile",
        names,
        key="profile",
        on_change=_apply_profile,
        help="Starting settings for the kind of site (tree criteria). Choosing one moves the "
        "sliders below; adjust them afterwards if needed. Ground and segmentation changes "
        "apply when you press **Run tree extraction**; fit changes apply at once. Profiles "
        "you saved (Settings tab) are listed after the built-in ones.",
    )
    scanner = st.sidebar.selectbox(
        "Scanner",
        list(site_profiles.SCANNERS),
        key="scanner",
        on_change=_apply_profile,
        help="What recorded the cloud. Sets how densely a trunk surface must be sampled; "
        "the site profile scales it.",
    )
    site, sc = site_profiles.get(name), site_profiles.get_scanner(scanner)
    st.sidebar.caption(site.summary)
    st.sidebar.caption(_evidence_line(site) + ("" if scanner == site_profiles.DEFAULT_SCANNER
                                               else f"  \nScanner: {_evidence_line(sc)}"))
    over = profile_sections(name, scanner)
    rows = [(SETTING_LABELS.get(k, k), v) for sec in ("ground", "segment", "fit")
            for k, v in over[sec].items()]
    if rows:
        with st.sidebar.expander(f"What this profile changes ({len(rows)})"):
            st.markdown("\n".join(f"- {lab}: **{v}**" for lab, v in rows))
    values, _ = profile_widget_values(name, units, scanner)
    changed = [k for k, v in values.items() if st.session_state.get(k) != v]
    if changed:
        c1, c2 = st.sidebar.columns([3, 2])
        c1.caption(f"✎ {len(changed)} setting(s) changed from this profile.")
        c2.button("↺ Reset", key="profile_reset", on_click=_apply_profile,
                  help="Put every slider back to the chosen profiles' values.")
        with st.sidebar.expander(f"Changed from the profile ({len(changed)})"):
            st.markdown(
                "| Setting | Profile | Now |\n|---|---|---|\n" + "\n".join(
                    f"| {widget_label(k, units)} | {_fmt_setting(values[k])} | "
                    f"{_fmt_setting(st.session_state.get(k))} |" for k in changed
                )
            )


#: organisation standards, never part of a site profile
NOT_IN_PROFILES = {"breast_height", "confidence", "ci_method", "model_sd", "dbh_offset", "n_bootstrap",
                   "bootstrap_block", "seed", "large_tree_dbh"}


def profile_overrides(gp, sp, fp) -> dict[str, dict]:
    """The current settings that differ from the defaults, as profile sections."""
    def diff(cur: dict, default: dict) -> dict:
        return {k: v for k, v in cur.items() if k not in NOT_IN_PROFILES and v != default.get(k)}

    return {
        "ground": diff(asdict(gp), asdict(inv.GroundParams())),
        "segment": diff(asdict(sp), asdict(inv.SegmentParams())),
        "fit": diff(fp.to_dict(), FitParams().to_dict()),
    }


def _save_profile(gp, sp, fp) -> None:
    name = (st.session_state.get("profile_save_name") or "").strip()
    try:
        site_profiles.save_user_profile(
            name, profile_overrides(gp, sp, fp), st.session_state.get("profile_save_summary", "")
        )
        st.session_state["profile_save_msg"] = ("success", f"Saved profile **{name}**; it is now in "
                                                "the *Site profile* list.")
    except FIPError as exc:
        st.session_state["profile_save_msg"] = ("error", str(exc))


def profile_save_panel(gp, sp, fp) -> None:
    st.markdown("**Save these settings as a profile**")
    over = profile_overrides(gp, sp, fp)
    n = sum(len(v) for v in over.values())
    st.caption(
        f"Stores the {n} setting(s) that differ from the defaults (not breast height, "
        "confidence or the calibrated uncertainty) under a name of your own. It appears in the "
        "*Site profile* list and works on the command line: `fip inventory INPUT --profile NAME`. "
        f"Saved in `{site_profiles.user_profiles_dir()}`."
    )
    c1, c2 = st.columns([1, 2])
    c1.text_input("Name", key="profile_save_name", placeholder="e.g. Old pines")
    c2.text_input("Description (optional)", key="profile_save_summary")
    st.button("Save as profile", on_click=_save_profile, args=(gp, sp, fp),
              disabled=not (st.session_state.get("profile_save_name") or "").strip())
    msg = st.session_state.pop("profile_save_msg", None)
    if msg:
        getattr(st, msg[0])(msg[1])
    mine = [p for p in site_profiles.all_profiles() if p not in site_profiles.PROFILES]
    if mine:
        c1, c2 = st.columns([2, 1])
        gone = c1.selectbox("Your saved profiles", mine, key="profile_delete_pick")
        if c2.button("Delete", key="profile_delete"):
            site_profiles.delete_user_profile(gone)
            st.rerun()
