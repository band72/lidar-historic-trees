"""
A file / folder chooser for the web app, browsing the disk of the computer running it.

A browser upload copies the file into the page (slow for 100 MB+ point clouds and limited in
size); this chooser picks files in place. It also works in the installed program, where
there is no terminal to type a path into. The last folders used are remembered per user.
"""

from __future__ import annotations

import json
from pathlib import Path

import streamlit as st


def prefs_path() -> Path:
    """Per-user settings file for the app (last folders)."""
    from forest_inventory_pipeline.profiles import config_dir

    return config_dir() / "app_prefs.json"


def load_prefs() -> dict:
    try:
        return json.loads(prefs_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_pref(key: str, value) -> None:
    prefs = load_prefs()
    if prefs.get(key) == value:
        return
    prefs[key] = value
    try:
        prefs_path().parent.mkdir(parents=True, exist_ok=True)
        prefs_path().write_text(json.dumps(prefs, indent=2), encoding="utf-8")
    except OSError:
        pass  # preferences are a convenience; never fail the app over them


def _size(p: Path) -> str:
    try:
        n = p.stat().st_size
    except OSError:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _start_dir(pref_key: str) -> Path:
    for cand in (load_prefs().get(pref_key), str(Path.home())):
        if cand and Path(cand).is_dir():
            return Path(cand)
    return Path.cwd()


def _is_bag_dir(p: Path) -> bool:
    return p.is_dir() and (p / "metadata.yaml").exists()


def browse(
    key: str,
    mode: str = "file",
    extensions: set[str] | None = None,
    pref_key: str | None = None,
    container=None,
) -> str:
    """
    Show a chooser and return the chosen path ("" until something is chosen).

    :param mode: "file" (a point cloud file, a folder of tiles or a ROS2 bag folder) or
        "dir" (an output folder).
    :param extensions: lower-case suffixes shown as choosable files, e.g. {".laz", ".ply"}.
    :param pref_key: remembers the folder of the last choice under this name.
    """
    c = container or st
    ss = st.session_state
    dir_key, sel_key, pick_key, typed_key = (
        f"{key}_dir",
        f"{key}_sel",
        f"{key}_pick",
        f"{key}_typed",
    )
    if dir_key not in ss:
        ss[dir_key] = str(_start_dir(pref_key or key))
        ss[typed_key] = ss[dir_key]
        if mode == "dir":
            ss[sel_key] = ss[dir_key]
    exts = {e.lower() for e in (extensions or set())}

    def go(path: str) -> None:
        p = Path(path).expanduser()
        if p.is_file() or (mode == "file" and _is_bag_dir(p)):
            ss[sel_key] = str(p)
            p = p.parent
            if pref_key:
                save_pref(pref_key, str(p.resolve()))
        if p.is_dir():
            ss[dir_key] = str(p.resolve())
            ss[typed_key] = ss[dir_key]
            if mode == "dir":
                ss[sel_key] = ss[dir_key]
        ss[pick_key] = None

    def on_typed() -> None:
        go(ss[typed_key])

    def on_pick() -> None:
        choice = ss.get(pick_key)
        if not choice:
            return
        target = Path(ss[dir_key]) / choice[1]
        if choice[0] == "up":
            go(str(Path(ss[dir_key]).parent))
        elif choice[0] == "dir":
            go(str(target))
        else:  # a file or a ROS2 bag folder
            ss[sel_key] = str(target)
            ss[pick_key] = None
            if pref_key:
                save_pref(pref_key, ss[dir_key])

    cwd = Path(ss[dir_key])
    c.text_input(
        "Folder",
        key=typed_key,
        on_change=on_typed,
        help="Type or paste a folder or file path and press Enter.",
    )
    entries: list[tuple[str, str, str]] = []
    if cwd.parent != cwd:
        entries.append(("up", "..", "⬆  .. (up one folder)"))
    try:
        items = sorted(cwd.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except OSError as exc:
        c.error(f"Cannot open {cwd}: {exc}")
        items = []
    n_files = 0
    for p in items[:2000]:
        if p.name.startswith("."):
            continue
        if _is_bag_dir(p) and mode == "file":
            entries.append(("file", p.name, f"🎒  {p.name}/  (ROS2 bag)"))
        elif p.is_dir():
            entries.append(("dir", p.name, f"📁  {p.name}/"))
        elif mode == "file" and p.suffix.lower() in exts:
            entries.append(("file", p.name, f"📄  {p.name}  ({_size(p)})"))
            n_files += 1
    c.selectbox(
        "Contents",
        entries,
        index=None,
        key=pick_key,
        format_func=lambda e: e[2],
        on_change=on_pick,
        placeholder="Open a folder or choose a file…" if mode == "file" else "Open a folder…",
        label_visibility="collapsed",
    )
    if mode == "file":
        if n_files > 1:
            if c.button(
                f"Use all {n_files} point cloud files in this folder (tiles)", key=f"{key}_all"
            ):
                ss[sel_key] = str(cwd)
                if pref_key:
                    save_pref(pref_key, str(cwd))
                st.rerun()
        elif n_files == 0 and not any(e[0] == "file" for e in entries):
            c.caption("No point cloud files here.")
    else:
        if pref_key and c.button("Use this folder", key=f"{key}_use"):
            ss[sel_key] = str(cwd)
            save_pref(pref_key, str(cwd))
    chosen = ss.get(sel_key, "")
    if chosen:
        c.success(f"{'Output folder' if mode == 'dir' else 'Selected'}: `{chosen}`")
    return chosen
