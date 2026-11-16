"""
Telling roof posts from trees by what is above them.

A post of a shed, carport or pole barn passes every trunk check: it is round, hollow to the
scanner, continuous and tall enough. What differs is overhead. A roof is one smooth sheet: at
3 cm tolerance, 30-60 % of the material within 1.5 m above a post lies on a single plane that
spans most of that area. Crowns are never that smooth (2-6 % on the 2022-03-08 drive), even
when a vehicle lidar sees only their underside. Coarser tests (10 cm voxels, layer thickness)
did not separate the two on real data.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.spatial import cKDTree

#: thinning of the overhead cloud; points keep their real coordinates, so 3 cm planes survive
VOXEL = 0.05
RADIUS = 1.5
#: roofs are looked for below this height (m); crowns higher up only dilute the plane search
ROOF_TOP = 8.0
#: plane hypotheses per stem; enough that a borderline post settles on its best plane
ROOF_TRIES = 800


@dataclass(frozen=True)
class RoofCheck:
    plane_fraction: float  # share of the overhead points on the best smooth plane
    span: float  # share of the disc around the stem the plane covers
    above_fraction: float  # share of overhead points > 0.5 m above the plane (a crown)
    plane_height: float  # median height of the plane above ground (m)
    #: share of the points within 0.5 m of the plane that lie on it (3 cm): a roof is nearly
    #: all of its layer, foliage never is. Unlike plane_fraction it is not diluted by a crown
    #: hanging over the roof.
    layer_fraction: float = 0.0

    def is_roof(self, min_fraction: float, min_span: float) -> bool:
        return self.layer_fraction >= min_fraction and self.span >= min_span


class OverheadIndex:
    """Height-normalized points between ``z_min`` and ``z_max``, thinned, indexed in XY."""

    def __init__(
        self,
        normalized: np.ndarray,
        raw_z: np.ndarray | None = None,
        z_min: float = 1.5,
        z_max: float = 40.0,
    ):
        """
        :param normalized: (N, 3) points with z = height above the terrain model.
        :param raw_z: the same points' elevations. Planes are fitted to these: the terrain
            model's few-cm bumps, subtracted point by point, roughen a smooth roof enough to
            fail the 3 cm plane test (a real post scored 0.17 instead of 0.42). Without it,
            heights are used (fine for flat ground).
        """
        p = np.asarray(normalized, dtype=float)
        rz = p[:, 2] if raw_z is None else np.asarray(raw_z, dtype=float)
        keep = (p[:, 2] >= z_min) & (p[:, 2] <= z_max)
        p, rz = p[keep], rz[keep]
        if len(p):
            k = np.floor(p / VOXEL).astype(np.int64)
            k -= k.min(axis=0)
            span = k.max(axis=0) + 1
            key = (k[:, 0] * span[1] + k[:, 1]) * span[2] + k[:, 2]
            _, first = np.unique(key, return_index=True)
            p, rz = p[first], rz[first]
        self.points = p  # x, y, height above terrain
        self.raw = np.column_stack([p[:, :2], rz]) if len(p) else p  # x, y, elevation
        self.tree = cKDTree(p[:, :2]) if len(p) else None

    def roof(
        self, x: float, y: float, radius: float, base: float, seed: int = 0
    ) -> RoofCheck | None:
        """
        The smoothest near-horizontal plane over the stem at (x, y) with stem ``radius``,
        using points above ``base`` (m above ground) and more than radius + 10 cm from the
        axis. None when too little is overhead to tell.
        """
        if self.tree is None:
            return None
        # sorted: the result must not depend on the k-d tree's internal order (it did, and
        # borderline posts flipped when the index changed)
        idx = np.sort(np.asarray(self.tree.query_ball_point([x, y], RADIUS), dtype=np.int64))
        h = self.points[idx]
        d = np.hypot(h[:, 0] - x, h[:, 1] - y)
        sel = (h[:, 2] > base) & (h[:, 2] < ROOF_TOP) & (d > radius + 0.10)
        q = self.raw[idx][sel]  # planes in elevation
        height_offset = float(np.median(h[sel, 2] - q[:, 2])) if sel.any() else 0.0
        if len(q) < 100:
            return None
        rng = np.random.default_rng(seed)
        best, best_n, best_count = None, None, -1
        for _ in range(ROOF_TRIES):
            s = q[rng.choice(len(q), 3, replace=False)]
            n = np.cross(s[1] - s[0], s[2] - s[0])
            norm = np.linalg.norm(n)
            if norm < 1e-9:
                continue
            n = n / norm
            if abs(n[2]) < 0.8:  # roofs are within ~37 deg of horizontal
                continue
            dist = (q - s[0]) @ n
            inl = np.abs(dist) < 0.03
            if inl.sum() > best_count:
                best_count, best, best_n = int(inl.sum()), (s[0], dist), n
        if best is None:
            return None
        dist = best[1] * np.sign(best_n[2])  # positive = above the plane
        inl = np.abs(dist) < 0.03
        cells = np.unique(np.floor(q[inl, :2] / 0.2).astype(np.int64), axis=0)
        disc = np.pi * RADIUS**2 / 0.04
        return RoofCheck(
            plane_fraction=float(inl.mean()),
            span=float(len(cells) / disc),
            above_fraction=float(np.mean(dist > 0.5)),
            plane_height=float(np.median(q[inl, 2]) + height_offset),
            layer_fraction=float(inl.sum() / max(1, np.sum(np.abs(dist) < 0.5))),
        )

    def visible_height(self, x: float, y: float, radius: float) -> float:
        """
        Height (m above ground) of the top of what is seen directly above a stem: the 99th
        percentile within max(1 m, 2 x radius) of its axis. A vehicle lidar often cannot see
        crown tops, so this is a lower bound on tree height, not the height. NaN if nothing.
        """
        if self.tree is None:
            return float("nan")
        idx = self.tree.query_ball_point([x, y], max(1.0, 2.0 * radius))
        if len(idx) < 20:
            return float("nan")
        return float(np.percentile(self.points[idx, 2], 99))

    def column_profile(self, x: float, y: float, radius: float) -> tuple[float, float]:
        """
        (top, crown_share) of what is above 2.5 m within 3 m of a stem: the column's top
        (99th percentile of points within radius + 0.3 m of the axis) and the share of the
        material that lies away from the column (more than radius + 0.5 m out). A tree shows
        a crown (0.7-0.9 on the 2022-03-08 drive); a pole, a dead snag, or a tall trunk whose
        crown is above the scanner's view, a bare column (0.05). NaN when nothing is seen.
        """
        nan = float("nan")
        if self.tree is None:
            return nan, nan
        idx = self.tree.query_ball_point([x, y], 3.0)
        q = self.points[idx]
        q = q[q[:, 2] > 2.5]
        if len(q) < 20:
            return nan, nan
        d = np.hypot(q[:, 0] - x, q[:, 1] - y)
        r = max(radius, 0.05)
        col, crown = d < r + 0.3, d >= r + 0.5
        top = float(np.percentile(q[col, 2], 99)) if col.sum() >= 20 else nan
        return top, float(crown.sum() / max(1, crown.sum() + col.sum()))
