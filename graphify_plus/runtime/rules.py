"""Unified constraint engine.

ONE primitive evaluator powers Features 5 (architectural guardrails),
10 (shadow-graph dry-run), and 18 (drift enforcement). Do not build
parallel evaluators for any of these. If a rule kind is missing, add
it here.

Rules are declarative YAML at ``<target_repo>/.graphify_plus/rules.yaml``::

    layers:
      ui:        ["frontend/**", "src/components/**"]
      api:       ["backend/api/**", "service/**"]
      database:  ["backend/db/**", "**/migrations/**"]

    rules:
      - id: no-ui-to-db
        description: UI must not import from the database layer.
        forbid_edge:
          from_layer: ui
          to_layer: database
          kinds: [imports, calls]
        severity: error

      - id: no-circular-imports
        forbid_cycle:
          kinds: [imports]
        severity: error

The shape is small, declarative, and composable — every later rule kind
slots in here. ``evaluate`` is a pure function; nothing in this module
performs I/O.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Iterable
from dataclasses import dataclass, field

import networkx as nx

from .overlay import GraphOverlay


@dataclass(frozen=True)
class Violation:
    rule_id: str
    severity: str  # 'error' | 'warning'
    message: str
    src: str | None = None
    dst: str | None = None
    kind: str | None = None  # edge kind, when applicable


@dataclass
class RuleSet:
    layers: dict[str, list[str]] = field(default_factory=dict)
    rules: list[dict] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> RuleSet:
        return cls(
            layers={k: list(v) for k, v in (data.get("layers") or {}).items()},
            rules=list(data.get("rules") or []),
        )


# ---------- layer assignment -------------------------------------------


def _matches_any(path: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatch(path, p) for p in patterns)


def assign_layer(path: str, layers: dict[str, list[str]]) -> str | None:
    """Return the first layer whose globs match ``path``, else None."""
    for name, globs in layers.items():
        if _matches_any(path, globs):
            return name
    return None


# ---------- evaluation -------------------------------------------------


def _layer_of_node(sid: str, overlay: GraphOverlay, layers: dict[str, list[str]]) -> str | None:
    attrs = overlay.get_node_attrs(sid)
    if attrs is None:
        return None
    path = attrs.get("path") or ""
    if not path:
        return None
    return assign_layer(path, layers)


def _evaluate_forbid_edge(
    rule: dict, overlay: GraphOverlay, layers: dict[str, list[str]]
) -> list[Violation]:
    forbidden_kinds = set(rule["forbid_edge"].get("kinds") or [])
    from_layer = rule["forbid_edge"].get("from_layer")
    to_layer = rule["forbid_edge"].get("to_layer")
    severity = rule.get("severity", "error")
    rid = rule.get("id", "<unnamed>")

    # Iterate every edge in the overlay-effective graph.
    base = overlay.base
    seen: set[tuple[str, str, str]] = set()

    def _check(u: str, v: str, data: dict) -> Violation | None:
        kind = (data or {}).get("kind") or ""
        if forbidden_kinds and kind not in forbidden_kinds:
            return None
        lu = _layer_of_node(u, overlay, layers)
        lv = _layer_of_node(v, overlay, layers)
        if from_layer and lu != from_layer:
            return None
        if to_layer and lv != to_layer:
            return None
        return Violation(
            rule_id=rid,
            severity=severity,
            message=f"{u} → {v} ({kind}) violates {from_layer}→{to_layer}",
            src=u,
            dst=v,
            kind=kind,
        )

    out: list[Violation] = []
    for u, v, data in base.edges(data=True):
        kind = (data or {}).get("kind") or ""
        if (u, v, kind) in overlay.removed_edges:
            continue
        if u in overlay.removed_nodes or v in overlay.removed_nodes:
            continue
        if (u, v, kind) in seen:
            continue
        seen.add((u, v, kind))
        vio = _check(u, v, data or {})
        if vio:
            out.append(vio)
    for u, v, data in overlay.added_edges:
        kind = (data or {}).get("kind") or ""
        if (u, v, kind) in seen:
            continue
        seen.add((u, v, kind))
        vio = _check(u, v, data or {})
        if vio:
            out.append(vio)
    return out


def _evaluate_forbid_cycle(rule: dict, overlay: GraphOverlay) -> list[Violation]:
    kinds = set(rule["forbid_cycle"].get("kinds") or [])
    severity = rule.get("severity", "error")
    rid = rule.get("id", "<unnamed>")

    G = overlay.materialise() if not overlay.is_empty() else overlay.base
    if kinds:
        sub = nx.MultiDiGraph()
        sub.add_nodes_from(G.nodes(data=True))
        for u, v, data in G.edges(data=True):
            if (data or {}).get("kind") in kinds:
                sub.add_edge(u, v, **(data or {}))
    else:
        sub = G
    try:
        cycles = list(nx.simple_cycles(sub))
    except Exception:  # noqa: BLE001
        cycles = []
    out: list[Violation] = []
    seen_keys: set[tuple[str, ...]] = set()
    for cy in cycles:
        if len(cy) < 2:
            continue
        key = tuple(sorted(cy))
        if key in seen_keys:
            continue
        seen_keys.add(key)
        out.append(
            Violation(
                rule_id=rid,
                severity=severity,
                message=f"cycle: {' → '.join(cy)} → {cy[0]}",
            )
        )
    return out


def evaluate(overlay: GraphOverlay, ruleset: RuleSet) -> list[Violation]:
    """Pure function — apply every rule in ``ruleset`` to the implied graph."""
    out: list[Violation] = []
    for rule in ruleset.rules:
        if "forbid_edge" in rule:
            out.extend(_evaluate_forbid_edge(rule, overlay, ruleset.layers))
        elif "forbid_cycle" in rule:
            out.extend(_evaluate_forbid_cycle(rule, overlay))
    out.sort(key=lambda v: (v.rule_id, v.src or "", v.dst or ""))
    return out


# ---------- structural grade -------------------------------------------


def grade(violations: list[Violation]) -> str:
    """Letter grade based on count + severity. Cheap proxy used by shadow."""
    errors = sum(1 for v in violations if v.severity == "error")
    warnings = sum(1 for v in violations if v.severity == "warning")
    if errors == 0 and warnings == 0:
        return "A"
    if errors == 0 and warnings <= 3:
        return "B"
    if errors <= 1:
        return "C"
    if errors <= 3:
        return "D"
    return "F"


__all__ = ["RuleSet", "Violation", "assign_layer", "evaluate", "grade"]
