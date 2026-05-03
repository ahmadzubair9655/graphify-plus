"""11.8 — opt-in telemetry.

Disabled by default. Activates only when ``GRAPHIFY_TELEMETRY_URL`` is
set in the environment. Sends a minimal envelope: command name,
duration_ms, file_count_bucket, error_code (if any), version, OS,
Python version. Never sends paths, source, symbol names, or repo
identity.

This module never raises into the caller — telemetry must not block or
crash the CLI. All exceptions are swallowed; a one-line debug message
goes to ``stderr`` only when ``GP_DEBUG=1`` is set.
"""

from __future__ import annotations

import os
import platform
import sys
import urllib.error
import urllib.request

from .. import __version__ as _GP_VERSION

_BUCKETS = (10, 100, 1_000, 10_000, 100_000)


def _bucket(n: int) -> str:
    for b in _BUCKETS:
        if n <= b:
            return f"<={b}"
    return f">{_BUCKETS[-1]}"


def is_enabled() -> bool:
    return bool(os.environ.get("GRAPHIFY_TELEMETRY_URL"))


def emit(
    command: str,
    duration_ms: int,
    file_count: int = 0,
    error_code: str | None = None,
) -> None:
    if not is_enabled():
        return
    url = os.environ["GRAPHIFY_TELEMETRY_URL"]
    payload = {
        "command": command,
        "duration_ms": duration_ms,
        "file_count_bucket": _bucket(file_count),
        "error_code": error_code,
        "version": _GP_VERSION,
        "os": platform.system().lower(),
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
    }
    try:
        import json

        data = json.dumps(payload, sort_keys=True).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=2.0).close()  # noqa: S310
    except (urllib.error.URLError, OSError, ValueError) as exc:
        if os.environ.get("GP_DEBUG") == "1":
            print(f"telemetry: dropped ({exc})", file=sys.stderr)


__all__ = ["emit", "is_enabled"]
