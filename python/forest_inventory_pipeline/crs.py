"""
Coordinate reference system and unit detection from LAS headers (no pyproj needed).

LAS files declare their CRS either as GeoTIFF keys (LAS <= 1.3, and 1.4 with point
formats 0-5) or as an OGC WKT string (LAS 1.4). From these we read:

- the EPSG code of the projected (or compound) CRS, if given
- the horizontal and vertical length units (metre, international foot, US survey foot)
- whether the CRS is geographic (longitude/latitude in degrees), which cannot be measured
  in metres directly and is rejected with a clear message
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# GeoTIFF key ids
_GT_MODEL_TYPE = 1024  # 1 = projected, 2 = geographic
_GEOGRAPHIC_TYPE = 2048
_PROJECTED_CS_TYPE = 3072
_PROJ_LINEAR_UNITS = 3076
_VERTICAL_CS_TYPE = 4096
_VERTICAL_UNITS = 4099

# EPSG unit-of-measure codes
_EPSG_UNITS = {9001: "m", 9002: "ft", 9003: "us_ft"}


@dataclass
class CrsInfo:
    crs: str | None = None  # "EPSG:xxxx"
    wkt: str | None = None
    units: str | None = None  # horizontal
    z_units: str | None = None
    geographic: bool = False


def _unit_from_factor(factor: float) -> str | None:
    if abs(factor - 1.0) < 1e-9:
        return "m"
    if abs(factor - 0.3048) < 1e-9:
        return "ft"
    if abs(factor - 1200.0 / 3937.0) < 1e-9:
        return "us_ft"
    return None


def _unit_from_name(name: str) -> str | None:
    n = name.lower().replace("_", " ")
    if "us survey" in n or "foot us" in n or "ftus" in n.replace(" ", ""):
        return "us_ft"
    if "foot" in n or "feet" in n:
        return "ft"
    if "metre" in n or "meter" in n:
        return "m"
    return None


def parse_wkt(wkt: str) -> CrsInfo:
    """Units and EPSG code from an OGC WKT1 or WKT2 string."""
    info = CrsInfo(wkt=wkt.strip().rstrip("\x00") or None)
    if not info.wkt:
        return info
    text = info.wkt
    head = text.lstrip().split("[", 1)[0].upper()
    info.geographic = head in ("GEOGCS", "GEOGCRS", "GEODCRS", "GEOGRAPHICCRS")

    # the CRS's own authority is the last top-level AUTHORITY / ID in the string
    codes = re.findall(r'(?:AUTHORITY|ID)\[\s*"EPSG"\s*,\s*"?(\d+)"?\s*\]', text)
    if codes:
        info.crs = f"EPSG:{codes[-1]}"

    # length units: UNIT["name", factor] (WKT1) or LENGTHUNIT["name", factor] (WKT2);
    # angular units (degree) are skipped. For compound CRSs the vertical part comes last.
    units = []
    for name, factor in re.findall(r'(?:LENGTHUNIT|UNIT)\[\s*"([^"]+)"\s*,\s*([0-9.eE+-]+)', text):
        if "degree" in name.lower() or "radian" in name.lower():
            continue
        u = _unit_from_factor(float(factor)) or _unit_from_name(name)
        if u:
            units.append(u)
    if units:
        if head in ("COMPD_CS", "COMPOUNDCRS"):
            info.units, info.z_units = units[0], units[-1]
        else:
            info.units = units[-1] if head in ("PROJCS", "PROJCRS") else units[0]
            info.z_units = info.units
    return info


def parse_geokeys(keys: dict[int, int]) -> CrsInfo:
    """Units and EPSG code from GeoTIFF keys {key_id: value} (SHORT values only)."""
    info = CrsInfo()
    if keys.get(_GT_MODEL_TYPE) == 2 or (
        _GEOGRAPHIC_TYPE in keys and _PROJECTED_CS_TYPE not in keys
    ):
        info.geographic = True
    proj = keys.get(_PROJECTED_CS_TYPE)
    if proj and proj not in (0, 32767):
        info.crs = f"EPSG:{proj}"
    info.units = _EPSG_UNITS.get(keys.get(_PROJ_LINEAR_UNITS, 0))
    info.z_units = _EPSG_UNITS.get(keys.get(_VERTICAL_UNITS, 0)) or info.units
    return info


def from_las_header(header) -> CrsInfo:
    """Read CRS/units from a laspy header (WKT preferred over GeoTIFF keys)."""
    vlrs = list(getattr(header, "vlrs", [])) + list(getattr(header, "evlrs", None) or [])
    wkt_info, key_info = None, None
    for vlr in vlrs:
        cls = type(vlr).__name__
        if cls == "WktCoordinateSystemVlr":
            s = getattr(vlr, "string", "") or ""
            if s.strip("\x00 "):
                wkt_info = parse_wkt(s)
        elif cls == "GeoKeyDirectoryVlr":
            keys = {}
            for k in getattr(vlr, "geo_keys", []):
                # tiff_tag_location 0 = value stored inline as SHORT
                if getattr(k, "tiff_tag_location", 0) == 0:
                    keys[int(k.id)] = int(k.value_offset)
            key_info = parse_geokeys(keys)
    if wkt_info and (wkt_info.units or wkt_info.crs):
        if key_info:  # fill gaps from the keys
            wkt_info.crs = wkt_info.crs or key_info.crs
            wkt_info.units = wkt_info.units or key_info.units
            wkt_info.z_units = wkt_info.z_units or key_info.z_units
        return wkt_info
    return key_info or CrsInfo()
