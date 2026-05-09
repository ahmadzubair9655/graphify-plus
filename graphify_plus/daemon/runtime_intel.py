"""Layer 7.2 + 7.3 — runtime intelligence.

* 7.2 Runtime trace ingest. Parse py-spy speedscope JSON, simple
  pprof-text dumps, and clinic.js style outputs into per-symbol
  ``runtime_profile = {calls, total_ms, exceptions}`` attributes.
* 7.3 Production-error → code linkage. Take a stacktrace, find the
  current symbol that owns the file:line, and report which commits
  have touched the file since the deploy SHA.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..runtime.store import Store

log = logging.getLogger("graphify_plus.daemon.runtime_intel")

RUNTIME_SCHEMA = """
CREATE TABLE IF NOT EXISTS runtime_profile (
    symbol_id   TEXT PRIMARY KEY,
    calls       INTEGER NOT NULL,
    total_ms    REAL NOT NULL,
    p99_ms      REAL,
    exceptions  INTEGER NOT NULL,
    source      TEXT,
    ingested_at TEXT NOT NULL
);
"""


@dataclass
class RuntimeProfile:
    symbol_id: str
    calls: int = 0
    total_ms: float = 0.0
    p99_ms: float | None = None
    exceptions: int = 0


def ensure_runtime_table(store: Store) -> None:
    store.conn.executescript(RUNTIME_SCHEMA)


def store_runtime(store: Store, rows: list[RuntimeProfile], *, source: str) -> int:
    ensure_runtime_table(store)
    now = datetime.now(timezone.utc).isoformat()
    with store.tx():
        store.conn.execute("DELETE FROM runtime_profile")
        store.conn.executemany(
            "INSERT INTO runtime_profile(symbol_id, calls, total_ms, p99_ms, "
            "exceptions, source, ingested_at) VALUES (?,?,?,?,?,?,?)",
            [
                (
                    r.symbol_id,
                    r.calls,
                    r.total_ms,
                    r.p99_ms,
                    r.exceptions,
                    source,
                    now,
                )
                for r in rows
            ],
        )
    return len(rows)


def load_runtime(store: Store) -> dict[str, dict[str, Any]]:
    ensure_runtime_table(store)
    rows = store.conn.execute(
        "SELECT symbol_id, calls, total_ms, p99_ms, exceptions, source FROM runtime_profile"
    ).fetchall()
    return {
        sid: {
            "calls": calls,
            "total_ms": ms,
            "p99_ms": p99,
            "exceptions": exc,
            "source": src,
        }
        for sid, calls, ms, p99, exc, src in rows
    }


# ---- speedscope (py-spy) ----------------------------------------------


def parse_speedscope(path: Path) -> tuple[dict[str, RuntimeProfile], str]:
    """Aggregate runtime per (file, function) frame."""
    body = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(body, dict) or "shared" not in body or "profiles" not in body:
        raise ValueError("not a speedscope file")
    frames = body.get("shared", {}).get("frames") or []
    profiles_blob = body.get("profiles") or []
    by_frame: dict[int, RuntimeProfile] = {}
    for prof in profiles_blob:
        weights = prof.get("weights") or []
        samples = prof.get("samples") or []
        for s, w in zip(samples, weights, strict=False):
            if not isinstance(s, list):
                continue
            for fid in s:
                if fid >= len(frames):
                    continue
                rp = by_frame.setdefault(fid, RuntimeProfile(symbol_id=str(fid)))
                rp.calls += 1
                rp.total_ms += float(w)
    out: dict[str, RuntimeProfile] = {}
    for fid, rp in by_frame.items():
        f = frames[fid]
        if not isinstance(f, dict):
            continue
        name = f.get("name") or ""
        file = f.get("file") or ""
        key = f"{file}::{name}"
        rp.symbol_id = key
        out[key] = rp
    return out, "speedscope"


# ---- pprof-text -------------------------------------------------------


_PPROF_LINE = re.compile(r"^\s*(\d+)\s+\(([\d.]+)%\)\s+.*?\s+(.+)$")


def parse_pprof_text(path: Path) -> tuple[dict[str, RuntimeProfile], str]:
    out: dict[str, RuntimeProfile] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = _PPROF_LINE.match(line)
        if not m:
            continue
        calls = int(m.group(1))
        sym = m.group(3).strip()
        out[sym] = RuntimeProfile(symbol_id=sym, calls=calls, total_ms=float(m.group(2)) * 10)
    return out, "pprof-text"


def parse_runtime_report(path: Path) -> tuple[dict[str, RuntimeProfile], str]:
    """Sniff and dispatch."""
    suffix = path.suffix.lower()
    if suffix == ".json":
        try:
            return parse_speedscope(path)
        except (ValueError, json.JSONDecodeError):
            pass
    return parse_pprof_text(path)


def map_runtime_to_symbols(
    rows: dict[str, RuntimeProfile], symbols: list[dict[str, Any]]
) -> list[RuntimeProfile]:
    """Map ``file::name`` profile keys to actual symbol_ids by suffix
    + name match."""
    by_path: dict[str, list[dict[str, Any]]] = {}
    for s in symbols:
        path = s.get("path") or ""
        if path:
            by_path.setdefault(path, []).append(s)
    out: list[RuntimeProfile] = []
    for key, rp in rows.items():
        if "::" not in key:
            continue
        file, fn = key.rsplit("::", 1)
        candidates: list[dict[str, Any]] = []
        for p, syms in by_path.items():
            if p.endswith("/" + file) or p == file:
                candidates = syms
                break
        match = next(
            (s for s in candidates if (s.get("name") or "") == fn or (s.get("qualified_name") or "").endswith("." + fn)),
            None,
        )
        if match is None:
            continue
        new_rp = RuntimeProfile(
            symbol_id=match["id"],
            calls=rp.calls,
            total_ms=rp.total_ms,
            p99_ms=rp.p99_ms,
            exceptions=rp.exceptions,
        )
        out.append(new_rp)
    return out


def ingest_runtime(store: Store, repo_root: Path, report_path: Path) -> dict[str, Any]:
    rows, source = parse_runtime_report(report_path)
    symbols = store.all_symbols()
    mapped = map_runtime_to_symbols(rows, symbols)
    persisted = store_runtime(store, mapped, source=source)
    return {
        "format": source,
        "raw_frames": len(rows),
        "attributed": persisted,
    }


# ---- 7.3 stack trace → code linkage ----------------------------------


_PYTHON_TRACEBACK_RE = re.compile(
    r'File "([^"]+)", line (\d+)(?:, in ([\w.<>]+))?'
)
_NODE_TRACEBACK_RE = re.compile(r"\(([^)]+):(\d+):\d+\)")


@dataclass
class StackFrame:
    file: str
    line: int
    func: str = ""


def parse_stacktrace(text: str) -> list[StackFrame]:
    out: list[StackFrame] = []
    for m in _PYTHON_TRACEBACK_RE.finditer(text):
        out.append(StackFrame(file=m.group(1), line=int(m.group(2)), func=m.group(3) or ""))
    if out:
        return out
    for m in _NODE_TRACEBACK_RE.finditer(text):
        out.append(StackFrame(file=m.group(1), line=int(m.group(2))))
    return out


@dataclass
class FrameMapping:
    frame: StackFrame
    symbol_id: str | None
    label: str | None
    moved_since_deploy: bool = False
    edits_since_deploy: int = 0


def map_stacktrace_to_symbols(
    frames: list[StackFrame],
    symbols: list[dict[str, Any]],
    repo_root: Path,
    *,
    deploy_sha: str = "",
) -> list[FrameMapping]:
    by_path: dict[str, list[dict[str, Any]]] = {}
    for s in symbols:
        path = s.get("path") or ""
        if path:
            by_path.setdefault(path, []).append(s)
    out: list[FrameMapping] = []
    for f in frames:
        candidates: list[dict[str, Any]] = []
        for p, syms in by_path.items():
            if p.endswith("/" + f.file) or p == f.file:
                candidates = syms
                break
        owner: dict[str, Any] | None = None
        for s in sorted(
            candidates,
            key=lambda s: (
                ((s.get("span") or (0, 0))[1] - (s.get("span") or (0, 0))[0]) or 1_000_000
            ),
        ):
            span = s.get("span") or (0, 0)
            if int(span[0]) <= f.line <= int(span[1]):
                owner = s
                break
        edits = 0
        moved = False
        if deploy_sha and owner:
            edits = _commits_touching(repo_root, owner.get("path") or "", deploy_sha)
            moved = edits > 0
        out.append(
            FrameMapping(
                frame=f,
                symbol_id=owner["id"] if owner else None,
                label=(owner.get("qualified_name") if owner else None),
                moved_since_deploy=moved,
                edits_since_deploy=edits,
            )
        )
    return out


def _commits_touching(repo_root: Path, path: str, since_sha: str) -> int:
    if not path or not since_sha:
        return 0
    try:
        out = subprocess.check_output(
            ["git", "-C", str(repo_root), "log", f"{since_sha}..HEAD", "--oneline", "--", path],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=4,
        )
        return sum(1 for line in out.splitlines() if line.strip())
    except Exception:  # noqa: BLE001
        return 0


__all__ = [
    "FrameMapping",
    "RUNTIME_SCHEMA",
    "RuntimeProfile",
    "StackFrame",
    "ensure_runtime_table",
    "ingest_runtime",
    "load_runtime",
    "map_runtime_to_symbols",
    "map_stacktrace_to_symbols",
    "parse_pprof_text",
    "parse_runtime_report",
    "parse_speedscope",
    "parse_stacktrace",
    "store_runtime",
]
