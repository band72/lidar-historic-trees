"""
DXF export: each tree's DBH as a circle lying in a horizontal plane.

``z_mode`` picks the plane's z:

- ``elevation``: absolute elevation of breast height (ground_z + breast_height), i.e. the
  real-world height of the measurement - for CAD/GIS overlays with terrain
- ``height``: breast height above ground (e.g. 1.3) for every tree
- ``plan``: z = 0, a flat 2D plan

Layers: FIP_DBH (ok trees), FIP_DBH_WARN (trees with quality warnings), FIP_CI (dashed
CI-bound circles), FIP_CENTER (stem centre points), FIP_LABEL (text), FIP_AXIS (ground ->
breast height stem line, elevation mode only). Each DBH circle carries XDATA under the
application id ``FIP`` with tree id, DBH, CI and status (in drawing units).
Drawing units follow the ``units`` option: metres (labels in cm) or feet (labels in inches;
``us_ft`` sets $INSUNITS to US survey feet).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from ...errors import ExportError
from ...records import STATUS_WARNING, TreeRecord
from ...utils import logger
from ...units import DXF_INSUNITS, from_metres, is_feet
from ..base import UNITS_OPTION, ExportContext, Exporter, OptionSpec, atomic_write, output_unit
from ..registry import register_exporter

LAYERS = {
    "FIP_DBH": dict(color=3),
    "FIP_DBH_WARN": dict(color=2),
    "FIP_CI": dict(color=4, linetype="DASHED"),
    "FIP_CENTER": dict(color=7),
    "FIP_LABEL": dict(color=7),
    "FIP_AXIS": dict(color=8),
}
APPID = "FIP"


@register_exporter
class DxfExporter(Exporter):
    name = "dxf"
    extension = ".dxf"
    label = "DXF drawing"
    description = (
        "DBH circles on a horizontal plane at breast-height elevation, with CI rings and labels."
    )
    mime = "application/dxf"
    options = (
        OptionSpec(
            "z_mode",
            "elevation",
            "plane height: absolute elevation, height above ground, or flat plan",
            choices=("elevation", "height", "plan"),
        ),
        OptionSpec("include_ci", True, "draw dashed circles at the CI bounds"),
        OptionSpec("include_labels", True, "write a text label per tree"),
        OptionSpec(
            "include_axis", True, "draw the stem line from ground to breast height (elevation mode)"
        ),
        OptionSpec(
            "text_height",
            0.25,
            "label text height in metres (converted to drawing units)",
            min=0.01,
            max=10.0,
        ),
        UNITS_OPTION,
    )

    def export(
        self, trees: Sequence[TreeRecord], path: Path, context: ExportContext, **options: Any
    ) -> Path:
        try:
            import ezdxf
        except ImportError as exc:
            raise ExportError(
                "DXF export needs ezdxf: pip install 'forest_inventory_pipeline[dxf]'"
            ) from exc

        opts = self.resolve_options(options)
        unit = output_unit(opts, context)
        feet = is_feet(unit)

        def L(v: float) -> float:  # metres -> drawing units
            return float(from_metres(v, unit))

        fitted = [t for t in trees if t.fitted]
        if not fitted:
            logger.warning("DXF export: no fitted trees; writing an empty drawing")

        doc = ezdxf.new("R2013", setup=True)
        doc.header["$INSUNITS"] = DXF_INSUNITS[unit]
        doc.header["$MEASUREMENT"] = 0 if feet else 1  # 0 = imperial, 1 = metric
        doc.appids.add(APPID)
        for name, attrs in LAYERS.items():
            if name not in doc.layers:
                doc.layers.add(name, **attrs)
        msp = doc.modelspace()

        for t in fitted:
            z = L(self._plane_z(t, opts["z_mode"]))
            x, y = L(t.x), L(t.y)
            center = (x, y, z)
            layer = "FIP_DBH_WARN" if t.status == STATUS_WARNING else "FIP_DBH"
            circle = msp.add_circle(center, radius=L(t.dbh) / 2.0, dxfattribs={"layer": layer})
            circle.set_xdata(
                APPID,
                [
                    (1000, f"tree_id={t.tree_id}"),
                    (1040, L(t.dbh)),
                    (1040, L(t.ci_low)),
                    (1040, L(t.ci_high)),
                    (1000, f"status={t.status}"),
                    (1000, f"ci={t.confidence:.0%} {t.ci_method}"),
                    (1000, f"units={unit}"),
                ],
            )
            msp.add_point(center, dxfattribs={"layer": "FIP_CENTER"})
            if opts["include_ci"]:
                for bound in (t.ci_low, t.ci_high):
                    if bound > 0 and abs(bound - t.dbh) > 1e-6:
                        msp.add_circle(
                            center, radius=L(bound) / 2.0, dxfattribs={"layer": "FIP_CI"}
                        )
            if opts["include_labels"]:
                if feet:
                    s_, u_ = 1 / 0.0254, "in"
                else:
                    s_, u_ = 100.0, "cm"
                label = (
                    f"T{t.tree_id} {t.dbh * s_:.1f}{u_} [{t.ci_low * s_:.1f}-{t.ci_high * s_:.1f}]"
                )
                msp.add_text(
                    label,
                    height=L(opts["text_height"]),
                    dxfattribs={
                        "layer": "FIP_LABEL",
                        "insert": (x + L(t.dbh / 2 + 0.1), y, z),
                    },
                )
            if opts["include_axis"] and opts["z_mode"] == "elevation":
                msp.add_line((x, y, L(t.ground_z)), center, dxfattribs={"layer": "FIP_AXIS"})

        try:
            with atomic_write(Path(path), "wt", encoding="utf-8", newline="") as fh:
                doc.write(fh)
        except OSError as exc:
            raise ExportError(f"could not write DXF {path}: {exc}") from exc
        logger.info(
            f"DXF written: {path} ({len(fitted)} trees, z_mode={opts['z_mode']}, units={unit})"
        )
        return Path(path)

    @staticmethod
    def _plane_z(t: TreeRecord, mode: str) -> float:
        if mode == "elevation":
            return float(t.bh_elevation)
        if mode == "height":
            return float(t.breast_height)
        return 0.0
