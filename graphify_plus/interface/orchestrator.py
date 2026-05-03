"""Multi-agent orchestrator.

Partitions the meta-graph by layer assignment (rules.yaml from Phase 6)
and emits one ``STAGING_PLAN_<AGENT>.md`` per layer in the **target
repo**. Each agent gets a context tailored to its sub-graph.

Agent dependency order is derived from the layer graph: edges of kind
``crosses_to`` and ``imports`` between layers determine which agent
must finish before another can start.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import networkx as nx

from ..core.symbol_graph import build as build_graph
from ..query.manifest import Checkpoint, ImpactedSymbol, Manifest, render
from ..query.stitch import stitch_into
from ..runtime.rules import RuleSet, assign_layer
from ..runtime.store import Store, cache_path


@dataclass(frozen=True)
class AgentSlice:
    name: str  # ui, api, db, ...
    symbol_ids: list[str]
    impacted: list[ImpactedSymbol]
    upstream_agents: list[str]  # must finish before this one


@dataclass(frozen=True)
class OrchestratorReport:
    agents: list[AgentSlice] = field(default_factory=list)
    written: list[Path] = field(default_factory=list)


def _layer_for_symbol(attrs: dict, layers: dict[str, list[str]]) -> str | None:
    return assign_layer(attrs.get("path") or "", layers)


def _topological_layer_order(
    G: nx.MultiDiGraph, layers: dict[str, list[str]], names: list[str]
) -> dict[str, list[str]]:
    """Return ``{layer: [upstream_layers]}`` derived from the cross-layer
    edges in ``G``. An edge ``a → b`` makes ``b`` depend on ``a``.
    """
    deps: dict[str, set[str]] = {n: set() for n in names}
    for u, v, data in G.edges(data=True):
        kind = (data or {}).get("kind") or ""
        if kind not in {"crosses_to", "imports", "calls"}:
            continue
        la = _layer_for_symbol(dict(G.nodes[u]), layers) if G.has_node(u) else None
        lb = _layer_for_symbol(dict(G.nodes[v]), layers) if G.has_node(v) else None
        if not la or not lb or la == lb:
            continue
        if la in deps and lb in deps:
            deps[lb].add(la)
    return {k: sorted(v) for k, v in deps.items()}


def coordinate(
    repo: Path,
    ruleset: RuleSet,
    *,
    task: str = "Cross-layer change",
    extra_repos: list[Path] | None = None,
) -> OrchestratorReport:
    """Slice the symbol graph by layer and write per-agent plans."""
    repo = repo.resolve()
    extras = extra_repos or []
    store = Store(cache_path(repo))
    try:
        symbols = store.all_symbols()
        edges = store.all_edges()
        G = build_graph(symbols, edges)
        # Stitch contracts where present (cheap if no specs).
        stitch_into(G, [repo, *extras])
        layers = ruleset.layers or {}
        agent_names = sorted(layers.keys())
        if not agent_names:
            return OrchestratorReport()

        # Bucket symbols by layer (skip unassigned + externals).
        by_layer: dict[str, list[str]] = {n: [] for n in agent_names}
        for sid, attrs in G.nodes(data=True):
            a = dict(attrs)
            if a.get("kind") in {"external", "endpoint"}:
                continue
            lyr = _layer_for_symbol(a, layers)
            if lyr in by_layer:
                by_layer[lyr].append(sid)

        deps = _topological_layer_order(G, layers, agent_names)

        out_dir = repo  # plans live in the TARGET repo's root
        slices: list[AgentSlice] = []
        written: list[Path] = []

        for name in agent_names:
            ids = sorted(by_layer.get(name, []))
            impacted: list[ImpactedSymbol] = []
            for sid in ids:
                a = dict(G.nodes[sid])
                impacted.append(
                    ImpactedSymbol(
                        id=sid,
                        qualified_name=a.get("qualified_name") or sid,
                        kind=a.get("kind") or "symbol",
                        path=a.get("path") or "",
                        impact=0.0,
                    )
                )
            checkpoints = [
                Checkpoint(
                    kind="symbol_exists",
                    label=f"symbol still exists: {s.qualified_name}",
                    args=(s.id,),
                )
                for s in impacted[:50]  # cap noise
            ]
            upstream = deps.get(name, [])
            notes = []
            if upstream:
                notes.append(
                    f"This agent runs **after**: {', '.join(upstream)}. "
                    "Wait for those plans to be marked complete before editing here."
                )
            else:
                notes.append("This agent has no upstream — it can start immediately.")

            manifest = Manifest(
                task=f"{task} — agent={name}",
                generated_at="",
                dependency_order=impacted,
                validation_checkpoints=checkpoints,
                notes=notes,
            )
            text = render(manifest)
            out_path = out_dir / f"STAGING_PLAN_{_slug(name)}.md"
            out_path.write_text(text)

            slices.append(
                AgentSlice(name=name, symbol_ids=ids, impacted=impacted, upstream_agents=upstream)
            )
            written.append(out_path)

        return OrchestratorReport(agents=slices, written=written)
    finally:
        store.close()


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_").upper()


__all__ = ["AgentSlice", "OrchestratorReport", "coordinate"]
