"""``gp doctor`` — single 'something feels off' diagnostic.

Inspects the target repo's cache, watcher state, and recent skips.
Returns exit 0 when no ERROR diagnostics are present, 1 otherwise.
Output is structured-text by default, JSON with ``--json``.
"""

from __future__ import annotations

import importlib.metadata
import json
import platform
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import click

from ...runtime.store import SCHEMA_VERSION, Store, cache_path

OK = "OK"
INFO = "INFO"
WARN = "WARN"
ERROR = "ERROR"

_GLYPH = {"OK": "✓", "INFO": "i", "WARN": "⚠", "ERROR": "✗"}


@dataclass(frozen=True)
class Diagnostic:
    section: str
    severity: str  # OK | INFO | WARN | ERROR
    message: str
    remediation: str | None = None


def _version() -> str:
    try:
        return importlib.metadata.version("graphify-plus")
    except importlib.metadata.PackageNotFoundError:
        return "(dev)"


def _check_system() -> list[Diagnostic]:
    return [
        Diagnostic("System", INFO, f"graphify-plus {_version()}"),
        Diagnostic(
            "System",
            INFO,
            f"Python {sys.version.split()[0]} on {platform.system()}-{platform.machine()}",
        ),
    ]


def _check_cache(repo: Path) -> list[Diagnostic]:
    out: list[Diagnostic] = []
    db = cache_path(repo)
    if not db.exists():
        return [
            Diagnostic(
                "Cache",
                ERROR,
                f"No cache at {db}",
                remediation=f"Run 'graphify-plus init --repo {repo}' to build it.",
            )
        ]
    try:
        store = Store(db)
    except Exception as e:  # noqa: BLE001
        return [
            Diagnostic(
                "Cache",
                ERROR,
                f"Cache failed to open: {e}",
                remediation="The corrupt file has been quarantined; run 'gp init --force'.",
            )
        ]
    try:
        size_mb = db.stat().st_size / (1024 * 1024)
        sym_count = store.get_meta("symbol_count") or "?"
        edge_count = store.get_meta("edge_count") or "?"
        repo_root = store.get_meta("repo_root") or "?"
        on_disk = store.get_meta("schema_version") or "?"
        out.append(Diagnostic("Cache", OK, f"healthy ({size_mb:.1f} MB)"))
        out.append(
            Diagnostic(
                "Cache",
                INFO,
                f"schema_version={on_disk} (tool: {SCHEMA_VERSION})",
            )
        )
        out.append(
            Diagnostic(
                "Cache",
                INFO,
                f"{sym_count} symbols / {edge_count} edges",
            )
        )
        out.append(Diagnostic("Cache", INFO, f"repo_root: {repo_root}"))
    finally:
        store.close()
    # Backups visible.
    for p in sorted(db.parent.glob("cache.db.corrupt.*")):
        out.append(
            Diagnostic(
                "Cache",
                WARN,
                f"quarantined: {p.name}",
                remediation="Inspect and delete once you've confirmed it's not needed.",
            )
        )
    return out


def _check_skipped(repo: Path) -> list[Diagnostic]:
    skipped = repo / ".graphify_plus" / "skipped.jsonl"
    if not skipped.exists() or skipped.read_text().strip() == "":
        return [Diagnostic("Adapter coverage", OK, "no skipped files")]
    lines = [ln for ln in skipped.read_text().splitlines() if ln.strip()]
    by_reason: dict[str, int] = {}
    for ln in lines:
        try:
            row = json.loads(ln)
        except json.JSONDecodeError:
            continue
        by_reason[row.get("reason", "unknown")] = by_reason.get(row.get("reason", "unknown"), 0) + 1
    out = [
        Diagnostic(
            "Adapter coverage",
            WARN,
            f"{len(lines)} files skipped",
            remediation=f"Inspect {skipped} for details.",
        )
    ]
    for reason, count in sorted(by_reason.items()):
        out.append(Diagnostic("Adapter coverage", INFO, f"  {reason}: {count}"))
    return out


