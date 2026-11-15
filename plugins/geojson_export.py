"""
Example plugin: GeoJSON export of tree positions with DBH and CI.

Load it with ``--plugin-dir plugins`` (CLI and app) or ``FIP_PLUGIN_PATH=plugins``.
Coordinates are written as-is; set ``crs`` if your cloud is georeferenced
(e.g. ``-E geojson.crs=EPSG:25832``).
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from forest_inventory_pipeline.plugins import (
    Exporter,
    OptionSpec,
    atomic_write,
    register_exporter,
)


def _num(v: float):
    return None if v is None or (isinstance(v, float) and math.isnan(v)) else v


@register_exporter
class GeoJsonExporter(Exporter):
    name = "geojson"
    extension = ".geojson"
    label = "GeoJSON points"
    description = "Tree positions as GeoJSON points (x, y, DBH-plane elevation) with DBH and CI."
    mime = "application/geo+json"
    options = (
        OptionSpec("crs", "", "CRS name to record, e.g. EPSG:25832 (empty = none)"),
        OptionSpec("include_failed", False, "include trees that could not be fitted"),
    )

    def export(self, trees, path: Path, context, **options) -> Path:
        opts = self.resolve_options(options)
        features = []
        for t in trees:
            if not (t.fitted or opts["include_failed"]):
                continue
            row = t.to_row()
            props = {
                k: _num(row[k])
                for k in ("tree_id", "status", "dbh", "ci_low", "ci_high", "confidence", "ground_z", "message")
            }
            props.update({k: _num(v) for k, v in t.extra.items()})
            features.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [t.x, t.y, t.bh_elevation]},
                "properties": props,
            })
        doc = {"type": "FeatureCollection", "features": features}
        if opts["crs"]:
            doc["crs"] = {"type": "name", "properties": {"name": opts["crs"]}}
        with atomic_write(Path(path), "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=1)
        return Path(path)
