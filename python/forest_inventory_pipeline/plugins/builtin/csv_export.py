"""CSV export: one row per tree, all fit/quality fields plus plugin metric columns."""

from __future__ import annotations

import csv
import math
from pathlib import Path
from typing import Any, Sequence

from ...records import CORE_COLUMNS, TreeRecord
from ...utils import logger
from ..base import ExportContext, Exporter, OptionSpec, atomic_write
from ..registry import register_exporter


@register_exporter
class CsvExporter(Exporter):
    name = "csv"
    extension = ".csv"
    label = "CSV table"
    description = "One row per tree: position, ground/DBH elevation, DBH with CI, quality metrics."
    mime = "text/csv"
    options = (
        OptionSpec(
            "include_failed", True, "include trees that could not be fitted (status=failed)"
        ),
        OptionSpec("precision", 4, "decimal places for floating point values", min=0, max=10),
    )

    def export(
        self, trees: Sequence[TreeRecord], path: Path, context: ExportContext, **options: Any
    ) -> Path:
        opts = self.resolve_options(options)
        rows = [t.to_row() for t in trees if opts["include_failed"] or t.fitted]
        # plugin columns in first-seen order (each metric's declared order)
        extra_cols = list(dict.fromkeys(k for r in rows for k in r if k not in CORE_COLUMNS))
        columns = CORE_COLUMNS + extra_cols
        prec = int(opts["precision"])

        def fmt(v):
            if isinstance(v, float):
                return "" if math.isnan(v) else f"{v:.{prec}f}"
            return v

        with atomic_write(Path(path), "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({k: fmt(row.get(k, "")) for k in columns})
        logger.info(f"CSV written: {path} ({len(rows)} trees)")
        return Path(path)
