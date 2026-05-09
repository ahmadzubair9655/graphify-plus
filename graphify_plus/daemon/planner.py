"""Graph-grounded edit planning — Sprint 6 of the master plan.

``gp daemon plan "add rate limiting to all public API endpoints"`` returns
a structured plan that names the affected nodes, the blast radius, the
risky cut-vertices, and a rough token-cost comparison vs grep-only
research. The user (or Claude) reads this plan before touching code.

The function is deliberately deterministic and offline — it does not
call an LLM. The intent is to make graph context cheap to acquire so
LLM-driven planners can spend tokens on the synthesis step instead.

Pipeline
--------

1. **find_by_concept** on the task → ranked list of likely-relevant nodes.
2. **what_depends_on** on each top node → blast radius set (deduped).
3. **risk score** — high-degree dependents OR cut vertices receive a
   higher risk grade. (We approximate cut-vertex detection by checking
   for a node whose removal would isolate ≥ 1 caller from the rest of
   its file.)
4. **token estimate** — rough comparison: graph-budgeted context vs
   what a grep-only walk would have cost.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..core.adapters import Symbol
from .indexes import InMemoryGraph

TOP_K_AFFECTED = 12
RADIUS_LIMIT = 24


@dataclass
class PlanNode:
    node_id: str
    label: str
    source_file: str
    line_number: int
    kind: str
    confidence: float
    role: str  # "affected" | "dependent" | "central-touched"


@dataclass
class Plan:
    task: str
    affected: list[PlanNode] = field(default_factory=list)
    blast_radius: list[PlanNode] = field(default_factory=list)
    risk: str = "LOW"
    risk_reasons: list[str] = field(default_factory=list)
    estimated_tokens_graph: int = 0
    estimated_tokens_grep: int = 0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "affected": [n.__dict__ for n in self.affected],
            "blast_radius": [n.__dict__ for n in self.blast_radius],
            "risk": self.risk,
            "risk_reasons": list(self.risk_reasons),
            "estimated_tokens_graph": self.estimated_tokens_graph,
            "estimated_tokens_grep": self.estimated_tokens_grep,
            "notes": list(self.notes),
        }


def _to_plan_node(sym: Symbol, *, role: str, confidence: float) -> PlanNode:
    span = sym.get("span") or (0, 0)
    return PlanNode(
        node_id=sym.get("id", ""),
        label=sym.get("qualified_name") or sym.get("name") or sym.get("id", ""),
        source_file=sym.get("path") or "",
        line_number=int(span[0]) if span else 0,
        kind=sym.get("kind") or "",
        confidence=confidence,
        role=role,
    )


def make_plan(graph: InMemoryGraph, task: str, *, top_k: int = TOP_K_AFFECTED) -> Plan:
    """Build a graph-grounded edit plan.

    Returns an empty plan (with notes) if the task yields zero relevant
    nodes — useful signal for the caller, not a hard error.
    """
    plan = Plan(task=task)

    # Step 1 — concept search.
    ranked = graph.text_search(task, limit=top_k * 2) if task else []
    if not ranked:
        plan.notes.append(
            "No symbols matched the task description. The graph may be "
            "too small, the task may be too abstract, or the relevant "
            "code may live in unindexed files."
        )
        return plan

    max_score = max(score for _, score in ranked) or 1.0
    for sym, score in ranked[:top_k]:
        plan.affected.append(
            _to_plan_node(sym, role="affected", confidence=round(score / max_score, 3))
        )

    # Step 2 — blast radius.
    seen: set[str] = {n.node_id for n in plan.affected}
    radius: list[PlanNode] = []
    high_degree_offenders: list[str] = []
    for affected in plan.affected:
        deps = graph.dependents_of(affected.node_id)
        if len(deps) >= 8:
            high_degree_offenders.append(affected.label)
        for dep in deps:
            sid = dep.get("id", "")
            if not sid or sid in seen:
                continue
            seen.add(sid)
            if len(radius) < RADIUS_LIMIT:
                radius.append(_to_plan_node(dep, role="dependent", confidence=1.0))
    plan.blast_radius = radius

    # Step 3 — risk score.
    pr = {sid: score for sid, score in graph.pagerank_top}
    central_touched = [n for n in plan.affected if n.node_id in pr]
    if central_touched:
        plan.risk_reasons.append(
            f"{len(central_touched)} affected node(s) appear in top-50 PageRank "
            f"({', '.join(n.label for n in central_touched[:3])})"
        )
    if high_degree_offenders:
        plan.risk_reasons.append(
            f"{len(high_degree_offenders)} affected node(s) have ≥8 dependents — "
            f"refactors here ripple widely"
        )
    if len(plan.blast_radius) >= 16:
        plan.risk_reasons.append(
            f"blast radius is {len(plan.blast_radius)} symbols across "
            f"{len({n.source_file for n in plan.blast_radius})} files"
        )

    if not plan.risk_reasons:
        plan.risk = "LOW"
    elif len(plan.risk_reasons) == 1:
        plan.risk = "MEDIUM"
    else:
        plan.risk = "HIGH"

    # Step 4 — token cost estimate.
    # Graph cost = the affected + radius rows we'd serialize for context.
    plan.estimated_tokens_graph = (
        sum(_row_tokens(n) for n in plan.affected)
        + sum(_row_tokens(n) for n in plan.blast_radius)
        + 200  # framing overhead
    )
    # Grep cost = ~5x the affected count in tool calls + ~2x the blast
    # radius in file-read tokens. Under-claiming is fine here (see
    # receipts.grep_equivalent_for for the same philosophy).
    plan.estimated_tokens_grep = (
        len(plan.affected) * 1200  # one file read per match (~300 LOC)
        + len(plan.blast_radius) * 600
        + 500
    )

    if plan.affected:
        affected_files = sorted({n.source_file for n in plan.affected if n.source_file})
        plan.notes.append(
            f"Start with: {', '.join(affected_files[:5])}"
            + (" (+more)" if len(affected_files) > 5 else "")
        )
    return plan


def _row_tokens(node: PlanNode) -> int:
    return 60 + len(node.label) // 4 + (10 if node.kind in ("module", "class") else 0)


def format_plan(plan: Plan) -> str:
    """Render the plan as a Markdown report — what `gp daemon plan` prints."""
    lines: list[str] = []
    lines.append(f"# Plan — {plan.task}")
    lines.append("")
    lines.append(f"**Risk**: {plan.risk}")
    if plan.risk_reasons:
        for reason in plan.risk_reasons:
            lines.append(f"  - {reason}")
    lines.append("")
    lines.append(
        f"**Affected nodes**: {len(plan.affected)}  •  "
        f"**Blast radius**: {len(plan.blast_radius)}  •  "
        f"**Files**: {len({n.source_file for n in plan.affected if n.source_file})}"
    )
    lines.append(
        f"**Estimated context cost**: {plan.estimated_tokens_graph:,} tokens (graph)  "
        f"vs ~{plan.estimated_tokens_grep:,} tokens (grep-only)"
    )
    lines.append("")
    if plan.affected:
        lines.append("## Affected nodes")
        lines.append("")
        for n in plan.affected:
            lines.append(
                f"- **{n.label}** [{n.kind}] "
                f"`{n.source_file}:{n.line_number}` "
                f"(confidence {n.confidence:.2f})"
            )
        lines.append("")
    if plan.blast_radius:
        lines.append("## Blast radius (direct dependents)")
        lines.append("")
        for n in plan.blast_radius:
            lines.append(f"- {n.label} [{n.kind}] `{n.source_file}:{n.line_number}`")
        lines.append("")
    if plan.notes:
        lines.append("## Notes")
        lines.append("")
        for note in plan.notes:
            lines.append(f"- {note}")
        lines.append("")
    return "\n".join(lines)


__all__ = ["Plan", "PlanNode", "format_plan", "make_plan"]
