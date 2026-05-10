"""Layer 21 — schema versioning + migrations for daemon artifacts.

Every artifact (snapshot json, telemetry log, ingest tables) carries a
``_schema_version`` integer. The daemon checks compatibility on load
and runs registered migrations forward-only. Migrations are
idempotent — re-running on an up-to-date artifact is a no-op.

Compatibility window: tool version N supports artifacts back to N-2.
Older artifacts trigger a one-shot auto-migrate prompt; newer ones
return a clear error with the version they need.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

log = logging.getLogger("graphify_plus.daemon.schema_version")

DAEMON_SCHEMA_VERSION = 1
COMPATIBILITY_WINDOW = 2  # support v(N-2) ... v(N) inclusive


class SchemaTooNew(RuntimeError):
    """Artifact written by a newer tool than this one."""


class SchemaTooOld(RuntimeError):
    """Artifact older than COMPATIBILITY_WINDOW; no migration registered."""


_MIGRATIONS: dict[tuple[int, int], Callable[[dict[str, Any]], dict[str, Any]]] = {}


def register_migration(
    from_v: int, to_v: int
) -> Callable[
    [Callable[[dict[str, Any]], dict[str, Any]]], Callable[[dict[str, Any]], dict[str, Any]]
]:
    """Decorator. Each migration is a pure dict→dict function."""

    def _decorator(
        fn: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> Callable[[dict[str, Any]], dict[str, Any]]:
        _MIGRATIONS[(from_v, to_v)] = fn
        return fn

    return _decorator


def stamp(payload: dict[str, Any], *, version: int = DAEMON_SCHEMA_VERSION) -> dict[str, Any]:
    """Add the ``_schema_version`` field to a payload."""
    out = dict(payload)
    out["_schema_version"] = version
    return out


def check(payload: dict[str, Any]) -> int:
    """Return the payload version, raising on incompatibility.

    Missing version is treated as v0 — accepted but flagged for
    migration.
    """
    v = int(payload.get("_schema_version", 0))
    if v > DAEMON_SCHEMA_VERSION:
        raise SchemaTooNew(f"artifact schema v{v} is newer than tool's v{DAEMON_SCHEMA_VERSION}")
    if v < DAEMON_SCHEMA_VERSION - COMPATIBILITY_WINDOW:
        raise SchemaTooOld(
            f"artifact schema v{v} is older than the {COMPATIBILITY_WINDOW}-version support window"
        )
    return v


def migrate(payload: dict[str, Any], *, target: int = DAEMON_SCHEMA_VERSION) -> dict[str, Any]:
    """Apply registered migrations until the payload is at ``target``.

    Forward (``cur < target``) and backward (``cur > target``) directions
    are both supported when the appropriate migration is registered.
    Idempotent: payload already at target returns unchanged.

    Layer 21.4 — lossy backward migrations declare what they drop. The
    migration function returns the body it produced; if any keys were
    removed, the migration is responsible for surfacing them via
    ``payload['_lost_in_migration']``.
    """
    cur = int(payload.get("_schema_version", 0))
    while cur != target:
        if cur < target:
            fn = _MIGRATIONS.get((cur, cur + 1))
            if fn is None:
                raise SchemaTooOld(f"no migration registered from v{cur} to v{cur + 1}")
            payload = fn(payload)
            cur += 1
        else:
            fn = _MIGRATIONS.get((cur, cur - 1))
            if fn is None:
                raise SchemaTooNew(f"no backward migration from v{cur} to v{cur - 1}")
            payload = fn(payload)
            cur -= 1
    payload["_schema_version"] = target
    return payload


def declare_lossy(payload: dict[str, Any], *, dropped: list[str]) -> dict[str, Any]:
    """Helper for backward migrations: record fields that the
    migration is dropping so the caller can warn the user.
    """
    out = dict(payload)
    out["_lost_in_migration"] = list(dropped)
    return out


def lost_in_migration(payload: dict[str, Any]) -> list[str]:
    return list(payload.get("_lost_in_migration") or [])


def load_artifact(path: Path) -> dict[str, Any]:
    """Read + check + auto-migrate a JSON artifact. Returns the body."""
    body = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(body, dict):
        raise ValueError(f"expected JSON object at {path}")
    check(body)
    if body.get("_schema_version", 0) < DAEMON_SCHEMA_VERSION:
        body = migrate(body)
        path.write_text(json.dumps(body, indent=2), encoding="utf-8")
    return body


def save_artifact(path: Path, body: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(stamp(body), indent=2), encoding="utf-8")


__all__ = [
    "COMPATIBILITY_WINDOW",
    "DAEMON_SCHEMA_VERSION",
    "SchemaTooNew",
    "SchemaTooOld",
    "check",
    "declare_lossy",
    "load_artifact",
    "lost_in_migration",
    "migrate",
    "register_migration",
    "save_artifact",
    "stamp",
]
