"""12.2 — repo-level lock at ``<repo>/.graphify_plus/.lock``.

A simple advisory lock for serialising ``gp`` invocations against the
same target repo. The lock file holds the holder's PID and an ISO-8601
timestamp. Stale locks (PID no longer alive per ``os.kill(pid, 0)``)
are silently reclaimed — without this check a crashed process would
permanently jam the tool.

Use as a context manager::

    from graphify_plus.runtime.repolock import RepoLock
    with RepoLock(repo_root):
        ...  # exclusive section

If another live process holds the lock, ``RepoLock`` raises
``RepoLocked``.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

LOCK_NAME = ".lock"


class RepoLocked(RuntimeError):
    pass


def _lock_path(repo_root: Path) -> Path:
    return repo_root / ".graphify_plus" / LOCK_NAME


def _is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we can't signal it — treat as alive.
        return True
    except OSError:
        return False
    return True


def _read_holder(p: Path) -> dict | None:
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


class RepoLock:
    def __init__(self, repo_root: Path, *, retry_for_s: float = 0.0):
        self.repo_root = repo_root
        self.path = _lock_path(repo_root)
        self.retry_for_s = retry_for_s
        self._held = False

    def __enter__(self) -> RepoLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + max(self.retry_for_s, 0.0)
        while True:
            if self._try_acquire():
                self._held = True
                return self
            holder = _read_holder(self.path)
            pid = int(holder.get("pid", 0)) if holder else 0
            if not _is_alive(pid):
                # Stale lock — reclaim.
                try:
                    self.path.unlink()
                except OSError:
                    pass
                continue
            if time.monotonic() >= deadline:
                raise RepoLocked(
                    f"repo {self.repo_root} is locked by pid {pid} "
                    f"since {holder.get('ts') if holder else '?'}"
                )
            time.sleep(0.1)

    def _try_acquire(self) -> bool:
        try:
            fd = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            return False
        try:
            payload = json.dumps(
                {"pid": os.getpid(), "ts": datetime.now(timezone.utc).isoformat()},
                sort_keys=True,
            )
            os.write(fd, payload.encode("utf-8"))
        finally:
            os.close(fd)
        return True

    def __exit__(self, *_exc) -> None:
        if not self._held:
            return
        # Only unlink if we still own it.
        holder = _read_holder(self.path)
        if holder and int(holder.get("pid", -1)) == os.getpid():
            try:
                self.path.unlink()
            except OSError:
                pass
        self._held = False


__all__ = ["LOCK_NAME", "RepoLock", "RepoLocked"]
