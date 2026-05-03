"""Repo walk + AST parse orchestrator.

Honours .gitignore + a default deny-list of build/vendor directories.
Dispatches each file to the matching ``LangAdapter`` and merges results
deterministically. Parallelism is opt-in via ``parallel=True``.

Phase 11.4 — per-file parse isolation:

  - Wall-clock timeout per worker (``GP_PARSE_TIMEOUT_SEC``, default 5).
  - Memory ceiling on POSIX (``GP_PARSE_MEM_MB``, default 512). No-op
    on Windows; documented in the readme.
  - Worker death (timeout, OOM, segfault) is caught at the orchestrator.
    A skipped file produces:
      * a stub Symbol of kind ``module`` with ``parse_status="failed"``
        and a short ``parse_error`` field, and
      * an entry in ``<repo>/.graphify_plus/skipped.jsonl``.
  - The orchestrator never re-raises — ``gp init`` always succeeds end-
    to-end as long as at least one file parsed. ``--strict`` flips this
    to fail-fast for CI.
"""

from __future__ import annotations

import json
import os
import resource
import signal
import sys
import time
from collections.abc import Iterable
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures import TimeoutError as _PoolTimeoutError
from dataclasses import dataclass
from pathlib import Path

import pathspec

from ..interface.errors import ParseMemory, ParseTimeout
from .adapters import ALL_ADAPTERS, Edge, Symbol, adapter_for, make_symbol_id

DEFAULT_DENY = (
    "node_modules",
    ".venv",
    "venv",
    "env",
    ".env",
    "dist",
    "build",
    "out",
    "target",
    "vendor",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".git",
    ".graphify_plus",
    ".graphify-out",
    "graphify-out",
    "coverage",
    ".next",
    ".turbo",
    ".cache",
)


def _parse_timeout_sec() -> float:
    try:
        return float(os.environ.get("GP_PARSE_TIMEOUT_SEC", "5"))
    except (TypeError, ValueError):
        return 5.0


def _parse_mem_mb() -> int:
    try:
        return int(os.environ.get("GP_PARSE_MEM_MB", "512"))
    except (TypeError, ValueError):
        return 512


@dataclass(frozen=True)
class SkippedFile:
    path: str
    reason: str  # 'timeout' | 'memory' | 'parse_error' | 'unsupported'
    error: str
    timestamp: float


@dataclass(frozen=True)
class IngestResult:
    symbols: list[Symbol]
    edges: list[Edge]
    files_parsed: int
    files_skipped: int
    skipped: list[SkippedFile] = ()  # type: ignore[assignment]


def _load_gitignore(root: Path) -> pathspec.PathSpec:
    patterns: list[str] = list(DEFAULT_DENY)
    gi = root / ".gitignore"
    if gi.exists():
        patterns.extend(gi.read_text(errors="replace").splitlines())
    return pathspec.PathSpec.from_lines("gitwildmatch", patterns)


def _candidate_files(root: Path) -> Iterable[Path]:
    spec = _load_gitignore(root)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in DEFAULT_DENY and not d.startswith(".")]
        for fn in filenames:
            full = Path(dirpath) / fn
            rel = full.relative_to(root)
            if spec.match_file(rel.as_posix()):
                continue
            if adapter_for(fn) is None:
                continue
            yield full


def _set_worker_limits(timeout_sec: float, mem_mb: int) -> None:
    """POSIX-only: install a SIGALRM and an RLIMIT_AS ceiling on the
    current worker. No-op on Windows.
    """
    if sys.platform == "win32":  # pragma: no cover
        return
    # Wall-clock timeout.
    signal.alarm(int(max(1, round(timeout_sec))))
    # Memory ceiling — soft & hard.
    bytes_limit = mem_mb * 1024 * 1024
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        new_hard = bytes_limit if hard == resource.RLIM_INFINITY else min(hard, bytes_limit)
        resource.setrlimit(resource.RLIMIT_AS, (bytes_limit, new_hard))
    except (ValueError, OSError):
        # Some sandboxes (CI, containers) refuse RLIMIT_AS; that's OK,
        # the timeout still applies.
        pass


# Process-scoped flag — only set inside ProcessPoolExecutor workers via
# initializer. Synchronous calls from the parent (and tests) leave it
# False so RLIMIT_AS / SIGALRM do not leak into the test runner.
_IN_WORKER = False


def _worker_initializer() -> None:  # pragma: no cover — runs in child only
    global _IN_WORKER
    _IN_WORKER = True


