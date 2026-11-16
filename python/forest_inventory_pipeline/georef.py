"""
Georeference a locally registered run (e.g. KISS-ICP, origin at the first scan) with the
GNSS fixes recorded during the drive, and give every tree a latitude and longitude.

How:
  1. GNSS fixes (WGS84 lat/lon) are converted to a local east-north-up (ENU) plane around
     their mean position (exact ellipsoidal formulas; no projection library needed).
  2. The scanner trajectory is interpolated at each fix's time.
  3. A 2D rigid transform (rotation + translation, no scale: lidar odometry is metric) maps
     trajectory x, y onto the fixes' east, north. Weighted least squares, with fixes more
     more than 3.5 robust standard deviations off dropped and the fit repeated.
  4. Tree positions go through the same transform and back to lat/lon.

Accuracy is that of the GNSS: standalone fixes (no RTK) are good to about +/- 1 m, and the
unknown offset between the GNSS antenna(s) and the lidar adds to that. The fit residuals
measure how well the run's shape matches the GNSS track (odometry drift plus fix noise);
they do not reveal a constant GNSS bias. Heights are not georeferenced (standalone GNSS
height is poor); the local ground elevations are kept as they are.
"""

from __future__ import annotations

import csv
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence

import numpy as np

from .errors import InputDataError
from .utils import logger

# WGS84
_A = 6378137.0
_F = 1 / 298.257223563
_E2 = _F * (2 - _F)

FORMAT = "lidar-tree-inventory/georeference"
VERSION = 1


def geodetic_to_ecef(lat, lon, h) -> np.ndarray:
    lat, lon, h = (
        np.radians(np.asarray(lat, float)),
        np.radians(np.asarray(lon, float)),
        np.asarray(h, float),
    )
    n = _A / np.sqrt(1 - _E2 * np.sin(lat) ** 2)
    return np.stack(
        [
            (n + h) * np.cos(lat) * np.cos(lon),
            (n + h) * np.cos(lat) * np.sin(lon),
            (n * (1 - _E2) + h) * np.sin(lat),
        ],
        axis=-1,
    )


