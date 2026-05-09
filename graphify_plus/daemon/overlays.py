"""Quality / security overlays — Layer 9 of the master plan.

Two overlays land in this module:

* **9.1 CVE overlay** — parses pip-audit / npm-audit JSON. Each
  vulnerability becomes a row keyed on `(package, version)`. Symbol
  attribution is by *import edge*: any symbol whose `imports` edge
  resolves to the vulnerable package is flagged as "reachable from
  CVE". Master plan: "CVE-2025-XXXX in `requests` is reached via
  `auth_service.fetch_token`."

* **9.2 SAST findings overlay** — parses Bandit / Semgrep / generic
  SARIF JSON. Each finding maps to a code position; we attribute it
  to the deepest containing symbol (same pattern as coverage).

Both overlays land in two tables (`cve`, `sast`) and the daemon's
`InMemoryGraph` reads them at build time so existing handlers see
`vulnerabilities` and `sast_findings` on every relevant row.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..core.adapters import Symbol
from ..runtime.store import Store

log = logging.getLogger("graphify_plus.daemon.overlays")


# =========================================================================
# Layer 9.1 — CVE overlay
# =========================================================================


@dataclass
class CVEEntry:
    cve_id: str
    package: str
    version: str
    severity: str  # CRITICAL | HIGH | MEDIUM | LOW | UNKNOWN
    summary: str
    fix_versions: list[str] = field(default_factory=list)
    advisory_url: str = ""

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


CVE_SCHEMA = """
CREATE TABLE IF NOT EXISTS cve (
    cve_id        TEXT NOT NULL,
    package       TEXT NOT NULL,
    version       TEXT NOT NULL,
    severity      TEXT NOT NULL,
    summary       TEXT NOT NULL,
    fix_versions  TEXT,            -- JSON array
    advisory_url  TEXT,
    ingested_at   TEXT NOT NULL,
    PRIMARY KEY (cve_id, package, version)
);
"""


def ensure_cve_table(store: Store) -> None:
    store.conn.executescript(CVE_SCHEMA)


def parse_pip_audit(path: Path) -> list[CVEEntry]:
    """Parse `pip-audit --format json` output.

    The format is `{"dependencies": [{"name": "...", "version": "...",
    "vulns": [{"id": "...", "fix_versions": [...], "description": "..."}, ...]}]}`.
    Severity isn't always provided — we default to UNKNOWN.
    """
    body = json.loads(path.read_text())
    out: list[CVEEntry] = []
    deps = body.get("dependencies") or []
    for dep in deps:
        name = dep.get("name") or ""
        version = dep.get("version") or ""
        for v in dep.get("vulns") or []:
            out.append(
                CVEEntry(
                    cve_id=v.get("id") or "",
                    package=name,
                    version=version,
                    severity=(v.get("severity") or "UNKNOWN").upper(),
                    summary=(v.get("description") or v.get("summary") or "")[:280],
                    fix_versions=list(v.get("fix_versions") or []),
                    advisory_url=v.get("link") or "",
                )
            )
    return out


def parse_npm_audit(path: Path) -> list[CVEEntry]:
    """Parse `npm audit --json` output (npm v7+ format).

    The shape is `{"vulnerabilities": {"<package>": {"name": "...", "severity": "...",
    "via": [...], "range": "...", "fixAvailable": ..., "url": "..."}}}`.
    """
    body = json.loads(path.read_text())
    out: list[CVEEntry] = []
    for name, info in (body.get("vulnerabilities") or {}).items():
        if not isinstance(info, dict):
            continue
        severity = (info.get("severity") or "UNKNOWN").upper()
        version = info.get("range") or ""
        for via in info.get("via") or []:
            if not isinstance(via, dict):
                continue
            cve_id = via.get("source") or via.get("url") or via.get("title") or ""
            out.append(
                CVEEntry(
                    cve_id=str(cve_id),
                    package=name,
                    version=str(version),
                    severity=severity,
                    summary=(via.get("title") or "")[:280],
                    fix_versions=[],
                    advisory_url=via.get("url") or "",
                )
            )
    return out


def parse_audit_report(path: Path) -> tuple[list[CVEEntry], str]:
    """Sniff and parse. Returns ``(rows, source)``."""
    body = json.loads(path.read_text())
    if isinstance(body, dict) and body.get("dependencies") is not None:
        return parse_pip_audit(path), "pip-audit"
    if isinstance(body, dict) and body.get("vulnerabilities") is not None:
        return parse_npm_audit(path), "npm-audit"
    raise ValueError(f"unrecognised audit format at {path}")


def store_cve(store: Store, rows: list[CVEEntry]) -> int:
    ensure_cve_table(store)
    now = datetime.now(timezone.utc).isoformat()
    with store.tx():
        store.conn.execute("DELETE FROM cve")
        store.conn.executemany(
            "INSERT INTO cve(cve_id, package, version, severity, summary, "
            "fix_versions, advisory_url, ingested_at) VALUES (?,?,?,?,?,?,?,?)",
            [
                (
                    r.cve_id,
                    r.package,
                    r.version,
                    r.severity,
                    r.summary,
                    json.dumps(r.fix_versions),
                    r.advisory_url,
                    now,
                )
                for r in rows
            ],
        )
    return len(rows)


def load_cve(store: Store) -> list[dict[str, Any]]:
    ensure_cve_table(store)
    rows = store.conn.execute(
        "SELECT cve_id, package, version, severity, summary, fix_versions, advisory_url FROM cve"
    ).fetchall()
    out: list[dict[str, Any]] = []
    for cve_id, pkg, ver, sev, summary, fvs, url in rows:
        try:
            fix = json.loads(fvs) if fvs else []
        except json.JSONDecodeError:
            fix = []
        out.append(
            {
                "cve_id": cve_id,
                "package": pkg,
                "version": ver,
                "severity": sev,
                "summary": summary,
                "fix_versions": fix,
                "advisory_url": url,
            }
        )
    return out


def cve_reach_map(
    cve_rows: list[dict[str, Any]], symbols: list[Symbol]
) -> dict[str, list[dict[str, Any]]]:
    """Map CVE-affected packages back to symbols whose qualified_name
    starts with the package name (best-effort reachability without
    re-implementing import-resolution).

    Returns ``{symbol_id: [cve_rows...]}``.
    """
    by_pkg: dict[str, list[dict[str, Any]]] = {}
    for r in cve_rows:
        by_pkg.setdefault(r["package"].lower(), []).append(r)
    out: dict[str, list[dict[str, Any]]] = {}
    for s in symbols:
        qname = (s.get("qualified_name") or "").lower()
        if not qname:
            continue
        for pkg, rows in by_pkg.items():
            if not pkg:
                continue
            if qname == pkg or qname.startswith(pkg + "."):
                out.setdefault(s["id"], []).extend(rows)
    return out


# =========================================================================
# Layer 9.2 — SAST findings overlay
# =========================================================================


@dataclass
class SASTFinding:
    rule_id: str
    severity: str
    message: str
    file: str
    line: int
    end_line: int = 0
    cwe: str = ""

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


SAST_SCHEMA = """
CREATE TABLE IF NOT EXISTS sast (
    rowid       INTEGER PRIMARY KEY,
    rule_id     TEXT NOT NULL,
    severity    TEXT NOT NULL,
    message     TEXT NOT NULL,
    file        TEXT NOT NULL,
    line        INTEGER NOT NULL,
    end_line    INTEGER NOT NULL,
    cwe         TEXT,
    symbol_id   TEXT,
    source      TEXT NOT NULL,
    ingested_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sast_symbol ON sast(symbol_id);
"""


def ensure_sast_table(store: Store) -> None:
    store.conn.executescript(SAST_SCHEMA)


def parse_bandit(path: Path) -> list[SASTFinding]:
    """Parse `bandit -f json` output."""
    body = json.loads(path.read_text())
    out: list[SASTFinding] = []
    for r in body.get("results") or []:
        out.append(
            SASTFinding(
                rule_id=r.get("test_id") or "",
                severity=(r.get("issue_severity") or "UNKNOWN").upper(),
                message=(r.get("issue_text") or "")[:280],
                file=_normalise_path(r.get("filename") or ""),
                line=int(r.get("line_number") or 0),
                end_line=int(r.get("line_range", [0])[-1] if r.get("line_range") else 0),
                cwe=str(r.get("issue_cwe", {}).get("id", "")),
            )
        )
    return out


def parse_semgrep(path: Path) -> list[SASTFinding]:
    """Parse `semgrep --json` output."""
    body = json.loads(path.read_text())
    out: list[SASTFinding] = []
    for r in body.get("results") or []:
        start = (r.get("start") or {}).get("line", 0)
        end = (r.get("end") or {}).get("line", start)
        extra = r.get("extra") or {}
        out.append(
            SASTFinding(
                rule_id=r.get("check_id") or "",
                severity=(extra.get("severity") or "UNKNOWN").upper(),
                message=(extra.get("message") or "")[:280],
                file=_normalise_path(r.get("path") or ""),
                line=int(start),
                end_line=int(end),
                cwe=str(((extra.get("metadata") or {}).get("cwe") or [""])[0])
                if isinstance(extra.get("metadata", {}).get("cwe"), list)
                else str((extra.get("metadata") or {}).get("cwe") or ""),
            )
        )
    return out


def parse_sast_report(path: Path) -> tuple[list[SASTFinding], str]:
    body = json.loads(path.read_text())
    if isinstance(body, dict) and "results" in body:
        # Both bandit and semgrep use "results" — disambiguate by shape.
        first = (body["results"] or [{}])[0]
        if "test_id" in first or "issue_severity" in first:
            return parse_bandit(path), "bandit"
        if "check_id" in first or ("start" in first and "end" in first):
            return parse_semgrep(path), "semgrep"
    raise ValueError(f"unrecognised SAST format at {path}")


def _normalise_path(raw: str) -> str:
    p = raw.replace("\\", "/").lstrip("./")
    if len(p) > 1 and p[1] == ":":
        p = p[2:]
    return p.lstrip("/")


def map_findings_to_symbols(
    findings: list[SASTFinding], symbols: list[Symbol]
) -> dict[int, str | None]:
    """Map each finding's index to a symbol_id (or None for "no match")."""
    by_path: dict[str, list[Symbol]] = {}
    for s in symbols:
        path = s.get("path") or ""
        if path:
            by_path.setdefault(path, []).append(s)
    for syms in by_path.values():
        syms.sort(
            key=lambda s: ((s.get("span") or (0, 0))[1] - (s.get("span") or (0, 0))[0]) or 1_000_000
        )
    out: dict[int, str | None] = {}
    for i, f in enumerate(findings):
        candidates = by_path.get(f.file)
        if not candidates:
            for path, syms in by_path.items():
                if path.endswith("/" + f.file) or f.file.endswith("/" + path):
                    candidates = syms
                    break
        if not candidates:
            out[i] = None
            continue
        owner: Symbol | None = None
        module: Symbol | None = None
        for s in candidates:
            span = s.get("span") or (0, 0)
            start, end = int(span[0]), int(span[1])
            if start <= f.line <= end:
                owner = s
                break
            if s.get("kind") == "module":
                module = s
        chosen = owner or module
        out[i] = chosen["id"] if chosen else None
    return out


def store_sast(
    store: Store, findings: list[SASTFinding], mapping: dict[int, str | None], *, source: str
) -> int:
    ensure_sast_table(store)
    now = datetime.now(timezone.utc).isoformat()
    with store.tx():
        store.conn.execute("DELETE FROM sast")
        store.conn.executemany(
            "INSERT INTO sast(rule_id, severity, message, file, line, end_line, cwe, "
            "symbol_id, source, ingested_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    f.rule_id,
                    f.severity,
                    f.message,
                    f.file,
                    f.line,
                    f.end_line,
                    f.cwe,
                    mapping.get(i),
                    source,
                    now,
                )
                for i, f in enumerate(findings)
            ],
        )
    return len(findings)


