"""
Merging georeferenced runs (e.g. the segments of one drive) into one tree map.

Each run folder holds ``trees_wgs84.csv`` from ``fip georeference``. Trees are compared in
metres on a UTM grid; a tree seen in two runs is kept once. Two records are taken to be the
same tree when they come from different runs, lie within the merge distance (default 2 m,
widened to 2.5 combined position sd up to 3 m), and their DBH agree (ranges overlap, or within
max(5 cm, 25 %)). Of a pair, the better measurement is kept: ok before warning, a measured
DBH before a lower bound, then the narrower interval.

Positions from separate runs carry independent GNSS alignment errors of about 1 m, so two
neighbouring trees of similar size closer than the merge distance can be merged by mistake,
and a tree seen twice can stay doubled when the alignments differ by more than it. The merge
report lists every pair that was merged with its distance, so this can be checked.
"""

from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

import numpy as np

from .errors import InputDataError
from .units import to_metres
from .utils.rich_console import logger

# --- UTM (WGS84), Krueger series to n^4: mm accuracy within the zone --------------------------

_A, _F = 6378137.0, 1 / 298.257223563
_K0, _E0 = 0.9996, 500000.0


def utm_zone(lon: float) -> int:
    return int((lon + 180.0) // 6.0) % 60 + 1


def latlon_to_utm(
    lat: np.ndarray, lon: np.ndarray, zone: int | None = None
) -> tuple[np.ndarray, np.ndarray, int, bool]:
    """(easting, northing, zone, northern hemisphere) of WGS84 lat/lon in degrees."""
    lat = np.asarray(lat, dtype=float)
    lon = np.asarray(lon, dtype=float)
    zone = zone or utm_zone(float(np.mean(lon)))
    north = bool(np.mean(lat) >= 0)
    n = _F / (2 - _F)
    a_ = _A / (1 + n) * (1 + n**2 / 4 + n**4 / 64)
    alpha = (
        n / 2 - 2 * n**2 / 3 + 5 * n**3 / 16 + 41 * n**4 / 180,
        13 * n**2 / 48 - 3 * n**3 / 5 + 557 * n**4 / 1440,
        61 * n**3 / 240 - 103 * n**4 / 140,
        49561 * n**4 / 161280,
    )
    phi = np.radians(lat)
    lam = np.radians(lon - (zone * 6 - 183))
    e = 2 * math.sqrt(n) / (1 + n)
    t = np.sinh(np.arctanh(np.sin(phi)) - e * np.arctanh(e * np.sin(phi)))
    xi = np.arctan2(t, np.cos(lam))
    eta = np.arctanh(np.sin(lam) / np.sqrt(1 + t**2))
    x, y = eta.copy(), xi.copy()
    for j, al in enumerate(alpha, start=1):
        x += al * np.cos(2 * j * xi) * np.sinh(2 * j * eta)
        y += al * np.sin(2 * j * xi) * np.cosh(2 * j * eta)
    easting = _E0 + _K0 * a_ * x
    northing = _K0 * a_ * y + (0.0 if north else 10_000_000.0)
    return easting, northing, zone, north


def utm_epsg(zone: int, north: bool) -> str:
    return f"EPSG:{(32600 if north else 32700) + zone}"


# --- reading -------------------------------------------------------------------------------------


@dataclass
class Tree:
    run: str
    tree_id: int
    lat: float
    lon: float
    position_sd: float
    status: str
    dbh: float  # m
    ci_low: float  # m
    ci_high: float  # m
    dbh_method: str
    message: str
    e: float = 0.0
    n: float = 0.0
    also_seen: list[str] = field(default_factory=list)

    @property
    def lower_bound(self) -> bool:
        return self.dbh_method.startswith("visible width")

    def quality(self) -> tuple:
        rel = (self.ci_high - self.ci_low) / self.dbh if self.dbh > 0 else math.inf
        return (self.status == "added", self.status != "ok", self.lower_bound, rel)

    def compatible(self, other: "Tree") -> bool:
        if not (math.isfinite(self.dbh) and math.isfinite(other.dbh)):
            return True  # a reviewer-added tree has no DBH to compare
        overlap = self.ci_low <= other.ci_high and other.ci_low <= self.ci_high
        close = abs(self.dbh - other.dbh) <= max(0.05, 0.25 * max(self.dbh, other.dbh))
        return overlap or close


def find_trees_csv(folder: Path) -> Path:
    folder = Path(folder)
    if folder.is_file():
        if folder.suffix.lower() != ".csv":
            raise InputDataError(f"{folder} is not a trees CSV (or a run folder)")
        return folder
    for name in ("trees_wgs84.csv",):
        if (folder / name).exists():
            return folder / name
    hits = sorted(folder.glob("*_wgs84.csv"))
    if not hits:
        raise InputDataError(
            f"{folder} has no trees_wgs84.csv; run `fip georeference` on its trees.csv first"
        )
    return hits[0]


def read_run(folder: Path, name: str | None = None) -> list[Tree]:
    path = find_trees_csv(folder)
    run = name or (path.parent.name if path.name.endswith("_wgs84.csv") else path.stem)
    out = []
    with path.open(newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            added = r.get("status") == "added"
            if not added and (
                r.get("status") not in ("ok", "warning") or r.get("dbh", "") in ("", "nan")
            ):
                continue
            unit = r.get("length_unit") or "m"

            def m(key):
                v = r.get(key, "")
                return float(to_metres(float(v), unit)) if v not in ("", "nan", None) else math.nan

            out.append(
                Tree(
                    run=run,
                    tree_id=int(r["tree_id"]),
                    lat=float(r["lat"]),
                    lon=float(r["lon"]),
                    position_sd=float(r.get("position_sd_m") or 1.0),
                    status=r["status"],
                    dbh=m("dbh"),
                    ci_low=m("ci_low"),
                    ci_high=m("ci_high"),
                    dbh_method=r.get("dbh_method", ""),
                    message=r.get("message", ""),
                )
            )
    logger.info(f"{run}: {len(out)} trees from {path}")
    return out


# --- merging -------------------------------------------------------------------------------------


@dataclass
class MergeResult:
    trees: list[Tree]  # kept, in merged-id order
    pairs: list[dict]  # merged duplicates, for the report
    zone: int
    north: bool
    n_input: int
    offsets: list[dict] = field(default_factory=list)  # per-run shifts from shared trees


def _tie_offset(
    run_trees: Sequence[Tree], kept: Sequence[Tree], search: float = 6.0
) -> tuple[np.ndarray, int, float] | None:
    """
    Offset (east, north; m) that moves ``run_trees`` onto ``kept``, from trees both saw:
    mutual nearest neighbours within ``search`` m whose measured DBH agree. None unless at
    least 5 such pairs agree with each other to within 0.75 m (median deviation), so a
    coincidence of neighbouring trees cannot shift a run.
    """
    a = [t for t in run_trees if math.isfinite(t.dbh) and not t.lower_bound]
    b = [t for t in kept if math.isfinite(t.dbh) and not t.lower_bound]
    if len(a) < 5 or len(b) < 5:
        return None
    pa = np.array([[t.e, t.n] for t in a])
    pb = np.array([[t.e, t.n] for t in b])
    d = np.hypot(pa[:, None, 0] - pb[None, :, 0], pa[:, None, 1] - pb[None, :, 1])
    ab, ba = d.argmin(axis=1), d.argmin(axis=0)
    deltas = [
        pb[j] - pa[i]
        for i, j in enumerate(ab)
        if ba[j] == i and d[i, j] <= search and a[i].compatible(b[j])
    ]
    if len(deltas) < 5:
        return None
    deltas = np.array(deltas)
    off = np.median(deltas, axis=0)
    spread = float(np.median(np.hypot(*(deltas - off).T)))
    if spread > 0.75:
        return None
    return off, len(deltas), spread


def merge_trees(
    trees: Sequence[Tree],
    distance: float = 1.5,
    max_distance: float = 3.0,
    run_quality: dict[str, float] | None = None,
    tie: bool = True,
) -> MergeResult:
    """
    Merge the trees of several runs. Runs are taken best-aligned first (``run_quality``:
    run -> GNSS residual RMS, lower is better); with ``tie``, each further run is first
    shifted onto the trees merged so far by the offset of the trees both saw (see
    :func:`_tie_offset`), which removes most of the run-to-run bias of separate GNSS
    alignments, then records are merged within ``distance`` (widened to 2.5 combined position
    sd for runs that could not be tied, up to ``max_distance``).
    """
    from . import georef

    if not trees:
        raise InputDataError("no measured trees in the given runs")
    lat0 = float(np.mean([t.lat for t in trees]))
    lon0 = float(np.mean([t.lon for t in trees]))
    enu = georef.geodetic_to_enu(
        np.array([t.lat for t in trees]), np.array([t.lon for t in trees]),
        np.zeros(len(trees)), lat0, lon0, 0.0,
    )
    for t, (ei, ni, _) in zip(trees, enu):
        t.e, t.n = float(ei), float(ni)
    by_run: dict[str, list[Tree]] = {}
    for t in trees:
        by_run.setdefault(t.run, []).append(t)
    quality = run_quality or {}
    order = sorted(by_run, key=lambda r: (quality.get(r, math.inf), -len(by_run[r])))

    kept: list[Tree] = []
    pairs, offsets = [], []
    for ri, run in enumerate(order):
        run_trees = sorted(by_run[run], key=Tree.quality)
        tied = False
        if tie and ri > 0:
            res = _tie_offset(run_trees, kept)
            if res is not None:
                off, n_pairs, spread = res
                for t in run_trees:
                    t.e, t.n = t.e + float(off[0]), t.n + float(off[1])
                tied = True
                offsets.append({"run": run, "shift_east_m": round(float(off[0]), 2),
                                "shift_north_m": round(float(off[1]), 2),
                                "tie_trees": n_pairs, "spread_m": round(spread, 2)})
            else:
                offsets.append({"run": run, "shift_east_m": 0.0, "shift_north_m": 0.0,
                                "tie_trees": 0, "note": "no shared trees to tie on"})
        for t in run_trees:
            match, best = None, math.inf
            for k in kept:
                if k.run == t.run or t.run in k.also_seen:
                    continue
                dd = math.hypot(k.e - t.e, k.n - t.n)
                limit = distance if tied else min(
                    max_distance, max(distance, 2.5 * math.hypot(k.position_sd, t.position_sd))
                )
                if dd <= limit and dd < best and k.compatible(t):
                    match, best = k, dd
            if match is None:
                kept.append(t)
            else:
                if Tree.quality(t) < Tree.quality(match):  # keep the better measurement
                    i = kept.index(match)
                    t.also_seen = [r for r in match.also_seen if r != t.run] + [match.run]
                    t.e, t.n = match.e, match.n  # keep the reference position
                    kept[i] = t
                    match, t = t, match
                else:
                    match.also_seen.append(t.run)
                pairs.append({"kept": f"{match.run}#{match.tree_id}",
                              "dropped": f"{t.run}#{t.tree_id}",
                              "distance_m": round(best, 2),
                              "dbh_cm": [round(match.dbh * 100, 1), round(t.dbh * 100, 1)]})
    # back to lat/lon (shifted runs moved), then the UTM grid for CAD/GIS
    pts = np.array([[t.e, t.n, 0.0] for t in kept])
    lat, lon, _ = georef.enu_to_geodetic(pts, lat0, lon0, 0.0)
    for t, la, lo in zip(kept, lat, lon):
        t.lat, t.lon = float(la), float(lo)
    e, n, zone, north = latlon_to_utm(lat, lon)
    for t, ei, ni in zip(kept, e, n):
        t.e, t.n = float(ei), float(ni)
    kept.sort(key=lambda t: (-t.n, t.e))  # north to south, for a stable numbering
    return MergeResult(kept, pairs, zone, north, len(trees), offsets)


def run_quality(folder: Path) -> float:
    """GNSS residual RMS (m) of a run's georef.json, or inf if there is none."""
    p = Path(folder) / "georef.json" if Path(folder).is_dir() else Path(folder).parent / "georef.json"
    try:
        return float(json.loads(p.read_text(encoding="utf-8"))["rms_m"])
    except (OSError, ValueError, KeyError, TypeError):
        return math.inf


# --- writing -------------------------------------------------------------------------------------


def _fmt(v: float, scale: float, digits: int) -> str:
    return f"{v * scale:.{digits}f}" if math.isfinite(v) else ""


def write_merged(result: MergeResult, out_dir: Path, inputs: Sequence[str]) -> list[Path]:
    from . import georef
    from . import inventory as inv
    from .records import TreeRecord
    from .traits.dbh.robust_fit import FitParams

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    epsg = utm_epsg(result.zone, result.north)
    rows = []
    for i, t in enumerate(result.trees, start=1):
        rows.append(
            {
                "tree_id": i,
                "status": t.status,
                "dbh": _fmt(t.dbh, 1, 4),
                "dbh_cm": _fmt(t.dbh, 100, 1),
                "dbh_in": _fmt(t.dbh, 1 / 0.0254, 1),
                "ci_low": _fmt(t.ci_low, 1, 4),
                "ci_high": _fmt(t.ci_high, 1, 4),
                "length_unit": "m",
                "dbh_method": t.dbh_method,
                "lat": f"{t.lat:.8f}",
                "lon": f"{t.lon:.8f}",
                "utm_e": f"{t.e:.3f}",
                "utm_n": f"{t.n:.3f}",
                "crs": epsg,
                "position_sd_m": f"{t.position_sd:.2f}",
                "run": t.run,
                "run_tree_id": t.tree_id,
                "also_seen_in": ";".join(t.also_seen),
                "message": t.message,
            }
        )
    cols = list(rows[0]) if rows else ["tree_id"]
    written = georef.write_outputs(rows, cols, out_dir, stem="trees_merged")

    # DXF on the UTM grid (metres), drawn by the standard exporter
    records = [
        TreeRecord(
            tree_id=i,
            status=t.status,
            x=t.e,
            y=t.n,
            ground_z=0.0,
            breast_height=1.3,
            bh_elevation=1.3,
            n_points=0,
            dbh=t.dbh,
            ci_low=t.ci_low,
            ci_high=t.ci_high,
            dbh_method=t.dbh_method,
            message=t.message,
        )
        for i, t in enumerate(result.trees, start=1)
    ]
    ctx = inv.context_for(
        "; ".join(inputs), FitParams(breast_height=1.3), units="m", crs=epsg,
        merged_runs=list(inputs),
    )
    written += inv.export_records(records, out_dir, ["dxf"], ctx)

    report = {
        "format": "fip-merge",
        "inputs": list(inputs),
        "crs": epsg,
        "trees_in": result.n_input,
        "trees_out": len(result.trees),
        "duplicates_merged": len(result.pairs),
        "run_offsets": result.offsets,
        "pairs": result.pairs,
        "note": "DXF and utm_e/utm_n are on the UTM grid; heights are not georeferenced "
        "(circles at z = breast height above a 0 datum).",
    }
    p = out_dir / "merge_report.json"
    p.write_text(json.dumps(report, indent=1), encoding="utf-8")
    written.append(p)
    return written
