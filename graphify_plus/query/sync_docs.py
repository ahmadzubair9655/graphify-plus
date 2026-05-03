"""Sync-Docs daemon.

Watches the target repo via the Phase 2 watcher; when a file in the
manifest's dependency order is touched, marks the corresponding step
as done and re-renders ``STAGING_PLAN.md``.

3-way merge: the daemon never overwrites human-authored content outside
the auto-generated headings (``## Dependency order`` and
``## Validation checkpoints`` blocks). Anything the user wrote elsewhere
is preserved verbatim. Use ``--force`` to regenerate the entire file.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..core.symbol_graph import build as build_graph
from ..runtime.store import Store, cache_path
from ..runtime.watcher import FileUpdate, Watcher
from .manifest import Manifest, generate, render

log = logging.getLogger("graphify_plus.sync_docs")

PLAN_FILENAME = "STAGING_PLAN.md"
BACKUP_NAME = "staging_plan.bak.md"
SECTION_HEADERS = ("## Dependency order", "## Validation checkpoints", "## Notes")


@dataclass
class SyncState:
    plan_path: Path
    backup_path: Path
    completed_paths: set[str]


def _section_block(text: str, header: str) -> tuple[int, int] | None:
    """Return (start, end) line indices of the named section, exclusive of
    next ``## `` heading. ``None`` if header is missing.
    """
    lines = text.splitlines()
    start: int | None = None
    for i, line in enumerate(lines):
        if line.strip() == header:
            start = i
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if re.match(r"^## (?!.*\b)", lines[j]) or lines[j].startswith("## "):
            end = j
            break
    return start, end


def merge_into_existing(existing: str, regenerated: str) -> str:
    """Replace each auto-generated section in ``existing`` with the
    matching section from ``regenerated``; leave everything else alone.
    """
    if not existing.strip():
        return regenerated
    out_lines = existing.splitlines()
    for header in SECTION_HEADERS:
        new_block = _section_block(regenerated, header)
        if new_block is None:
            continue
        new_start, new_end = new_block
        new_segment = "\n".join(regenerated.splitlines()[new_start:new_end])

        old_block = _section_block("\n".join(out_lines), header)
        if old_block is None:
            # Append at the end before any trailing blank lines.
            out_lines.append("")
            out_lines.extend(new_segment.splitlines())
        else:
            old_start, old_end = old_block
            out_lines = out_lines[:old_start] + new_segment.splitlines() + out_lines[old_end:]
    out = "\n".join(out_lines)
    if not out.endswith("\n"):
        out += "\n"
    return out


def write_plan(repo: Path, manifest: Manifest, *, force: bool = False) -> Path:
    """Write ``STAGING_PLAN.md`` at the repo root, merging into existing
    content unless ``force`` is set.
    """
    plan_path = repo / PLAN_FILENAME
    backup_path = repo / ".graphify_plus" / BACKUP_NAME
    backup_path.parent.mkdir(parents=True, exist_ok=True)

    rendered = render(manifest)
    if force or not plan_path.exists():
        if plan_path.exists():
            backup_path.write_text(plan_path.read_text(errors="replace"))
        plan_path.write_text(rendered)
        return plan_path

    existing = plan_path.read_text(errors="replace")
    backup_path.write_text(existing)
    merged = merge_into_existing(existing, rendered)
    plan_path.write_text(merged)
    return plan_path


def mark_completed(plan_path: Path, completed_paths: set[str]) -> None:
    """Annotate the dependency-order table with ✓ for paths the watcher
    has seen change since the manifest was generated.
    """
    if not plan_path.exists():
        return
    text = plan_path.read_text(errors="replace")
    out_lines: list[str] = []
    for line in text.splitlines():
        # Rows look like: | 1 | `function` | `qname` | `path/to/file.py` | 0.42 |
        m = re.match(r"^\|\s*\d+\s*\|.*\|\s*`([^`]+)`\s*\|\s*[\d.]+\s*\|\s*$", line)
        if m and m.group(1) in completed_paths and "✓" not in line:
            out_lines.append(line[:-1] + " ✓ |")
        else:
            out_lines.append(line)
    plan_path.write_text("\n".join(out_lines) + "\n")


# ---------- daemon ------------------------------------------------------


class SyncDocsDaemon:
    """Watches the repo and updates STAGING_PLAN.md as the agent works."""

    def __init__(self, repo: Path):
        self.repo = repo.resolve()
        self.state = SyncState(
            plan_path=self.repo / PLAN_FILENAME,
            backup_path=self.repo / ".graphify_plus" / BACKUP_NAME,
            completed_paths=set(),
        )
        self._watcher: Watcher | None = None
        self._lock = threading.Lock()

    def start(self, on_event: Callable[[FileUpdate], None] | None = None) -> None:
        w = Watcher(self.repo)
        w.subscribe(self._handle)
        if on_event is not None:
            w.subscribe(on_event)
        w.start()
        self._watcher = w

    def stop(self) -> None:
        if self._watcher is not None:
            self._watcher.stop()
            self._watcher = None

    def _handle(self, update: FileUpdate) -> None:
        with self._lock:
            self.state.completed_paths.add(update.path)
            mark_completed(self.state.plan_path, self.state.completed_paths)


def regenerate(repo: Path, task: str, *, force: bool = False) -> Path:
    """One-shot helper used by ``gp plan``."""
    repo = repo.resolve()
    store = Store(cache_path(repo))
    try:
        symbols = store.all_symbols()
        edges = store.all_edges()
        G = build_graph(symbols, edges)
        manifest = generate(task, G, store)
    finally:
        store.close()
    return write_plan(repo, manifest, force=force)


__all__ = [
    "SECTION_HEADERS",
    "SyncDocsDaemon",
    "mark_completed",
    "merge_into_existing",
    "regenerate",
    "write_plan",
]
