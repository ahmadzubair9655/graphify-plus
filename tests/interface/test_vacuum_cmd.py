"""12.3 — gp vacuum reports before/after and reclaims artefacts."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path


def _init(repo: Path) -> None:
    (repo / "a.py").write_text("def f(): pass\n")
    subprocess.run(
        [sys.executable, "-m", "graphify_plus", "init", "--repo", str(repo), "--no-parallel"],
        check=True,
        capture_output=True,
    )


def test_vacuum_prints_before_after_and_removes_bak(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init(repo)
    cache_dir = repo / ".graphify_plus"
    bak = cache_dir / "cache.db.bak"
    bak.write_bytes(b"x" * 1024)
    out = subprocess.run(
        [sys.executable, "-m", "graphify_plus", "vacuum", "--repo", str(repo)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "before:" in out.stdout
    assert "after:" in out.stdout
    assert "saved:" in out.stdout
    assert "removed cache.db.bak" in out.stdout
    assert not bak.exists()


def test_vacuum_removes_old_quarantine(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init(repo)
    cache_dir = repo / ".graphify_plus"
    old = cache_dir / "cache.db.corrupt.19700101T000000Z"
    old.write_bytes(b"x")
    eight_days_ago = time.time() - (8 * 24 * 3600)
    os.utime(old, (eight_days_ago, eight_days_ago))
    subprocess.run(
        [sys.executable, "-m", "graphify_plus", "vacuum", "--repo", str(repo)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert not old.exists()
