"""``gp claude-md`` — write or refresh a Graphify-Plus guidance section
inside the **target repo's** CLAUDE.md.

Behaviour:

  * If no CLAUDE.md exists in the target repo, create one containing
    only the auto-generated section.
  * If one exists, replace only the section delimited by
    ``<!-- graphify-plus:start -->`` / ``<!-- graphify-plus:end -->``,
    preserving every human-authored line outside those markers.
  * Idempotent — re-running on an unchanged graph produces a section
    whose content is byte-identical.

Section content is derived from the live graph: detected stack
languages, the four required workflow steps, the unified terminology
constraint reminder (currently empty per project owner direction —
§4.5 was dropped), and the MCP tool surface.
"""

from __future__ import annotations

import re
from pathlib import Path

import click

from ...core.symbol_graph import build as build_graph
from ...interface.mcp_server import TOOLS
from ...runtime.store import Store, cache_path

START_MARKER = "<!-- graphify-plus:start -->"
END_MARKER = "<!-- graphify-plus:end -->"
TEMPLATE_VERSION = 1


def _detect_stack(symbols) -> list[str]:
    langs: dict[str, int] = {}
    for s in symbols:
        lang = (s.get("language") or "").lower()
        if lang in {"", "external", "openapi", "dep", "repo"}:
            continue
        langs[lang] = langs.get(lang, 0) + 1
    ranked = sorted(langs.items(), key=lambda kv: (-kv[1], kv[0]))
    return [f"{name} ({count} symbols)" for name, count in ranked]


def render_section(repo: Path) -> str:
    if not cache_path(repo).exists():
        raise click.ClickException(
            f"No cache at {cache_path(repo)}. Run 'graphify-plus init --repo {repo}' first."
        )
    store = Store(cache_path(repo))
    try:
        symbols = store.all_symbols()
        edges = store.all_edges()
        G = build_graph(symbols, edges)
        layer_node_count = sum(1 for _, a in G.nodes(data=True) if a.get("layer"))
        del layer_node_count  # currently unused — Phase 11 will surface it
    finally:
        store.close()

    stack = _detect_stack(symbols)
    lines: list[str] = [
        START_MARKER,
        f"<!-- graphify-plus-template-version: {TEMPLATE_VERSION} -->",
        "## Graphify-Plus",
        "",
        "This repository is indexed by [Graphify-Plus](https://github.com/ahmadzubair9655/graphify-plus)."
        " The cache lives at `.graphify_plus/cache.db` (gitignored).",
        "",
        "### Detected stack",
        "",
    ]
    if stack:
        for s in stack:
            lines.append(f"- {s}")
    else:
        lines.append("- _(empty — run `graphify-plus init --repo .` to populate)_")
    lines.append("")

    lines.extend(
        [
            "### Workflow",
            "",
            '1. **Plan** — `graphify-plus plan --task "<description>"` writes '
            "`STAGING_PLAN.md` with the impacted symbols in topological order.",
            "2. **Context** — `graphify-plus context --target <symbol>` returns a "
            "token-budgeted slice of the symbol graph for the AI to read.",
            "3. **Guardrails** — pipe a proposed-edit JSON into "
            "`graphify-plus guardrails` before applying changes; non-zero exit means "
            "a rule (`.graphify_plus/rules.yaml`) blocks the edit.",
            "4. **Audit** — `graphify-plus audit <graph.json>` runs the structural "
            "audit; `graphify-plus drift check` re-evaluates the live rule set.",
            "",
            "### MCP server",
            "",
            "Run `python -m graphify_plus.interface.mcp_server` to expose the "
            "following tools over stdio:",
            "",
        ]
    )
    for name in sorted(TOOLS.keys()):
        lines.append(f"- `{name}`")
    lines.append("")
    lines.append(
        "Generated artefacts (`STAGING_PLAN.md`, drift_report.md, visuals/, etc.) "
        "are written into THIS repository's `.graphify_plus/` directory at runtime, "
        "never into the graphify-plus tool's own source tree."
    )
    lines.append("")
    lines.append(END_MARKER)
    return "\n".join(lines) + "\n"


_SECTION_RE = re.compile(
    re.escape(START_MARKER) + r".*?" + re.escape(END_MARKER) + r"\n?",
    re.DOTALL,
)


def amend(existing: str, section: str) -> str:
    if not existing.strip():
        return section
    if START_MARKER in existing and END_MARKER in existing:
        return _SECTION_RE.sub(section, existing, count=1)
    if existing.endswith("\n"):
        return existing + "\n" + section
    return existing + "\n\n" + section


@click.command("claude-md")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
@click.option(
    "--write/--print",
    "do_write",
    default=True,
    help="--write amends <repo>/CLAUDE.md; --print emits the section to stdout.",
)
def claude_md_cmd(repo: Path, do_write: bool) -> None:
    """Refresh the Graphify-Plus section in the target repo's CLAUDE.md."""
    repo = repo.resolve()
    section = render_section(repo)
    if not do_write:
        click.echo(section)
        return
    target = repo / "CLAUDE.md"
    existing = target.read_text(errors="replace") if target.exists() else ""
    target.write_text(amend(existing, section))
    click.echo(f"wrote {target}")


__all__ = ["amend", "claude_md_cmd", "render_section"]
