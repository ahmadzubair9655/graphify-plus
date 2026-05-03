"""End-to-end test for ``gp skeleton``."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "sample_repo"


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "graphify_plus", *args],
        capture_output=True,
        check=False,
    )


def test_skeleton_file(tmp_path: Path):
    target = tmp_path / "repo"
    shutil.copytree(FIXTURE, target)
    init = _run("init", "--repo", str(target), "--no-parallel")
    assert init.returncode == 0, init.stderr.decode()

    out = _run(
        "skeleton",
        "--repo",
        str(target),
        "--file",
        "backend/billing/invoice.py",
    )
    assert out.returncode == 0, out.stderr.decode()
    text = out.stdout.decode()
    assert "@python class invoice.Invoice" in text
    assert "def total(self, tax_rate: float = 0.20) -> float" in text
    assert "« body omitted" in text


def test_skeleton_target_with_suffix_match(tmp_path: Path):
    target = tmp_path / "repo"
    shutil.copytree(FIXTURE, target)
    _run("init", "--repo", str(target), "--no-parallel")
    out = _run("skeleton", "--repo", str(target), "--target", "Invoice.total")
    assert out.returncode == 0, out.stderr.decode()
    assert b"def total" in out.stdout