def _check_audit_capability(repo: Path) -> list[Diagnostic]:
    """Surface audit-probe capability gaps before the operator runs `gp audit`.

    Three probes (edge_deletion_stability, modularity_quality,
    confidence_drift) require enrichment data that ``gp init`` alone
    does not produce — Louvain communities and INFERRED-confidence
    edges. If those signals are absent, the probes will SKIP. Tell the
    operator NOW so they don't first run an audit, see SKIPs, and have
    to backtrack.
    """
    db = cache_path(repo)
    if not db.exists():
        return []
    try:
        store = Store(db, integrity_check=False)
    except Exception:  # noqa: BLE001
        return []
    try:
        symbols = store.all_symbols()
        edges = store.all_edges()
    finally:
        store.close()
    needs_community = not any("community" in s for s in symbols)
    needs_inferred = sum(1 for e in edges if e.get("confidence_score") is not None) < 3
    out: list[Diagnostic] = []
    if needs_community or needs_inferred:
        gaps: list[str] = []
        if needs_community:
            gaps.append("edge_deletion_stability + modularity_quality (need community)")
        if needs_inferred:
            gaps.append("confidence_drift (needs ≥3 INFERRED-confidence edges)")
        out.append(
            Diagnostic(
                "Audit capability",
                WARN,
                f"{len(gaps)} audit probe set(s) will SKIP without enrichment",
                remediation=(
                    "Run 'gp enrich --community' and/or feed telemetry "
                    "before 'gp audit' to lift the trust score:\n      " + "; ".join(gaps)
                ),
            )
        )
    else:
        out.append(Diagnostic("Audit capability", OK, "all probes have required signals"))
    return out


def _check_debug_log(repo: Path) -> list[Diagnostic]:
    log = repo / ".graphify_plus" / "debug.log"
    if not log.exists() or log.stat().st_size == 0:
        return [Diagnostic("Recent errors", OK, "none")]
    # Show the last 24h count.
    cutoff = datetime.now(timezone.utc).timestamp() - 24 * 3600
    recent = 0
    last_code: str | None = None
    for line in log.read_text(errors="replace").splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        # No timestamp field today — just count entries.
        recent += 1
        last_code = payload.get("code") or last_code
    _ = cutoff
    if recent:
        return [
            Diagnostic(
                "Recent errors",
                WARN,
                f"{recent} entries in {log.name} (latest code: {last_code or '?'})",
                remediation=f"Inspect {log} or run with GP_DEBUG=1 for next-time tracebacks.",
            )
        ]
    return [Diagnostic("Recent errors", OK, "none")]


def diagnose(repo: Path) -> list[Diagnostic]:
    out: list[Diagnostic] = []
    out.extend(_check_system())
    out.extend(_check_cache(repo))
    out.extend(_check_skipped(repo))
    out.extend(_check_audit_capability(repo))
    out.extend(_check_debug_log(repo))
    return out


def render(diags: list[Diagnostic]) -> str:
    sections: dict[str, list[Diagnostic]] = {}
    for d in diags:
        sections.setdefault(d.section, []).append(d)
    lines: list[str] = []
    lines.append("Graphify-Plus Diagnostic Report")
    lines.append("================================")
    for sec, entries in sections.items():
        lines.append("")
        lines.append(sec)
        for d in entries:
            mark = _GLYPH.get(d.severity, " ")
            lines.append(f"  {mark} {d.message}")
            if d.remediation:
                lines.append(f"     → {d.remediation}")
    return "\n".join(lines) + "\n"


@click.command("doctor")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
@click.option("--json", "as_json", is_flag=True)
def doctor_cmd(repo: Path, as_json: bool) -> None:
    """Run a structured diagnostic against the target repo."""
    repo = repo.resolve()
    diags = diagnose(repo)
    if as_json:
        click.echo(json.dumps([asdict(d) for d in diags], indent=2))
    else:
        click.echo(render(diags))
    has_error = any(d.severity == ERROR for d in diags)
    sys.exit(1 if has_error else 0)


__all__ = ["Diagnostic", "diagnose", "doctor_cmd", "render"]
