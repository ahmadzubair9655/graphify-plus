"""Structured error contract.

Every user-facing error in graphify-plus raises a ``GraphifyError`` subclass
carrying:

  - ``code`` — stable string identifier (e.g. ``GP-CACHE-CORRUPT``).
  - ``message`` — one-line human summary suitable for stderr.
  - ``remediation`` — multi-line "What to try next" text. Always present.
  - ``context`` — dict of structured details for the debug log.
  - ``exit_code`` — process exit code (1–63 reserved for known classes).

The CLI wrapper (``__main__.main``) catches ``GraphifyError`` and prints
``code + message + remediation`` to stderr, dumps ``context`` to the
debug log, and exits with the per-code exit code. Any other uncaught
exception is rewrapped as ``GP-INTERNAL-ERROR`` with the full traceback
preserved in the debug log only.

Code registry — keep this docstring in sync with the subclasses below.

  GP-CACHE-CORRUPT          exit 10  cache.db failed integrity check
  GP-CACHE-VERSION-MISMATCH exit 11  cache schema version mismatch
  GP-CACHE-LOCKED           exit 12  another process holds exclusive lock
  GP-PARSE-TIMEOUT          exit 20  per-file parse exceeded timeout
  GP-PARSE-MEMORY           exit 21  per-file parse exceeded memory ceiling
  GP-PARSE-UNSUPPORTED      exit 22  no adapter for file extension
  GP-CONFIG-INVALID         exit 30  rules.yaml or config malformed
  GP-RESOURCE-EXHAUSTED     exit 40  graph node count or cache size ceiling hit
  GP-INTEGRITY-CHECK-FAIL   exit 50  graph self-check found inconsistency
  GP-INTERNAL-ERROR         exit 60  uncaught exception, see debug log

To raise from a new module: subclass ``GraphifyError`` (or pick the
closest existing subclass), set ``code`` / ``exit_code`` once on the
class, and pass ``message=`` and ``remediation=`` per call.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import traceback
from pathlib import Path
from typing import Any

log = logging.getLogger("graphify_plus.errors")

DEBUG_LOG_NAME = "debug.log"


class GraphifyError(Exception):
    """Base class for every user-facing error.

    Subclasses should set ``code`` and ``exit_code`` as class attributes.
    """

    code: str = "GP-INTERNAL-ERROR"
    exit_code: int = 60
    default_remediation: str = "Re-run with --debug and share .graphify_plus/debug.log."

    def __init__(
        self,
        message: str,
        *,
        remediation: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.remediation = remediation or self.default_remediation
        self.context: dict[str, Any] = dict(context or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "remediation": self.remediation,
            "context": self.context,
            "exit_code": self.exit_code,
        }


# ---- concrete codes ---------------------------------------------------


class CacheCorrupt(GraphifyError):
    code = "GP-CACHE-CORRUPT"
    exit_code = 10
    default_remediation = (
        "1. Run 'graphify-plus init --force --repo <path>' to rebuild the cache.\n"
        "2. The corrupt cache has been preserved alongside the fresh one with a\n"
        "   .corrupt.<timestamp> suffix; share it if the issue recurs."
    )


class CacheVersionMismatch(GraphifyError):
    code = "GP-CACHE-VERSION-MISMATCH"
    exit_code = 11
    default_remediation = (
        "1. The cache was written by a different graphify-plus version.\n"
        "2. If your tool is older than the cache, upgrade graphify-plus.\n"
        "3. If your tool is newer, re-run 'graphify-plus init --force'."
    )


class CacheLocked(GraphifyError):
    code = "GP-CACHE-LOCKED"
    exit_code = 12
    default_remediation = (
        "Another graphify-plus process is writing to this cache. Wait for it to\n"
        "finish, or run 'graphify-plus doctor' to identify the holder."
    )


class ParseTimeout(GraphifyError):
    code = "GP-PARSE-TIMEOUT"
    exit_code = 20
    default_remediation = (
        "1. Raise the per-file timeout: GP_PARSE_TIMEOUT_SEC=30 graphify-plus init\n"
        "2. Or exclude the offending file via .gitignore."
    )


class ParseMemory(GraphifyError):
    code = "GP-PARSE-MEMORY"
    exit_code = 21
    default_remediation = (
        "1. The file's parse exceeded the per-worker memory ceiling.\n"
        "2. Raise GP_PARSE_MEM_MB or exclude the file via .gitignore."
    )


class ParseUnsupported(GraphifyError):
    code = "GP-PARSE-UNSUPPORTED"
    exit_code = 22
    default_remediation = (
        "No language adapter matches this file extension. The file was skipped.\n"
        "If support is missing, file an issue with a representative source sample."
    )


class ConfigInvalid(GraphifyError):
    code = "GP-CONFIG-INVALID"
    exit_code = 30
    default_remediation = (
        "1. Check the YAML/JSON syntax of .graphify_plus/rules.yaml.\n"
        "2. Each rule needs an 'id' and exactly one of forbid_edge / forbid_cycle."
    )


class ResourceExhausted(GraphifyError):
    code = "GP-RESOURCE-EXHAUSTED"
    exit_code = 40
    default_remediation = (
        "Repo too large for current limits. Options:\n"
        "  1. Add ignore patterns to .gitignore.\n"
        "  2. Raise GP_MAX_GRAPH_NODES / GP_MAX_CACHE_MB.\n"
        "  3. Inspect: 'graphify-plus doctor --resource-breakdown'."
    )


class IntegrityCheckFail(GraphifyError):
    code = "GP-INTEGRITY-CHECK-FAIL"
    exit_code = 50
    default_remediation = (
        "Graph self-check found an inconsistency. Re-run 'graphify-plus init'\n"
        "to rebuild from source."
    )


class InternalError(GraphifyError):
    code = "GP-INTERNAL-ERROR"
    exit_code = 60


# ---- top-level CLI handler -------------------------------------------


def _debug_log_path(repo: Path | None) -> Path:
    repo = (repo or Path.cwd()).resolve()
    return repo / ".graphify_plus" / DEBUG_LOG_NAME


def write_debug(repo: Path | None, payload: dict[str, Any]) -> None:
    path = _debug_log_path(repo)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(json.dumps(payload, default=str) + "\n")
    except OSError:
        # Best-effort — never let logging crash the CLI.
        pass


def render_for_stderr(err: GraphifyError) -> str:
    return (
        f"✗ {err.code}\n  {err.message}\n\n  What to try next:\n    "
        + err.remediation.replace("\n", "\n    ")
        + "\n"
    )


def handle(err: BaseException, *, repo: Path | None = None, debug: bool = False) -> int:
    """Top-level error handler used by ``__main__``.

    Returns the exit code. Always writes to debug log; only prints the
    structured remediation block when the error is a known
    ``GraphifyError`` subclass. Unexpected exceptions are rewrapped as
    ``GP-INTERNAL-ERROR`` with the traceback in the log only.
    """
    if isinstance(err, GraphifyError):
        sys.stderr.write(render_for_stderr(err))
        write_debug(
            repo,
            {
                "code": err.code,
                "message": err.message,
                "context": err.context,
                "traceback": traceback.format_exc() if debug else None,
            },
        )
        return err.exit_code

    # Unknown exception — rewrap. Never leak raw traceback to stderr by
    # default. ``--debug`` flips this for tooling.
    wrapped = InternalError(
        f"unexpected {type(err).__name__}: {err}",
        context={"exception_type": type(err).__name__},
    )
    if debug or os.environ.get("GP_DEBUG") == "1":
        sys.stderr.write(traceback.format_exc())
    sys.stderr.write(render_for_stderr(wrapped))
    write_debug(
        repo,
        {
            "code": wrapped.code,
            "message": wrapped.message,
            "exception_type": type(err).__name__,
            "traceback": traceback.format_exc(),
        },
    )
    return wrapped.exit_code


# ---- helpers used by CLIs --------------------------------------------


def require_cache(repo: Path) -> None:
    """Raise CacheCorrupt-style guidance if the target repo has no cache.

    Distinct from corruption (cache exists but failed integrity check); this
    is the "cache missing" case which is operationally identical from the
    user's POV — they need to run init.
    """
    from ..runtime.store import cache_path

    db = cache_path(repo)
    if db.exists():
        return
    raise GraphifyError(
        f"No cache at {db}.",
        remediation=f"Run 'graphify-plus init --repo {repo}' to build the symbol graph.",
        context={"repo": str(repo), "cache_path": str(db)},
    )


__all__ = [
    "CacheCorrupt",
    "CacheLocked",
    "CacheVersionMismatch",
    "ConfigInvalid",
    "GraphifyError",
    "IntegrityCheckFail",
    "InternalError",
    "ParseMemory",
    "ParseTimeout",
    "ParseUnsupported",
    "ResourceExhausted",
    "handle",
    "render_for_stderr",
    "require_cache",
    "write_debug",
]
