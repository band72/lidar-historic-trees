# MIT License
#
# Copyright (c) 2024 Meher Malladi, Luca Lobefaro, Tiziano Guadagnino, Cyrill Stachniss
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
"""Plugin interfaces. Subclass one of these and decorate it with a ``register_*`` function."""

from __future__ import annotations

import os
import tempfile
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Sequence

import numpy as np

from ..errors import ExportError
from ..records import TreeRecord


@dataclass
class OptionSpec:
    """Describes one exporter option so the CLI and web app can render a control for it."""

    name: str
    default: Any
    help: str = ""
    choices: Sequence[Any] | None = None
    min: float | None = None
    max: float | None = None


@dataclass
class ExportContext:
    """Run-level information handed to exporters alongside the trees."""

    source: str = ""
    breast_height: float = 1.3
    confidence: float = 0.95
    units: str = "m"
    parameters: dict[str, Any] = field(default_factory=dict)


class Exporter(ABC):
    """Writes a set of trees to a file format."""

    #: unique id used on the CLI (``--export <name>``) and in the app
    name: str = ""
    #: file extension including the dot, e.g. ".csv"
    extension: str = ""
    label: str = ""
    description: str = ""
    #: MIME type for browser downloads
    mime: str = "application/octet-stream"
    options: Sequence[OptionSpec] = ()

    def resolve_options(self, given: dict[str, Any] | None) -> dict[str, Any]:
        """Fill defaults and validate values against ``options``."""
        given = dict(given or {})
        known = {o.name: o for o in self.options}
        unknown = set(given) - set(known)
        if unknown:
            raise ExportError(
                f"{self.name}: unknown option(s) {sorted(unknown)}; valid: {sorted(known)}"
            )
        # values from the CLI arrive as strings; coerce to the default's type
        for name, value in list(given.items()):
            default = known[name].default
            if isinstance(value, str) and not isinstance(default, str) and default is not None:
                try:
                    if isinstance(default, bool):
                        if value.lower() not in ("1", "0", "true", "false", "yes", "no"):
                            raise ValueError(value)
                        given[name] = value.lower() in ("1", "true", "yes")
                    else:
                        given[name] = type(default)(value)
                except ValueError:
                    raise ExportError(
                        f"{self.name}: option {name}={value!r} is not a valid {type(default).__name__}"
                    ) from None
        out = {}
        for name, spec in known.items():
            value = given.get(name, spec.default)
            if spec.choices is not None and value not in spec.choices:
                raise ExportError(
                    f"{self.name}: option {name}={value!r} not in {list(spec.choices)}"
                )
            if spec.min is not None and value < spec.min:
                raise ExportError(f"{self.name}: option {name}={value} below minimum {spec.min}")
            if spec.max is not None and value > spec.max:
                raise ExportError(f"{self.name}: option {name}={value} above maximum {spec.max}")
            out[name] = value
        return out

    @abstractmethod
    def export(
        self, trees: Sequence[TreeRecord], path: Path, context: ExportContext, **options: Any
    ) -> Path:
        """Write ``trees`` to ``path`` and return the path written."""


class Reader(ABC):
    """Loads a point cloud source (file or directory) into an (N, 3) float64 array."""

    name: str = ""
    #: lower-case suffixes this reader accepts, e.g. (".ply", ".pcd"); "/" for directories
    extensions: Sequence[str] = ()
    label: str = ""
    description: str = ""
    options: Sequence[OptionSpec] = ()

    resolve_options = Exporter.resolve_options

    def accepts(self, path: Path) -> bool:
        path = Path(path)
        if path.is_dir():
            return "/" in self.extensions
        return path.suffix.lower() in self.extensions

    @abstractmethod
    def read(self, path: Path, **options: Any) -> "np.ndarray":
        """Return finite (N, 3) float64 xyz. Raise InputDataError on bad input."""


class TreeMetric(ABC):
    """Computes extra per-tree columns after fitting (stored in ``TreeRecord.extra``)."""

    name: str = ""
    description: str = ""
    #: column name -> description; every key must be returned by ``compute``
    columns: dict[str, str] = {}

    @abstractmethod
    def compute(self, tree: TreeRecord) -> dict[str, Any]:
        """Return values for ``columns``. Only called for fitted trees."""


@contextmanager
def atomic_write(path: Path, mode: str = "w", **kwargs) -> Iterator[Any]:
    """Write to a temp file in the target directory, then rename: no half-written outputs."""
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ExportError(f"cannot create output directory {path.parent}: {exc}") from exc
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, mode, **kwargs) as fh:
            yield fh
        # mkstemp creates 0600; give the result normal permissions (respecting umask)
        umask = os.umask(0)
        os.umask(umask)
        os.chmod(tmp, 0o666 & ~umask)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
