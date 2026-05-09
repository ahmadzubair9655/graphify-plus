"""Test-coverage overlay — Sprint 8 of the master plan.

Layer 7.1: "On `pytest --cov`, `jest --coverage`, `go test -cover`, parse the
coverage report and tag every node with `covered_by`, `coverage_pct`,
`last_covered`. The graph answers `whats_untested` in ms with no test
execution at query time."

Two report formats are supported here:

* **Cobertura XML** — produced by `coverage xml` / `pytest --cov-report=xml`.
  Each `<class filename="x.py">` carries a list of `<line number="N" hits="K"/>`
  entries.
* **Istanbul JSON** — produced by Jest / `nyc` / Vitest. The top-level
  object maps absolute file paths to `{statementMap, s, …}` blocks; each
  ``statementMap`` entry has a ``start.line`` and ``end.line``, and ``s``
  is the hit count for that statement id.

Both flatten to the same intermediate shape: a `dict[str, list[Hit]]`
keyed by repo-relative POSIX path, where each `Hit` is `(line, hits)`.
That intermediate is then mapped onto the symbol graph: the symbol whose
``span = (start, end)`` contains the line — preferring the deepest
container — owns the hit. Module-scope lines go to the module symbol.

Storage is a new SQLite table ``coverage(symbol_id, lines_covered,
lines_total, pct, source, ingested_at)``. The daemon reads it at
``InMemoryGraph`` build time and surfaces ``coverage_pct`` /
``coverage_lines`` on every symbol, so existing handlers (whats_in,
who_calls, etc.) get coverage attribution for free.
"""

from __future__ import annotations

import json
import logging
import os
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..core.adapters import Symbol
from ..runtime.store import Store

log = logging.getLogger("graphify_plus.daemon.coverage")


# ---- intermediate types --------------------------------------------------


@dataclass(frozen=True)
class LineHit:
    line: int
    hits: int  # 0 means "executable but not hit"


@dataclass
class CoverageReport:
    """Parsed coverage report, normalised across formats."""

    files: dict[str, list[LineHit]] = field(default_factory=dict)
    source: str = ""  # "cobertura" | "istanbul" | "unknown"

    @property
    def total_lines(self) -> int:
        return sum(len(v) for v in self.files.values())

    @property
    def total_covered(self) -> int:
        return sum(1 for hits in self.files.values() for h in hits if h.hits > 0)


# ---- parsers -------------------------------------------------------------


def parse_report(path: Path) -> CoverageReport:
    """Sniff the format from the file extension and dispatch.

    Cobertura XML and Istanbul JSON are both common; we pick by extension
    first and fall back to peeking at the file body.
    """
    suffix = path.suffix.lower()
    if suffix == ".xml":
        return _parse_cobertura(path)
    if suffix == ".json":
        return _parse_istanbul(path)
    # Fallback sniff.
    head = path.read_text(encoding="utf-8", errors="replace")[:200].lstrip()
    if head.startswith("<?xml") or head.startswith("<coverage"):
        return _parse_cobertura(path)
    if head.startswith("{"):
        return _parse_istanbul(path)
    raise ValueError(f"unrecognised coverage format at {path}")


def _parse_cobertura(path: Path) -> CoverageReport:
    """Cobertura XML — the lingua franca of Python coverage."""
    tree = ET.parse(path)
    root = tree.getroot()
    files: dict[str, list[LineHit]] = {}
    for cls in root.iter("class"):
        filename = cls.get("filename") or ""
        if not filename:
            continue
        rel = _normalise_path(filename)
        bucket: list[LineHit] = []
        for line in cls.iter("line"):
            try:
                num = int(line.get("number") or "0")
                hits = int(line.get("hits") or "0")
            except ValueError:
                continue
            bucket.append(LineHit(line=num, hits=hits))
        if bucket:
            files[rel] = bucket
    return CoverageReport(files=files, source="cobertura")


