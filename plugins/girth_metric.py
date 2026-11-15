"""
Example plugin: stem girth (circumference at breast height), as foresters often record it.

Load it with ``--plugin-dir plugins`` (CLI and app) or ``FIP_PLUGIN_PATH=plugins``. The
columns appear in the CSV, GeoJSON and the app table like any built-in column.
"""

from __future__ import annotations

import math

from forest_inventory_pipeline.plugins import TreeMetric, register_metric


@register_metric
class Girth(TreeMetric):
    name = "girth"
    description = "Stem circumference at breast height (pi * DBH), with CI bounds"
    columns = {
        "girth_cm": "circumference at breast height (cm)",
        "girth_ci_low_cm": "lower CI bound (cm)",
        "girth_ci_high_cm": "upper CI bound (cm)",
    }

    def compute(self, tree):
        return {
            "girth_cm": math.pi * tree.dbh * 100,
            "girth_ci_low_cm": math.pi * tree.ci_low * 100,
            "girth_ci_high_cm": math.pi * tree.ci_high * 100,
        }
