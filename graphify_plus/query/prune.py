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


__all__ = [
    "PruneReport",
    "find_dead_code",
    "find_deprecated",
    "find_orphans",
    "report",
]
