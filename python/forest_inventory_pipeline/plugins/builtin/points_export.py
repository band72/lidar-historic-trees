"""
Segmented point cloud export: every tree point with its tree id, at real elevation.

Use it to check the segmentation in CloudCompare, QGIS or any LAS viewer: colour by
``tree_id``, and compare against the DBH table. Contains the points of the height slice
that was segmented (``segment.min_z``..``max_z``), not the full input cloud.

- ``las`` / ``laz``: LAS 1.4, point format 6, extra dimensions ``tree_id`` (int32) and
  ``height_above_ground`` (float32); classification 5 (high vegetation); the input CRS
  (WKT) is written when known. ``laz`` needs the lazrs backend (included in installers).
- ``ply``: binary PLY with x, y, z (double), tree_id (int), height_above_ground (float).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

import numpy as np

from ...errors import ExportError
from ...records import TreeRecord
from ...units import from_metres
from ...utils import logger
from ..base import UNITS_OPTION, ExportContext, Exporter, OptionSpec, atomic_write, output_unit
from ..registry import register_exporter


@register_exporter
class PointsExporter(Exporter):
    name = "points"
    extension = ".laz"
    label = "Segmented points"
    description = "Tree points with tree_id and height above ground (LAS/LAZ/PLY) for checking the segmentation."
    mime = "application/octet-stream"
    options = (
        OptionSpec("format", "laz", "file format", choices=("laz", "las", "ply")),
        OptionSpec("include_failed", True, "also write points of trees that could not be fitted"),
        UNITS_OPTION,
    )

    def export(
        self, trees: Sequence[TreeRecord], path: Path, context: ExportContext, **options: Any
    ) -> Path:
        opts = self.resolve_options(options)
        unit = output_unit(opts, context)
        path = Path(path).with_suffix("." + opts["format"])
        chunks, ids, hag = [], [], []
        missing = 0
        for t in trees:
            if not (t.fitted or opts["include_failed"]):
                continue
            w = t.world_points()
            if w is None:
                missing += 1
                continue
            chunks.append(w)
            ids.append(np.full(len(w), t.tree_id, dtype=np.int32))
            hag.append(t.points[:, 2].astype(np.float32))
        if missing and not chunks:
            raise ExportError(
                "trees were fitted without keeping their points; the points export needs "
                "fit_trees(..., keep_points=True)"
            )
        xyz = np.concatenate(chunks) if chunks else np.empty((0, 3))
        tid = np.concatenate(ids) if ids else np.empty(0, dtype=np.int32)
        h = np.concatenate(hag) if hag else np.empty(0, dtype=np.float32)
        xyz = from_metres(xyz, unit)
        h = from_metres(h, unit).astype(np.float32)
        if opts["format"] == "ply":
            self._write_ply(path, xyz, tid, h)
        else:
            self._write_las(path, xyz, tid, h, context, unit)
        logger.info(
            f"segmented points written: {path} ({len(xyz):,} points, {len(set(tid.tolist()))} trees, units={unit})"
        )
        return path

    @staticmethod
    def _write_ply(path: Path, xyz, tid, hag) -> None:
        header = (
            "ply\nformat binary_little_endian 1.0\n"
            f"comment LiDAR Tree Inventory segmented points\nelement vertex {len(xyz)}\n"
            "property double x\nproperty double y\nproperty double z\n"
            "property int tree_id\nproperty float height_above_ground\nend_header\n"
        ).encode("ascii")
        rec = np.empty(
            len(xyz), dtype=[("x", "<f8"), ("y", "<f8"), ("z", "<f8"), ("t", "<i4"), ("h", "<f4")]
        )
        rec["x"], rec["y"], rec["z"], rec["t"], rec["h"] = xyz[:, 0], xyz[:, 1], xyz[:, 2], tid, hag
        with atomic_write(path, "wb") as fh:
            fh.write(header)
            fh.write(rec.tobytes())

    @staticmethod
    def _write_las(path: Path, xyz, tid, hag, context: ExportContext, unit: str) -> None:
        try:
            import laspy
        except ImportError as exc:
            raise ExportError(
                "LAS export needs laspy: pip install 'forest_inventory_pipeline[las]'"
            ) from exc
        header = laspy.LasHeader(point_format=6, version="1.4")
        header.add_extra_dims(
            [
                laspy.ExtraBytesParams(name="tree_id", type=np.int32, description="tree id"),
                laspy.ExtraBytesParams(
                    name="height_above_ground",
                    type=np.float32,
                    description=f"height above ground ({unit})",
                ),
            ]
        )
        scale = 0.001 if unit == "m" else 0.001 / 0.3048  # mm resolution either way
        header.scales = np.array([scale] * 3)
        header.offsets = np.floor(xyz.min(axis=0)) if len(xyz) else np.zeros(3)
        if context.crs_wkt:
            header.vlrs.append(laspy.vlrs.known.WktCoordinateSystemVlr(context.crs_wkt))
            header.global_encoding.wkt = True
        las = laspy.LasData(header)
        las.x, las.y, las.z = xyz[:, 0], xyz[:, 1], xyz[:, 2]
        las.classification = np.full(len(xyz), 5, dtype=np.uint8)
        las.tree_id = tid
        las.height_above_ground = hag
        laz = path.suffix.lower() == ".laz"
        try:
            with atomic_write(path, "wb") as fh:
                las.write(fh, do_compress=laz)
        except Exception as exc:
            if laz and "laz" in str(exc).lower():
                raise ExportError(
                    f"cannot write LAZ ({exc}); install lazrs or use format=las"
                ) from exc
            raise
