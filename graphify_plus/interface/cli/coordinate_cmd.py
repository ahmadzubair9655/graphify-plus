"""``gp stitch`` and ``gp coordinate`` CLIs."""

from __future__ import annotations

import json
from pathlib import Path

import click

from ...core.symbol_graph import build as build_graph
from ...interface.orchestrator import coordinate
from ...query.stitch import stitch_into
from ...runtime.store import Store, cache_path
from .safety_cmds import _load_ruleset


@click.command("stitch")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
@click.option(
    "--with-repo",
    "extra_repos",
    multiple=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Additional repo roots to scan for OpenAPI specs.",
)
@click.option("--json", "as_json", is_flag=True)
def stitch_cmd(repo: Path, extra_repos: tuple[Path, ...], as_json: bool) -> None:
    """Discover OpenAPI specs and add cross-layer edges to the cache."""
    repo = repo.resolve()
    if not cache_path(repo).exists():
        raise click.ClickException(
            f"No cache at {cache_path(repo)}. Run 'graphify-plus init --repo {repo}' first."
        )
    store = Store(cache_path(repo))
    try:
        G = build_graph(store.all_symbols(), store.all_edges())
        result = stitch_into(G, [repo, *(p.resolve() for p in extra_repos)])
        # Persist the new crosses_to edges back to the cache so downstream
        # commands (gp coordinate, gp context) see them without re-stitching.
        new_edges = []
        for e in result.edges:
            new_edges.append(
                {
                    "src": e.src,
                    "dst": e.dst,
                    "kind": "crosses_to",
                    "resolved": True,
                    "confidence": e.confidence,
                    "op_id": e.op_id,
                    "span": None,
                }
            )
        if new_edges:
            existing = store.all_edges()
            # Avoid duplicates: key on (src,dst,kind,op_id).
            seen = {(e.get("src"), e.get("dst"), e.get("kind"), e.get("op_id")) for e in existing}
            merged = list(existing) + [
                e for e in new_edges if (e["src"], e["dst"], e["kind"], e["op_id"]) not in seen
            ]
            store.replace_all(store.all_symbols(), merged)
    finally:
        store.close()

    if as_json:
        click.echo(
            json.dumps(
                {
                    "endpoints": [
                        {
                            "op_id": e.op_id,
                            "method": e.method,
                            "path": e.path,
                            "spec_path": e.spec_path,
                        }
                        for e in result.endpoints
                    ],
                    "edges": [
                        {
                            "src": e.src,
                            "dst": e.dst,
                            "op_id": e.op_id,
                            "confidence": e.confidence,
                            "reason": e.reason,
                        }
                        for e in result.edges
                    ],
                },
                indent=2,
            )
        )
        return
    click.echo(f"stitch: {len(result.endpoints)} endpoints, {len(result.edges)} cross-edges added")


@click.command("coordinate")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
@click.option(
    "--with-repo",
    "extra_repos",
    multiple=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
@click.option("--task", default="Cross-layer change")
@click.option(
    "--agents",
    default=None,
    help="Comma-separated layer names to include (default: every layer in rules.yaml).",
)
def coordinate_cmd(
    repo: Path,
    extra_repos: tuple[Path, ...],
    task: str,
    agents: str | None,
) -> None:
    """Slice the meta-graph by layer; write one STAGING_PLAN_<AGENT>.md per agent."""
    repo = repo.resolve()
    if not cache_path(repo).exists():
        raise click.ClickException(
            f"No cache at {cache_path(repo)}. Run 'graphify-plus init --repo {repo}' first."
        )
    rs = _load_ruleset(repo)
    if agents:
        wanted = {a.strip() for a in agents.split(",") if a.strip()}
        rs.layers = {k: v for k, v in rs.layers.items() if k in wanted}
    if not rs.layers:
        raise click.ClickException(
            "No layers configured. Add a layers: section to .graphify_plus/rules.yaml."
        )
    report = coordinate(repo, rs, task=task, extra_repos=[p.resolve() for p in extra_repos])
    click.echo(f"coordinate: wrote {len(report.written)} agent plans")
    for path in report.written:
        click.echo(f"  {path}")


__all__ = ["coordinate_cmd", "stitch_cmd"]
