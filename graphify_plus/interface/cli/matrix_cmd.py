"""``gp matrix`` and ``gp visual`` CLIs."""

from __future__ import annotations

from pathlib import Path

import click

from ...core.symbol_graph import build as build_graph
from ...interface.visual import render_cluster
from ...query.semantic import communities
from ...query.tests_gen import for_subgraph, write_scaffolds
from ...runtime.store import Store, cache_path
from .safety_cmds import _load_ruleset


def _open(repo: Path) -> Store:
    if not cache_path(repo).exists():
        raise click.ClickException(
            f"No cache at {cache_path(repo)}. Run 'graphify-plus init --repo {repo}' first."
        )
    return Store(cache_path(repo))


@click.command("matrix")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
@click.option("--generate-tests", is_flag=True, required=True)
@click.option(
    "--target",
    "targets",
    multiple=True,
    help="Repo-relative source paths to scope the test scaffolds (default: every edge).",
)
@click.option(
    "--write/--print",
    "write_mode",
    default=False,
    help="--write places scaffolds under <target>/.graphify_plus/generated_tests/.",
)
def matrix_cmd(
    repo: Path, generate_tests: bool, targets: tuple[str, ...], write_mode: bool
) -> None:
    """Edge-centric test scaffolds. Currently only --generate-tests is wired."""
    if not generate_tests:
        raise click.UsageError("Use --generate-tests (the only supported mode in v3.x).")
    repo = repo.resolve()
    store = _open(repo)
    try:
        G = build_graph(store.all_symbols(), store.all_edges())
        scaffolds = for_subgraph(G, target_paths=targets or None)
    finally:
        store.close()

    if write_mode:
        out = repo / ".graphify_plus" / "generated_tests"
        paths = write_scaffolds(scaffolds, out)
        click.echo(f"matrix: wrote {len(paths)} scaffold files under {out}")
    else:
        for sc in scaffolds:
            click.echo(f"# {sc.edge_kind}: {sc.src_qname} → {sc.dst_qname} ({sc.language})")
            click.echo(sc.body)
            click.echo("")


@click.command("visual")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
@click.option("--cluster", "cluster_id", type=int, default=0)
def visual_cmd(repo: Path, cluster_id: int) -> None:
    """Render community ``--cluster`` to SVG/PNG/DOT under target's
    .graphify_plus/visuals/ directory."""
    repo = repo.resolve()
    store = _open(repo)
    try:
        G = build_graph(store.all_symbols(), store.all_edges())
        comm = communities(store, G)
    finally:
        store.close()
    nodes = [sid for sid, c in comm.items() if c == cluster_id]
    if not nodes:
        raise click.ClickException(
            f"No community with id {cluster_id}. Try 'gp find --intent ...' to see ids."
        )
    rs = _load_ruleset(repo)
    result = render_cluster(
        G,
        nodes,
        repo / ".graphify_plus" / "visuals",
        cluster_id=cluster_id,
        layers=rs.layers,
    )
    click.echo(f"visual: dot={result.dot_path}")
    if result.svg_path:
        click.echo(f"        svg={result.svg_path}")
    if result.png_path:
        click.echo(f"        png={result.png_path}")
    if not result.svg_path:
        click.echo(
            "        (svg/png skipped — install 'graphviz' Python bindings AND the\n"
            "         'dot' system binary to enable rasterised output)",
            err=True,
        )


__all__ = ["matrix_cmd", "visual_cmd"]
