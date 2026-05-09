"""Tests for PR review co-pilot (Sprint 10.1)."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from graphify_plus.daemon.coverage import ingest_report
from graphify_plus.daemon.handlers import review as review_handler
from graphify_plus.daemon.indexes import InMemoryGraph
from graphify_plus.daemon.review import (
    Review,
    TouchedNode,
    changes_from_unified_diff,
    format_review,
    make_review,
)
from graphify_plus.interface.cli.daemon_cmd import daemon_cmd
from graphify_plus.runtime.store import Store, cache_path


SAMPLE_DIFF = """\
diff --git a/auth.py b/auth.py
index 1111111..2222222 100644
--- a/auth.py
+++ b/auth.py
@@ -5,3 +5,7 @@ class AuthService:
     def login(self, user, password):
+        # new logging — touches login()
+        print('logging in', user)
         return self.validate(user, password)
+    def new_thing(self):
+        return 1
"""


def test_changes_from_unified_diff_extracts_added_lines() -> None:
    changes = changes_from_unified_diff(SAMPLE_DIFF)
    assert len(changes) == 1
    assert changes[0].path == "auth.py"
    assert changes[0].added_lines  # at least one added line


def test_changes_handles_empty_diff() -> None:
    assert changes_from_unified_diff("") == []


def test_make_review_attributes_diff_to_symbols(snapshot: InMemoryGraph) -> None:
    rev = make_review(snapshot, base="main", head="HEAD", diff_text=SAMPLE_DIFF)
    assert rev.files_changed == 1
    labels = {n.label for n in rev.touched}
    # The login method's span includes the modified lines.
    assert any("login" in lab for lab in labels)


def test_review_marks_untested_touched(repo: Path) -> None:
    cov_xml = repo / ".graphify_plus" / "cov.xml"
    cov_xml.write_text(
        '<?xml version="1.0"?>\n<coverage><packages><package name="x">'
        '<classes><class filename="auth.py" name="x">'
        '<lines><line number="7" hits="0"/><line number="8" hits="0"/></lines>'
        '</class></classes></package></packages></coverage>'
    )
    store = Store(cache_path(repo))
    try:
        ingest_report(store, repo, cov_xml)
        snap = InMemoryGraph.from_store(store, repo)
    finally:
        store.close()
    rev = make_review(snap, base="main", head="HEAD", diff_text=SAMPLE_DIFF)
    # Diff touches login (lines 7-9 zone after edits) — login has 0% cov.
    assert rev.untested_touched, f"expected untested-touched; got {rev.touched}"


def test_format_review_includes_summary(snapshot: InMemoryGraph) -> None:
    rev = make_review(snapshot, base="main", head="HEAD", diff_text=SAMPLE_DIFF)
    md = format_review(rev)
    assert md.startswith("# Review —")
    assert "Symbols touched" in md or "structural" in md.lower()


def test_review_summary_no_changes(snapshot: InMemoryGraph) -> None:
    rev = make_review(snapshot, base="main", head="HEAD", diff_text="")
    assert rev.touched == []
    assert "No structural changes" in rev.summary or "Could not compute" in rev.summary


def test_review_handler_returns_payload(snapshot: InMemoryGraph) -> None:
    resp = review_handler(snapshot, {"diff_text": SAMPLE_DIFF})
    body = resp["extra"]["review"]
    assert "touched" in body
    assert "summary" in body


def test_cli_review_human_output(repo: Path) -> None:
    """The CLI invokes git diff under the hood. Without setting up a real
    git history we exercise the error path — a graceful 'could not
    compute diff' message rather than a crash.
    """
    runner = CliRunner()
    result = runner.invoke(
        daemon_cmd, ["review", "--repo", str(repo), "--base", "no-such-ref"]
    )
    assert result.exit_code == 0, result.output
    assert "Review —" in result.output


def test_cli_review_json_round_trip(repo: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        daemon_cmd, ["review", "--repo", str(repo), "--json", "--base", "no-such-ref"]
    )
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert "touched" in body
    assert "summary" in body
    # We can rebuild dataclasses from the body.
    rev = Review(
        base=body["base"],
        head=body["head"],
        files_changed=body["files_changed"],
        files_renamed=body["files_renamed"],
        touched=[TouchedNode(**n) for n in body["touched"]],
        untested_touched=[TouchedNode(**n) for n in body["untested_touched"]],
        central_touched=[TouchedNode(**n) for n in body["central_touched"]],
        rules_violations=list(body["rules_violations"]),
        rules_grade=body["rules_grade"],
        blast_radius=[TouchedNode(**n) for n in body["blast_radius"]],
        summary=body["summary"],
    )
    assert rev.summary
