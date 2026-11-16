"""
A reviewer's decisions saved to a file, so a review can be reopened and reused on later runs.

A reviewer removes shrubs, posts and other false trees by hand (the app's *Exclude this
tree*), and keeps real trees that the checks rejected (*This is a tree*). Tree ids are only
stable for one run: change a setting and the same stem can get a different id. So each
decision is stored with its stem position (x, y in metres, input frame) and matched to the
new results by position; the id is only a fallback for entries without a position.
Exclusions match measured trees; additions match rejected clusters.

File format (``exclusions.json``, written next to the results whenever there is a decision)::

    {
        "format": "lidar-tree-inventory/exclusions",
        "version": 2,
        "source": "segment_0-120s.laz",
        "units": "m",
        "trees": [
            {"tree_id": 12, "x": -37.69, "y": 4.60, "dbh": 0.42, "reason": "shrub"},
            {"tree_id": 40, "x": 3.10, "y": 7.25, "action": "add", "reason": "tree"},
        ],
    }

``action`` is ``exclude`` (the default, and the only one in version 1) or ``add``.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

import numpy as np

from .errors import InputDataError
from .records import STATUS_ADDED, STATUS_EXCLUDED, STATUS_FAILED, TreeRecord
from .utils import logger

FILE_NAME = "exclusions.json"
FORMAT = "lidar-tree-inventory/exclusions"
VERSION = 2
EXCLUDE, ADD = "exclude", "add"
#: default position tolerance (m) for matching a saved exclusion to a tree of a new run
MATCH_TOLERANCE = 0.3

REASONS = ("shrub", "post or structure", "duplicate", "not a tree", "other")
ADD_REASONS = ("tree (checks too strict)", "tree seen through a gap", "leaning tree", "other")


@dataclass(frozen=True)
class Exclusion:
    tree_id: int | None = None
    x: float | None = None
    y: float | None = None
    dbh: float | None = None
    reason: str = ""
    action: str = EXCLUDE

    @property
    def has_position(self) -> bool:
        return self.x is not None and self.y is not None

    def to_dict(self) -> dict:
        d = {"tree_id": self.tree_id, "x": self.x, "y": self.y, "dbh": self.dbh}
        d = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in d.items() if v is not None}
        if self.reason:
            d["reason"] = self.reason
        if self.action != EXCLUDE:
            d["action"] = self.action
        return d


def _finite(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def from_records(
    records: Sequence[TreeRecord],
    ids: Iterable[int],
    reasons: Mapping[int, str] | None = None,
    action: str = EXCLUDE,
) -> list[Exclusion]:
    """Decisions (``action``) for the given tree ids, with each stem's position and DBH."""
    reasons = reasons or {}
    by_id = {r.tree_id: r for r in records}
    out = []
    for tid in sorted({int(i) for i in ids}):
        r = by_id.get(tid)
        if r is None:
            logger.warning(f"exclusion of tree {tid}: no such tree in the results; not saved")
            continue
        out.append(
            Exclusion(
                tree_id=tid,
                x=_finite(r.x),
                y=_finite(r.y),
                dbh=_finite(r.dbh),
                reason=str(reasons.get(tid, "")),
                action=action,
            )
        )
    return out


def excluded_in(records: Sequence[TreeRecord]) -> list[Exclusion]:
    """The reviewer decisions already applied to these records (excluded and added trees)."""
    reasons = {r.tree_id: r.extra.get("exclusion_reason", "") for r in records}
    return from_records(
        records, [r.tree_id for r in records if r.status == STATUS_EXCLUDED], reasons
    ) + from_records(
        records, [r.tree_id for r in records if r.status == STATUS_ADDED], reasons, action=ADD
    )


