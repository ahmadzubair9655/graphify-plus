"""11.10 — gp explain command."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_explain_renders_neighbours(tmp_path: Path):
    repo = tmp_path / "sample"
    repo.mkdir()
    (repo / "a.py").write_text("def f():\n    return 1\n")
    subprocess.run(
        [sys.executable, "-m", "graphify_plus", "init", "--repo", str(repo), "--no-parallel"],
        check=True,
        capture_output=True,
    )
    out = subprocess.run(
        [sys.executable, "-m", "graphify_plus", "explain", "--repo", str(repo), "f"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "function" in out.stdout
    assert "Outbound edges" in out.stdout
    assert "Inbound edges" in out.stdout
