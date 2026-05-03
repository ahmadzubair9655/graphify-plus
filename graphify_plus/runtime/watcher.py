"""Live AST watcher.

Watches the target repo's source tree. On any save it:

  1. Re-parses *only* the changed file with the appropriate adapter.
  2. Computes the symbol delta against the cached state.
  3. Writes the delta into SQLite in a single transaction.
  4. Re-skeletonises changed symbols and updates the CAS + symbol→hash
     map.
  5. Notifies subscribers (used by Phase 5's sync-docs daemon).

Latency budget: ≤80 ms p95 per single-file save (small file, warm cache).

Design notes
------------
- Uses ``watchdog.Observer`` on Linux/Windows. macOS and any path with
  ``GP_FORCE_POLLING=1`` set falls back to ``PollingObserver`` because
  kqueue silently drops events on directories with > ~10k inodes.
- Events are debounced at 250 ms per path — many editors save twice (a
  temp file then atomic rename) and we only want to re-parse once.
- Tests must NOT use ``time.sleep`` for synchronisation. The watcher
  exposes a ``wait_for_path`` helper backed by ``threading.Event``.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver

from ..core import cas
from ..core.adapters import Edge, Symbol, adapter_for
from ..core.skeletonizer import skeletonize_all
from .store import Store, cache_path

log = logging.getLogger("graphify_plus.watcher")

DEBOUNCE_MS = 250


@dataclass
class FileUpdate:
    """A single observed-and-applied file change."""

    path: str  # repo-relative POSIX
    added: list[str] = field(default_factory=list)  # symbol ids
    removed: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    elapsed_ms: float = 0.0


def _use_polling() -> bool:
    if os.environ.get("GP_FORCE_POLLING") == "1":
        return True
    # macOS kqueue dropouts on large trees → polling is the safer default.
    import platform

    return platform.system() == "Darwin"


class _Debouncer:
    """Coalesces bursts of events on the same path."""

    def __init__(self, interval_s: float, fn: Callable[[str], None]) -> None:
        self._interval = interval_s
        self._fn = fn
        self._timers: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()

    def trigger(self, path: str) -> None:
        with self._lock:
            existing = self._timers.pop(path, None)
            if existing is not None:
                existing.cancel()

            def _fire() -> None:
                with self._lock:
                    self._timers.pop(path, None)
                try:
                    self._fn(path)
                except Exception:
                    log.exception("watcher: handler failed for %s", path)

            t = threading.Timer(self._interval, _fire)
            t.daemon = True
            self._timers[path] = t
            t.start()

    def cancel_all(self) -> None:
        with self._lock:
            for t in self._timers.values():
                t.cancel()
            self._timers.clear()


class _Handler(FileSystemEventHandler):
    def __init__(self, on_change: Callable[[str], None], repo: Path) -> None:
        self._on_change = on_change
        self._repo = repo

    def _maybe_dispatch(self, event_path: str) -> None:
        try:
            full = Path(event_path).resolve()
            rel = full.relative_to(self._repo).as_posix()
        except (ValueError, OSError):
            return
        if adapter_for(full.name) is None:
            return
        if ".graphify_plus" in rel:
            return
        self._on_change(rel)

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._maybe_dispatch(event.src_path)

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._maybe_dispatch(event.src_path)

    def on_deleted(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._maybe_dispatch(event.src_path)

    def on_moved(self, event: FileSystemEvent) -> None:
        # both ends matter
        if not event.is_directory:
            src = getattr(event, "src_path", None)
            dst = getattr(event, "dest_path", None)
            if src:
                self._maybe_dispatch(src)
            if dst:
                self._maybe_dispatch(dst)


class Watcher:
    """Live watcher orchestrator. Single-process, single-thread reparse.

    Usage::

        w = Watcher(repo_root)
        w.subscribe(my_callback)
        w.start()
        ...
        w.stop()
    """

    def __init__(self, repo: Path):
        self.repo = repo.resolve()
        self._observer = PollingObserver() if _use_polling() else Observer()
        self._handler = _Handler(self._enqueue, self.repo)
        self._debouncer = _Debouncer(DEBOUNCE_MS / 1000, self._handle)
        self._subs: list[Callable[[FileUpdate], None]] = []
        self._path_events: dict[str, threading.Event] = {}
        self._path_events_lock = threading.Lock()
        self._started = False

    # ---- subscription API ----------------------------------------------
    def subscribe(self, fn: Callable[[FileUpdate], None]) -> None:
        self._subs.append(fn)

    def wait_for_path(self, rel_path: str, timeout: float = 5.0) -> bool:
        """Block until the next FileUpdate for ``rel_path`` is delivered.

        Test-only synchronisation helper — never use ``time.sleep`` for
        watcher coordination.
        """
        with self._path_events_lock:
            ev = self._path_events.setdefault(rel_path, threading.Event())
        return ev.wait(timeout)

    # ---- lifecycle ------------------------------------------------------
    def start(self) -> None:
        if self._started:
            return
        self._observer.schedule(self._handler, str(self.repo), recursive=True)
        self._observer.start()
        self._started = True
        log.info("watcher started on %s (polling=%s)", self.repo, _use_polling())

    def stop(self) -> None:
        if not self._started:
            return
        self._debouncer.cancel_all()
        self._observer.stop()
        self._observer.join(timeout=5.0)
        self._started = False

    # ---- internals ------------------------------------------------------
    def _enqueue(self, rel_path: str) -> None:
        self._debouncer.trigger(rel_path)

    def _handle(self, rel_path: str) -> None:
        t0 = time.perf_counter()
        full = self.repo / rel_path
        store = Store(cache_path(self.repo))
        try:
            existing = store.symbols_in_path(rel_path)
            old_ids = {s["id"] for s in existing}

            new_syms: list[Symbol] = []
            new_edges: list[Edge] = []
            if full.exists():
                ad = adapter_for(full.name)
                if ad is not None:
                    try:
                        new_syms, new_edges = ad.parse(Path(rel_path), full.read_bytes())
                    except Exception:
                        log.exception("watcher: parse failed for %s", rel_path)
                        return

            new_ids = {s["id"] for s in new_syms}
            added = sorted(new_ids - old_ids)
            removed = sorted(old_ids - new_ids)
            persistent = old_ids & new_ids
            old_by_id = {s["id"]: s for s in existing}
            new_by_id = {s["id"]: s for s in new_syms}
            changed = sorted(sid for sid in persistent if old_by_id[sid] != new_by_id[sid])

            import orjson

            with store.tx():
                if old_ids:
                    placeholders = ",".join(["?"] * len(old_ids))
                    params = tuple(old_ids)
                    store.conn.execute(f"DELETE FROM symbols WHERE id IN ({placeholders})", params)
                    store.conn.execute(
                        f"DELETE FROM symbol_skeletons WHERE symbol_id IN ({placeholders})",
                        params,
                    )
                    store.conn.execute(f"DELETE FROM edges WHERE src IN ({placeholders})", params)
                if new_syms:
                    store.conn.executemany(
                        "INSERT INTO symbols(id, json) VALUES (?, ?)",
                        [(s["id"], orjson.dumps(s, option=orjson.OPT_SORT_KEYS)) for s in new_syms],
                    )
                if new_edges:
                    store.conn.executemany(
                        "INSERT INTO edges(src, dst, kind, json) VALUES (?, ?, ?, ?)",
                        [
                            (
                                e["src"],
                                e["dst"],
                                e.get("kind") or "",
                                orjson.dumps(e, option=orjson.OPT_SORT_KEYS),
                            )
                            for e in new_edges
                        ],
                    )

            # Skeletons (outside the main tx — CAS dedup is idempotent).
            sk = skeletonize_all(new_syms)
            for sid, body in sk.items():
                h = cas.put(store, body)
                store.link_skeleton(sid, h)

            update = FileUpdate(
                path=rel_path,
                added=added,
                removed=removed,
                changed=changed,
                elapsed_ms=(time.perf_counter() - t0) * 1000,
            )
        finally:
            store.close()

        # Fan-out
        for fn in self._subs:
            try:
                fn(update)
            except Exception:
                log.exception("watcher subscriber raised")

        with self._path_events_lock:
            ev = self._path_events.get(rel_path)
            if ev is not None:
                ev.set()
                # Reset for next round-trip.
                self._path_events[rel_path] = threading.Event()


__all__ = ["FileUpdate", "Watcher"]
