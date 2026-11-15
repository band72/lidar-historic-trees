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
"""
Plugin discovery and registration.

Plugins are found in three places, in this order:

1. built-ins: every module in ``forest_inventory_pipeline/plugins/builtin/``
2. installed packages exposing the entry-point group ``forest_inventory_pipeline.plugins``
3. extra directories: ``*.py`` files in folders given via ``load_plugins(extra_dirs=...)``,
   the CLI ``--plugin-dir`` option, or the ``FIP_PLUGIN_PATH`` environment variable
   (``os.pathsep``-separated)

A plugin that fails to import or register is logged and recorded in
``registry.load_errors``; it never prevents the rest of the application from running.
Loading a plugin executes its code, so only point at directories you trust.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import pkgutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, TypeVar

from ..errors import FIPError
from ..utils import logger

from .base import Exporter, Reader, TreeMetric

ENTRY_POINT_GROUP = "forest_inventory_pipeline.plugins"
ENV_VAR = "FIP_PLUGIN_PATH"

T = TypeVar("T", bound=type)


class PluginError(FIPError):
    pass


@dataclass
class PluginRegistry:
    exporters: dict[str, Exporter] = field(default_factory=dict)
    metrics: dict[str, TreeMetric] = field(default_factory=dict)
    readers: dict[str, Reader] = field(default_factory=dict)
    load_errors: dict[str, str] = field(default_factory=dict)
    _loaded_sources: set[str] = field(default_factory=set)

    def add_exporter(self, cls: type[Exporter]) -> None:
        inst = cls()
        if not inst.name or not inst.extension.startswith("."):
            raise PluginError(
                f"exporter {cls.__name__} needs a name and an extension starting with '.'"
            )
        if inst.name in self.exporters and type(self.exporters[inst.name]) is not cls:
            logger.warning(f"exporter '{inst.name}' from {cls.__module__} replaces an earlier one")
        self.exporters[inst.name] = inst

    def add_metric(self, cls: type[TreeMetric]) -> None:
        inst = cls()
        if not inst.name or not inst.columns:
            raise PluginError(f"metric {cls.__name__} needs a name and at least one column")
        if inst.name in self.metrics and type(self.metrics[inst.name]) is not cls:
            logger.warning(f"metric '{inst.name}' from {cls.__module__} replaces an earlier one")
        self.metrics[inst.name] = inst

    def add_reader(self, cls: type[Reader]) -> None:
        inst = cls()
        if not inst.name or not inst.extensions:
            raise PluginError(f"reader {cls.__name__} needs a name and at least one extension")
        if inst.name in self.readers and type(self.readers[inst.name]) is not cls:
            logger.warning(f"reader '{inst.name}' from {cls.__module__} replaces an earlier one")
        self.readers[inst.name] = inst

    def reader_for(self, path: str | Path, name: str | None = None) -> Reader:
        """Reader by explicit name, else the first one whose extensions match ``path``."""
        if name:
            try:
                return self.readers[name]
            except KeyError:
                raise PluginError(
                    f"no reader named '{name}'. available: {sorted(self.readers)}"
                ) from None
        path = Path(path)
        for reader in self.readers.values():
            if reader.accepts(path):
                return reader
        supported = sorted({e for r in self.readers.values() for e in r.extensions})
        raise PluginError(f"no reader for '{path.name}'. supported: {supported}")

    def exporter(self, name: str) -> Exporter:
        try:
            return self.exporters[name]
        except KeyError:
            raise PluginError(
                f"no exporter named '{name}'. available: {sorted(self.exporters)}"
            ) from None


registry = PluginRegistry()


def register_exporter(cls: T) -> T:
    """Class decorator: make an :class:`Exporter` subclass available."""
    if not (isinstance(cls, type) and issubclass(cls, Exporter)):
        raise PluginError(f"@register_exporter expects an Exporter subclass, got {cls!r}")
    registry.add_exporter(cls)
    return cls


def register_metric(cls: T) -> T:
    """Class decorator: make a :class:`TreeMetric` subclass available."""
    if not (isinstance(cls, type) and issubclass(cls, TreeMetric)):
        raise PluginError(f"@register_metric expects a TreeMetric subclass, got {cls!r}")
    registry.add_metric(cls)
    return cls


def register_reader(cls: T) -> T:
    """Class decorator: make a :class:`Reader` subclass available."""
    if not (isinstance(cls, type) and issubclass(cls, Reader)):
        raise PluginError(f"@register_reader expects a Reader subclass, got {cls!r}")
    registry.add_reader(cls)
    return cls


def _record_failure(source: str, exc: BaseException) -> None:
    registry.load_errors[source] = f"{type(exc).__name__}: {exc}"
    logger.warning(f"plugin '{source}' failed to load: {type(exc).__name__}: {exc}")
    logger.debug("plugin load traceback", exc_info_str(exc))


def exc_info_str(exc: BaseException) -> str:
    import traceback

    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))


def _load_builtin() -> None:
    from . import builtin

    for mod in pkgutil.iter_modules(builtin.__path__):
        source = f"builtin:{mod.name}"
        if source in registry._loaded_sources:
            continue
        try:
            importlib.import_module(f"{builtin.__name__}.{mod.name}")
            registry._loaded_sources.add(source)
        except Exception as exc:  # noqa: BLE001 - isolate plugin failures
            _record_failure(source, exc)


def _load_entry_points() -> None:
    from importlib.metadata import entry_points

    try:
        eps = entry_points(group=ENTRY_POINT_GROUP)
    except Exception as exc:  # noqa: BLE001
        _record_failure("entry-points", exc)
        return
    for ep in eps:
        source = f"entry-point:{ep.name}"
        if source in registry._loaded_sources:
            continue
        try:
            ep.load()
            registry._loaded_sources.add(source)
        except Exception as exc:  # noqa: BLE001
            _record_failure(source, exc)


def _load_directory(directory: Path) -> None:
    directory = directory.expanduser().resolve()
    if not directory.is_dir():
        _record_failure(
            f"dir:{directory}", FileNotFoundError(f"plugin directory {directory} does not exist")
        )
        return
    for py in sorted(directory.glob("*.py")):
        if py.name.startswith("_"):
            continue
        source = f"file:{py}"
        if source in registry._loaded_sources:
            continue
        mod_name = f"fip_user_plugin_{abs(hash(str(py))):x}_{py.stem}"
        try:
            spec = importlib.util.spec_from_file_location(mod_name, py)
            if spec is None or spec.loader is None:
                raise PluginError(f"cannot import {py}")
            module = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = module
            spec.loader.exec_module(module)
            registry._loaded_sources.add(source)
            logger.info(f"loaded plugin file {py}")
        except Exception as exc:  # noqa: BLE001
            sys.modules.pop(mod_name, None)
            _record_failure(source, exc)


def load_plugins(
    extra_dirs: Iterable[str | Path] = (), entry_points: bool = True
) -> PluginRegistry:
    """Discover and register plugins. Safe to call repeatedly; sources load once."""
    _load_builtin()
    if entry_points:
        _load_entry_points()
    dirs = [Path(d) for d in extra_dirs]
    env = os.environ.get(ENV_VAR, "")
    dirs += [Path(p) for p in env.split(os.pathsep) if p.strip()]
    for d in dirs:
        _load_directory(d)
    logger.debug(
        f"plugins: readers={sorted(registry.readers)} exporters={sorted(registry.exporters)}"
        f" metrics={sorted(registry.metrics)}"
        f" errors={len(registry.load_errors)}"
    )
    return registry
