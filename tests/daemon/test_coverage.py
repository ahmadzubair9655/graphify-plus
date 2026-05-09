"""Tests for the test-coverage overlay (Sprint 8)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from graphify_plus.daemon.coverage import (
    LineHit,
    coverage_summary,
    ingest_report,
    load_coverage,
    map_to_symbols,
    parse_report,
    store_coverage,
)
from graphify_plus.daemon.handlers import (
    HANDLERS,
    coverage_for,
    coverage_summary as cov_summary_handler,
    whats_untested,
)
from graphify_plus.daemon.indexes import InMemoryGraph
from graphify_plus.interface.cli.daemon_cmd import daemon_cmd
from graphify_plus.runtime.store import Store, cache_path


# ---- parser tests --------------------------------------------------------


def _write_cobertura(path: Path, data: dict[str, list[tuple[int, int]]]) -> None:
    """Write a minimal Cobertura XML with the given file→[(line, hits)] data."""
    parts = ['<?xml version="1.0"?>\n<coverage>\n  <packages>\n']
    for filename, lines in data.items():
        parts.append(
            f'    <package name="x"><classes>\n'
            f'      <class filename="{filename}" name="x">\n'
            f'        <lines>\n'
        )
        for ln, hits in lines:
            parts.append(f'          <line number="{ln}" hits="{hits}"/>\n')
        parts.append('        </lines>\n      </class>\n    </classes></package>\n')
    parts.append('  </packages>\n</coverage>\n')
    path.write_text("".join(parts))


def _write_istanbul(path: Path, data: dict[str, list[tuple[int, int]]]) -> None:
    """Write a minimal Istanbul JSON with file→[(line, hits)] data."""
    out: dict = {}
    for filename, lines in data.items():
        statement_map = {}
        s = {}
        for i, (ln, hits) in enumerate(lines):
            sid = str(i)
            statement_map[sid] = {"start": {"line": ln}, "end": {"line": ln}}
            s[sid] = hits
        out[filename] = {"statementMap": statement_map, "s": s}
    path.write_text(json.dumps(out))


def test_parse_cobertura(tmp_path: Path) -> None:
    p = tmp_path / "coverage.xml"
    _write_cobertura(p, {"auth.py": [(1, 3), (5, 0), (7, 2)]})
    report = parse_report(p)
    assert report.source == "cobertura"
    assert "auth.py" in report.files
    hits = report.files["auth.py"]
    assert [(h.line, h.hits) for h in hits] == [(1, 3), (5, 0), (7, 2)]


def test_parse_istanbul(tmp_path: Path) -> None:
    p = tmp_path / "coverage-final.json"
    _write_istanbul(p, {"src/auth.ts": [(1, 4), (5, 0)]})
    report = parse_report(p)
    assert report.source == "istanbul"
    assert "src/auth.ts" in report.files


def test_parse_unrecognised_format(tmp_path: Path) -> None:
    p = tmp_path / "report.txt"
    p.write_text("not coverage data")
    with pytest.raises(ValueError):
        parse_report(p)


def test_parse_format_sniff_xml(tmp_path: Path) -> None:
    """A file without a recognised extension falls back to body sniffing."""
    p = tmp_path / "coverage_report"
    _write_cobertura(p, {"a.py": [(1, 1)]})
    report = parse_report(p)
    assert report.source == "cobertura"


# ---- mapping tests -------------------------------------------------------


def test_map_to_symbols_attributes_lines_to_deepest_owner(snapshot: InMemoryGraph) -> None:
    """For a symbol whose span is (5, 8), lines 5–8 must attribute to it
    (not to the enclosing module / class)."""
    login = next(
        s
        for s in snapshot.by_id.values()
        if s.get("qualified_name") == "auth.AuthService.login"
    )
    span = login["span"]
    from graphify_plus.daemon.coverage import CoverageReport

    report = CoverageReport(
        files={
            "auth.py": [
                LineHit(line=int(span[0]), hits=3),
                LineHit(line=int(span[1]), hits=0),
            ]
        },
        source="cobertura",
    )
    rows = map_to_symbols(report, list(snapshot.by_id.values()), Path("."))
    by_id = {r.symbol_id: r for r in rows}
    assert login["id"] in by_id
    cov = by_id[login["id"]]
    assert cov.lines_total == 2
    assert cov.lines_covered == 1


def test_map_to_symbols_falls_back_to_module_for_top_level_lines(
    snapshot: InMemoryGraph,
) -> None:
    """Top-level lines (e.g. line 1, the docstring) attribute to the module."""
    module = next(s for s in snapshot.by_id.values() if s.get("qualified_name") == "auth")
    from graphify_plus.daemon.coverage import CoverageReport

    report = CoverageReport(
        files={"auth.py": [LineHit(line=1, hits=1)]}, source="cobertura"
    )
    rows = map_to_symbols(report, list(snapshot.by_id.values()), Path("."))
    by_id = {r.symbol_id: r for r in rows}
    assert module["id"] in by_id


def test_ingest_report_end_to_end(repo: Path, tmp_path: Path) -> None:
    coverage_xml = tmp_path / "coverage.xml"
    _write_cobertura(coverage_xml, {"auth.py": [(5, 1), (6, 0), (7, 1), (8, 0), (10, 1)]})
    store = Store(cache_path(repo))
    try:
        summary = ingest_report(store, repo, coverage_xml)
        assert summary["format"] == "cobertura"
        assert summary["files"] == 1
        assert summary["symbols_attributed"] >= 1
        loaded = load_coverage(store)
        assert loaded
        # Some symbol now has coverage data with the right shape.
        any_row = next(iter(loaded.values()))
        assert "pct" in any_row
        assert "lines_total" in any_row
    finally:
        store.close()


# ---- handler tests -------------------------------------------------------


def test_whats_untested_no_data_returns_hint(snapshot: InMemoryGraph) -> None:
    resp = whats_untested(snapshot, {})
    assert resp["results"] == []
    assert "no coverage" in resp["extra"]["reason"].lower()


def test_whats_untested_filters_by_threshold(repo: Path, tmp_path: Path) -> None:
    cov_xml = tmp_path / "cov.xml"
    # login fully covered, validate untested
    _write_cobertura(cov_xml, {"auth.py": [(7, 3), (8, 1), (10, 0), (11, 0)]})
    store = Store(cache_path(repo))
    try:
        ingest_report(store, repo, cov_xml)
        snap = InMemoryGraph.from_store(store, repo)
    finally:
        store.close()
    resp = whats_untested(snap, {"max_pct": 10.0})
    # The 0%-covered functions show up; the fully-covered ones don't.
    pcts = [r["coverage_pct"] for r in resp["results"]]
    assert all(p <= 10.0 for p in pcts)
    assert resp["results"], "expected at least one untested symbol"


def test_coverage_for_handler(repo: Path, tmp_path: Path) -> None:
    cov_xml = tmp_path / "cov.xml"
    _write_cobertura(cov_xml, {"auth.py": [(7, 1), (8, 1)]})
    store = Store(cache_path(repo))
    try:
        ingest_report(store, repo, cov_xml)
        snap = InMemoryGraph.from_store(store, repo)
    finally:
        store.close()
    resp = coverage_for(snap, {"node": "AuthService.login"})
    assert resp["results"]
    row = resp["results"][0]
    assert "coverage_pct" in row
    assert row["coverage_pct"] == 100.0


def test_coverage_summary_handler_with_data(repo: Path, tmp_path: Path) -> None:
    cov_xml = tmp_path / "cov.xml"
    _write_cobertura(cov_xml, {"auth.py": [(7, 1), (8, 0), (10, 1), (11, 0)]})
    store = Store(cache_path(repo))
    try:
        ingest_report(store, repo, cov_xml)
        snap = InMemoryGraph.from_store(store, repo)
    finally:
        store.close()
    resp = cov_summary_handler(snap, {})
    extra = resp["extra"]
    assert extra["overall_pct"] == 50.0
    assert extra["lines_covered"] == 2
    assert extra["lines_total"] == 4
    assert "auth.py" in {w["path"] for w in extra["worst_files"]}


def test_coverage_summary_handler_no_data(snapshot: InMemoryGraph) -> None:
    resp = cov_summary_handler(snapshot, {})
    assert "no coverage" in resp["extra"]["reason"].lower()


def test_handler_registry_includes_coverage_ops() -> None:
    for op in ("whats_untested", "coverage_for", "coverage_summary"):
        assert op in HANDLERS


# ---- CLI tests -----------------------------------------------------------


def test_cli_coverage_ingest_and_summary(repo: Path, tmp_path: Path) -> None:
    cov_xml = tmp_path / "cov.xml"
    _write_cobertura(cov_xml, {"auth.py": [(7, 1), (8, 1), (10, 0), (11, 0)]})
    runner = CliRunner()
    result = runner.invoke(
        daemon_cmd, ["coverage", "ingest", str(cov_xml), "--repo", str(repo)]
    )
    assert result.exit_code == 0, result.output
    assert "ingested" in result.output

    result = runner.invoke(daemon_cmd, ["coverage", "summary", "--repo", str(repo)])
    assert result.exit_code == 0, result.output
    assert "overall:" in result.output

    result = runner.invoke(daemon_cmd, ["coverage", "untested", "--repo", str(repo)])
    assert result.exit_code == 0, result.output


def test_cli_coverage_untested_json(repo: Path, tmp_path: Path) -> None:
    cov_xml = tmp_path / "cov.xml"
    _write_cobertura(cov_xml, {"auth.py": [(10, 0), (11, 0)]})
    runner = CliRunner()
    runner.invoke(daemon_cmd, ["coverage", "ingest", str(cov_xml), "--repo", str(repo)])
    result = runner.invoke(
        daemon_cmd, ["coverage", "untested", "--repo", str(repo), "--json"]
    )
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert "results" in body


def test_whats_in_includes_coverage_pct(repo: Path, tmp_path: Path) -> None:
    """whats_in output rows must surface coverage when ingested — Layer 7.1
    promise: every existing handler should benefit from coverage signal
    without callers having to ask for it explicitly."""
    cov_xml = tmp_path / "cov.xml"
    _write_cobertura(cov_xml, {"auth.py": [(7, 1), (8, 0)]})
    store = Store(cache_path(repo))
    try:
        ingest_report(store, repo, cov_xml)
        snap = InMemoryGraph.from_store(store, repo)
    finally:
        store.close()
    from graphify_plus.daemon.handlers import whats_in

    resp = whats_in(snap, {"path": "auth.py"})
    has_pct = [r for r in resp["results"] if "coverage_pct" in r]
    assert has_pct, "at least one row should carry coverage_pct"


# ---- store tests ---------------------------------------------------------


def test_coverage_summary_query_aggregates(repo: Path, tmp_path: Path) -> None:
    cov_xml = tmp_path / "cov.xml"
    _write_cobertura(cov_xml, {"auth.py": [(7, 1), (8, 0)]})
    store = Store(cache_path(repo))
    try:
        ingest_report(store, repo, cov_xml)
        s = coverage_summary(store)
        assert s["lines_total"] == 2
        assert s["lines_covered"] == 1
        assert s["overall_pct"] == 0.5
    finally:
        store.close()
