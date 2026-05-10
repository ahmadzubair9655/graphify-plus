"""Plugin architecture — Layer 11.2 of the master plan.

Domain-specific extensions register custom handlers, ingestors, or node
types via Python entry points without forking graphify-plus.

A plugin is any installed package that declares a
``graphify_plus.plugins`` entry point pointing at a callable. The
callable receives a ``PluginRegistry`` and registers what it provides::

    # smart_energie_extension/plugin.py
    from graphify_plus.daemon.plugins import PluginRegistry

    def register(reg: PluginRegistry) -> None:
        reg.register_handler("boiler_for", boiler_handler)
        reg.register_ingestor("pricing", ingest_pricing)

In ``pyproject.toml``::

    [project.entry-points."graphify_plus.plugins"]
    smart_energie = "smart_energie_extension.plugin:register"

Plugin handlers are merged into ``HANDLERS`` at daemon startup and are
indistinguishable from built-ins from the caller's perspective. Plugin
ingestors are exposed as ``gp daemon plugin ingest <name> <args>``.

Failures during plugin discovery are logged at warning and *never*
prevent the daemon from starting — a broken plugin should degrade
gracefully, not take the whole tool down.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from typing import Any

log = logging.getLogger("graphify_plus.daemon.plugins")

ENTRY_POINT_GROUP = "graphify_plus.plugins"


HandlerFn = Callable[[Any, dict[str, Any]], dict[str, Any]]
IngestorFn = Callable[..., dict[str, Any]]


@dataclass
class PluginRegistry:
    handlers: dict[str, HandlerFn] = field(default_factory=dict)
    ingestors: dict[str, IngestorFn] = field(default_factory=dict)
    metadata: dict[str, dict[str, str]] = field(default_factory=dict)
    _current: str = ""

    def register_handler(self, op: str, fn: HandlerFn) -> None:
        if op in self.handlers:
            log.warning(
                "plugin %s overrides existing handler %r — earlier registration wins",
                self._current,
                op,
            )
            return
        self.handlers[op] = fn
        self.metadata.setdefault(self._current, {})["handler:" + op] = ""

    def register_ingestor(self, name: str, fn: IngestorFn) -> None:
        if name in self.ingestors:
            log.warning(
                "plugin %s overrides existing ingestor %r — earlier registration wins",
                self._current,
                name,
            )
            return
        self.ingestors[name] = fn
        self.metadata.setdefault(self._current, {})["ingestor:" + name] = ""


def discover() -> PluginRegistry:
    """Walk every ``graphify_plus.plugins`` entry point. Returns a
    populated registry. Never raises — broken plugins are logged
    and skipped.
    """
    reg = PluginRegistry()
    try:
        eps = entry_points(group=ENTRY_POINT_GROUP)
    except Exception as exc:  # noqa: BLE001
        log.warning("plugin discovery failed: %s", exc)
        return reg
    for ep in eps:
        reg._current = ep.name
        try:
            register_fn = ep.load()
        except Exception as exc:  # noqa: BLE001
            log.warning("plugin %r failed to load: %s", ep.name, exc)
            continue
        try:
            register_fn(reg)
            log.info("plugin %r loaded", ep.name)
        except Exception as exc:  # noqa: BLE001
            log.warning("plugin %r register() raised: %s", ep.name, exc)
            continue
    reg._current = ""
    return reg


_REGISTRY: PluginRegistry | None = None


def get_registry() -> PluginRegistry:
    """Lazy-init the process-wide registry."""
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = discover()
    return _REGISTRY


def reset_registry_for_tests(new: PluginRegistry | None = None) -> None:
    """Test hook — replace the cached registry."""
    global _REGISTRY
    _REGISTRY = new


__all__ = [
    "ENTRY_POINT_GROUP",
    "PluginRegistry",
    "discover",
    "get_registry",
    "reset_registry_for_tests",
]