def ecef_to_geodetic(xyz: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Bowring's method, iterated: sub-millimetre for terrestrial points."""
    x, y, z = np.asarray(xyz, float).T
    lon = np.arctan2(y, x)
    p = np.hypot(x, y)
    lat = np.arctan2(z, p * (1 - _E2))
    for _ in range(5):
        n = _A / np.sqrt(1 - _E2 * np.sin(lat) ** 2)
        h = p / np.cos(lat) - n
        lat = np.arctan2(z, p * (1 - _E2 * n / (n + h)))
    n = _A / np.sqrt(1 - _E2 * np.sin(lat) ** 2)
    h = p / np.cos(lat) - n
    return np.degrees(lat), np.degrees(lon), h


def _enu_basis(lat0: float, lon0: float) -> np.ndarray:
    la, lo = math.radians(lat0), math.radians(lon0)
    return np.array(
        [
            [-math.sin(lo), math.cos(lo), 0.0],
            [-math.sin(la) * math.cos(lo), -math.sin(la) * math.sin(lo), math.cos(la)],
            [math.cos(la) * math.cos(lo), math.cos(la) * math.sin(lo), math.sin(la)],
        ]
    )


def geodetic_to_enu(lat, lon, h, lat0: float, lon0: float, h0: float) -> np.ndarray:
    d = geodetic_to_ecef(lat, lon, h) - geodetic_to_ecef(lat0, lon0, h0)
    return d @ _enu_basis(lat0, lon0).T


def enu_to_geodetic(enu: np.ndarray, lat0: float, lon0: float, h0: float):
    ecef = np.asarray(enu, float) @ _enu_basis(lat0, lon0) + geodetic_to_ecef(lat0, lon0, h0)
    return ecef_to_geodetic(np.atleast_2d(ecef))


# --- inputs -----------------------------------------------------------------------------------


def _read_csv(path: Path, required: Sequence[str]) -> dict[str, np.ndarray]:
    try:
        with Path(path).open(newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
    except OSError as exc:
        raise InputDataError(f"cannot read {path}: {exc}") from exc
    if not rows:
        raise InputDataError(f"{path} has no rows")
    missing = [c for c in required if c not in rows[0]]
    if missing:
        raise InputDataError(f"{path} lacks column(s) {missing}; has {list(rows[0])}")
    out = {}
    for c in rows[0]:
        try:
            out[c] = np.array([float(r[c]) if r[c] not in ("", None) else np.nan for r in rows])
        except ValueError:
            out[c] = np.array([r[c] for r in rows], dtype=object)
    return out


def read_trajectory(path: Path) -> tuple[np.ndarray, np.ndarray]:
    """``time_s, x, y[, z]`` (metres, local frame) -> times, xy."""
    d = _read_csv(path, ["time_s", "x", "y"])
    t, xy = d["time_s"].astype(float), np.stack([d["x"], d["y"]], axis=1).astype(float)
    order = np.argsort(t)
    return t[order], xy[order]


@dataclass
class GnssFixes:
    time_s: np.ndarray
    lat: np.ndarray
    lon: np.ndarray
    alt: np.ndarray
    #: reported horizontal standard deviation (m); NaN if not reported
    h_sd: np.ndarray
    source: str = ""

    def __len__(self) -> int:
        return len(self.time_s)

    def write_csv(self, path: Path) -> Path:
        from .plugins import atomic_write

        with atomic_write(Path(path), "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["time_s", "lat", "lon", "alt", "h_sd"])
            for row in zip(self.time_s, self.lat, self.lon, self.alt, self.h_sd):
                w.writerow(
                    [
                        f"{row[0]:.4f}",
                        f"{row[1]:.9f}",
                        f"{row[2]:.9f}",
                        f"{row[3]:.3f}",
                        "" if not np.isfinite(row[4]) else f"{row[4]:.3f}",
                    ]
                )
        return Path(path)


def read_gnss_csv(path: Path) -> GnssFixes:
    """``time_s, lat, lon[, alt][, h_sd]`` in decimal degrees / metres."""
    d = _read_csv(path, ["time_s", "lat", "lon"])
    n = len(d["time_s"])
    return GnssFixes(
        d["time_s"].astype(float),
        d["lat"].astype(float),
        d["lon"].astype(float),
        d.get("alt", np.zeros(n)).astype(float),
        d.get("h_sd", np.full(n, np.nan)).astype(float),
        source=str(path),
    )


def read_gnss_bag(path: Path, topics: Sequence[str] | None = None) -> GnssFixes:
    """
    ``sensor_msgs/NavSatFix`` messages from a ROS1 bag or ROS2 bag folder (all NavSatFix
    topics unless ``topics`` is given). Fixes with status < 0 (no fix) are dropped. Several
    antennas are pooled: their mean is a fixed point on the vehicle.
    """
    try:
        from rosbags.highlevel import AnyReader
        from rosbags.typesys import Stores, get_typestore
    except ImportError as exc:
        raise InputDataError("reading GNSS from a bag needs the 'rosbags' package") from exc
    rows = []
    with AnyReader([Path(path)], default_typestore=get_typestore(Stores.ROS1_NOETIC)) as r:
        cons = [
            c
            for c in r.connections
            if c.msgtype == "sensor_msgs/msg/NavSatFix" and (not topics or c.topic in topics)
        ]
        if not cons:
            have = sorted({c.topic for c in r.connections if "NavSatFix" in c.msgtype})
            raise InputDataError(
                f"no NavSatFix topic {list(topics) if topics else ''} in {path}; found {have}"
            )
        logger.info(f"reading GNSS fixes from {', '.join(c.topic for c in cons)}")
        n_bad = 0
        for c, _t, raw in r.messages(connections=cons):
            m = r.deserialize(raw, c.msgtype)
            if m.status.status < 0 or not (np.isfinite(m.latitude) and np.isfinite(m.longitude)):
                n_bad += 1
                continue
            cov = m.position_covariance
            h_sd = (
                math.sqrt(max(0.0, (cov[0] + cov[4]) / 2))
                if m.position_covariance_type > 0
                else float("nan")
            )
            rows.append(
                (
                    m.header.stamp.sec + m.header.stamp.nanosec * 1e-9,
                    m.latitude,
                    m.longitude,
                    m.altitude,
                    h_sd,
                )
            )
    if n_bad:
        logger.warning(f"dropped {n_bad} GNSS message(s) without a fix")
    if not rows:
        raise InputDataError(f"no usable GNSS fixes in {path}")
    a = np.array(sorted(rows))
    return GnssFixes(a[:, 0], a[:, 1], a[:, 2], a[:, 3], a[:, 4], source=str(path))


def read_gnss(path: Path, topics: Sequence[str] | None = None) -> GnssFixes:
    path = Path(path)
    if path.suffix.lower() == ".csv":
        return read_gnss_csv(path)
    if path.suffix.lower() in (".bag", ".db3", ".mcap") or path.is_dir():
        return read_gnss_bag(path, topics)
    raise InputDataError(f"GNSS input must be a .csv, a ROS bag or a ROS2 bag folder: {path}")


# --- alignment --------------------------------------------------------------------------------


@dataclass
class Alignment:
    """local (x, y) -> ENU (e, n) = R(theta) @ (x, y) + (te, tn); ENU around lat0/lon0/h0."""

    theta_rad: float
    te: float
    tn: float
    lat0: float
    lon0: float
    h0: float
    rms_m: float
    max_residual_m: float
    n_used: int
    n_fixes: int
    track_length_m: float
    #: median reported GNSS horizontal sd (m), NaN if not reported
    gnss_h_sd_m: float
    #: 1-sd uncertainty of the rotation (rad) and of the translation at the track centroid (m)
    theta_sd_rad: float
    centroid_sd_m: float
    centroid_x: float
    centroid_y: float
    note: str = ""

    def to_enu(self, xy: np.ndarray) -> np.ndarray:
        c, s = math.cos(self.theta_rad), math.sin(self.theta_rad)
        xy = np.atleast_2d(np.asarray(xy, float))
        return xy @ np.array([[c, -s], [s, c]]).T + [self.te, self.tn]

    def to_latlon(self, xy: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        en = self.to_enu(xy)
        lat, lon, _ = enu_to_geodetic(
            np.column_stack([en, np.zeros(len(en))]), self.lat0, self.lon0, self.h0
        )
        return lat, lon

    def position_sd(self, xy: np.ndarray) -> np.ndarray:
        """1-sd horizontal uncertainty (m) of mapped points: GNSS + alignment (lever arm excluded)."""
        xy = np.atleast_2d(np.asarray(xy, float))
        r = np.hypot(xy[:, 0] - self.centroid_x, xy[:, 1] - self.centroid_y)
        gnss = self.gnss_h_sd_m if np.isfinite(self.gnss_h_sd_m) else self.rms_m
        return np.sqrt(gnss**2 + self.centroid_sd_m**2 + (self.theta_sd_rad * r) ** 2)

    @property
    def heading_deg(self) -> float:
        """Rotation from the local frame to east-north, degrees counter-clockwise."""
        return math.degrees(self.theta_rad)

    def to_dict(self) -> dict:
        return {
            "format": FORMAT,
            "version": VERSION,
            **asdict(self),
            "heading_deg": round(self.heading_deg, 4),
        }

    def save(self, path: Path) -> Path:
        from .plugins import atomic_write

        with atomic_write(Path(path), "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, indent=2)
        return Path(path)

    @classmethod
    def load(cls, path: Path) -> "Alignment":
        try:
            d = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise InputDataError(f"cannot read georeference {path}: {exc}") from exc
        if d.get("format") != FORMAT or int(d.get("version", 0)) > VERSION:
            raise InputDataError(f"{path} is not a georeference file this program can read")
        names = cls.__dataclass_fields__
        return cls(**{k: v for k, v in d.items() if k in names})


def _rigid_2d(src: np.ndarray, dst: np.ndarray, w: np.ndarray) -> tuple[float, np.ndarray]:
    """Weighted least-squares rotation angle and translation with dst ~ R src + t."""
    w = w / w.sum()
    ms, md = w @ src, w @ dst
    a, b = src - ms, dst - md
    num = np.sum(w * (a[:, 0] * b[:, 1] - a[:, 1] * b[:, 0]))
    den = np.sum(w * (a[:, 0] * b[:, 0] + a[:, 1] * b[:, 1]))
    theta = math.atan2(num, den)
    c, s = math.cos(theta), math.sin(theta)
    t = md - np.array([[c, -s], [s, c]]) @ ms
    return theta, t


def fit_alignment(
    traj_t: np.ndarray,
    traj_xy: np.ndarray,
    gnss: GnssFixes,
    min_track_m: float = 20.0,
    reject_sigma: float = 3.5,
) -> Alignment:
    """
    Fit the local-to-ENU transform from the fixes recorded during the trajectory.

    :raises InputDataError: no time overlap, too few fixes or too short a track (a rotation
        cannot be determined from a vehicle that hardly moved).
    """
    inside = (gnss.time_s >= traj_t[0]) & (gnss.time_s <= traj_t[-1])
    n_fixes = int(inside.sum())
    if n_fixes < 5:
        raise InputDataError(
            f"only {n_fixes} GNSS fix(es) fall within the trajectory's time span "
            f"({traj_t[0]:.1f}-{traj_t[-1]:.1f} s; fixes {gnss.time_s[0]:.1f}-{gnss.time_s[-1]:.1f} s)."
            " Are both from the same recording, and are the times in the same clock?"
        )
    t = gnss.time_s[inside]
    xy = np.column_stack([np.interp(t, traj_t, traj_xy[:, 0]), np.interp(t, traj_t, traj_xy[:, 1])])
    track = float(np.sum(np.hypot(*np.diff(traj_xy, axis=0).T)))
    extent = float(np.max(np.hypot(*(xy - xy.mean(0)).T)))
    if extent < min_track_m / 2:
        raise InputDataError(
            f"the trajectory spans only {2 * extent:.1f} m during the fixes; at least "
            f"{min_track_m:.0f} m is needed to orient the run with GNSS"
        )
    lat0, lon0, h0 = (
        float(np.mean(gnss.lat[inside])),
        float(np.mean(gnss.lon[inside])),
        float(np.mean(gnss.alt[inside])),
    )
    en = geodetic_to_enu(gnss.lat[inside], gnss.lon[inside], gnss.alt[inside], lat0, lon0, h0)[
        :, :2
    ]
    sd = gnss.h_sd[inside]
    med_sd = float(np.nanmedian(sd)) if np.isfinite(sd).any() else float("nan")
    w = 1.0 / np.where(np.isfinite(sd) & (sd > 0), sd, med_sd if np.isfinite(med_sd) else 1.0) ** 2

    keep = np.ones(len(t), bool)
    # 2D residual lengths of isotropic noise are Rayleigh: median = 1.1774 sigma (per axis), and
    # P(length > k sigma) = exp(-k^2 / 2). A fixed k clips good fixes by chance on long drives
    # (k = 3.5 expects 0.5 false rejections in 230 fixes), so k grows with n to keep the expected
    # number of good fixes rejected at 0.05.
    k = max(reject_sigma, math.sqrt(2.0 * math.log(len(t) / 0.05)))
    for _ in range(10):
        theta, tr = _rigid_2d(xy[keep], en[keep], w[keep])
        c, s = math.cos(theta), math.sin(theta)
        res = np.hypot(*(xy @ np.array([[c, -s], [s, c]]).T + tr - en).T)
        # sigma from the median of ALL residuals (robust to a minority of outliers); the median
        # of the kept ones shrinks with every pass and tightens the cut until good fixes go
        sigma = float(np.median(res)) / 1.1774 + 1e-3
        new = res <= max(k * sigma, 0.5)
        if new.sum() < 5 or np.array_equal(new, keep):
            break
        keep = new
    rms = float(np.sqrt(np.mean(res[keep] ** 2)))
    cx, cy = (w[keep] / w[keep].sum()) @ xy[keep]
    r2 = np.sum((xy[keep, 0] - cx) ** 2 + (xy[keep, 1] - cy) ** 2)
    n = int(keep.sum())
    # residuals of consecutive fixes are strongly correlated (GNSS errors wander slowly), so
    # the statistical sd is inflated by an effective-sample factor (fixes ~10 s apart independent)
    duration = float(t[keep][-1] - t[keep][0]) if n > 1 else 0.0
    n_eff = max(2.0, min(n, duration / 10.0))
    inflate = math.sqrt(n / n_eff)
    theta_sd = rms / math.sqrt(max(r2, 1e-9)) * inflate
    centroid_sd = rms / math.sqrt(n) * inflate
    a = Alignment(
        theta_rad=float(theta),
        te=float(tr[0]),
        tn=float(tr[1]),
        lat0=lat0,
        lon0=lon0,
        h0=h0,
        rms_m=rms,
        max_residual_m=float(res[keep].max()),
        n_used=n,
        n_fixes=n_fixes,
        track_length_m=track,
        gnss_h_sd_m=med_sd,
        theta_sd_rad=float(theta_sd),
        centroid_sd_m=float(centroid_sd),
        centroid_x=float(cx),
        centroid_y=float(cy),
        note="2D rigid fit of the local trajectory to GNSS fixes; antenna-to-lidar offset not "
        "modelled; heights not georeferenced",
    )
    logger.info(
        f"georeference: {n}/{n_fixes} fixes used over a {track:.0f} m track; residual RMS "
        f"{rms:.2f} m (max {a.max_residual_m:.2f} m), rotation {a.heading_deg:.2f} "
        f"+/- {math.degrees(theta_sd):.2f} deg; GNSS reported sd {med_sd:.2f} m"
    )
    if rms > 3.0:
        logger.warning(
            f"GNSS residual RMS {rms:.1f} m is large: odometry drift, a time offset, or fixes from "
            "another recording. Check the inputs; positions may be off by several metres."
        )
    return a


# --- outputs ----------------------------------------------------------------------------------


def georeference_trees(trees_csv: Path, alignment: Alignment) -> tuple[list[dict], list[str]]:
    """Rows of ``trees_csv`` with ``lat``, ``lon`` and ``position_sd_m`` added."""
    from .units import to_metres

    try:
        with Path(trees_csv).open(newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            rows, columns = list(reader), list(reader.fieldnames or [])
    except OSError as exc:
        raise InputDataError(f"cannot read {trees_csv}: {exc}") from exc
    if not rows or not {"x", "y"} <= set(columns):
        raise InputDataError(f"{trees_csv} is not a trees CSV (needs x, y columns)")
    unit = rows[0].get("length_unit") or "m"
    xy = np.array([[float(r["x"]), float(r["y"])] for r in rows])
    xy = np.asarray(to_metres(xy, unit), float)
    lat, lon = alignment.to_latlon(xy)
    sd = alignment.position_sd(xy)
    for r, la, lo, s in zip(rows, lat, lon, sd):
        r["lat"], r["lon"], r["position_sd_m"] = f"{la:.8f}", f"{lo:.8f}", f"{s:.2f}"
    return rows, columns + ["lat", "lon", "position_sd_m"]


def _is_tree(row: dict) -> bool:
    return row.get("status", "ok") in ("ok", "warning") and row.get("dbh", "") not in ("", "nan")


def write_outputs(
    rows: list[dict], columns: list[str], out_dir: Path, stem: str = "trees"
) -> list[Path]:
    """``<stem>_wgs84.csv`` (all rows), ``<stem>_wgs84.geojson`` and ``<stem>.gpx`` (trees)."""
    from xml.sax.saxutils import escape

    from .plugins import atomic_write

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []

    p = out_dir / f"{stem}_wgs84.csv"
    with atomic_write(p, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    written.append(p)

    trees = [r for r in rows if _is_tree(r)]

    def num(v):
        try:
            f = float(v)
            return f if math.isfinite(f) else None
        except (TypeError, ValueError):
            return v or None

    keep = (
        "tree_id",
        "status",
        "dbh_cm",
        "dbh_in",
        "ci_low",
        "ci_high",
        "confidence",
        "length_unit",
        "arc_coverage",
        "position_sd_m",
        "message",
    )
    fc = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [round(float(r["lon"]), 8), round(float(r["lat"]), 8)],
                },
                "properties": {
                    k: int(r[k]) if k == "tree_id" else num(r.get(k)) for k in keep if k in r
                },
            }
            for r in trees
        ],
    }
    p = out_dir / f"{stem}_wgs84.geojson"  # RFC 7946: WGS84 lon/lat, no crs member
    with atomic_write(p, "w", encoding="utf-8") as fh:
        json.dump(fc, fh, indent=1)
    written.append(p)

    p = out_dir / f"{stem}.gpx"
    with atomic_write(p, "w", encoding="utf-8") as fh:
        fh.write(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<gpx version="1.1" creator="LiDAR Tree Inventory" '
            'xmlns="http://www.topografix.com/GPX/1/1">\n'
        )
        for r in trees:
            dbh = num(r.get("dbh_cm"))
            name = f"T{r['tree_id']}" + (f" {dbh:.0f}cm" if isinstance(dbh, float) else "")
            desc = (
                (
                    f"DBH {dbh:.1f} cm; position +/- {r['position_sd_m']} m (1 sd); "
                    f"status {r.get('status', '')}"
                )
                if isinstance(dbh, float)
                else ""
            )
            fh.write(
                f'  <wpt lat="{float(r["lat"]):.8f}" lon="{float(r["lon"]):.8f}">'
                f"<name>{escape(name)}</name><desc>{escape(desc)}</desc>"
                "<sym>Park</sym></wpt>\n"
            )
        fh.write("</gpx>\n")
    written.append(p)
    return written
