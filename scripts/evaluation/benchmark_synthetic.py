"""
Accuracy benchmark on synthetic plots with known truth. Exits 1 on a regression.

    python scripts/evaluation/benchmark_synthetic.py [--plots 3] [--trees 20] [--method analytic|bootstrap]

Baseline (2026-11-14, defaults): 60/60 trees found, DBH RMSE 0.23 cm, bias -0.09 cm,
95% CI coverage 60/60, no false detections, ground elevation error < 1 cm (median).
Used by .github/workflows/weekly-health.yml to catch regressions from dependency updates.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import sys

import numpy as np

from forest_inventory_pipeline import inventory as inv
from forest_inventory_pipeline import synthetic
from forest_inventory_pipeline.traits.dbh.robust_fit import FitParams

# regression thresholds
MIN_FOUND = 0.95
MAX_RMSE_CM = 0.5
MAX_ABS_BIAS_CM = 0.3
MIN_COVERAGE = 0.90
MAX_FALSE_POSITIVES = 0
MAX_GROUND_ERR_M = 0.05


def run(plots: int, trees: int, method: str) -> dict:
    err, covered, gz, found, total, false_pos = [], 0, [], 0, 0, 0
    for seed in range(plots):
        plot = synthetic.make_plot(n_trees=trees, seed=seed)
        gp, sp, fp = inv.GroundParams(), inv.SegmentParams(), FitParams(ci_method=method)
        with contextlib.redirect_stdout(io.StringIO()):  # quickshift++ prints progress bars
            ground, non_ground = inv.segment_ground(plot.points, gp)
            normalized = inv.normalize_heights(ground, non_ground, gp)
            pts, labels = inv.segment_trees(normalized, sp)
        recs = inv.fit_trees(inv.group_trees(pts, labels, sp.min_tree_points), ground, fp, gp)
        truth = np.array([[t["x"], t["y"], t["dbh"], t["ground_z"]] for t in plot.truth])
        hit = set()
        for r in (r for r in recs if r.fitted):
            d = np.hypot(truth[:, 0] - r.x, truth[:, 1] - r.y)
            j = int(d.argmin())
            if d[j] > 0.5:
                false_pos += 1
                continue
            hit.add(j)
            err.append(r.dbh - truth[j, 2])
            covered += r.ci_low <= truth[j, 2] <= r.ci_high
            gz.append(r.ground_z - truth[j, 3])
        found += len(hit)
        total += len(truth)
    e = np.array(err)
    return dict(
        found=found,
        total=total,
        false_positives=false_pos,
        matched=len(e),
        rmse_cm=float(np.sqrt(np.mean(e**2)) * 100),
        bias_cm=float(e.mean() * 100),
        coverage=covered / max(len(e), 1),
        ground_err_m=float(np.median(np.abs(gz))),
    )


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--plots", type=int, default=3)
    ap.add_argument("--trees", type=int, default=20)
    ap.add_argument("--method", choices=("analytic", "bootstrap"), default="analytic")
    a = ap.parse_args()
    m = run(a.plots, a.trees, a.method)
    print(
        f"{a.method}: found {m['found']}/{m['total']}  false positives {m['false_positives']}  "
        f"RMSE {m['rmse_cm']:.2f} cm  bias {m['bias_cm']:+.2f} cm  "
        f"CI coverage {m['coverage']:.0%}  ground error {m['ground_err_m'] * 100:.1f} cm"
    )
    problems = []
    if m["found"] < MIN_FOUND * m["total"]:
        problems.append(f"found {m['found']}/{m['total']} trees (need >= {MIN_FOUND:.0%})")
    if m["false_positives"] > MAX_FALSE_POSITIVES:
        problems.append(f"{m['false_positives']} false detections")
    if m["rmse_cm"] > MAX_RMSE_CM:
        problems.append(f"RMSE {m['rmse_cm']:.2f} cm > {MAX_RMSE_CM}")
    if abs(m["bias_cm"]) > MAX_ABS_BIAS_CM:
        problems.append(f"bias {m['bias_cm']:+.2f} cm beyond +/-{MAX_ABS_BIAS_CM}")
    if m["coverage"] < MIN_COVERAGE:
        problems.append(f"CI coverage {m['coverage']:.0%} < {MIN_COVERAGE:.0%}")
    if m["ground_err_m"] > MAX_GROUND_ERR_M:
        problems.append(f"ground error {m['ground_err_m']:.3f} m > {MAX_GROUND_ERR_M}")
    if problems:
        print("REGRESSION: " + "; ".join(problems))
        return 1
    print("OK: within thresholds")
    return 0


if __name__ == "__main__":
    sys.exit(main())
