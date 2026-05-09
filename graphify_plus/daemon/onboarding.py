"""Onboarding mode — Sprint 10.2 of the master plan.

``gp daemon onboard`` generates a literal Markdown walkthrough for a
new contributor — "Welcome. This codebase has 312 nodes and 891 edges.
The 5 most central concepts are: AuthService, RequestRouter, …. Start
here: read auth_service.py:42-87 (the canonical auth flow). Then follow
this causal chain: …".

Generated from:

* ``whats_central`` — the top-N PageRank nodes (the "must-read" list)
* ``by_path`` — the file/module map grouped by directory
* ``coverage`` — to mark "well-tested" sections as good starting points
* ``communities`` — to group conceptually related nodes

This is a derivative feature — every primitive it consumes is already
in the daemon. The point is *packaging*: turning a graph query into a
guided tour Claude (or a junior dev) can follow without prior context.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from .indexes import InMemoryGraph


@dataclass
class WalkStop:
    label: str
    source_file: str
    line_number: int
    kind: str
    why: str  # one sentence: "this is the canonical X"
    coverage_pct: float | None = None


@dataclass
class OnboardingPlan:
    repo: str
    persona: str
    n_symbols: int
    n_files: int
    central: list[WalkStop] = field(default_factory=list)
    by_module: dict[str, list[WalkStop]] = field(default_factory=dict)
    welltested_examples: list[WalkStop] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "persona": self.persona,
            "n_symbols": self.n_symbols,
            "n_files": self.n_files,
            "central": [s.__dict__ for s in self.central],
            "by_module": {k: [s.__dict__ for s in v] for k, v in self.by_module.items()},
            "welltested_examples": [s.__dict__ for s in self.welltested_examples],
        }


def make_plan(graph: InMemoryGraph, persona: str = "engineer") -> OnboardingPlan:
    plan = OnboardingPlan(
        repo=str(graph.repo_root.name),
        persona=persona,
        n_symbols=len(graph.by_id),
        n_files=len(graph.by_path),
    )

    # Top central nodes — the must-read list. Use top_k sized for the
    # codebase: 3 for tiny, up to 10 for a real repo.
    top_n = min(10, max(3, plan.n_symbols // 100))
    for sid, score in graph.pagerank_top[:top_n]:
        sym = graph.by_id.get(sid)
        if not sym:
            continue
        plan.central.append(_to_stop(graph, sym, why=_central_why(sym, score)))

    # Per-module map. We group symbols by their top-level directory and
    # pick the highest-PageRank exemplar for each. Skip noise (modules
    # with one tiny symbol, dotfile dirs).
    pr = {sid: score for sid, score in graph.pagerank_top}
    by_dir: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for sid, sym in graph.by_id.items():
        path = sym.get("path") or ""
        if not path:
            continue
        top_dir = path.split("/", 1)[0]
        if top_dir.startswith("."):
            continue
        by_dir[top_dir].append((sid, pr.get(sid, 0.0)))

    for top_dir, syms in sorted(by_dir.items()):
        ranked = sorted(syms, key=lambda kv: -kv[1])
        stops: list[WalkStop] = []
        seen_files: set[str] = set()
        for sid, _score in ranked:
            sym = graph.by_id.get(sid)
            if not sym:
                continue
            file = sym.get("path") or ""
            if file in seen_files:
                continue
            seen_files.add(file)
            stops.append(_to_stop(graph, sym, why="exemplar of " + top_dir))
            if len(stops) >= 3:
                break
        if stops:
            plan.by_module[top_dir] = stops

    # Well-tested examples — high coverage AND high pagerank. Best places
    # to learn the codebase's style by reading both code and tests.
    if graph.coverage:
        candidates: list[tuple[str, float, float]] = []
        for sid, cov in graph.coverage.items():
            pct = float(cov.get("pct", 0.0))
            if pct < 0.7:
                continue
            score = pr.get(sid, 0.0)
            candidates.append((sid, pct, score))
        candidates.sort(key=lambda r: (-r[2], -r[1]))
        for sid, pct, _score in candidates[:5]:
            sym = graph.by_id.get(sid)
            if sym:
                plan.welltested_examples.append(
                    _to_stop(
                        graph,
                        sym,
                        why=f"well-tested ({pct:.0%}) and structurally central",
                    )
                )

    return plan


def _central_why(sym: dict[str, Any], score: float) -> str:
    kind = sym.get("kind") or "node"
    return f"top-PageRank {kind} (centrality={score:.4f})"


def _to_stop(graph: InMemoryGraph, sym: dict[str, Any], *, why: str) -> WalkStop:
    cov = graph.coverage.get(sym.get("id", ""))
    return WalkStop(
        label=sym.get("qualified_name") or sym.get("name") or sym.get("id", ""),
        source_file=sym.get("path") or "",
        line_number=int((sym.get("span") or (0, 0))[0]),
        kind=sym.get("kind") or "",
        why=why,
        coverage_pct=(round(cov.get("pct", 0.0) * 100, 1) if cov else None),
    )


def format_plan(plan: OnboardingPlan) -> str:
    """Render as a guided-tour Markdown document."""
    lines: list[str] = []
    lines.append(f"# Onboarding — {plan.repo} ({plan.persona})")
    lines.append("")
    lines.append(
        f"This codebase has **{plan.n_symbols:,}** symbols across "
        f"**{plan.n_files:,}** files. Here's where to start."
    )
    lines.append("")

    if plan.central:
        lines.append("## 1. Read these first — the most central concepts")
        lines.append("")
        for stop in plan.central:
            lines.append(
                f"- **{stop.label}** [{stop.kind}] "
                f"`{stop.source_file}:{stop.line_number}` — {stop.why}"
            )
        lines.append("")

    if plan.welltested_examples:
        lines.append("## 2. Learn the style — well-tested examples")
        lines.append("")
        for stop in plan.welltested_examples:
            lines.append(
                f"- {stop.label} [{stop.kind}] `{stop.source_file}:{stop.line_number}` — {stop.why}"
            )
        lines.append("")

    if plan.by_module:
        lines.append("## 3. Tour the codebase — one exemplar per top-level module")
        lines.append("")
        for module, stops in plan.by_module.items():
            lines.append(f"### `{module}/`")
            lines.append("")
            for stop in stops:
                lines.append(
                    f"- {stop.label} [{stop.kind}] `{stop.source_file}:{stop.line_number}`"
                )
            lines.append("")

    lines.append("## Next steps")
    lines.append("")
    lines.append(
        '- `gp daemon plan "<your task>"` — graph-grounded edit plan for '
        "any change you're about to make"
    )
    lines.append(
        '- `gp daemon query find_by_concept --arg query="<your concept>"` — '
        "find code by concept rather than name"
    )
    lines.append(
        "- `gp daemon coverage untested` — find places where adding tests is high-leverage"
    )
    return "\n".join(lines)


__all__ = ["OnboardingPlan", "WalkStop", "format_plan", "make_plan"]
