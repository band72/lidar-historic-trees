"""Why objects were rejected: counts by reason, each with the setting to try."""

from __future__ import annotations

from typing import Sequence

from .records import STATUS_FAILED, TreeRecord

#: rejection reasons: (reason, words in the message, the setting to try)
REJECTION_REASONS = (
    ("under a roof (post)", "under a roof", "Reject posts under a roof (Trunk check)"),
    ("bare column rejected (pole?)", "rejected: bare column", "Pole and shrub checks"),
    ("short object rejected (shrub?)", "rejected: only", "Pole and shrub checks"),
    ("lean beyond the limit", "tilt",
     "Max stem tilt (Fitting details). In a reviewed sample these were hedges, shrubs and "
     "foliage, or trees whose trunk is hidden at breast height: keep those with 'This is a tree'"),
    ("too few points at breast height", "in the breast-height band", "Band half-width; Min points per tree"),
    ("fitted DBH outside the limits", "fitted radius", "Max DBH (Fitting details)"),
    ("below the minimum DBH", "below the minimum", "Minimum DBH (Trunk check)"),
    ("too little of the cluster on the trunk", "lie on the trunk surface", "Share of points on the trunk (Trunk check)"),
    ("trunk not continuous", "continuous over", "Trunk checked up to (Trunk check)"),
    ("filled inside (foliage)", "fill the inside", "Max. share of points filling the inside (Trunk check)"),
    ("too low (shrub or stump)", "only reaches", "Must reach above breast height (Trunk check)"),
    ("point density too low", "points per m^2", "Min. points per m² (Trunk check), or the Scanner profile"),
    ("few breast-height points on the circle", "breast-height points on the fitted surface", "Min. points per m²; the cluster may be clutter"),
    ("seen from one side, cluttered", "seen from one side", "none: rejected as clutter, structures or several stems"),
    ("flat strip (wall, fence)", "strip is flat", "none: not a stem"),
    ("no circle found", "no circle", "Max DBH; Inlier tolerance"),
)


def rejection_summary(records: Sequence[TreeRecord]) -> list[dict]:
    """
    Why objects were rejected (status ``failed``): one row per reason, most common first:
    ``{"reason", "count", "setting"}``. An object rejected for several reasons counts under
    each; a message matching no known reason counts as "other".
    """
    counts: dict[str, int] = {}
    for r in records:
        if r.status != STATUS_FAILED:
            continue
        hit = [name for name, words, _ in REJECTION_REASONS if words in r.message]
        if hit and hit[0] in ("bare column rejected (pole?)", "short object rejected (shrub?)"):
            hit = hit[:1]  # "rejected: only 2.6 m tall" is not also "only reaches"
        for name in hit or ["other"]:
            counts[name] = counts.get(name, 0) + 1
    setting = {name: s for name, _, s in REJECTION_REASONS} | {"other": "see the message in the table"}
    return [
        {"reason": k, "count": v, "setting": setting[k]}
        for k, v in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
