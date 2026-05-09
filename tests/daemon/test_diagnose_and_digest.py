"""Tests for `gp daemon diagnose` (Layer 12.4) and session-digest (Layer 13.3)."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from graphify_plus.daemon.diagnose import diagnose, format_diagnose
from graphify_plus.daemon.handlers import session_digest as session_digest_handler
from graphify_plus.daemon.indexes import InMemoryGraph
from graphify_plus.daemon.session_digest import (
    SessionDigest,
    format_digest,
    make_digest,
)
from graphify_plus.interface.cli.daemon_cmd import daemon_cmd


# ---- diagnose ------------------------------------------------------------


def test_diagnose_no_cache(tmp_path: Path) -> None:
    report = diagnose(tmp_path)
    assert report["repo"] == str(tmp_path)
    assert report["cache"]["exists"] is False
    assert report["daemon"]["running"] is False


def test_diagnose_with_cache(repo: Path) -> None:
    report = diagnose(repo)
    assert report["cache"]["exists"] is True
    snap = report["snapshot"]
    assert snap["source"] in ("daemon", "cache")
    if "error" not in snap:
        assert snap["symbols"] >= 1
        assert snap["files"] >= 1


def test_diagnose_format_renders(repo: Path) -> None:
    report = diagnose(repo)
    md = format_diagnose(report)
    assert md.startswith("# graphify-plus diagnose")
    assert "Daemon" in md
    assert "Cache" in md


def test_diagnose_includes_telemetry_section(repo: Path) -> None:
    from graphify_plus.daemon.telemetry import append_event

    append_event(
        repo, "who_calls", elapsed_ms=1.2, tokens=20, n_results=1, trust="FRESH", ok=True
    )
    report = diagnose(repo)
    assert report["telemetry"]["events"] >= 1


def test_cli_diagnose_runs(repo: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(daemon_cmd, ["diagnose", "--repo", str(repo)])
    assert result.exit_code == 0, result.output
    assert "diagnose" in result.output.lower()


def test_cli_diagnose_json(repo: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(daemon_cmd, ["diagnose", "--repo", str(repo), "--json"])
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert "daemon" in body
    assert "snapshot" in body


# ---- session digest ------------------------------------------------------


SAMPLE_DIFF = """\
diff --git a/auth.py b/auth.py
--- a/auth.py
+++ b/auth.py
@@ -5,3 +5,4 @@ class AuthService:
     def login(self, user, password):
+        # added line
         return self.validate(user, password)
"""


def test_make_digest_composes_review(snapshot: InMemoryGraph) -> None:
    digest = make_digest(snapshot, since="main", head="HEAD", diff_text=SAMPLE_DIFF)
    assert digest.review is not None
    assert digest.review.touched
    assert digest.next_steps  # always at least one suggestion


def test_make_digest_includes_coverage_when_present(repo: Path) -> None:
    cov_xml = repo / "cov.xml"
    cov_xml.write_text(
        '<?xml version="1.0"?>\n<coverage><packages><package name="x">'
        '<classes><class filename="auth.py" name="x">'
        '<lines><line number="7" hits="1"/><line number="8" hits="0"/></lines>'
        '</class></classes></package></packages></coverage>'
    )
    from graphify_plus.daemon.coverage import ingest_report
    from graphify_plus.runtime.store import Store, cache_path

    store = Store(cache_path(repo))
    try:
        ingest_report(store, repo, cov_xml)
        snap = InMemoryGraph.from_store(store, repo)
    finally:
        store.close()
    digest = make_digest(snap, since="main", head="HEAD", diff_text=SAMPLE_DIFF)
    assert digest.coverage_overall_pct is not None


def test_format_digest_renders_markdown(snapshot: InMemoryGraph) -> None:
    digest = make_digest(snapshot, diff_text=SAMPLE_DIFF)
    md = format_digest(digest)
    assert md.startswith("# Session digest")
    assert "What changed" in md or "ready to push" in md.lower()
    assert "Recommended next steps" in md


def test_session_digest_handler(snapshot: InMemoryGraph) -> None:
    resp = session_digest_handler(snapshot, {"diff_text": SAMPLE_DIFF})
    body = resp["extra"]["digest"]
    assert "review" in body
    assert "next_steps" in body
    assert body["repo"]


def test_cli_session_digest(repo: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        daemon_cmd,
        ["session-digest", "--repo", str(repo), "--since", "no-such-ref"],
    )
    assert result.exit_code == 0, result.output
    assert "Session digest" in result.output


def test_cli_session_digest_json(repo: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        daemon_cmd,
        ["session-digest", "--repo", str(repo), "--since", "no-such-ref", "--json"],
    )
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert "next_steps" in body