def _parse_istanbul(path: Path) -> CoverageReport:
    """Istanbul / Jest / nyc / Vitest JSON.

    The top-level dict maps file paths to per-file coverage blocks. We
    expand each statement into a per-line hit entry — same intermediate
    shape as Cobertura.
    """
    with path.open() as fp:
        body = json.load(fp)
    if not isinstance(body, dict):
        raise ValueError("istanbul report must be a JSON object")
    files: dict[str, list[LineHit]] = {}
    for raw_path, info in body.items():
        if not isinstance(info, dict):
            continue
        rel = _normalise_path(raw_path)
        statements = info.get("statementMap") or {}
        hits = info.get("s") or {}
        # accumulate hits per line: a single line may host multiple statements.
        per_line: dict[int, int] = {}
        for sid, span in statements.items():
            if not isinstance(span, dict):
                continue
            start = (span.get("start") or {}).get("line")
            end = (span.get("end") or {}).get("line", start)
            if start is None:
                continue
            count = int(hits.get(sid, 0) or 0)
            for ln in range(int(start), int(end) + 1):
                per_line[ln] = per_line.get(ln, 0) + count
        if per_line:
            files[rel] = [LineHit(line=ln, hits=ct) for ln, ct in sorted(per_line.items())]
    return CoverageReport(files=files, source="istanbul")


def _normalise_path(raw: str) -> str:
    """Convert any abs/relative path into a repo-relative POSIX string.

    Coverage reports often carry absolute paths from CI, or paths
    rooted in the test runner's CWD. We strip a leading absolute prefix
    (best-effort: ``/build/repo/`` becomes ``src/foo.py``) and convert
    backslashes to forward.
    """
    p = raw.replace("\\", "/")
    # Drop volume prefixes (Windows) and leading slashes.
    if len(p) > 1 and p[1] == ":":
        p = p[2:]
    p = p.lstrip("/")
    return p


# ---- per-symbol mapping --------------------------------------------------


@dataclass
class SymbolCoverage:
    symbol_id: str
    lines_covered: int
    lines_total: int

    @property
    def pct(self) -> float:
        if self.lines_total == 0:
            return 0.0
        return self.lines_covered / self.lines_total


def map_to_symbols(
    report: CoverageReport, symbols: Iterable[Symbol], repo_root: Path
) -> list[SymbolCoverage]:
    """Attribute each report line to the deepest symbol containing it.

    Lines outside any symbol's span fall to the file's module symbol
    when one exists. Symbols that don't appear in the report at all are
    *omitted* — meaning "no signal", not "0% covered". This avoids
    blanket-zero-ing untested files when only part of the test suite ran.
    """
    by_path: dict[str, list[Symbol]] = {}
    for s in symbols:
        path = s.get("path") or ""
        if path:
            by_path.setdefault(path, []).append(s)

    # Pre-sort each file's symbols by ascending span size. Finding the
    # deepest container at query time is then "first match in narrowest-
    # first order" — O(symbols-in-file) per line.
    for syms in by_path.values():
        syms.sort(
            key=lambda s: (
                ((s.get("span") or (0, 0))[1] - (s.get("span") or (0, 0))[0]) or 1_000_000
            )
        )

    accum: dict[str, list[int]] = {}  # symbol_id → [covered, total]
    for rel, hits in report.files.items():
        # Try repo-relative match first; if missing, also try matching by
        # suffix in case the report was rooted somewhere else.
        candidates = by_path.get(rel)
        if not candidates:
            candidates = _match_by_suffix(rel, by_path)
        if not candidates:
            continue
        for hit in hits:
            owner = _deepest_owner(hit.line, candidates)
            if owner is None:
                continue
            sid = owner["id"]
            bucket = accum.setdefault(sid, [0, 0])
            bucket[1] += 1
            if hit.hits > 0:
                bucket[0] += 1

    return [
        SymbolCoverage(symbol_id=sid, lines_covered=cov, lines_total=tot)
        for sid, (cov, tot) in accum.items()
        if tot > 0
    ]


def _match_by_suffix(rel: str, by_path: dict[str, list[Symbol]]) -> list[Symbol] | None:
    """If ``rel`` doesn't exactly match any indexed path, try suffix match.

    A coverage report rooted at ``/build/work/repo/auth.py`` and an
    indexed path ``auth.py`` should still align.
    """
    for path, syms in by_path.items():
        if path.endswith("/" + rel) or rel.endswith("/" + path):
            return syms
    # Last resort: basename match.
    base = os.path.basename(rel)
    for path, syms in by_path.items():
        if os.path.basename(path) == base:
            return syms
    return None


