"""Latitude/longitude in the app: georeferencing a run, the satellite map, and whole drives."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from forest_inventory_pipeline.app.file_browser import browse
from forest_inventory_pipeline.app.styles import CATEGORY_STYLE, DisplayUnits, map_category
from forest_inventory_pipeline.errors import FIPError

ESRI_IMAGERY = (
    "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
)
SATELLITE_STYLE = {
    "version": 8,
    "sources": {
        "imagery": {
            "type": "raster",
            "tiles": [ESRI_IMAGERY],
            "tileSize": 256,
            "attribution": "Imagery © Esri, Maxar, Earthstar Geographics",
        }
    },
    "layers": [{"id": "imagery", "type": "raster", "source": "imagery"}],
}


@st.cache_resource(show_spinner="Fitting the trajectory to the GNSS fixes…", max_entries=8)
def _fit_georef(trajectory: str, gnss: str):
    from forest_inventory_pipeline import georef

    fixes = georef.read_gnss(Path(gnss))
    t, xy = georef.read_trajectory(Path(trajectory))
    return georef.fit_alignment(t, xy, fixes)


def georef_rows(records, alignment) -> list[dict]:
    """CSV-style rows (metres) of the located trees with lat, lon and position_sd_m."""
    located = [r for r in records if r.located]
    if not located:
        return []
    xy = np.array([[r.x, r.y] for r in located])
    lat, lon = alignment.to_latlon(xy)
    sd = alignment.position_sd(xy)
    rows = []
    for r, la, lo, s in zip(located, lat, lon, sd):
        row = r.to_row("m")
        row.update(lat=f"{la:.8f}", lon=f"{lo:.8f}", position_sd_m=f"{s:.2f}")
        rows.append(row)
    return rows


def plot_satellite(rows: list[dict], satellite: bool, units: DisplayUnits):
    d = pd.DataFrame(rows)
    d["dbh"] = pd.to_numeric(d["dbh"], errors="coerce")
    fig = go.Figure()
    cat = map_category(d)
    for key, style in CATEGORY_STYLE.items():
        sub = d[cat == key]
        if sub.empty:
            continue
        fig.add_trace(
            go.Scattermap(
                lat=sub.lat.astype(float),
                lon=sub.lon.astype(float),
                mode="markers",
                name=style["label"],
                marker=dict(
                    size=np.clip(np.nan_to_num(sub.dbh * 40, nan=10), 9, 30), color=style["color"]
                ),
                text=[
                    f"<b>Tree {r.tree_id}</b> · {style['label']}<br>"
                    + (f"DBH {units.D(r.dbh):.1f} {units.dl}<br>" if np.isfinite(r.dbh) else "")
                    + f"{float(r.lat):.6f}, {float(r.lon):.6f} (± {r.position_sd_m} m)"
                    for r in sub.itertuples()
                ],
                hovertemplate="%{text}<extra></extra>",
            )
        )
    fig.update_layout(
        height=560,
        margin=dict(l=0, r=0, t=30, b=0),
        legend=dict(orientation="h", y=1.06),
        map=dict(
            style=SATELLITE_STYLE if satellite else "open-street-map",
            center=dict(lat=float(d.lat.astype(float).mean()), lon=float(d.lon.astype(float).mean())),
            zoom=17,
        ),
    )
    return fig


def georef_panel(records, seg: dict, units: DisplayUnits) -> None:
    """Latitude/longitude for the trees, a satellite map, and GPX/GeoJSON for the field."""
    from forest_inventory_pipeline import georef

    key = f"georef_{seg['run_id']}"
    with st.expander("🌍 Georeference: satellite map and GPX", expanded=key in st.session_state):
        st.caption(
            "For clouds in local SLAM coordinates: fit the scanner trajectory to the GNSS fixes "
            "of the same recording (as `fip georeference`), or load the `georef.json` it wrote."
        )
        how = st.radio(
            "Source",
            ["Trajectory + GNSS", "Saved georef.json"],
            horizontal=True,
            key="georef_how",
            label_visibility="collapsed",
        )
        try:
            if how == "Saved georef.json":
                path = browse("georef_file", mode="file", extensions={".json"}, pref_key="last_georef_dir")
                if path and st.button("Use this georeference"):
                    st.session_state[key] = georef.Alignment.load(Path(path))
            else:
                st.caption("Trajectory CSV (time_s, x, y), e.g. from scripts/register_rosbag_kiss_icp.py")
                traj = browse("georef_traj", mode="file", extensions={".csv"}, pref_key="last_georef_dir")
                st.caption("GNSS: a CSV (time_s, lat, lon) or the ROS bag with NavSatFix messages")
                gnss = browse("georef_gnss", mode="file", extensions={".csv", ".bag", ".db3", ".mcap"},
                              pref_key="last_georef_dir")
                if traj and gnss and st.button("Georeference"):
                    st.session_state[key] = _fit_georef(traj, gnss)
        except FIPError as exc:
            st.error(str(exc))
        alignment = st.session_state.get(key)
        if alignment is None:
            return
        rows = georef_rows(records, alignment)
        if not rows:
            st.info("No located trees to show.")
            return
        st.success(
            f"Georeferenced: residual {alignment.rms_m:.2f} m over {alignment.n_used} fixes; "
            f"tree positions ± {min(float(r['position_sd_m']) for r in rows):.1f}-"
            f"{max(float(r['position_sd_m']) for r in rows):.1f} m (1 sd)."
        )
        sat = st.toggle(
            "Satellite imagery",
            True,
            key="georef_sat",
            help="Imagery tiles are loaded by your browser from Esri's public service (which "
            "sees the map area you view). Off: OpenStreetMap.",
        )
        st.plotly_chart(plot_satellite(rows, sat, units), width="stretch", theme="streamlit")
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            cols = list(rows[0])
            written = georef.write_outputs(rows, cols, Path(tmp))
            files = {p.name: p.read_bytes() for p in written}
        c1, c2, c3, c4 = st.columns(4)
        c1.download_button("Download trees.gpx (field)", files["trees.gpx"], "trees.gpx",
                           mime="application/gpx+xml", width="stretch")
        c2.download_button("Download GeoJSON (WGS84)", files["trees_wgs84.geojson"],
                           "trees_wgs84.geojson", mime="application/geo+json", width="stretch")
        c3.download_button("Download CSV (with lat/lon)", files["trees_wgs84.csv"],
                           "trees_wgs84.csv", mime="text/csv", width="stretch")
        c4.download_button("Download field sheet", files["field_sheet.csv"], "field_sheet.csv",
                           mime="text/csv", width="stretch",
                           help="One row per tree, named as in the GPX, with blank columns for "
                           "tape DBH, 'is it a tree?' and species. Fill it in on site, then run "
                           "`fip calibrate trees.csv field_sheet.csv`.")


def drive_panel(units: DisplayUnits, settings: dict | None) -> None:
    """
    A whole drive in one go: every segment measured on its own, georeferenced and merged
    into one map (the app form of ``fip drive``).

    :param settings: the sidebar's current settings ({"ground", "segment", "fit"}), used for
        every segment; None when they are invalid.
    """
    import csv

    from forest_inventory_pipeline import drive

    with st.expander("🚗 Whole drive: several segments into one map",
                     expanded="drive_result" in st.session_state):
        st.caption(
            "A long drive is stitched in segments, each with its own local coordinates, so the "
            "segments cannot be opened together as one cloud. Choose the folder that holds the "
            "segment files (each with its `<name>_trajectory.csv`) and the drive's GNSS. Every "
            "segment is measured with the sidebar's current settings, given latitude and "
            "longitude, and merged into one map."
        )
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**Segments folder**")
            folder = browse("drive_dir", mode="dir", pref_key="last_drive_dir")
        with c2:
            st.markdown("**GNSS of the drive** (CSV or the ROS bag)")
            gnss = browse("drive_gnss", mode="file", extensions={".csv", ".bag", ".db3", ".mcap"},
                          pref_key="last_georef_dir")
        segments, problem = [], ""
        if folder:
            try:
                segments = drive.find_segments(Path(folder))
            except FIPError as exc:
                problem = str(exc)
        if segments:
            st.caption(f"{len(segments)} segments: " + ", ".join(c.name for c, _ in segments[:8])
                       + (" …" if len(segments) > 8 else ""))
        elif problem:
            st.caption(f"Not a drive folder yet: {problem}")
        out = Path(folder) / "drive_output" if folder else None
        ready = bool(segments) and bool(gnss) and Path(gnss).is_file() and settings is not None
        if st.button("Run whole drive", type="primary", disabled=not ready,
                     help="Takes a few minutes per segment. Results go to a 'drive_output' "
                          "folder next to the segments."):
            import yaml

            bar = st.progress(0.0, "starting…")
            try:
                out.mkdir(parents=True, exist_ok=True)
                cfg = out / "inventory.yaml"
                cfg.write_text(yaml.safe_dump(settings, sort_keys=False), encoding="utf-8")
                res = drive.run_drive(
                    Path(folder), Path(gnss), out, config_fp=cfg,
                    progress=lambda f, m: bar.progress(min(max(f, 0.0), 1.0), m),
                )
                st.session_state["drive_result"] = {**res, "merged": str(res["merged"])}
            except FIPError as exc:
                st.error(f"**Whole drive failed:** {exc}")
            finally:
                bar.empty()
        res = st.session_state.get("drive_result")
        if not res:
            return
        merged = Path(res["merged"])
        if not (merged / "trees_merged_wgs84.csv").exists():
            return
        st.success(
            f"The drive is one map: {res['trees']} trees from "
            f"{sum(1 for s in res['segments'] if not s['error'])} segments "
            f"({res['duplicates']} seen twice and merged). Files are in `{merged}`."
        )
        st.dataframe(
            pd.DataFrame([{"Segment": s["name"], "Trees": s["trees"],
                           "GNSS fit (m)": s["gnss_rms_m"], "Seconds": s["seconds"],
                           "Problem": s["error"]} for s in res["segments"]]),
            hide_index=True, width="stretch",
        )
        with (merged / "trees_merged_wgs84.csv").open(newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        if rows:
            sat = st.toggle("Satellite imagery", True, key="drive_sat",
                            help="Imagery tiles are loaded by your browser from Esri's public "
                                 "service (which sees the map area you view). Off: OpenStreetMap.")
            st.plotly_chart(plot_satellite(rows, sat, units), width="stretch", theme="streamlit")
        cols = st.columns(3)
        for col, (name, label, mime) in zip(cols, [
            ("trees_merged.gpx", "Download trees_merged.gpx (field)", "application/gpx+xml"),
            ("field_sheet.csv", "Download field sheet", "text/csv"),
            ("trees_merged_wgs84.csv", "Download CSV (lat/lon, UTM)", "text/csv"),
        ]):
            if (merged / name).exists():
                col.download_button(label, (merged / name).read_bytes(), name, mime=mime, width="stretch")
