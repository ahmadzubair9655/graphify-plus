"""Layer 27 — distribution and ecosystem mechanics.

* version_check() — best-effort daily probe of the latest released
  version on PyPI. Cached in ``.graphify_plus/version-check.json``;
  opt-out via ``GP_NO_UPDATE_CHECK=1``.
* publish_changelog_entry() — append-only changelog writer that the
  release flow uses.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("graphify_plus.daemon.distribution")

PYPI_URL = "https://pypi.org/pypi/graphify-plus/json"
CACHE_TTL = 86_400  # 1 day


@dataclass
class VersionInfo:
    current: str
    latest: str | None
    update_available: bool
    checked_at: float
    error: str = ""


def cache_path(repo: Path) -> Path:
    return repo / ".graphify_plus" / "version-check.json"


def is_disabled() -> bool:
    return os.environ.get("GP_NO_UPDATE_CHECK") == "1"


def version_check(repo: Path, current: str) -> VersionInfo:
    p = cache_path(repo)
    if is_disabled():
        return VersionInfo(
            current=current,
            latest=None,
            update_available=False,
            checked_at=time.time(),
            error="disabled",
        )
    if p.exists():
        try:
            cached = json.loads(p.read_text(encoding="utf-8"))
            if time.time() - float(cached.get("checked_at", 0)) < CACHE_TTL:
                return VersionInfo(
                    current=current,
                    latest=cached.get("latest"),
                    update_available=cached.get("update_available", False),
                    checked_at=cached.get("checked_at", 0.0),
                )
        except Exception:  # noqa: BLE001
            pass
    info = _probe_pypi(current)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(
                {
                    "current": info.current,
                    "latest": info.latest,
                    "update_available": info.update_available,
                    "checked_at": info.checked_at,
                    "error": info.error,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass
    return info


def _probe_pypi(current: str, *, timeout: float = 1.5) -> VersionInfo:
    try:
        req = urllib.request.Request(PYPI_URL, headers={"User-Agent": "graphify-plus"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        latest = body.get("info", {}).get("version")
        return VersionInfo(
            current=current,
            latest=latest,
            update_available=bool(latest and _newer(latest, current)),
            checked_at=time.time(),
        )
    except urllib.error.URLError as exc:
        return VersionInfo(
            current=current,
            latest=None,
            update_available=False,
            checked_at=time.time(),
            error=str(exc.reason),
        )
    except Exception as exc:  # noqa: BLE001
        return VersionInfo(
            current=current,
            latest=None,
            update_available=False,
            checked_at=time.time(),
            error=str(exc),
        )


def _newer(a: str, b: str) -> bool:
    """``a > b`` for PEP-440-ish version strings, best-effort."""
    parts_a = [int(x) for x in a.split(".") if x.isdigit()]
    parts_b = [int(x) for x in b.split(".") if x.isdigit()]
    return parts_a > parts_b


__all__ = ["VersionInfo", "cache_path", "is_disabled", "version_check"]
