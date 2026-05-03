"""Repo walk + AST parse orchestrator.

Honours .gitignore + a default deny-list of build/vendor directories.
Dispatches each file to the matching ``LangAdapter`` and merges results
deterministically. Parallelism is opt-in via ``parallel=True``.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import pathspec

from .adapters import ALL_ADAPTERS, Edge, Symbol, adapter_for

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


@dataclass(frozen=True)
class IngestResult:
    symbols: list[Symbol]
    edges: list[Edge]
    files_parsed: int
    files_skipped: int


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


def _parse_one(args: tuple[str, str]) -> tuple[list[Symbol], list[Edge], bool]:
    root_str, rel_str = args
    full = Path(root_str) / rel_str
    ad = adapter_for(full.name)
    if ad is None:
        return [], [], False
    try:
        source = full.read_bytes()
        # Rewrite path on each Symbol/Edge to use repo-relative POSIX.
        syms, edges = ad.parse(Path(rel_str), source)
        return syms, edges, True
    except Exception:
        return [], [], False


def ingest(root: Path, *, parallel: bool = True) -> IngestResult:
    root = root.resolve()
    rels: list[str] = []
    for f in _candidate_files(root):
        rels.append(f.relative_to(root).as_posix())
    rels.sort()

    syms_all: list[Symbol] = []
    edges_all: list[Edge] = []
    parsed = 0
    skipped = 0

    args = [(str(root), r) for r in rels]
    if parallel and len(args) >= 8:
        with ProcessPoolExecutor(max_workers=os.cpu_count() or 4) as ex:
            for s, e, ok in ex.map(_parse_one, args, chunksize=4):
                if ok:
                    parsed += 1
                    syms_all.extend(s)
                    edges_all.extend(e)
                else:
                    skipped += 1
    else:
        for a in args:
            s, e, ok = _parse_one(a)
            if ok:
                parsed += 1
                syms_all.extend(s)
                edges_all.extend(e)
            else:
                skipped += 1

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
        symbols=syms_all, edges=edges_all, files_parsed=parsed, files_skipped=skipped
    )


__all__ = ["ALL_ADAPTERS", "IngestResult", "ingest"]
