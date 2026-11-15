"""
GeoJSON export for GIS (QGIS, ArcGIS, web maps).

``geometry=point`` writes one point per tree at (x, y, DBH-plane elevation);
``geometry=circle`` writes the DBH cross-section as a polygon, so stems draw to scale.
The CRS of the input file is recorded when known (or set with ``crs``). Note that
RFC 7946 GeoJSON assumes WGS84 longitude/latitude; projected coordinates (UTM, State
Plane) are written as-is with a legacy ``crs`` member, which QGIS and GDAL honour.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Sequence

from ...records import TreeRecord
from ...units import from_metres
from ...utils import logger
from ..base import UNITS_OPTION, ExportContext, Exporter, OptionSpec, atomic_write, output_unit
from ..registry import register_exporter

_PROPS = (
    "tree_id",
    "status",
    "length_unit",
    "dbh",
    "dbh_cm",
    "dbh_in",
    "ci_low",
    "ci_high",
    "dbh_se",
    "confidence",
    "ground_z",
    "bh_elevation",
    "arc_coverage",
    "tilt_deg",
    "message",
)


def _num(v):
    if isinstance(v, float) and not math.isfinite(v):
        return None
    return v


@register_exporter
class GeoJsonExporter(Exporter):
    name = "geojson"
    extension = ".geojson"
    label = "GeoJSON"
    description = "Trees for GIS: points or DBH-circle polygons with DBH, CI and status."
    mime = "application/geo+json"
    options = (
        OptionSpec(
            "geometry",
            "point",
            "point per tree, or the DBH circle as a polygon",
            choices=("point", "circle"),
        ),
        OptionSpec("crs", "", "CRS to record, e.g. EPSG:25832 (empty = from the input file)"),
        OptionSpec("include_failed", False, "include trees that could not be fitted (points only)"),
        OptionSpec("circle_vertices", 36, "polygon vertices per DBH circle", min=8, max=360),
        UNITS_OPTION,
    )

    def export(
        self, trees: Sequence[TreeRecord], path: Path, context: ExportContext, **options: Any
    ) -> Path:
        opts = self.resolve_options(options)
        unit = output_unit(opts, context)
        crs = opts["crs"] or context.crs
        features = []
        for t in trees:
            if not t.fitted and (opts["geometry"] == "circle" or not opts["include_failed"]):
                continue
            row = t.to_row(unit)
            props = {k: _num(row.get(k)) for k in _PROPS}
            props.update({k: _num(v) for k, v in t.extra.items()})
            x, y, z = row["x"], row["y"], row["bh_elevation"]
            if opts["geometry"] == "point":
                geom = {"type": "Point", "coordinates": [x, y, _num(z)]}
            else:
                r = float(from_metres(t.dbh / 2, unit))
                n = int(opts["circle_vertices"])
                ring = [
                    [x + r * math.cos(2 * math.pi * i / n), y + r * math.sin(2 * math.pi * i / n)]
                    for i in range(n)
                ]
                ring.append(ring[0])
                geom = {"type": "Polygon", "coordinates": [ring]}
            features.append(
                {"type": "Feature", "id": t.tree_id, "geometry": geom, "properties": props}
            )
        doc: dict[str, Any] = {"type": "FeatureCollection", "features": features}
        if crs:
            name = (
                crs
                if not crs.upper().startswith("EPSG:")
                else f"urn:ogc:def:crs:EPSG::{crs.split(':', 1)[1]}"
            )
            doc["crs"] = {"type": "name", "properties": {"name": name}}
        with atomic_write(Path(path), "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=1)
        logger.info(
            f"GeoJSON written: {path} ({len(features)} {opts['geometry']}s, units={unit}, crs={crs or 'none'})"
        )
        return Path(path)
