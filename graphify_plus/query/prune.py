"""Ghost-Dependency / Dead-Code Pruning.

Heuristics applied to the symbol graph:

  - **Dead code**: leaf symbols (no out-edges of kind calls/references)
    that also have no inbound calls/references/extends/implements.
  - **Orphans**: symbols in a weakly-connected component of size 1.
  - **Deprecated**: docstring contains "deprecated" (case-insensitive),
    or path matches ``**/legacy/**`` / ``**/deprecated/**``.

These are *recommendations*, not mutations. ``gp prune`` returns the IDs
to ignore; the budgeter (Phase 3) consumes that set to exclude them
from any framed context.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import networkx as nx

from ..core.adapters import Symbol

DEPRECATED_RE = re.compile(r"\bdeprecated\b", re.IGNORECASE)
LEGACY_PATH_RE = re.compile(r"(?:^|/)(?:legacy|deprecated)/", re.IGNORECASE)

_INBOUND_KINDS = {"calls", "references", "extends", "implements", "jsx_render"}
_OUTBOUND_KINDS = {"calls", "references", "jsx_render"}


@dataclass(frozen=True)
class PruneReport:
    dead: set[str] = field(default_factory=set)
    orphans: set[str] = field(default_factory=set)
    deprecated: set[str] = field(default_factory=set)

    def all_ids(self, *, aggressive: bool = False) -> set[str]:
        out = set(self.dead) | set(self.orphans)
        if aggressive:
            out |= self.deprecated
        return out


def _has_inbound(G: nx.MultiDiGraph, sid: str) -> bool:
    for _, _, data in G.in_edges(sid, data=True):
        if (data or {}).get("kind") in _INBOUND_KINDS:
            return True
    return False


def _has_outbound(G: nx.MultiDiGraph, sid: str) -> bool:
    for _, _, data in G.out_edges(sid, data=True):
        if (data or {}).get("kind") in _OUTBOUND_KINDS:
            return True
    return False


def _is_protected(sym: Symbol) -> bool:
    """Module-level synthetic symbols and exported APIs are never pruned."""
    kind = sym.get("kind") or ""
    if kind in {"module", "external", "endpoint"}:
        return True
    if sym.get("exported"):
        return True
    return False


def find_dead_code(G: nx.MultiDiGraph) -> set[str]:
    out: set[str] = set()
    for sid, attrs in G.nodes(data=True):
        if (attrs or {}).get("kind") == "external":
            continue
        sym: Symbol = dict(attrs)  # type: ignore[assignment]
        if _is_protected(sym):
            continue
        if _has_inbound(G, sid):
            continue
        if _has_outbound(G, sid):
            continue
        out.add(sid)
    return out


def find_orphans(G: nx.MultiDiGraph) -> set[str]:
    UG = G.to_undirected(as_view=False)
    out: set[str] = set()
    for comp in nx.connected_components(UG):
        if len(comp) == 1:
            (sid,) = comp
            sym: Symbol = dict(G.nodes[sid])  # type: ignore[assignment]
            if _is_protected(sym):
                continue
            out.add(sid)
    return out


def find_deprecated(G: nx.MultiDiGraph) -> set[str]:
    out: set[str] = set()
    for sid, attrs in G.nodes(data=True):
        a = attrs or {}
        doc = a.get("docstring") or ""
        path = a.get("path") or ""
        if DEPRECATED_RE.search(doc) or LEGACY_PATH_RE.search(path):
            out.add(sid)
    return out


def report(G: nx.MultiDiGraph) -> PruneReport:
    return PruneReport(
        dead=find_dead_code(G),
        orphans=find_orphans(G),
        deprecated=find_deprecated(G),
    )


# ---------------------------------------------------------------------------
# Dead-code classification (v5.1+)
# ---------------------------------------------------------------------------
#
# `gp prune` historically returned a flat list of "dead" symbols, leaving
# users to manually triage which were genuine and which were
# false-positives caused by limited static analysis. Tag each candidate
# with a category + confidence so callers (and the budgeter) can filter.

CATEGORIES: tuple[str, ...] = (
    "plausibly_dead",
    "jsx_internal",
    "reducer_case",
    "test_internal",
    "prop_type",
    "pytest_fixture",
    "dunder_method",
    "unknown",
)

CATEGORY_CONFIDENCE: dict[str, float] = {
    "plausibly_dead": 0.85,
    "prop_type": 0.4,
    "jsx_internal": 0.15,
    "reducer_case": 0.15,
    "test_internal": 0.15,
    "pytest_fixture": 0.15,
    "dunder_method": 0.15,
    "unknown": 0.5,
}

_TEST_PATH_RE = re.compile(
    r"(?:^|/)(?:__tests__|tests?)/|(?:\.test|\.spec)\.(?:[jt]sx?|py)$",
    re.IGNORECASE,
)
_PYTEST_FIXTURE_NAME_RE = re.compile(r"^(?:fixture_|setup_|teardown_)", re.IGNORECASE)
_REDUCER_HINT_RE = re.compile(
    r"^(?:on[A-Z_]|handle[A-Z_]|reduce[A-Z_]|reducer$|case[A-Z_]|.*Reducer$|.*Handler$)"
)
_PROP_TYPE_NAME_RE = re.compile(r"(?:Props|Properties|Propz|PropTypes)$")


def _is_jsx_file(path: str) -> bool:
    p = path.lower()
    return p.endswith((".tsx", ".jsx"))


def _is_python_file(path: str) -> bool:
    return path.lower().endswith(".py")


def classify_dead_candidate(sym: Symbol) -> str:
    """Return one of CATEGORIES describing why this symbol may have been
    flagged as dead. Heuristics are deliberately conservative — when in
    doubt, prefer ``unknown`` over a confident-sounding mislabel.
    """
    name = sym.get("name") or ""
    qname = sym.get("qualified_name") or ""
    path = sym.get("path") or ""
    kind = sym.get("kind") or ""
    parent_id = sym.get("parent_id")
    language = sym.get("language") or ""

    if _TEST_PATH_RE.search(path):
        return "test_internal"

    if _is_python_file(path):
        if name.startswith("__") and name.endswith("__") and len(name) > 4:
            return "dunder_method"
        if "conftest.py" in path or _PYTEST_FIXTURE_NAME_RE.match(name):
            return "pytest_fixture"

    if kind in {"interface", "type"} and _PROP_TYPE_NAME_RE.search(name):
        return "prop_type"

    if language in {"typescript", "javascript"} and _is_jsx_file(path):
        # Nested function/component (parent is itself a function, not the
        # module). After Task A most of these get a JSX_RENDER edge and
        # never appear here — but dynamic-dispatch (`React.createElement`,
        # `cloneElement`) still slips through.
        if parent_id is not None and "." in qname:
            # qname has at least one dot beyond the module segment
            depth = qname.count(".")
            if depth >= 2:
                return "jsx_internal"

    if _REDUCER_HINT_RE.match(name):
        return "reducer_case"

    if sym.get("exported") and not _TEST_PATH_RE.search(path):
        return "plausibly_dead"

    return "unknown"


def confidence_for(category: str) -> float:
    return CATEGORY_CONFIDENCE.get(category, 0.5)


def classify_all(
    G: nx.MultiDiGraph, dead_ids: set[str]
) -> list[dict]:
    """Return a sorted list of {id, category, confidence, ...} dicts for
    every symbol in ``dead_ids``. Sorted by (-confidence, qname) so the
    most-likely-dead candidates appear first.
    """
    out: list[dict] = []
    for sid in dead_ids:
        attrs = G.nodes.get(sid) or {}
        sym: Symbol = dict(attrs)  # type: ignore[assignment]
        category = classify_dead_candidate(sym)
        out.append(
            {
                "id": sid,
                "qualified_name": sym.get("qualified_name") or sid,
                "path": sym.get("path") or "",
                "kind": sym.get("kind") or "",
                "language": sym.get("language") or "",
                "likely_category": category,
                "confidence": confidence_for(category),
            }
        )
    out.sort(key=lambda r: (-r["confidence"], r["qualified_name"]))
    return out


__all__ = [
    "CATEGORIES",
    "CATEGORY_CONFIDENCE",
    "PruneReport",
    "classify_all",
    "classify_dead_candidate",
    "confidence_for",
    "find_dead_code",
    "find_deprecated",
    "find_orphans",
    "report",
]
