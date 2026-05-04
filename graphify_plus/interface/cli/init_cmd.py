"""``gp init`` — walk a target repo, parse with tree-sitter, write SQLite cache
and a deterministic ``graph_symbols.jsonl`` for human inspection / CI diffing.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click
import orjson

from ...core import cas
from ...core.ingest import ingest
from ...core.skeletonizer import skeletonize_all
from ...core.symbol_graph import build as build_graph
from ...runtime.store import Store, cache_path


def _assign_communities(symbols: list, edges: list) -> dict[str, int]:
    """Compute Louvain communities and write a `community` attribute onto
    each symbol in-place.

    Three audit probes (edge_deletion_stability, confidence_drift,
    modularity_quality) skip when nodes lack this attribute, so we set it
    eagerly during init. Returns the {symbol_id -> community_index} map for
    callers that want to persist it elsewhere (e.g. the meta table for
    backwards compatibility with the v4.x semantic-search cache).

    Symbols with no community assignment (isolated nodes / phantom symbols)
    receive `community = -1` so audit can distinguish "unassigned" from
    "assigned to community 0".
    """
    import json

    import networkx as nx

    G = build_graph(symbols, edges)
    UG = nx.Graph()
    UG.add_nodes_from(G.nodes(data=True))
    for u, v in G.edges():
        if u != v and not UG.has_edge(u, v):
            UG.add_edge(u, v)

    if not UG.nodes:
        return {}

    parts = nx.community.louvain_communities(UG, seed=1337)
    parts_sorted = [sorted(p) for p in parts]
    parts_sorted.sort(key=lambda p: (-len(p), p[0] if p else ""))
    comm_map: dict[str, int] = {}
    for idx, part in enumerate(parts_sorted):
        for sid in part:
            comm_map[sid] = idx

    for s in symbols:
        s["community"] = comm_map.get(s["id"], -1)
    # Defensive: callers downstream may rely on `_meta` knowing this ran.
    _ = json.dumps  # keep import-time side-effect free
    return comm_map


def _write_jsonl(out: Path, symbols: list, edges: list) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    opt = orjson.OPT_SORT_KEYS | orjson.OPT_APPEND_NEWLINE
    with out.open("wb") as f:
        f.write(
            orjson.dumps(
                {
                    "_meta": {
                        "kind": "header",
                        "version": 1,
                        "symbols": len(symbols),
                        "edges": len(edges),
                    }
                },
                option=opt,
            )
        )
        for s in symbols:
            f.write(orjson.dumps({"kind": "symbol", **s}, option=opt))
        for e in edges:
            f.write(orjson.dumps({"kind": "edge", **e}, option=opt))


@click.command("init")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
    help="Target repository root (defaults to cwd).",
)
@click.option(
    "--print",
    "print_jsonl",
    is_flag=True,
    help="Emit graph_symbols.jsonl to stdout instead of writing files.",
)
@click.option(
    "--no-parallel",
    is_flag=True,
    help="Disable ProcessPoolExecutor (useful for tiny repos / debugging).",
)
def init_cmd(repo: Path, print_jsonl: bool, no_parallel: bool) -> None:
    """Build the symbol graph for the target repository."""
    repo = repo.resolve()
    result = ingest(repo, parallel=not no_parallel)

    # Compute Louvain communities and write per-node `community` attributes.
    # Three audit probes need this; leaving it implicit was a v4.x footgun.
    comm_map = _assign_communities(result.symbols, result.edges)

    if print_jsonl:
        opt = orjson.OPT_SORT_KEYS | orjson.OPT_APPEND_NEWLINE
        out = sys.stdout.buffer
        out.write(
            orjson.dumps(
                {
                    "_meta": {
                        "kind": "header",
                        "version": 1,
                        "symbols": len(result.symbols),
                        "edges": len(result.edges),
                    }
                },
                option=opt,
            )
        )
        for s in result.symbols:
            out.write(orjson.dumps({"kind": "symbol", **s}, option=opt))
        for e in result.edges:
            out.write(orjson.dumps({"kind": "edge", **e}, option=opt))
        return

    # Build module-level export lists by walking export edges.
    exports_by_module: dict[str, list[str]] = {}
    sym_by_id = {s["id"]: s for s in result.symbols}
    for e in result.edges:
        if e.get("kind") == "exports":
            mod_id = e.get("src")
            tgt = e.get("dst")
            if mod_id and tgt:
                target_sym = sym_by_id.get(tgt)
                name = target_sym["name"] if target_sym else tgt
                exports_by_module.setdefault(mod_id, []).append(name)

    skeletons = skeletonize_all(result.symbols, exports_by_module=exports_by_module)

    db = cache_path(repo)
    store = Store(db)
    try:
        store.replace_all(result.symbols, result.edges)
        store.set_meta("repo_root", str(repo))
        store.set_meta("symbol_count", str(len(result.symbols)))
        store.set_meta("edge_count", str(len(result.edges)))
        if comm_map:
            store.set_meta(
                "communities_v1", orjson.dumps(comm_map, option=orjson.OPT_SORT_KEYS).decode()
            )
        # CAS: dedupe identical skeleton bodies.
        links: list[tuple[str, str]] = []
        for sid, body in skeletons.items():
            h = cas.put(store, body)
            links.append((sid, h))
        store.link_skeletons_bulk(links)
        store.set_meta("skeleton_count", str(len(links)))
    finally:
        store.close()

    jsonl_out = repo / ".graphify_plus" / "graph_symbols.jsonl"
    _write_jsonl(jsonl_out, result.symbols, result.edges)

    click.echo(
        f"gp init: parsed {result.files_parsed} files, "
        f"{len(result.symbols)} symbols, {len(result.edges)} edges "
        f"(skipped {result.files_skipped})"
    )
    click.echo(f"  cache: {db}")
    click.echo(f"  graph: {jsonl_out}")


__all__ = ["init_cmd"]