def save(path: Path, exclusions: Sequence[Exclusion], source: str = "") -> Path:
    """Write the exclusions file atomically (a crash never leaves a half-written file)."""
    from .plugins import atomic_write

    path = Path(path)
    if path.is_dir():
        path = path / FILE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "format": FORMAT,
        "version": VERSION,
        "source": source,
        "units": "m",
        "trees": [e.to_dict() for e in exclusions],
    }
    with atomic_write(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
    logger.info(f"saved {len(exclusions)} reviewer decision(s) to {path}")
    return path


def load(path: Path) -> list[Exclusion]:
    """
    Read an exclusions file. Also accepts a bare list of tree ids (``[3, 7]``) or a ``run.json``
    (its ``parameters.excluded_by_reviewer``), matched by id only.

    :raises InputDataError: unreadable file, unknown format or a newer version.
    """
    path = Path(path)
    if path.is_dir():
        path = path / FILE_NAME
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise InputDataError(f"cannot read exclusions file {path}: {exc}") from exc
    except ValueError as exc:
        raise InputDataError(f"exclusions file {path} is not valid JSON: {exc}") from exc

    if isinstance(doc, list):
        entries = [{"tree_id": t} for t in doc]
    elif isinstance(doc, dict) and doc.get("format") == FORMAT:
        if int(doc.get("version", 0)) > VERSION:
            raise InputDataError(
                f"exclusions file {path} is version {doc['version']}; this program reads up to "
                f"version {VERSION}. Update the program."
            )
        if doc.get("units", "m") != "m":
            raise InputDataError(f"exclusions file {path}: positions must be in metres")
        entries = doc.get("trees", [])
    elif isinstance(doc, dict) and "parameters" in doc:  # run.json
        entries = [{"tree_id": t} for t in doc["parameters"].get("excluded_by_reviewer", [])]
    else:
        raise InputDataError(
            f"{path} is not an exclusions file (expected format '{FORMAT}', a list of tree ids "
            "or a run.json)"
        )
    if not isinstance(entries, list):
        raise InputDataError(f"exclusions file {path}: 'trees' must be a list")

    out = []
    for i, e in enumerate(entries):
        if not isinstance(e, dict):
            raise InputDataError(f"exclusions file {path}: entry {i} is not an object")
        tid = e.get("tree_id")
        try:
            tid = None if tid is None else int(tid)
        except (TypeError, ValueError) as exc:
            raise InputDataError(f"exclusions file {path}: entry {i} has a bad tree_id") from exc
        action = str(e.get("action", EXCLUDE))
        if action not in (EXCLUDE, ADD):
            raise InputDataError(f"exclusions file {path}: entry {i} has unknown action {action!r}")
        ex = Exclusion(
            tree_id=tid,
            x=_finite(e.get("x")),
            y=_finite(e.get("y")),
            dbh=_finite(e.get("dbh")),
            reason=str(e.get("reason", "")),
            action=action,
        )
        if ex.tree_id is None and not ex.has_position:
            raise InputDataError(f"exclusions file {path}: entry {i} has neither tree_id nor x, y")
        out.append(ex)
    return out


def match(
    records: Sequence[TreeRecord],
    exclusions: Sequence[Exclusion],
    tolerance: float = MATCH_TOLERANCE,
    action: str = EXCLUDE,
) -> tuple[dict[int, str], list[Exclusion]]:
    """
    Find the trees of ``records`` that saved decisions with this ``action`` refer to.

    Entries with a position match the nearest unmatched candidate whose stem centre is within
    ``tolerance`` (or the saved stem's radius, if larger), closest pairs first, so two
    neighbouring entries never claim the same tree. Entries without a position match by tree
    id. Exclusions match measured trees; additions match rejected (failed) clusters, whose
    position may move more between runs, so their tolerance is doubled.

    :return: ``{tree_id: reason}`` of matched trees and the entries that matched nothing.
    """
    exclusions = [e for e in exclusions if e.action == action]
    if not exclusions:
        return {}, []
    if action == ADD:
        candidates = [r for r in records if r.status == STATUS_FAILED and np.isfinite(r.x)]
        tolerance *= 2
    else:
        candidates = [r for r in records if r.status != STATUS_FAILED and np.isfinite(r.x)]
    matched: dict[int, str] = {}
    unmatched: list[Exclusion] = []

    positioned = [e for e in exclusions if e.has_position]
    pairs = []
    for ei, e in enumerate(positioned):
        tol = max(tolerance, (e.dbh or 0.0) / 2)
        for r in candidates:
            d = math.hypot(r.x - e.x, r.y - e.y)
            if d <= tol:
                pairs.append((d, ei, r.tree_id))
    used_e: set[int] = set()
    for d, ei, tid in sorted(pairs):
        if ei in used_e or tid in matched:
            continue
        used_e.add(ei)
        matched[tid] = positioned[ei].reason
    unmatched += [e for ei, e in enumerate(positioned) if ei not in used_e]

    ids = {r.tree_id for r in candidates}
    for e in exclusions:
        if e.has_position:
            continue
        if e.tree_id in ids and e.tree_id not in matched:
            matched[e.tree_id] = e.reason
        else:
            unmatched.append(e)

    what = "addition(s)" if action == ADD else "exclusion(s)"
    if unmatched:
        logger.warning(
            f"{len(unmatched)} saved {what} matched no tree of this run (the stem was not "
            "found, or the input/coordinates differ): "
            + ", ".join(
                f"T{e.tree_id}" if not e.has_position else f"({e.x:.2f}, {e.y:.2f})"
                for e in unmatched[:10]
            )
            + (" …" if len(unmatched) > 10 else "")
        )
    logger.info(f"applied {len(matched)} of {len(exclusions)} saved {what}")
    return matched, unmatched
