"""Tests for Layer 6 ingestors (ADR + GitHub)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from graphify_plus.daemon.handlers import why_does_this_exist
from graphify_plus.daemon.indexes import InMemoryGraph
from graphify_plus.daemon.ingestors import (
    IngestNode,
    attribute_refs,
    fetch_github_issues,
    ingest_adr,
    ingest_github,
    load_ingest_nodes,
    parse_adr,
    parse_adr_folder,
)
from graphify_plus.interface.cli.daemon_cmd import daemon_cmd
from graphify_plus.runtime.store import Store, cache_path

# ---- ADR -----------------------------------------------------------------


def test_parse_adr_extracts_title_and_status(tmp_path: Path) -> None:
    p = tmp_path / "0042-use-postgres.md"
    p.write_text(
        "# Use Postgres for the orders table\n\n"
        "## Status\n\nAccepted\n\n"
        "## Context\n\nWe need durability.\n"
    )
    node = parse_adr(p)
    assert node is not None
    assert node.id == "adr-0042"
    assert node.title == "Use Postgres for the orders table"
    assert node.state == "Accepted"
    assert node.kind == "adr"


def test_parse_adr_handles_no_number(tmp_path: Path) -> None:
    p = tmp_path / "decision.md"
    p.write_text("# Some decision\n")
    node = parse_adr(p)
    assert node is not None
    assert node.id == "adr-decision"


def test_parse_adr_skips_files_without_title(tmp_path: Path) -> None:
    p = tmp_path / "empty.md"
    p.write_text("just text, no heading")
    assert parse_adr(p) is None


def test_parse_adr_folder_skips_readme(tmp_path: Path) -> None:
    (tmp_path / "0001-x.md").write_text("# X")
    (tmp_path / "README.md").write_text("# Readme")
    nodes = parse_adr_folder(tmp_path)
    assert len(nodes) == 1
    assert nodes[0].id == "adr-0001"


def test_attribute_refs_finds_qname_in_body() -> None:
    nodes = [
        IngestNode(
            id="adr-1",
            kind="adr",
            title="t",
            body="We added auth.AuthService.login to handle Stripe webhooks.",
            source="adr",
        )
    ]
    symbols = [
        {
            "id": "sid1",
            "qualified_name": "auth.AuthService.login",
            "name": "login",
            "kind": "method",
        }
    ]
    attribute_refs(nodes, symbols)
    assert "sid1" in nodes[0].refs


def test_ingest_adr_end_to_end(repo: Path, tmp_path: Path) -> None:
    folder = tmp_path / "adr"
    folder.mkdir()
    (folder / "0001-auth.md").write_text(
        "# Use AuthService\n\n## Status\n\nAccepted\n\n"
        "We use auth.AuthService.login for everything.\n"
    )
    store = Store(cache_path(repo))
    try:
        summary = ingest_adr(store, repo, folder)
        loaded = load_ingest_nodes(store, kind="adr")
    finally:
        store.close()
    assert summary["files"] == 1
    assert loaded[0]["title"] == "Use AuthService"
    assert loaded[0]["refs"]


# ---- GitHub --------------------------------------------------------------


def test_fetch_github_issues_handles_missing_gh(repo: Path) -> None:
    """When `gh` isn't available, return an empty list rather than crash."""
    with patch("graphify_plus.daemon.ingestors._gh", return_value=None):
        rows = fetch_github_issues(repo)
    assert rows == []


def test_fetch_github_issues_parses_payload(repo: Path) -> None:
    fake_payload = json.dumps(
        [
            {
                "number": 42,
                "title": "fix login race condition",
                "body": "auth.AuthService.login is not thread-safe.",
                "state": "OPEN",
                "url": "https://github.com/x/y/issues/42",
                "labels": [{"name": "bug"}],
                "author": {"login": "alice"},
                "createdAt": "2024-01-01T00:00:00Z",
                "updatedAt": "2024-01-02T00:00:00Z",
            }
        ]
    )
    with patch("graphify_plus.daemon.ingestors._gh", return_value=fake_payload):
        rows = fetch_github_issues(repo)
    assert len(rows) == 1
    assert rows[0].id == "gh-issue-42"
    assert rows[0].kind == "issue"
    assert "thread-safe" in rows[0].body


def test_ingest_github_end_to_end(repo: Path) -> None:
    fake_issue = json.dumps(
        [
            {
                "number": 1,
                "title": "test",
                "body": "auth.AuthService is broken.",
                "state": "OPEN",
                "url": "u",
                "labels": [],
                "author": {"login": "x"},
                "createdAt": "",
                "updatedAt": "",
            }
        ]
    )
    fake_pr = json.dumps([])
    store = Store(cache_path(repo))
    try:
        with patch(
            "graphify_plus.daemon.ingestors._gh",
            side_effect=[fake_issue, fake_pr],
        ):
            summary = ingest_github(store, repo)
    finally:
        store.close()
    assert summary["issues"] == 1
    assert summary["prs"] == 0


def test_why_does_this_exist_handler_no_data(snapshot: InMemoryGraph) -> None:
    resp = why_does_this_exist(snapshot, {"node": "AuthService.login"})
    assert resp["results"] == []
    assert "no issue" in resp["extra"]["reason"]


def test_why_does_this_exist_handler_with_data(snapshot: InMemoryGraph) -> None:
    target_sid = next(
        sid
        for sid, sym in snapshot.by_id.items()
        if sym.get("qualified_name") == "auth.AuthService.login"
    )
    snapshot.ingest_refs[target_sid] = [
        {
            "id": "gh-issue-42",
            "kind": "issue",
            "title": "race condition",
            "body": "...",
            "url": "https://github.com/x/y/issues/42",
            "state": "OPEN",
            "source": "github",
            "refs": [target_sid],
        }
    ]
    resp = why_does_this_exist(snapshot, {"node": "AuthService.login"})
    assert resp["results"]
    assert resp["results"][0]["node_id"] == "gh-issue-42"


def test_cli_ingest_adr(repo: Path, tmp_path: Path) -> None:
    folder = tmp_path / "adr"
    folder.mkdir()
    (folder / "0001-x.md").write_text("# X\n## Status\nAccepted\n")
    runner = CliRunner()
    result = runner.invoke(daemon_cmd, ["ingest", "adr", str(folder), "--repo", str(repo)])
    assert result.exit_code == 0, result.output
    assert "ADR" in result.output


def test_cli_ingest_github_runs_with_mocked_gh(repo: Path) -> None:
    runner = CliRunner()
    with patch(
        "graphify_plus.daemon.ingestors._gh",
        side_effect=[json.dumps([]), json.dumps([])],
    ):
        result = runner.invoke(daemon_cmd, ["ingest", "github", "--repo", str(repo)])
    assert result.exit_code == 0, result.output
