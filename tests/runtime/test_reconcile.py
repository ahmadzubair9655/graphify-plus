"""12.1 — watcher reconciliation tests."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from graphify_plus.runtime.reconcile import (
    PeriodicReconciler,
    install_git_hooks,
    reconcile_once,
)


def _init(repo: Path) -> None:
    (repo / "a.py").write_text("def f(): pass\n")
    (repo / "b.py").write_text("def g(): pass\n")
    subprocess.run(
        [sys.executable, "-m", "graphify_plus", "init", "--repo", str(repo), "--no-parallel"],
        check=True,
        capture_output=True,
    )


def test_reconcile_detects_vanished_files(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init(repo)
    (repo / "a.py").unlink()
    out = reconcile_once(repo)
    assert "a.py" in out["vanished"]


def test_reconcile_detects_changed_files(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init(repo)
    # Bump mtime forward.
    f = repo / "a.py"
    future = time.time() + 10
    os.utime(f, (future, future))
    out = reconcile_once(repo)
    assert "a.py" in out["changed"]


def test_reconcile_handles_no_cache(tmp_path: Path):
    out = reconcile_once(tmp_path)
    assert out == {"vanished": [], "changed": [], "unchanged": 0}


def test_install_git_hooks_writes_post_checkout_and_post_merge(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git" / "hooks").mkdir(parents=True)
    written = install_git_hooks(repo)
    names = sorted(p.name for p in written)
    assert names == ["post-checkout", "post-merge"]
    for p in written:
        body = p.read_text()
        assert "graphify-plus" in body
        assert os.access(p, os.X_OK)


def test_install_git_hooks_skips_user_hook(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    hooks = repo / ".git" / "hooks"
    hooks.mkdir(parents=True)
    user_hook = hooks / "post-checkout"
    user_hook.write_text("#!/bin/sh\necho user hook\n")
    written = install_git_hooks(repo)
    # post-checkout left alone, post-merge installed.
    assert user_hook.read_text() == "#!/bin/sh\necho user hook\n"
    assert any(p.name == "post-merge" for p in written)


def test_install_git_hooks_no_op_outside_git(tmp_path: Path):
    assert install_git_hooks(tmp_path) == []


def test_periodic_reconciler_fires(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init(repo)
    results: list[dict] = []
    pr = PeriodicReconciler(repo, results.append, interval_s=0.1)
    pr.start()
    try:
        deadline = time.time() + 2.0
        while time.time() < deadline and not results:
            time.sleep(0.05)
    finally:
        pr.stop()
    assert results, "reconciler did not fire"
