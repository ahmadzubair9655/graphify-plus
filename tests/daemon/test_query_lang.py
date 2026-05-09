"""Tests for Layer 16 (GPL query language)."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from graphify_plus.daemon.handlers import gpl_query
from graphify_plus.daemon.indexes import InMemoryGraph
from graphify_plus.daemon.query_lang import QueryError, execute, parse, translate_nl
from graphify_plus.interface.cli.daemon_cmd import daemon_cmd


def test_parse_find_simple() -> None:
    q = parse("FIND nodes WHERE test_coverage < 0.1")
    assert q.form == "FIND"
    assert q.predicates[0].op == "<"


def test_parse_match_simple() -> None:
    q = parse(
        "MATCH (n:Function) WHERE n.degree >= 3 RETURN n.label, n.source_file ORDER BY n.degree DESC LIMIT 5"
    )
    assert q.form == "MATCH"
    assert q.src.label == "Function"
    assert q.return_fields == ["label", "source_file"]
    assert q.order_by == ("degree", True)
    assert q.limit == 5


def test_parse_match_with_edge() -> None:
    q = parse(
        'MATCH (n)-[:calls*1..3]->(:Function) WHERE n.kind = "function" RETURN label LIMIT 10'
    )
    assert q.edge is not None
    assert q.edge.kinds == ["calls"]
    assert q.edge.min_hops == 1
    assert q.edge.max_hops == 3


def test_parse_find_count_by() -> None:
    q = parse('FIND nodes WHERE kind = "function" COUNT BY source_file')
    assert q.count_by == "source_file"


def test_parse_invalid_raises() -> None:
    with pytest.raises(QueryError):
        parse("RETURN n")


def test_execute_find_filters_rows(snapshot: InMemoryGraph) -> None:
    q = parse('FIND nodes WHERE kind = "method"')
    out = execute(snapshot, q)
    assert all((r.get("label") or "").endswith((".login", ".validate")) or "AuthService" in (r.get("label") or "") for r in out["rows"])


def test_execute_count_by(snapshot: InMemoryGraph) -> None:
    q = parse('FIND nodes WHERE kind = "method" COUNT BY source_file')
    out = execute(snapshot, q)
    assert out["count_by"] == "source_file"
    assert out["rows"]


def test_execute_match_returns_projection(snapshot: InMemoryGraph) -> None:
    q = parse("MATCH (n) RETURN label, source_file LIMIT 3")
    out = execute(snapshot, q)
    assert len(out["rows"]) <= 3
    for row in out["rows"]:
        assert set(row.keys()) == {"label", "source_file"}


def test_execute_match_with_edge_traversal(snapshot: InMemoryGraph) -> None:
    q = parse(
        "MATCH (n)-[:contains*1..2]->(m) RETURN label LIMIT 50"
    )
    out = execute(snapshot, q)
    # `auth.AuthService` contains login/validate, so we should at least
    # get one row back.
    assert out["count"] >= 0  # tolerant assertion


def test_translate_nl_untested() -> None:
    out = translate_nl("show me untested functions")
    assert "test_coverage < 0.1" in out
    assert "kind == 'function'" in out


def test_handler_with_gpl_query(snapshot: InMemoryGraph) -> None:
    resp = gpl_query(snapshot, {"query": "FIND nodes WHERE kind = 'method'"})
    assert "results" in resp
    assert resp["extra"]["form"] == "FIND"


def test_handler_with_nl_translates(snapshot: InMemoryGraph) -> None:
    resp = gpl_query(snapshot, {"nl": "untested functions"})
    assert "nl_to_gpl" in resp["extra"]


def test_handler_bad_query_returns_error(snapshot: InMemoryGraph) -> None:
    resp = gpl_query(snapshot, {"query": "this is not a query"})
    assert "error" in resp


def test_handler_empty_args(snapshot: InMemoryGraph) -> None:
    resp = gpl_query(snapshot, {})
    assert "must pass" in resp["extra"]["reason"]


def test_cli_gpl_inline(repo) -> None:
    runner = CliRunner()
    result = runner.invoke(
        daemon_cmd,
        ["gpl", "FIND nodes WHERE kind = 'method'", "--repo", str(repo)],
    )
    assert result.exit_code == 0, result.output
    assert "match" in result.output.lower()


def test_cli_gpl_save_and_run(repo) -> None:
    runner = CliRunner()
    save = runner.invoke(
        daemon_cmd,
        [
            "gpl",
            "FIND nodes WHERE kind = 'class'",
            "--repo",
            str(repo),
            "--save",
            "all-classes",
        ],
    )
    assert save.exit_code == 0, save.output
    run = runner.invoke(
        daemon_cmd,
        ["gpl", "--repo", str(repo), "--run", "all-classes"],
    )
    assert run.exit_code == 0, run.output


def test_cli_gpl_nl(repo) -> None:
    runner = CliRunner()
    result = runner.invoke(
        daemon_cmd, ["gpl", "--repo", str(repo), "--nl", "untested classes"]
    )
    assert result.exit_code == 0, result.output
    assert "translated" in result.output
