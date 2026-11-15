"""
Length units. The pipeline works in metres internally; these helpers convert at the edges
(reading point clouds, writing exports, user-facing values).

- ``m``     metre
- ``ft``    international foot = 0.3048 m exactly
- ``us_ft`` US survey foot = 1200/3937 m (used by many US State Plane coordinate systems)
"""

from __future__ import annotations

import numpy as np

from .errors import ParameterError

METRES_PER_UNIT: dict[str, float] = {
    "m": 1.0,
    "ft": 0.3048,
    "us_ft": 1200.0 / 3937.0,
}

LABELS = {"m": "m", "ft": "ft", "us_ft": "US survey ft"}
SHORT = {"m": "m", "ft": "ft", "us_ft": "ft"}

#: DXF $INSUNITS codes
DXF_INSUNITS = {"m": 6, "ft": 2, "us_ft": 21}

#: US breast height (4.5 ft) in metres
US_BREAST_HEIGHT_M = 4.5 * 0.3048

_ALIASES = {
    "m": "m",
    "meter": "m",
    "meters": "m",
    "metre": "m",
    "metres": "m",
    "ft": "ft",
    "foot": "ft",
    "feet": "ft",
    "intl_ft": "ft",
    "international_ft": "ft",
    "us_ft": "us_ft",
    "us-ft": "us_ft",
    "usft": "us_ft",
    "us_survey_ft": "us_ft",
    "us-survey-ft": "us_ft",
    "survey_ft": "us_ft",
    "us_survey_foot": "us_ft",
}


def normalize_unit(unit: str) -> str:
    """Canonical unit name (``m``, ``ft`` or ``us_ft``) from common spellings."""
    key = str(unit).strip().lower().replace(" ", "_")
    try:
        return _ALIASES[key]
    except KeyError:
        raise ParameterError(f"unknown length unit {unit!r}; use m, ft or us_ft") from None


def is_feet(unit: str) -> bool:
    return normalize_unit(unit) in ("ft", "us_ft")


def to_metres(value, unit: str):
    """Convert a scalar or array from ``unit`` to metres."""
    f = METRES_PER_UNIT[normalize_unit(unit)]
    return value * f if f != 1.0 else value


def from_metres(value, unit: str):
    """Convert a scalar or array from metres to ``unit``."""
    f = METRES_PER_UNIT[normalize_unit(unit)]
    return value / f if f != 1.0 else value


def guess_unit_hint(points: np.ndarray) -> str | None:
    """
    A warning when the cloud's size suggests it is not in metres, else None.

    Typical stems are 0.1-1 m thick and trees 5-50 m tall; a cloud in feet is ~3.3x
    larger and one in millimetres ~1000x.
    """
    extent = points.max(axis=0) - points.min(axis=0)
    height = float(extent[2])
    if height > 2000:
        return f"vertical extent {height:.0f} units: millimetres? convert to metres first"
    if 60 < height <= 250:
        return (
            f"vertical extent {height:.0f} units is tall for trees in metres; "
            "if the cloud is in feet, set units to ft (or us_ft)"
        )
    return None
