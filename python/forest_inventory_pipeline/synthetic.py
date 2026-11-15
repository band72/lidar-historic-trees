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
Synthetic forest plots with known ground truth, for demos and end-to-end tests.

Produces sloped, bumpy terrain, tapered and leaning stems with partial (occluded)
scan coverage, sensor noise, understory clutter and crown blobs. Absolute elevations
are offset by ``base_elevation`` so DXF/CSV exports can be checked for real-world z.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .errors import ParameterError


@dataclass
class SyntheticPlot:
    points: np.ndarray  # (N, 3) absolute coordinates
    instance: np.ndarray  # (N,) true tree id, -1 for ground/clutter
    is_ground: np.ndarray  # (N,) bool
    truth: list[dict]  # per tree: id, x, y, ground_z, dbh, lean_x, lean_y


def ground_height(x, y, slope=(0.08, -0.04), base_elevation=250.0):
    return base_elevation + slope[0] * x + slope[1] * y + 0.25 * np.sin(x / 4.0) * np.cos(y / 5.0)


def make_plot(
    n_trees: int = 25,
    size: float = 30.0,
    dbh_range: tuple[float, float] = (0.12, 0.70),
    point_spacing: float = 0.01,
    noise: float = 0.004,
    base_elevation: float = 250.0,
    seed: int = 0,
) -> SyntheticPlot:
    if n_trees < 1 or n_trees > 500:
        raise ParameterError(f"n_trees must be in [1, 500], got {n_trees}")
    if size < 5:
        raise ParameterError(f"size must be >= 5 m, got {size}")
    lo, hi = dbh_range
    if not (0.02 < lo < hi < 3.0):
        raise ParameterError(f"invalid dbh_range {dbh_range}")
    rng = np.random.default_rng(seed)

    # place trees with a minimum spacing so instances are separable
    positions: list[np.ndarray] = []
    attempts = 0
    while len(positions) < n_trees and attempts < 20000:
        attempts += 1
        p = rng.uniform(1.5, size - 1.5, size=2)
        if all(np.linalg.norm(p - q) > 2.5 for q in positions):
            positions.append(p)

    pts, inst, ground_flags, truth = [], [], [], []

    # ground: jittered grid
    g = np.arange(0, size, 0.15)
    gx, gy = np.meshgrid(g, g)
    gx = gx.ravel() + rng.uniform(-0.05, 0.05, gx.size)
    gy = gy.ravel() + rng.uniform(-0.05, 0.05, gy.size)
    gz = ground_height(gx, gy, base_elevation=base_elevation) + rng.normal(0, 0.01, gx.size)
    pts.append(np.c_[gx, gy, gz])
    inst.append(np.full(gx.size, -1))
    ground_flags.append(np.ones(gx.size, dtype=bool))

    for tid, (x, y) in enumerate(positions):
        dbh = rng.uniform(lo, hi)
        r_bh = dbh / 2
        lean = rng.normal(0, 0.03, size=2)
        gz0 = float(ground_height(x, y, base_elevation=base_elevation))
        height = rng.uniform(6, 14)
        # visible arc: occlusion leaves 1-3 visible sectors
        n_vis = rng.integers(1, 4)
        centers = rng.uniform(0, 2 * np.pi, n_vis)
        widths = rng.uniform(np.pi / 2, 1.6 * np.pi, n_vis)
        z = np.arange(0.0, height, point_spacing * 2)
        circumference = 2 * np.pi * max(r_bh, 0.05)
        n_around = max(int(circumference / point_spacing), 24)
        th = np.linspace(0, 2 * np.pi, n_around, endpoint=False)
        Z, T = np.meshgrid(z, th)
        Z, T = Z.ravel(), T.ravel()
        vis = np.zeros(T.size, dtype=bool)
        for c, w in zip(centers, widths):
            d = np.angle(np.exp(1j * (T - c)))
            vis |= np.abs(d) < w / 2
        Z, T = Z[vis], T[vis]
        # taper (~1 cm per m) and butt swell below 0.5 m
        r = r_bh - 0.005 * (Z - 1.3) + 0.15 * r_bh * np.exp(-Z / 0.3)
        r = np.maximum(r, 0.02)
        sx = x + lean[0] * (Z - 1.3) + r * np.cos(T) + rng.normal(0, noise, Z.size)
        sy = y + lean[1] * (Z - 1.3) + r * np.sin(T) + rng.normal(0, noise, Z.size)
        sz = ground_height(x, y, base_elevation=base_elevation) + Z
        keep = rng.random(Z.size) < 0.5  # thin
        pts.append(np.c_[sx, sy, sz][keep])
        inst.append(np.full(int(keep.sum()), tid))
        ground_flags.append(np.zeros(int(keep.sum()), dtype=bool))
        # crown blob
        nc = 1500
        cz = rng.uniform(height * 0.6, height, nc)
        cr = rng.uniform(0, 2.0, nc)
        ca = rng.uniform(0, 2 * np.pi, nc)
        pts.append(np.c_[x + cr * np.cos(ca), y + cr * np.sin(ca), gz0 + cz])
        inst.append(np.full(nc, tid))
        ground_flags.append(np.zeros(nc, dtype=bool))
        truth.append(
            dict(
                id=tid,
                x=float(x),
                y=float(y),
                ground_z=gz0,
                dbh=float(dbh),
                lean_x=float(lean[0]),
                lean_y=float(lean[1]),
            )
        )

    # understory clutter (0-0.8 m), mostly below the breast-height band
    nu = int(size * size * 2)
    ux, uy = rng.uniform(0, size, nu), rng.uniform(0, size, nu)
    uz = ground_height(ux, uy, base_elevation=base_elevation) + rng.uniform(0.05, 0.8, nu)
    pts.append(np.c_[ux, uy, uz])
    inst.append(np.full(nu, -1))
    ground_flags.append(np.zeros(nu, dtype=bool))

    return SyntheticPlot(
        points=np.concatenate(pts).astype(np.float64),
        instance=np.concatenate(inst).astype(np.int32),
        is_ground=np.concatenate(ground_flags),
        truth=truth,
    )