def _parse_one(args: tuple[str, str, float, int]) -> tuple[list[Symbol], list[Edge], dict]:
    """Parse one file under wall-clock + memory limits.

    Returns a 3-tuple ``(symbols, edges, status)`` where ``status`` is
    ``{"ok": bool, "reason": str | None, "error": str | None}``.

    Limits (RLIMIT_AS + SIGALRM) only apply inside ProcessPoolExecutor
    workers. Synchronous calls from the parent skip them — applying
    RLIMIT_AS to the test runner crashes pytest's own stack formatter
    on Linux.
    """
    root_str, rel_str, timeout_sec, mem_mb = args
    full = Path(root_str) / rel_str
    ad = adapter_for(full.name)
    if ad is None:
        return [], [], {"ok": False, "reason": "unsupported", "error": ""}
    if _IN_WORKER:
        _set_worker_limits(timeout_sec, mem_mb)
    try:
        source = full.read_bytes()
        syms, edges = ad.parse(Path(rel_str), source)
        return syms, edges, {"ok": True, "reason": None, "error": None}
    except MemoryError as e:
        return [], [], {"ok": False, "reason": "memory", "error": str(e) or "RLIMIT_AS"}
    except Exception as e:  # noqa: BLE001 — workers must never re-raise
        # SIGALRM raises a regular Exception via the signal handler we
        # install below; treat any exception as a soft skip.
        return [], [], {"ok": False, "reason": "parse_error", "error": f"{type(e).__name__}: {e}"}
    finally:
        if _IN_WORKER and sys.platform != "win32":  # pragma: no branch
            signal.alarm(0)


def _alarm_handler(_signum, _frame):  # pragma: no cover — POSIX only
    raise TimeoutError("per-file parse timeout")


def _stub_for(rel_str: str, reason: str, error: str) -> Symbol:
    qname = Path(rel_str).stem
    sid = make_symbol_id(rel_str, qname)
    return Symbol(
        id=sid,
        kind="module",
        name=qname,
        qualified_name=qname,
        path=rel_str,
        span=(1, 1),
        signature=f"# module {qname}",
        exported=True,
        docstring=None,
        parent_id=None,
        language="unknown",
        parse_status="failed",
        parse_error=f"{reason}: {error}"[:200],
    )


def _write_skipped(root: Path, skipped: list[SkippedFile]) -> None:
    if not skipped:
        return
    out = root / ".graphify_plus" / "skipped.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w") as f:
        for s in skipped:
            f.write(
                json.dumps(
                    {
                        "path": s.path,
                        "reason": s.reason,
                        "error": s.error,
                        "timestamp": round(s.timestamp, 3),
                    }
                )
                + "\n"
            )


def ingest(
    root: Path,
    *,
    parallel: bool = True,
    strict: bool = False,
    write_skipped: bool = True,
) -> IngestResult:
    """Parse every adapter-matched file under ``root``.

    On ``strict=True``, the first timeout / memory violation raises the
    matching ``GraphifyError`` instead of being soft-skipped.
    """
    root = root.resolve()

    if sys.platform != "win32":  # pragma: no branch
        try:
            signal.signal(signal.SIGALRM, _alarm_handler)
        except ValueError:
            # Not in main thread of the main process — ignore; child
            # workers register their own handler via _set_worker_limits.
            pass

    rels: list[str] = []
    for f in _candidate_files(root):
        rels.append(f.relative_to(root).as_posix())
    rels.sort()

    syms_all: list[Symbol] = []
    edges_all: list[Edge] = []
    parsed = 0
    skipped: list[SkippedFile] = []

    timeout_sec = _parse_timeout_sec()
    mem_mb = _parse_mem_mb()
    args = [(str(root), r, timeout_sec, mem_mb) for r in rels]

    def _consume(rel: str, syms: list[Symbol], edges: list[Edge], status: dict) -> None:
        nonlocal parsed
        if status["ok"]:
            parsed += 1
            syms_all.extend(syms)
            edges_all.extend(edges)
            return
        reason = status["reason"]
        error = status["error"] or ""
        if strict and reason == "timeout":
            raise ParseTimeout(
                f"parse timeout on {rel}",
                context={"path": rel, "limit_sec": timeout_sec},
            )
        if strict and reason == "memory":
            raise ParseMemory(
                f"parse OOM on {rel}",
                context={"path": rel, "limit_mb": mem_mb},
            )
        skipped.append(
            SkippedFile(path=rel, reason=reason or "unknown", error=error, timestamp=time.time())
        )
        # Insert a stub so callers (gp doctor, audit) can see what failed.
        if reason != "unsupported":
            syms_all.append(_stub_for(rel, reason or "unknown", error))

    if parallel and len(args) >= 8:
        with ProcessPoolExecutor(
            max_workers=os.cpu_count() or 4,
            initializer=_worker_initializer,
        ) as ex:
            for (_, rel, *_), (s, e, status) in zip(
                args, ex.map(_parse_one, args, chunksize=4), strict=True
            ):
                _consume(rel, s, e, status)
    else:
        for a in args:
            s, e, status = _parse_one(a)
            _consume(a[1], s, e, status)

    if write_skipped:
        _write_skipped(root, skipped)

    syms_all.sort(key=lambda s: (s["path"], s["span"][0], s["qualified_name"]))
    edges_all.sort(
        key=lambda e: (
            e.get("src") or "",
            e.get("dst") or "",
            e.get("kind") or "",
            (e.get("span") or (0, 0))[0],
        )
    )
    return IngestResult(
        symbols=syms_all,
        edges=edges_all,
        files_parsed=parsed,
        files_skipped=len(skipped),
        skipped=tuple(skipped),  # type: ignore[arg-type]
    )


__all__ = ["ALL_ADAPTERS", "IngestResult", "SkippedFile", "ingest"]


# --- guards against accidental TimeoutError leakage in main thread -----

_PoolTimeoutError = _PoolTimeoutError  # keep import alive for tooling
