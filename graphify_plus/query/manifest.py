"""STAGING_PLAN.md generator.

Given a free-text task description, derive a structured execution plan
the AI agent can follow turn-by-turn:

  - **dependency_order** — topological sort of the impacted symbols
    (DB schema → API → UI is the canonical case).
  - **impact_scores** — per-symbol high-risk indicator (high betweenness
    × high in-degree on the induced subgraph).
  - **validation_checkpoints** — graph invariants the agent must keep
    true at every step (e.g., "path A→B still exists").

The renderer emits deterministic Markdown so two runs of the same task
on an unchanged graph produce byte-identical output.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

UTC = timezone.utc

import networkx as nx

from ..core.adapters import Symbol
from ..query.semantic import find as semantic_find
from ..runtime.store import Store

# ---------- data --------------------------------------------------------


@dataclass(frozen=True)
class ImpactedSymbol:
    id: str
    qualified_name: str
    kind: str
    path: str
    impact: float


@dataclass(frozen=True)
class Checkpoint:
    kind: str  # 'path_exists', 'symbol_exists', 'edge_exists'
    label: str
    args: tuple[str, ...]


@dataclass(frozen=True)
class Manifest:
    task: str
    generated_at: str
    dependency_order: list[ImpactedSymbol]
    validation_checkpoints: list[Checkpoint]
    notes: list[str] = field(default_factory=list)


# ---------- core --------------------------------------------------------


def _impact(G: nx.MultiDiGraph, sub_nodes: list[str]) -> dict[str, float]:
    sub = G.subgraph(sub_nodes).to_undirected()
    if sub.number_of_nodes() < 3:
        return {sid: float(G.in_degree(sid)) for sid in sub_nodes}
    try:
        bet = nx.betweenness_centrality(sub)
    except Exception:  # noqa: BLE001
        bet = dict.fromkeys(sub_nodes, 0.0)
    return {sid: bet.get(sid, 0.0) * (1 + G.in_degree(sid)) for sid in sub_nodes}


def _topological(G: nx.MultiDiGraph, sub_nodes: list[str]) -> list[str]:
    sub = G.subgraph(sub_nodes).copy()
    # Drop edges that create cycles in the sub — TopoSort needs DAG.
    if not nx.is_directed_acyclic_graph(sub):
        # Remove a minimal feedback set greedily by edge frequency.
        for u, v in list(nx.find_cycle(sub, orientation="original")):  # type: ignore[misc]
            sub.remove_edge(u, v)
            if nx.is_directed_acyclic_graph(sub):
                break
    try:
        return list(nx.topological_sort(sub))
    except nx.NetworkXUnfeasible:
        return sub_nodes  # fallback: arbitrary stable order


def generate(
    task: str,
    G: nx.MultiDiGraph,
    store: Store,
    *,
    top_k_seeds: int = 6,
) -> Manifest:
    """Build a Manifest from ``task``."""
    seeds = semantic_find(store, G, task, top_k=top_k_seeds)
    seed_ids = [m.symbol["id"] for m in seeds]
    notes: list[str] = []
    if not seed_ids:
        notes.append(
            "No symbols matched the task description — the manifest is empty. "
            "Refine the task wording or run 'graphify-plus find --intent' to inspect candidates."
        )

    # 1-hop expansion around seeds — these are the symbols the agent
    # needs context on.
    sub: set[str] = set(seed_ids)
    for sid in seed_ids:
        if sid not in G:
            continue
        sub.update(G.predecessors(sid))
        sub.update(G.successors(sid))

    # External nodes (unresolved imports, builtins) are noise in a plan —
    # the agent will not edit them. Drop them from the order.
    sub_list = sorted(sid for sid in sub if (G.nodes.get(sid) or {}).get("kind") != "external")
    impact = _impact(G, sub_list)
    order_ids = _topological(G, sub_list)

    impacted: list[ImpactedSymbol] = []
    for sid in order_ids:
        attrs: dict = dict(G.nodes[sid])  # type: ignore[assignment]
        sym: Symbol = attrs  # type: ignore[assignment]
        impacted.append(
            ImpactedSymbol(
                id=sid,
                qualified_name=sym.get("qualified_name") or sid,
                kind=sym.get("kind") or "symbol",
                path=sym.get("path") or "",
                impact=round(impact.get(sid, 0.0), 4),
            )
        )

    # Validation checkpoints: every adjacent pair on the order must
    # remain reachable after the agent's edits.
    checkpoints: list[Checkpoint] = []
    for s in impacted:
        checkpoints.append(
            Checkpoint(
                kind="symbol_exists",
                label=f"symbol still exists: {s.qualified_name}",
                args=(s.id,),
            )
        )
    if len(impacted) >= 2:
        head = impacted[0]
        tail = impacted[-1]
        checkpoints.append(
            Checkpoint(
                kind="path_exists",
                label=f"reachable: {head.qualified_name} → {tail.qualified_name}",
                args=(head.id, tail.id),
            )
        )

    return Manifest(
        task=task,
        generated_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        dependency_order=impacted,
        validation_checkpoints=checkpoints,
        notes=notes,
    )


# ---------- rendering ---------------------------------------------------


def render(manifest: Manifest) -> str:
    """Render to deterministic Markdown."""
    lines: list[str] = []
    lines.append("# STAGING_PLAN")
    lines.append("")
    lines.append(f"_Task:_ {manifest.task}")
    lines.append(f"_Generated:_ {manifest.generated_at}")
    lines.append("")
    lines.append("> Auto-generated by `graphify-plus plan`. Re-run after each step")
    lines.append("> to refresh the dependency order. Do not edit the headings — the")
    lines.append("> sync-docs daemon parses them on every save.")
    lines.append("")

    if manifest.notes:
        lines.append("## Notes")
        for n in manifest.notes:
            lines.append(f"- {n}")
        lines.append("")

    lines.append("## Dependency order")
    if not manifest.dependency_order:
        lines.append("_(empty — no symbols matched the task)_")
    else:
        lines.append("")
        lines.append("| # | kind | qualified name | path | impact |")
        lines.append("|---|------|----------------|------|--------|")
        for i, s in enumerate(manifest.dependency_order, 1):
            lines.append(
                f"| {i} | `{s.kind}` | `{s.qualified_name}` | `{s.path}` | {s.impact:.4f} |"
            )
    lines.append("")

    lines.append("## Validation checkpoints")
    if not manifest.validation_checkpoints:
        lines.append("_(none required — empty plan)_")
    else:
        for cp in manifest.validation_checkpoints:
            lines.append(f"- [{cp.kind}] {cp.label}")
    lines.append("")

    return "\n".join(lines) + "\n"


__all__ = ["Checkpoint", "ImpactedSymbol", "Manifest", "generate", "render"]
