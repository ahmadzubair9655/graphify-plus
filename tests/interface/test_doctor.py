from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from graphify_plus.core import cas
from graphify_plus.core.ingest import ingest
from graphify_plus.core.skeletonizer import skeletonize_all
from graphify_plus.interface.cli.doctor_cmd import diagnose, render
from graphify_plus.runtime.store import Store, cache_path

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "sample_repo"


def _seed(tmp_path: Path) -> Path:
    target = tmp_path / "repo"
    shutil.copytree(FIXTURE, target)
    res = ingest(target, parallel=False)
    skel = skeletonize_all(res.symbols)
    s = Store(cache_path(target))
    try:
        s.replace_all(res.symbols, res.edges)
        s.set_meta("repo_root", str(target))
        s.set_meta("symbol_count", str(len(res.symbols)))
        s.set_meta("edge_count", str(len(res.edges)))
        for sid, body in skel.items():
            h = cas.put(s, body)
            s.link_skeleton(sid, h)
    finally:
        s.close()
    return target


def test_diagnose_reports_healthy_seeded_repo(tmp_path: Path):
    repo = _seed(tmp_path)
    diags = diagnose(repo)
    assert any(d.section == "System" for d in diags)
    assert any(d.section == "Cache" and d.severity == "OK" for d in diags)
    assert all(d.severity != "ERROR" for d in diags)
    text = render(diags)
    assert "Diagnostic Report" in text
    assert "Cache" in text


def test_diagnose_reports_missing_cache(tmp_path: Path):
    diags = diagnose(tmp_path)  # never initialised
    assert any(d.section == "Cache" and d.severity == "ERROR" for d in diags)


def test_diagnose_reports_skipped_files(tmp_path: Path):
    repo = _seed(tmp_path)
    skipped = repo / ".graphify_plus" / "skipped.jsonl"
    skipped.write_text(
        '{"path":"x.py","reason":"timeout","error":"alarm","timestamp":1}\n'
        '{"path":"y.go","reason":"parse_error","error":"oops","timestamp":1}\n'
    )
    diags = diagnose(repo)
    assert any(d.section == "Adapter coverage" and d.severity == "WARN" for d in diags)


def test_doctor_cli_exit_codes(tmp_path: Path):
    repo = _seed(tmp_path)
    res = subprocess.run(
        [sys.executable, "-m", "graphify_plus", "doctor", "--repo", str(repo)],
        capture_output=True,
        check=False,
    )
    assert res.returncode == 0, res.stderr.decode()

    res2 = subprocess.run(
        [sys.executable, "-m", "graphify_plus", "doctor", "--repo", str(tmp_path)],
        capture_output=True,
        check=False,
    )
    # No cache → ERROR diagnostic → exit 1.
    assert res2.returncode == 1
