"""12.1 — Watcher reconciliation.

Three responsibilities:

1. **Initial sweep** at watcher startup — compare the set of paths
   recorded in the cache against the live filesystem. Paths missing
   from the filesystem are dropped from the cache (their symbols and
   edges are removed). Paths whose mtime is newer than what the cache
   recorded are scheduled for re-ingest.

2. **Periodic mtime sweep** every 60 s, same logic as the initial
   sweep — catches changes the OS event stream missed (NFS mounts,
   crash-recovery, etc.).

3. **Git-hook bridge** — ``install_git_hooks(repo)`` writes
   ``post-checkout`` and ``post-merge`` hooks that run
   ``graphify-plus init --repo . --reconcile`` so a branch switch
   triggers a forced reconcile.

The functions here are I/O-only — the watcher invokes them and
delivers any resulting file updates through its existing dispatch.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

from .store import Store, cache_path

SWEEP_INTERVAL_S = 60.0
MTIME_META_KEY_PREFIX = "mtime:"


def _path_mtime(p: Path) -> float | None:
    try:
        return p.stat().st_mtime
    except OSError:
        return None


def _cached_mtime(store: Store, rel_path: str) -> float | None:
    raw = store.get_meta(MTIME_META_KEY_PREFIX + rel_path)
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _record_mtime(store: Store, rel_path: str, mtime: float) -> None:
    store.set_meta(MTIME_META_KEY_PREFIX + rel_path, str(mtime))


def reconcile_once(repo: Path) -> dict:
    """Run one reconciliation sweep against the cache. Returns
    ``{"vanished": [...], "changed": [...], "unchanged": int}``.

    The watcher is responsible for re-ingesting ``changed`` paths and
    removing ``vanished`` paths from its symbol set.
    """
    repo = repo.resolve()
    db = cache_path(repo)
    if not db.exists():
        return {"vanished": [], "changed": [], "unchanged": 0}
    store = Store(db)
    try:
        symbols = store.all_symbols()
    finally:
        store.close()
    seen_paths: set[str] = set()
    for s in symbols:
        p = s.get("path")
        if p:
            seen_paths.add(p)

    vanished: list[str] = []
    changed: list[str] = []
    unchanged = 0
    store = Store(db)
    try:
        for rel in sorted(seen_paths):
            full = repo / rel
            mt = _path_mtime(full)
            if mt is None:
                vanished.append(rel)
                continue
            cached_mt = _cached_mtime(store, rel)
            if cached_mt is None or mt > cached_mt:
                changed.append(rel)
                _record_mtime(store, rel, mt)
            else:
                unchanged += 1
    finally:
        store.close()
    return {"vanished": vanished, "changed": changed, "unchanged": unchanged}


class PeriodicReconciler:
    """Runs ``reconcile_once`` every ``interval_s`` seconds in a daemon thread.

    The on_result callback is invoked with the reconcile dict so the
    watcher can dispatch follow-up updates.
    """

    def __init__(
        self,
        repo: Path,
        on_result,
        *,
        interval_s: float = SWEEP_INTERVAL_S,
    ):
        self.repo = repo
        self.on_result = on_result
        self.interval_s = interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        t = threading.Thread(target=self._loop, daemon=True, name="gp-reconciler")
        self._thread = t
        t.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_s):
            try:
                result = reconcile_once(self.repo)
            except Exception:  # noqa: BLE001
                continue
            try:
                self.on_result(result)
            except Exception:  # noqa: BLE001
                continue


_HOOK_BODY = """#!/usr/bin/env bash
# Installed by graphify-plus (12.1 — watcher reconciliation).
exec graphify-plus init --repo "$(git rev-parse --show-toplevel)" --reconcile >/dev/null 2>&1 || true
"""


def install_git_hooks(repo: Path) -> list[Path]:
    """Write post-checkout + post-merge hooks. Returns the paths
    written. Refuses to overwrite an existing user-authored hook
    (one that does not contain the graphify-plus marker comment).
    """
    git_dir = repo / ".git"
    if not git_dir.is_dir():
        return []
    hooks_dir = git_dir / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name in ("post-checkout", "post-merge"):
        p = hooks_dir / name
        if p.exists():
            existing = p.read_text(errors="replace")
            if "graphify-plus" not in existing:
                continue  # leave user's hook alone
        p.write_text(_HOOK_BODY)
        os.chmod(p, 0o755)
        written.append(p)
    return written


__all__ = [
    "PeriodicReconciler",
    "SWEEP_INTERVAL_S",
    "install_git_hooks",
    "reconcile_once",
]
