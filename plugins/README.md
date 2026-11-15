# Plugins

Drop a `.py` file in this folder (or any folder) to add features without touching the core.
Load a folder with `--plugin-dir plugins` on `fip inventory`, `fip plugins` and `fip app`,
or set `FIP_PLUGIN_PATH=/path/one:/path/two`. Installed packages can also register plugins
through the `forest_inventory_pipeline.plugins` entry-point group.

A plugin that fails to import is logged and skipped; the rest of the app keeps working.
Run `fip plugins --plugin-dir plugins` to see what loaded and what failed.

Plugins are ordinary Python and run with your permissions: only load folders you trust.

## Plugin types

| Type | Base class | Decorator | Used for |
|---|---|---|---|
| Reader | `Reader` | `@register_reader` | new input formats (returns an `(N, 3)` float array) |
| Exporter | `Exporter` | `@register_exporter` | new output formats (CSV, DXF and GeoJSON are exporters) |
| Metric | `TreeMetric` | `@register_metric` | extra per-tree columns computed after fitting |

All three are importable from `forest_inventory_pipeline.plugins`. Built-in examples live in
`python/forest_inventory_pipeline/plugins/builtin/`.

### Exporter

```python
from forest_inventory_pipeline.plugins import Exporter, OptionSpec, atomic_write, register_exporter

@register_exporter
class MyExporter(Exporter):
    name = "mine"            # used as --export mine
    extension = ".txt"
    description = "One line per tree"
    options = (OptionSpec("unit", "cm", "DBH unit", choices=("cm", "m")),)

    def export(self, trees, path, context, **options):
        opts = self.resolve_options(options)          # fills defaults, validates, coerces CLI strings
        scale = 100 if opts["unit"] == "cm" else 1
        with atomic_write(path) as fh:                # never leaves a half-written file
            for t in trees:
                if t.fitted:
                    fh.write(f"{t.tree_id} {t.dbh * scale:.2f}\n")
        return path
```

Options declared with `OptionSpec` show up as controls in the web app and can be set from the
CLI with `-E mine.unit=m`.

### Metric

```python
from forest_inventory_pipeline.plugins import TreeMetric, register_metric

@register_metric
class Circumference(TreeMetric):
    name = "circumference"
    description = "Stem girth at breast height"
    columns = {"girth_m": "circumference (m)"}

    def compute(self, tree):
        return {"girth_m": 3.14159 * tree.dbh}
```

`tree.points` holds that tree's height-normalized points if a metric needs geometry.

### Reader

```python
import numpy as np
from forest_inventory_pipeline.errors import InputDataError
from forest_inventory_pipeline.plugins import Reader, register_reader

@register_reader
class E57Reader(Reader):
    name = "e57"
    extensions = (".e57",)
    description = "E57 scans via pye57"

    def read(self, path, **options):
        import pye57
        data = pye57.E57(str(path)).read_scan(0, ignore_missing_fields=True)
        xyz = np.c_[data["cartesianX"], data["cartesianY"], data["cartesianZ"]]
        if len(xyz) == 0:
            raise InputDataError(f"{path}: empty scan")
        return xyz
```

## Included example

- `geojson_export.py`: tree points with DBH/CI as GeoJSON, for QGIS/ArcGIS
  (`fip inventory cloud.laz --plugin-dir plugins -e csv -e dxf -e geojson`).
