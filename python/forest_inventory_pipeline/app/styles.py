"""Display units and the status styles shared by the app's maps and panels."""

from __future__ import annotations

import pandas as pd

from forest_inventory_pipeline.records import (
    STATUS_ADDED,
    STATUS_EXCLUDED,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_WARNING,
)
from forest_inventory_pipeline.units import from_metres, is_feet, normalize_unit

# status palette (fixed) + marker shape + label: status is never carried by colour alone
STATUS_STYLE = {
    STATUS_OK: dict(color="#0ca30c", symbol="circle", label="✔ ok"),
    STATUS_WARNING: dict(color="#fab219", symbol="triangle-up", label="⚠ warning"),
    STATUS_FAILED: dict(color="#d03b3b", symbol="x", label="✖ failed"),
    STATUS_EXCLUDED: dict(color="#8a8a8a", symbol="circle-x-open", label="⊘ excluded"),
    STATUS_ADDED: dict(color="#2a78d6", symbol="cross", label="✚ added by reviewer"),
}
NOT_MEASURED = (STATUS_FAILED, STATUS_EXCLUDED, STATUS_ADDED)
#: map categories: statuses, with lower-bound DBH trees (seen over too little of the stem to
#: fix the diameter) split from the other warnings, so their DBH is not read as measured
LOWER_BOUND = "lower_bound"
CHECK = "check"  # flagged: possibly a pole/snag, or a shrub (see inventory.column_flag)
CATEGORY_STYLE = {
    STATUS_OK: STATUS_STYLE[STATUS_OK],
    STATUS_WARNING: STATUS_STYLE[STATUS_WARNING],
    LOWER_BOUND: dict(color="#fab219", symbol="triangle-up-open", label="◬ DBH lower bound"),
    CHECK: dict(color="#9a4fd0", symbol="diamond-open", label="◇ check: pole or shrub?"),
    **{k: v for k, v in STATUS_STYLE.items() if k not in (STATUS_OK, STATUS_WARNING)},
}


def map_category(df: pd.DataFrame) -> pd.Series:
    method = df["dbh_method"] if "dbh_method" in df else pd.Series("", index=df.index)
    lower = (df.status == STATUS_WARNING) & method.fillna("").str.startswith("visible width")
    msg = df["message"].fillna("") if "message" in df else pd.Series("", index=df.index)
    check = (df.status == STATUS_WARNING) & msg.str.contains("bare column|tall for a", regex=True)
    return df.status.where(~lower, LOWER_BOUND).where(~check, CHECK)


class DisplayUnits:
    """Converts internal metres to the unit chosen for display and export."""

    def __init__(self, unit: str = "m"):
        self.unit = normalize_unit(unit)
        self.feet = is_feet(self.unit)
        self.ul = "ft" if self.feet else "m"  # length label
        self.dl = "in" if self.feet else "cm"  # diameter label
        self.al = "ft²/ac" if self.feet else "m²/ha"  # basal area per area label

    def L(self, v):
        return from_metres(v, self.unit)

    def D(self, v_m):
        return v_m / 0.0254 if self.feet else v_m * 100.0

    def BA(self, m2_per_ha: float) -> float:
        return m2_per_ha * 4.356 if self.feet else m2_per_ha  # 1 m²/ha = 4.356 ft²/acre

SERIES_ESTIMATE = "#2a78d6"  # categorical slot 1
SERIES_TRUTH = "#eb6834"  # categorical slot 2
