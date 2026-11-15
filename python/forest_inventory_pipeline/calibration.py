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
    if dbh_unit == "cm":
        ref = ref.copy()
        ref[:, 2] /= 100.0
    elif dbh_unit != "m":
        raise ParameterError(f"dbh_unit must be 'auto', 'm' or 'cm', got {dbh_unit!r}")
    return ref


def calibrate(
    records: Sequence[TreeRecord],
    reference: np.ndarray,
    current_model_sd: float,
    max_distance: float = 0.5,
) -> CalibrationResult:
    """Match fitted trees to reference trees and estimate ``model_sd``."""
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
    return CalibrationResult(
        n_matched=len(best),
        n_reference=len(ref),
        bias=float(resid.mean()),
        rmse=float(np.sqrt(np.mean(resid**2))),
        coverage=float(np.mean((refd >= lo) & (refd <= hi))),
        model_sd=float(np.sqrt(model_var)),
        matches=[(r.tree_id, r.dbh, float(ref[j, 2])) for j, (_, r) in best.items()],
    )
