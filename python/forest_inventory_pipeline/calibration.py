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
Calibrate the systematic DBH uncertainty (``FitParams.model_sd``) against field data.

Given trees measured by tape (x, y, dbh), estimated trees are matched by nearest stem
position. The residual variance not explained by each tree's statistical standard error
is attributed to model error:

    model_sd^2 = max(0, var(residual) - mean(se_stat^2))

Re-running the fit with that ``model_sd`` makes the reported intervals reach nominal
coverage on data like the reference plot.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import numpy as np
from scipy.spatial import KDTree

from .errors import InputDataError, ParameterError
from .records import TreeRecord


@dataclass
class CalibrationResult:
    n_matched: int
    n_reference: int
    bias: float
    rmse: float
    coverage: float
    model_sd: float
    matches: list[tuple[int, float, float]]  # (tree_id, estimated dbh, reference dbh)
    #: standard error of the bias, and whether the bias is clearly more than noise (> 2 se)
    bias_se: float = float("nan")
    bias_significant: bool = False
    #: fit.dbh_offset to use: the current one minus the bias when significant (else the
    #: current one), capped at +/- 2 cm (offset_capped says when the cap was hit)
    suggested_dbh_offset: float = 0.0
    offset_capped: bool = False
    #: how the error changes with tree size (m per 10 cm of DBH), and whether that trend is
    #: real: then a constant offset over-corrects small trees and under-corrects big ones
    bias_per_10cm: float = 0.0
    bias_grows_with_dbh: bool = False


def read_reference_csv(path: str | Path, dbh_unit: str = "auto") -> np.ndarray:
    """Read columns x, y, dbh (case-insensitive) into an (N, 3) array with DBH in metres."""
    path = Path(path)
    if not path.exists():
        raise InputDataError(f"reference file not found: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            raise InputDataError(f"{path.name}: empty file")
        cols = {c.strip().lower(): c for c in reader.fieldnames}
        missing = [c for c in ("x", "y", "dbh") if c not in cols]
        if missing:
            raise InputDataError(
                f"{path.name}: missing column(s) {missing}; found {list(reader.fieldnames)}"
            )
        rows = []
        for i, row in enumerate(reader, start=2):
            try:
                rows.append([float(row[cols["x"]]), float(row[cols["y"]]), float(row[cols["dbh"]])])
            except (TypeError, ValueError):
                raise InputDataError(f"{path.name}: line {i} has a non-numeric x/y/dbh") from None
    if not rows:
        raise InputDataError(f"{path.name}: no reference trees")
    ref = np.asarray(rows)
    return _to_metres(ref, dbh_unit)


def _to_metres(ref: np.ndarray, dbh_unit: str) -> np.ndarray:
    if dbh_unit == "auto":
        dbh_unit = "cm" if np.median(ref[:, 2]) > 3.0 else "m"
    scale = {"m": 1.0, "cm": 0.01, "in": 0.0254, "ft": 0.3048}.get(dbh_unit)
    if scale is None:
        raise ParameterError(f"dbh_unit must be 'auto', 'm', 'cm', 'in' or 'ft', got {dbh_unit!r}")
    ref = ref.copy()
    ref[:, 2] *= scale
    return ref


def calibrate(
    records: Sequence[TreeRecord],
    reference: np.ndarray,
    current_model_sd: float,
    max_distance: float = 0.5,
    current_dbh_offset: float = 0.0,
) -> CalibrationResult:
    """
    Match fitted trees to reference trees; estimate ``model_sd``, the bias, the
    ``dbh_offset`` that removes it, and whether the bias depends on tree size.

    :param current_dbh_offset: the fit.dbh_offset the estimates were made with (m).
    """
    if max_distance <= 0:
        raise ParameterError("max_distance must be > 0")
    fitted = [r for r in records if r.fitted]
    if not fitted:
        raise InputDataError("no fitted trees to calibrate against")
    ref = np.asarray(reference, dtype=float)
    tree = KDTree(ref[:, :2])
    dist, idx = tree.query(np.array([[r.x, r.y] for r in fitted]), k=1)
    # one-to-one: each reference tree keeps only its closest estimate
    best: dict[int, tuple[float, TreeRecord]] = {}
    for r, d, j in zip(fitted, dist, idx):
        if d <= max_distance and (j not in best or d < best[j][0]):
            best[int(j)] = (float(d), r)
    if len(best) < 3:
        raise InputDataError(
            f"only {len(best)} trees matched within {max_distance} m; check that both use the same coordinate frame"
        )
    est = np.array([r.dbh for _, r in best.values()])
    refd = np.array([ref[j, 2] for j in best])
    lo = np.array([r.ci_low for _, r in best.values()])
    hi = np.array([r.ci_high for _, r in best.values()])
    se_total = np.array([r.dbh_se for _, r in best.values()])
    se_stat2 = np.maximum(se_total**2 - current_model_sd**2, 0.0)
    resid = est - refd
    model_var = max(0.0, float(np.var(resid, ddof=1)) - float(np.mean(se_stat2)))
    n = len(resid)
    bias = float(resid.mean())
    bias_se = float(np.std(resid, ddof=1) / np.sqrt(n))
    significant = abs(bias) > 2 * bias_se
    want = current_dbh_offset - bias if significant else current_dbh_offset
    suggested = float(np.clip(want, -0.02, 0.02))
    # error against tree size: slope of the residuals on the tape DBH, with its standard error
    slope, slope_se = 0.0, float("inf")
    if n >= 5 and np.ptp(refd) > 0.05:
        A = np.c_[refd - refd.mean(), np.ones(n)]
        coef, *_ = np.linalg.lstsq(A, resid, rcond=None)
        r = resid - A @ coef
        slope = float(coef[0])
        slope_se = float(np.sqrt(np.sum(r**2) / (n - 2) / np.sum((refd - refd.mean()) ** 2)))
    grows = abs(slope) > 2 * slope_se and abs(slope * 0.1) > 0.002
    return CalibrationResult(
        n_matched=len(best),
        n_reference=len(ref),
        bias=bias,
        rmse=float(np.sqrt(np.mean(resid**2))),
        coverage=float(np.mean((refd >= lo) & (refd <= hi))),
        model_sd=float(np.sqrt(model_var)),
        matches=[(r.tree_id, r.dbh, float(ref[j, 2])) for j, (_, r) in best.items()],
        bias_se=bias_se,
        bias_significant=bool(significant),
        suggested_dbh_offset=suggested,
        offset_capped=bool(significant and abs(want) > 0.02 + 1e-12),
        bias_per_10cm=slope * 0.1,
        bias_grows_with_dbh=bool(grows),
    )