def _deepest_owner(line: int, candidates: list[Symbol]) -> Symbol | None:
    """Return the smallest symbol whose span contains ``line``.

    ``candidates`` is sorted ascending by span size, so the first hit is
    the deepest container.
    """
    module: Symbol | None = None
    for s in candidates:
        span = s.get("span")
        if not span:
            continue
        start, end = int(span[0]), int(span[1])
        if start <= line <= end:
            return s
        if s.get("kind") == "module":
            module = s
    # Lines outside any class/function (e.g. top-level imports) fall to
    # the module symbol.
    return module


# ---- store layer ---------------------------------------------------------

COVERAGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS coverage (
    symbol_id     TEXT PRIMARY KEY,
    lines_covered INTEGER NOT NULL,
    lines_total   INTEGER NOT NULL,
    pct           REAL    NOT NULL,
    source        TEXT    NOT NULL,
    ingested_at   TEXT    NOT NULL
);
"""


def ensure_coverage_table(store: Store) -> None:
    store.conn.executescript(COVERAGE_SCHEMA)


def store_coverage(store: Store, rows: list[SymbolCoverage], *, source: str) -> int:
    """Replace coverage data wholesale with the new ingest.

    Coverage runs are typically authoritative (a fresh test run replaces
    the previous picture), so we replace rather than merge. If you need
    additive merging, run two ingests targeting different output files
    and combine before passing here.
    """
    ensure_coverage_table(store)
    now = datetime.now(timezone.utc).isoformat()
    with store.tx():
        store.conn.execute("DELETE FROM coverage")
        store.conn.executemany(
            "INSERT INTO coverage(symbol_id, lines_covered, lines_total, pct, source, ingested_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [
                (r.symbol_id, r.lines_covered, r.lines_total, r.pct, source, now)
                for r in rows
            ],
        )
    return len(rows)


def load_coverage(store: Store) -> dict[str, dict[str, Any]]:
    """Return ``{symbol_id: {pct, lines_covered, lines_total, source}}``."""
    ensure_coverage_table(store)
    out: dict[str, dict[str, Any]] = {}
    rows = store.conn.execute(
        "SELECT symbol_id, lines_covered, lines_total, pct, source FROM coverage"
    ).fetchall()
    for sid, cov, tot, pct, source in rows:
        out[sid] = {
            "lines_covered": cov,
            "lines_total": tot,
            "pct": pct,
            "source": source,
        }
    return out


def coverage_summary(store: Store) -> dict[str, Any]:
    ensure_coverage_table(store)
    row = store.conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(lines_covered), 0), COALESCE(SUM(lines_total), 0), "
        "MAX(ingested_at), source "
        "FROM coverage GROUP BY source"
    ).fetchall()
    if not row:
        return {"symbols": 0, "lines_covered": 0, "lines_total": 0, "overall_pct": 0.0}
    # If multiple sources, keep the most recent.
    row.sort(key=lambda r: r[3] or "", reverse=True)
    n_syms, lc, lt, ts, source = row[0]
    return {
        "symbols": n_syms,
        "lines_covered": int(lc),
        "lines_total": int(lt),
        "overall_pct": (lc / lt) if lt else 0.0,
        "source": source,
        "ingested_at": ts,
    }


# ---- end-to-end ingest ---------------------------------------------------


def ingest_report(store: Store, repo_root: Path, report_path: Path) -> dict[str, Any]:
    """Parse a coverage report, map onto symbols, and persist. Returns
    a small summary suitable for the CLI to print.
    """
    report = parse_report(report_path)
    symbols = store.all_symbols()
    rows = map_to_symbols(report, symbols, repo_root)
    persisted = store_coverage(store, rows, source=report.source)
    return {
        "format": report.source,
        "files": len(report.files),
        "report_total_lines": report.total_lines,
        "report_covered_lines": report.total_covered,
        "symbols_attributed": persisted,
    }


__all__ = [
    "CoverageReport",
    "LineHit",
    "SymbolCoverage",
    "coverage_summary",
    "ensure_coverage_table",
    "ingest_report",
    "load_coverage",
    "map_to_symbols",
    "parse_report",
    "store_coverage",
]
