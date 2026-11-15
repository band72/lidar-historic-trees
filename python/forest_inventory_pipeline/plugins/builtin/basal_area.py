"""Basal area metric: cross-sectional stem area at breast height, with CI bounds."""

from __future__ import annotations

import math

from ...records import TreeRecord
from ..base import TreeMetric
from ..registry import register_metric


@register_metric
class BasalArea(TreeMetric):
    name = "basal_area"
    description = "Basal area pi*(DBH/2)^2 in m^2; CI bounds follow from the DBH CI (monotonic)."
    columns = {
        "basal_area_m2": "basal area at breast height (m^2)",
        "basal_area_ci_low_m2": "lower CI bound of basal area (m^2)",
        "basal_area_ci_high_m2": "upper CI bound of basal area (m^2)",
    }

    def compute(self, tree: TreeRecord) -> dict[str, float]:
        def ba(d: float) -> float:
            return math.pi * (d / 2.0) ** 2 if math.isfinite(d) else float("nan")

        return {
            "basal_area_m2": ba(tree.dbh),
            "basal_area_ci_low_m2": ba(tree.ci_low),
            "basal_area_ci_high_m2": ba(tree.ci_high),
        }
