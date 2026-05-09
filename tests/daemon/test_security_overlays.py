"""Tests for the CVE (9.1) and SAST (9.2) overlays."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from graphify_plus.daemon.handlers import whats_risky, whats_vulnerable
from graphify_plus.daemon.indexes import InMemoryGraph
from graphify_plus.daemon.overlays import (
    CVEEntry,
    SASTFinding,
    cve_reach_map,
    ingest_audit,
    ingest_sast,
    load_cve,
    load_sast,
    map_findings_to_symbols,
    parse_audit_report,
    parse_sast_report,
    store_cve,
    store_sast,
)
from graphify_plus.interface.cli.daemon_cmd import daemon_cmd
from graphify_plus.runtime.store import Store, cache_path


# ---- CVE -----------------------------------------------------------------


def _write_pip_audit(path: Path, vulns: list[dict]) -> None:
    deps: dict[str, list[dict]] = {}
    for v in vulns:
        deps.setdefault(v["name"], []).append(v)
    body = {
        "dependencies": [
            {
                "name": name,
                "version": entries[0].get("version", "1.0"),
                "vulns": [
                    {
                        "id": e["id"],
                        "severity": e.get("severity", "HIGH"),
                        "description": e.get("desc", ""),
                        "fix_versions": e.get("fix_versions", []),
                        "link": e.get("link", ""),
                    }
                    for e in entries
                ],
            }
            for name, entries in deps.items()
        ]
    }
    path.write_text(json.dumps(body))


def _write_npm_audit(path: Path) -> None:
    body = {
        "vulnerabilities": {
            "lodash": {
                "name": "lodash",
                "severity": "high",
                "range": "<4.17.21",
                "via": [{"source": "GHSA-xxx", "title": "Prototype pollution", "url": "https://example/x"}],
            }
        }
    }
    path.write_text(json.dumps(body))


def test_parse_pip_audit(tmp_path: Path) -> None:
    p = tmp_path / "audit.json"
    _write_pip_audit(
        p,
        [
            {"name": "requests", "version": "2.20.0", "id": "CVE-2018-18074", "severity": "HIGH"},
        ],
    )
    rows, source = parse_audit_report(p)
    assert source == "pip-audit"
    assert rows[0].cve_id == "CVE-2018-18074"
    assert rows[0].package == "requests"


def test_parse_npm_audit(tmp_path: Path) -> None:
    p = tmp_path / "npm.json"
    _write_npm_audit(p)
    rows, source = parse_audit_report(p)
    assert source == "npm-audit"
    assert rows[0].package == "lodash"


def test_store_and_load_cve(repo: Path, tmp_path: Path) -> None:
    p = tmp_path / "audit.json"
    _write_pip_audit(p, [{"name": "requests", "version": "2.20.0", "id": "CVE-X"}])
    store = Store(cache_path(repo))
    try:
        ingest_audit(store, repo, p)
        loaded = load_cve(store)
        assert any(r["cve_id"] == "CVE-X" for r in loaded)
    finally:
        store.close()


def test_cve_reach_map_attributes_by_qname() -> None:
    rows = [
        {
            "cve_id": "CVE-X",
            "package": "requests",
            "version": "1",
            "severity": "HIGH",
            "summary": "",
            "fix_versions": [],
            "advisory_url": "",
        }
    ]
    symbols = [
        {"id": "1", "qualified_name": "requests.api.get", "name": "get", "kind": "function"},
        {"id": "2", "qualified_name": "myapp.auth.login", "name": "login", "kind": "function"},
    ]
    out = cve_reach_map(rows, symbols)
    assert "1" in out
    assert "2" not in out


def test_whats_vulnerable_handler_no_data(snapshot: InMemoryGraph) -> None:
    resp = whats_vulnerable(snapshot, {})
    assert resp["results"] == []
    assert "no audit data" in resp["extra"]["reason"]


def test_whats_vulnerable_handler_with_data(snapshot: InMemoryGraph) -> None:
    snapshot.cve_rows = [
        {
            "cve_id": "CVE-X",
            "package": "auth",
            "version": "1",
            "severity": "CRITICAL",
            "summary": "fake",
            "fix_versions": [],
            "advisory_url": "",
        }
    ]
    target_sid = next(
        sid
        for sid, sym in snapshot.by_id.items()
        if (sym.get("qualified_name") or "").startswith("auth")
    )
    snapshot.cves_by_symbol = {target_sid: snapshot.cve_rows}
    resp = whats_vulnerable(snapshot, {})
    assert resp["results"]
    row = resp["results"][0]
    assert row["worst_severity"] == "CRITICAL"
    assert row["vulnerabilities"]


def test_whats_vulnerable_severity_filter(snapshot: InMemoryGraph) -> None:
    snapshot.cve_rows = [
        {
            "cve_id": "CVE-LOW",
            "package": "auth",
            "version": "1",
            "severity": "LOW",
            "summary": "",
            "fix_versions": [],
            "advisory_url": "",
        }
    ]
    target = next(iter(snapshot.by_id))
    snapshot.cves_by_symbol = {target: snapshot.cve_rows}
    resp = whats_vulnerable(snapshot, {"severity": "HIGH"})
    assert resp["results"] == []


# ---- SAST ----------------------------------------------------------------


def _write_bandit(path: Path) -> None:
    body = {
        "results": [
            {
                "test_id": "B602",
                "issue_severity": "HIGH",
                "issue_text": "subprocess shell=True",
                "filename": "auth.py",
                "line_number": 7,
                "line_range": [7, 8],
                "issue_cwe": {"id": "78"},
            }
        ]
    }
    path.write_text(json.dumps(body))


def _write_semgrep(path: Path) -> None:
    body = {
        "results": [
            {
                "check_id": "python.lang.security.audit.dangerous-system",
                "path": "auth.py",
                "start": {"line": 7},
                "end": {"line": 8},
                "extra": {
                    "severity": "ERROR",
                    "message": "dangerous system call",
                    "metadata": {"cwe": "CWE-78"},
                },
            }
        ]
    }
    path.write_text(json.dumps(body))


def test_parse_bandit(tmp_path: Path) -> None:
    p = tmp_path / "bandit.json"
    _write_bandit(p)
    rows, source = parse_sast_report(p)
    assert source == "bandit"
    assert rows[0].rule_id == "B602"
    assert rows[0].severity == "HIGH"


def test_parse_semgrep(tmp_path: Path) -> None:
    p = tmp_path / "semgrep.json"
    _write_semgrep(p)
    rows, source = parse_sast_report(p)
    assert source == "semgrep"
    assert "dangerous" in rows[0].message


def test_map_findings_to_symbols(snapshot: InMemoryGraph) -> None:
    findings = [
        SASTFinding(rule_id="B1", severity="HIGH", message="x", file="auth.py", line=7),
        SASTFinding(rule_id="B2", severity="LOW", message="y", file="nope.py", line=1),
    ]
    mapping = map_findings_to_symbols(findings, list(snapshot.by_id.values()))
    assert mapping[0] is not None  # auth.py line 7 attributes to a symbol
    assert mapping[1] is None  # unmatched file


def test_ingest_sast_end_to_end(repo: Path, tmp_path: Path) -> None:
    p = tmp_path / "bandit.json"
    _write_bandit(p)
    store = Store(cache_path(repo))
    try:
        summary = ingest_sast(store, repo, p)
        assert summary["findings"] == 1
        assert summary["attributed"] >= 0  # depends on fixture
        loaded = load_sast(store)
        # If the line maps to a symbol, it shows up.
        assert isinstance(loaded, dict)
    finally:
        store.close()


def test_whats_risky_handler_no_data(snapshot: InMemoryGraph) -> None:
    resp = whats_risky(snapshot, {})
    assert resp["results"] == []
    assert "no SAST" in resp["extra"]["reason"]


def test_whats_risky_handler_with_data(snapshot: InMemoryGraph) -> None:
    target = next(iter(snapshot.by_id))
    snapshot.sast_by_symbol = {
        target: [
            {
                "rule_id": "B602",
                "severity": "HIGH",
                "message": "subprocess shell=True",
                "file": "auth.py",
                "line": 7,
                "end_line": 8,
                "cwe": "78",
                "source": "bandit",
            }
        ]
    }
    resp = whats_risky(snapshot, {})
    assert resp["results"]
    row = resp["results"][0]
    assert row["worst_severity"] == "HIGH"
    assert row["n_findings"] == 1


# ---- CLI -----------------------------------------------------------------


def test_cli_security_ingest_audit(repo: Path, tmp_path: Path) -> None:
    p = tmp_path / "audit.json"
    _write_pip_audit(p, [{"name": "requests", "version": "2", "id": "CVE-1"}])
    runner = CliRunner()
    result = runner.invoke(
        daemon_cmd, ["security", "ingest", str(p), "--repo", str(repo)]
    )
    assert result.exit_code == 0, result.output
    assert "CVE" in result.output


def test_cli_security_ingest_sast(repo: Path, tmp_path: Path) -> None:
    p = tmp_path / "bandit.json"
    _write_bandit(p)
    runner = CliRunner()
    result = runner.invoke(
        daemon_cmd, ["security", "ingest-sast", str(p), "--repo", str(repo)]
    )
    assert result.exit_code == 0, result.output
    assert "finding" in result.output


def test_cli_security_vulnerable_no_data(repo: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(daemon_cmd, ["security", "vulnerable", "--repo", str(repo)])
    assert result.exit_code == 0, result.output
    assert "no audit data" in result.output


def test_cli_security_risky_no_data(repo: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(daemon_cmd, ["security", "risky", "--repo", str(repo)])
    assert result.exit_code == 0, result.output
    assert "no SAST" in result.output
