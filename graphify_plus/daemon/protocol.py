"""Wire protocol for the daemon.

JSON-line framing over a Unix domain socket. Each request is one line of
JSON terminated by ``\\n``; each response is one line of JSON terminated
by ``\\n``. Requests are independent — no streaming, no multiplexing — so
the server keeps its dispatch loop dead simple.

Request shape
-------------
```json
{"op": "whats_in", "args": {"path": "src/auth.py"}, "request_id": "abc"}
```

Response shape
--------------
```json
{
  "request_id": "abc",
  "ok": true,
  "freshness": {...},
  "receipt": {...},
  "results": [...],
  "more_available": 0
}
```

Errors come back as ``{"ok": false, "error": {"code": ..., "message": ...}}``.

Freshness contract
------------------
Trust levels mirror Layer 2 of the master plan:

    FRESH                — graph reflects HEAD exactly
    LIVE_AHEAD           — uncommitted edits applied incrementally
    STALE_<N>_FILES      — N source files modified since last rebuild
    STALE_REBUILD_NEEDED — schema / fundamental change; rebuild required

The daemon tracks file mtimes against the ingest time to compute this
without needing git in the hot path.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from enum import Enum
from pathlib import Path
from typing import Any, TypedDict

# ---- socket / pid layout -------------------------------------------------

SOCKET_NAME = "daemon.sock"
PID_NAME = "daemon.pid"
DAEMON_DIR = ".graphify_plus"

# macOS caps AF_UNIX paths at ~104 bytes; Linux at 108. The repo-relative
# location (``<repo>/.graphify_plus/daemon.sock``) blows past that for any
# repo nested deep under ``/private/var/folders/...``. So the socket lives
# under ``$TMPDIR/gp-<hash>.sock`` instead — the hash is taken over the
# absolute repo path so two repos always get distinct sockets, and the
# socket path stays short enough to bind on macOS.
SOCKET_PATH_MAX = 100


def _socket_dir() -> Path:
    """Where to put the socket. Honour TMPDIR but never put the daemon
    socket inside the repo (path-length headroom on macOS).
    """
    tmp = os.environ.get("GP_DAEMON_SOCKET_DIR") or tempfile.gettempdir()
    return Path(tmp)


def socket_path(repo_root: Path) -> Path:
    """Short, stable socket path keyed by the absolute repo path."""
    canonical = str(Path(repo_root).resolve())
    digest = hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:12]
    return _socket_dir() / f"gp-{digest}.sock"


def pid_path(repo_root: Path) -> Path:
    """PID file lives alongside the cache so ``gp doctor`` finds it."""
    return repo_root / DAEMON_DIR / PID_NAME


# ---- freshness -----------------------------------------------------------


class FreshnessTrust(str, Enum):
    FRESH = "FRESH"
    LIVE_AHEAD = "LIVE_AHEAD"
    STALE_FILES = "STALE_FILES"
    STALE_REBUILD_NEEDED = "STALE_REBUILD_NEEDED"


class Freshness(TypedDict, total=False):
    graph_built_at: str         # ISO-8601 UTC
    files_changed_since: int    # 0 means fully fresh
    stale_paths: list[str]      # subset of changed paths (clipped)
    trust: str                  # one of FreshnessTrust values
    freshness_token: str        # short hash that changes whenever graph changes
    hint: str                   # human-readable advice when trust degrades


# ---- envelope ------------------------------------------------------------


class Receipt(TypedDict, total=False):
    op: str
    elapsed_ms: float
    tokens: int                 # estimated tokens in the result body
    grep_equivalent: str        # human-readable "would have taken ~14 file reads"


class ErrorBody(TypedDict, total=False):
    code: str                   # short uppercase identifier
    message: str
    detail: dict[str, Any]


class Request(TypedDict, total=False):
    op: str
    args: dict[str, Any]
    request_id: str


class Response(TypedDict, total=False):
    request_id: str
    ok: bool
    error: ErrorBody
    freshness: Freshness
    receipt: Receipt
    results: list[dict[str, Any]]
    more_available: int
    extra: dict[str, Any]


# ---- error codes ---------------------------------------------------------

ERR_BAD_REQUEST = "BAD_REQUEST"
ERR_UNKNOWN_OP = "UNKNOWN_OP"
ERR_NO_GRAPH = "NO_GRAPH"
ERR_NOT_FOUND = "NOT_FOUND"
ERR_AMBIGUOUS = "AMBIGUOUS"
ERR_INTERNAL = "INTERNAL"


__all__ = [
    "DAEMON_DIR",
    "ERR_AMBIGUOUS",
    "ERR_BAD_REQUEST",
    "ERR_INTERNAL",
    "ERR_NO_GRAPH",
    "ERR_NOT_FOUND",
    "ERR_UNKNOWN_OP",
    "ErrorBody",
    "Freshness",
    "FreshnessTrust",
    "PID_NAME",
    "Receipt",
    "Request",
    "Response",
    "SOCKET_NAME",
    "pid_path",
    "socket_path",
]
