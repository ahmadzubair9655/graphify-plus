"""Pre-tool-use guardrails.

Reads a proposed-edit JSON payload (typically piped in from an AI agent's
tool-use hook), constructs an implied overlay, evaluates the unified
rules engine, and returns either ``OK`` or a structured block payload.

Payload shape::

    {
      "file": "frontend/Catalogue.tsx",
      "add_imports": ["backend.db.client"],
      "remove_imports": [],
      "add_calls": [["frontend.Catalogue.fetchAll", "backend.db.client.query"]],
      "remove_calls": []
    }

This is intentionally minimal — it is a *guardrail*, not a full diff
engine. The shadow command (Phase 6) handles richer simulations.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..runtime.overlay import GraphOverlay, empty
from ..runtime.rules import RuleSet, Violation, evaluate

NodeRef = str  # qualified name or symbol id


@dataclass(frozen=True)
class GuardrailDecision:
    block: bool
    rule_id: str | None
    suggestion: str | None
    violations: list[Violation]


def _resolve(store, ref: NodeRef) -> str | None:
    """Best-effort symbol id resolution: id → qualified name → suffix."""
    if store.get_symbol(ref) is not None:
        return ref
    matches = store.find_symbols_by_qualified_name(ref)
    if matches:
        return matches[0]["id"]
    return None


def _module_for_file(store, file_path: str) -> str | None:
    syms = store.symbols_in_path(file_path)
    for s in syms:
        if s.get("kind") == "module":
            return s["id"]
    return syms[0]["id"] if syms else None


def build_overlay(store, base_graph, payload: dict) -> GraphOverlay:
    overlay = empty(base_graph)
    file_path = payload.get("file") or ""
    mod_id = _module_for_file(store, file_path) if file_path else None

    for imp in payload.get("add_imports") or []:
        if mod_id is None:
            continue
        # Imports targeting an unresolved name attach as a string-id node.
        target = _resolve(store, imp) or imp
        if not base_graph.has_node(target):
            overlay.add_node(target, kind="external", path="", name=imp)
        overlay.add_edge(mod_id, target, kind="imports", resolved=False)

    for imp in payload.get("remove_imports") or []:
        if mod_id is None:
            continue
        target = _resolve(store, imp) or imp
        overlay.remove_edge(mod_id, target, kind="imports")

    for src, dst in payload.get("add_calls") or []:
        sid = _resolve(store, src)
        did = _resolve(store, dst)
        if sid is None or did is None:
            continue
        overlay.add_edge(sid, did, kind="calls", resolved=True)

    for src, dst in payload.get("remove_calls") or []:
        sid = _resolve(store, src)
        did = _resolve(store, dst)
        if sid is None or did is None:
            continue
        overlay.remove_edge(sid, did, kind="calls")

    return overlay


_LAYER_HINT_RE = re.compile(r"\b(database|db)\b", re.IGNORECASE)


def _suggestion(v: Violation) -> str:
    if v.kind == "imports" and v.dst and _LAYER_HINT_RE.search(v.dst):
        return (
            "Move the database access behind an API/service boundary, then "
            "import that boundary instead."
        )
    return "Refactor so the source layer does not depend on the target layer."


def evaluate_payload(store, base_graph, payload: dict, ruleset: RuleSet) -> GuardrailDecision:
    overlay = build_overlay(store, base_graph, payload)
    violations = evaluate(overlay, ruleset)
    blocking = [v for v in violations if v.severity == "error"]
    if not blocking:
        return GuardrailDecision(block=False, rule_id=None, suggestion=None, violations=violations)
    first = blocking[0]
    return GuardrailDecision(
        block=True,
        rule_id=first.rule_id,
        suggestion=_suggestion(first),
        violations=violations,
    )


__all__ = ["GuardrailDecision", "build_overlay", "evaluate_payload"]
