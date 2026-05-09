"""Layer 25 — built-in tutorial corpus.

``gp daemon tutorial`` runs a 5-minute walkthrough against a built-in
toy codebase, demonstrating 10 questions a graph can answer that grep
can't. The corpus is shipped as text strings so we don't need to copy
files at install time.

Recipes ship under ``recipes/`` as named GPL queries with explanations.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TutorialStep:
    title: str
    question: str
    command: str
    why: str


TUTORIAL_STEPS: list[TutorialStep] = [
    TutorialStep(
        title="What's in this file?",
        question="Where would you start in an unfamiliar file?",
        command="gp daemon query whats_in -a path=src/auth.py",
        why="Returns symbols ranked by structural weight. Grep just lists every line.",
    ),
    TutorialStep(
        title="Who calls this function?",
        question="If I rename `login`, what breaks?",
        command="gp daemon query who_calls -a node=AuthService.login",
        why="Walks the call graph. Grep can't tell `AuthService.login` from `Foo.login`.",
    ),
    TutorialStep(
        title="What's most central?",
        question="Where is the load-bearing code in this codebase?",
        command="gp daemon query whats_central -a top_k=10",
        why="PageRank over the symbol graph. No grep equivalent.",
    ),
    TutorialStep(
        title="What's untested?",
        question="Where should I add tests next?",
        command="gp daemon coverage untested",
        why="Composes the test-coverage overlay with file:line attribution.",
    ),
    TutorialStep(
        title="Why does this exist?",
        question="What issue / PR / ADR justified this code?",
        command="gp daemon query why_does_this_exist -a node=AuthService.login",
        why="Routes through ingested external context.",
    ),
    TutorialStep(
        title="Plan an edit",
        question="If I add rate limiting to all endpoints, what's the blast radius?",
        command='gp daemon plan "add rate limiting to all REST endpoints"',
        why="Concept search → blast radius → risk score → token cost.",
    ),
    TutorialStep(
        title="Cross-stack: HTTP",
        question="If I rename /api/users, what frontend breaks?",
        command="gp daemon cross-stack --node /api/users",
        why="Synthesises edges across language boundaries.",
    ),
    TutorialStep(
        title="Architectural rules",
        question="Did this PR introduce a forbidden import?",
        command="gp daemon rules check --fail-on-error",
        why="CI-ready check; surfaces rule_id with file:line.",
    ),
    TutorialStep(
        title="Find by concept",
        question="Where is the login flow handled?",
        command='gp daemon query find_by_concept -a query="authentication login"',
        why="BM25 + community + optional embedding rerank. Grep can't do concept search.",
    ),
    TutorialStep(
        title="Session digest",
        question="What should the PR description say about this change?",
        command="gp daemon session-digest --since main",
        why="Composes review + coverage + rules into a Markdown block.",
    ),
]


def render_tutorial() -> str:
    out: list[str] = []
    out.append("# graphify-plus tutorial")
    out.append("")
    out.append(
        "Ten questions the graph answers that grep can't. Each takes one "
        "tool call instead of an exploratory grep + read sequence."
    )
    out.append("")
    for i, step in enumerate(TUTORIAL_STEPS, 1):
        out.append(f"## {i}. {step.title}")
        out.append("")
        out.append(f"**Question:** {step.question}")
        out.append("")
        out.append(f"```\n{step.command}\n```")
        out.append("")
        out.append(f"_Why this works:_ {step.why}")
        out.append("")
    out.append("## Honest limitations")
    out.append("")
    out.append("graphify-plus is bad at:")
    out.append("- exact-string searches (grep wins)")
    out.append("- jump-to-definition / hover types (your LSP wins)")
    out.append("- live runtime state (it's a static graph)")
    out.append("- editing code itself (it informs the edit; Claude / you do the edit)")
    return "\n".join(out)


# ---- recipes ---------------------------------------------------------


@dataclass
class Recipe:
    name: str
    description: str
    gpl: str


RECIPES: list[Recipe] = [
    Recipe(
        name="untested-public-apis",
        description="Exported functions or classes with <50% test coverage.",
        gpl='FIND nodes WHERE exported == 1 AND test_coverage < 0.5',
    ),
    Recipe(
        name="dependency-cycles",
        description="Symbols involved in a 2-3 hop self-cycle via 'imports'.",
        gpl="MATCH (n)-[:imports*1..3]->(n) RETURN label, source_file, line_number LIMIT 50",
    ),
    Recipe(
        name="oversized-functions",
        description="Functions wider than ~100 lines (heuristic via end_line - line_number).",
        gpl="FIND nodes WHERE kind == 'function' AND end_line > 100",
    ),
    Recipe(
        name="god-nodes",
        description="High-degree nodes — best candidates for refactor splits.",
        gpl="MATCH (n) RETURN label, source_file, line_number ORDER BY degree DESC LIMIT 25",
    ),
    Recipe(
        name="top-untested",
        description="Lowest-coverage symbols, ranked.",
        gpl="MATCH (n) RETURN label, source_file, line_number, test_coverage ORDER BY test_coverage ASC LIMIT 25",
    ),
]


def render_recipes() -> str:
    out: list[str] = ["# graphify-plus recipes", ""]
    for r in RECIPES:
        out.append(f"## `{r.name}`")
        out.append(f"_{r.description}_")
        out.append("")
        out.append(f"```\n{r.gpl}\n```")
        out.append("")
        out.append(f"Run via: `gp daemon gpl --run {r.name}`")
        out.append("")
    return "\n".join(out)


def install_recipes(repo_path) -> list[str]:
    """Write each recipe under ``.graphify_plus/queries/`` so they're
    immediately runnable via ``gp daemon gpl --run NAME``.
    """
    from pathlib import Path

    base = Path(repo_path) / ".graphify_plus" / "queries"
    base.mkdir(parents=True, exist_ok=True)
    written = []
    for r in RECIPES:
        p = base / f"{r.name}.gpl"
        p.write_text(r.gpl, encoding="utf-8")
        written.append(str(p))
    return written


__all__ = [
    "RECIPES",
    "Recipe",
    "TUTORIAL_STEPS",
    "TutorialStep",
    "install_recipes",
    "render_recipes",
    "render_tutorial",
]
