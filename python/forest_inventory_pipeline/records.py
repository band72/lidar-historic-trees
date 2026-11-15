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
"""Per-tree result record shared by the pipeline, the app, exporters and plugins."""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Any

import numpy as np

STATUS_OK = "ok"
STATUS_WARNING = "warning"
STATUS_FAILED = "failed"


@dataclass
class TreeRecord:
    """
    One tree. All lengths in metres (the input cloud's unit).

    ``x``, ``y`` are the stem centre at breast height in the input coordinate frame.
    ``ground_z`` is the interpolated terrain elevation under the stem and
    ``bh_elevation = ground_z + breast_height`` is the absolute elevation of the DBH plane.
    For failed trees the fit fields are NaN and ``message`` says why.
    """

    tree_id: int
    status: str
    x: float
    y: float
    ground_z: float
    breast_height: float
    bh_elevation: float
    n_points: int
    dbh: float = float("nan")
    dbh_se: float = float("nan")
    ci_low: float = float("nan")
    ci_high: float = float("nan")
    confidence: float = float("nan")
    ci_method: str = ""
    n_band_points: int = 0
    inlier_ratio: float = float("nan")
    rmse: float = float("nan")
    arc_coverage: float = float("nan")
    max_gap_deg: float = float("nan")
    tilt_deg: float = float("nan")
    axis: tuple[float, float, float] = (0.0, 0.0, 1.0)
    message: str = ""
    extra: dict[str, Any] = field(default_factory=dict)
    # height-normalized points of this tree; never exported, used by metric plugins / viewer
    points: np.ndarray | None = field(default=None, repr=False, compare=False)

    @property
    def fitted(self) -> bool:
        return self.status != STATUS_FAILED and np.isfinite(self.dbh)

    def to_row(self) -> dict[str, Any]:
        """Flat dict for tables/CSV. Lengths in metres plus a convenience DBH in cm."""
        # not dataclasses.asdict: that would deep-copy the point array
        d = {f.name: getattr(self, f.name) for f in fields(self) if f.name != "points"}
        extra = dict(d.pop("extra"))
        ax = d.pop("axis")
        d["axis_x"], d["axis_y"], d["axis_z"] = ax
        d["dbh_cm"] = self.dbh * 100.0 if self.fitted else float("nan")
        d.update(extra)
        return d


# Stable column order for tabular exports; plugin metric columns are appended after these.
CORE_COLUMNS = [
    "tree_id",
    "status",
    "x",
    "y",
    "ground_z",
    "breast_height",
    "bh_elevation",
    "dbh",
    "dbh_cm",
    "dbh_se",
    "ci_low",
    "ci_high",
    "confidence",
    "ci_method",
    "n_points",
    "n_band_points",
    "inlier_ratio",
    "rmse",
    "arc_coverage",
    "max_gap_deg",
    "tilt_deg",
    "axis_x",
    "axis_y",
    "axis_z",
    "message",
]