def load_sast(store: Store) -> dict[str, list[dict[str, Any]]]:
    """Return ``{symbol_id: [finding_rows...]}`` for symbols with attribution."""
    ensure_sast_table(store)
    out: dict[str, list[dict[str, Any]]] = {}
    rows = store.conn.execute(
        "SELECT rule_id, severity, message, file, line, end_line, cwe, symbol_id, source "
        "FROM sast WHERE symbol_id IS NOT NULL"
    ).fetchall()
    for rid, sev, msg, file, line, end_line, cwe, sid, source in rows:
        out.setdefault(sid, []).append(
            {
                "rule_id": rid,
                "severity": sev,
                "message": msg,
                "file": file,
                "line": line,
                "end_line": end_line,
                "cwe": cwe,
                "source": source,
            }
        )
    return out


def load_sast_summary(store: Store) -> dict[str, Any]:
    ensure_sast_table(store)
    rows = store.conn.execute("SELECT severity, COUNT(*) FROM sast GROUP BY severity").fetchall()
    return {sev: int(n) for sev, n in rows}


# =========================================================================
# End-to-end ingest
# =========================================================================


def ingest_audit(store: Store, repo_root: Path, report_path: Path) -> dict[str, Any]:
    rows, source = parse_audit_report(report_path)
    persisted = store_cve(store, rows)
    return {
        "format": source,
        "cves": persisted,
        "packages": len({r.package for r in rows}),
    }


def ingest_sast(store: Store, repo_root: Path, report_path: Path) -> dict[str, Any]:
    findings, source = parse_sast_report(report_path)
    symbols = store.all_symbols()
    mapping = map_findings_to_symbols(findings, symbols)
    persisted = store_sast(store, findings, mapping, source=source)
    return {
        "format": source,
        "findings": persisted,
        "attributed": sum(1 for v in mapping.values() if v),
        "by_severity": _count_by_severity(findings),
    }


def _count_by_severity(findings: list[SASTFinding]) -> dict[str, int]:
    out: dict[str, int] = {}
    for f in findings:
        out[f.severity] = out.get(f.severity, 0) + 1
    return out


__all__ = [
    "CVEEntry",
    "SASTFinding",
    "cve_reach_map",
    "ingest_audit",
    "ingest_sast",
    "load_cve",
    "load_sast",
    "load_sast_summary",
    "map_findings_to_symbols",
    "parse_audit_report",
    "parse_bandit",
    "parse_npm_audit",
    "parse_pip_audit",
    "parse_sast_report",
    "parse_semgrep",
    "store_cve",
    "store_sast",
]
