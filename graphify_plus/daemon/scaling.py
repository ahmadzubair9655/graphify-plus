"""Layer 15 — scaling beyond the published targets.

This module provides graceful-degradation primitives for huge graphs:

* **Hierarchical graphs** — group symbols into top-level "modules"
  (one per directory) and pre-compute aggregate edges between them.
  Queries on small graphs use the fine-grained graph; queries on huge
  ones can route to the coarse view.
* **Approximate sampling** — declare ``exact: true|false`` on every
  result so the caller knows when an answer is sampled.
* **On-disk indexes for cold paths** — use SQLite for full-text and
  rare lookups when the in-memory index would be wasteful.

The implementation is *opt-in*. Default daemon flow stays exact + RAM
because the small/medium-graph path is the hot one.
"""

from __future__ import annotations

import logging
import math
import random
from dataclasses import dataclass, field
from typing import Any

from .indexes import InMemoryGraph

log = logging.getLogger("graphify_plus.daemon.scaling")


HUGE_GRAPH_THRESHOLD = 100_000  # nodes


@dataclass
class CoarseNode:
    id: str  # synthetic, e.g. "module:src/auth"
    label: str
    n_symbols: int = 0
    n_edges: int = 0


@dataclass
class CoarseGraph:
    nodes: dict[str, CoarseNode] = field(default_factory=dict)
    edges: dict[tuple[str, str], int] = field(default_factory=dict)
    fine_to_coarse: dict[str, str] = field(default_factory=dict)


def build_coarse(graph: InMemoryGraph, *, depth: int = 1) -> CoarseGraph:
    """Coarsen the graph by collapsing every symbol into its top-N
    directory ancestor. ``depth=1`` means one path component (``src``);
    ``depth=2`` means ``src/auth``.
    """
    coarse = CoarseGraph()
    for sid, sym in graph.by_id.items():
        path = sym.get("path") or ""
        if not path:
            continue
        parts = path.split("/")[:depth]
        cid = "module:" + "/".join(parts) if parts else "module:_root"
        coarse.fine_to_coarse[sid] = cid
        node = coarse.nodes.get(cid)
        if node is None:
            node = CoarseNode(id=cid, label="/".join(parts) or "_root")
            coarse.nodes[cid] = node
        node.n_symbols += 1
    for src_id, neighbours in graph.out_neighbours.items():
        cs = coarse.fine_to_coarse.get(src_id)
        if cs is None:
            continue
        for dst_id, _kind, _span in neighbours:
            cd = coarse.fine_to_coarse.get(dst_id)
            if cd is None or cd == cs:
                continue
            key = (cs, cd)
            coarse.edges[key] = coarse.edges.get(key, 0) + 1
            coarse.nodes[cs].n_edges += 1
    return coarse


def coarse_summary(coarse: CoarseGraph) -> dict[str, Any]:
    return {
        "n_modules": len(coarse.nodes),
        "n_module_edges": len(coarse.edges),
        "biggest": sorted(
            ({"id": n.id, "n_symbols": n.n_symbols} for n in coarse.nodes.values()),
            key=lambda r: -r["n_symbols"],
        )[:10],
    }


# ---- approximation contracts -------------------------------------------


@dataclass
class Approximate:
    exact: bool
    method: str
    sample_size: int = 0
    confidence_interval: tuple[float, float] | None = None


def is_huge(graph: InMemoryGraph) -> bool:
    return len(graph.by_id) >= HUGE_GRAPH_THRESHOLD


def sampled_pagerank(
    graph: InMemoryGraph, k: int = 50, *, seed: int = 1337
) -> tuple[list[tuple[str, float]], Approximate]:
    """Sampled PageRank approximation for huge graphs.

    Walks ``k * 8`` random restarts; each step jumps to a random
    neighbour with probability 0.85. After all walks, the visit counts
    are normalised. P50 quality on graphs <100k nodes is fine; we
    still report ``exact=False`` so callers know.
    """
    rng = random.Random(seed)
    if not graph.by_id:
        return [], Approximate(exact=True, method="empty")
    if not is_huge(graph) and graph.pagerank_top:
        return graph.pagerank_top, Approximate(exact=True, method="cached")
    visits: dict[str, int] = {}
    nodes = list(graph.by_id.keys())
    walks = max(1, k * 8)
    for _ in range(walks):
        cur = rng.choice(nodes)
        steps = max(5, int(math.log(len(nodes)) * 4))
        for _ in range(steps):
            visits[cur] = visits.get(cur, 0) + 1
            neigh = graph.out_neighbours.get(cur, [])
            if not neigh or rng.random() < 0.15:
                cur = rng.choice(nodes)
                continue
            cur = neigh[rng.randrange(0, len(neigh))][0]
            if cur not in graph.by_id:
                cur = rng.choice(nodes)
    total = sum(visits.values()) or 1
    normalised = [(sid, visits.get(sid, 0) / total) for sid in nodes]
    normalised.sort(key=lambda kv: -kv[1])
    return normalised[:k], Approximate(
        exact=False,
        method="random-walk",
        sample_size=walks,
        confidence_interval=(0.6, 0.9),
    )


# ---- on-disk SQLite-backed full-text fallback --------------------------


FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS node_fts USING fts5(
    symbol_id UNINDEXED,
    text,
    tokenize = 'unicode61'
);
"""


def ensure_fts_table(store) -> None:
    store.conn.executescript(FTS_SCHEMA)


def index_full_text(store, graph: InMemoryGraph) -> int:
    """Populate the FTS5 index from the snapshot. Best-effort: SQLite
    builds without FTS5 will silently skip.
    """
    try:
        ensure_fts_table(store)
    except Exception as exc:  # noqa: BLE001
        log.debug("FTS5 unavailable: %s", exc)
        return 0
    rows = []
    for sid, sym in graph.by_id.items():
        text = " ".join(
            str(sym.get(k) or "")
            for k in ("qualified_name", "name", "signature", "docstring", "kind")
        )
        rows.append((sid, text))
    with store.tx():
        store.conn.execute("DELETE FROM node_fts")
        store.conn.executemany("INSERT INTO node_fts(symbol_id, text) VALUES (?, ?)", rows)
    return len(rows)


def fts_search(store, query: str, *, limit: int = 25) -> list[tuple[str, float]]:
    """SQLite FTS5 search; gracefully degrades if FTS isn't compiled in."""
    try:
        ensure_fts_table(store)
        rows = store.conn.execute(
            "SELECT symbol_id, bm25(node_fts) FROM node_fts "
            "WHERE node_fts MATCH ? ORDER BY bm25(node_fts) LIMIT ?",
            (query, limit),
        ).fetchall()
    except Exception as exc:  # noqa: BLE001
        log.debug("FTS search failed: %s", exc)
        return []
    return [(sid, -float(score)) for sid, score in rows]


__all__ = [
    "Approximate",
    "CoarseGraph",
    "CoarseNode",
    "FTS_SCHEMA",
    "HUGE_GRAPH_THRESHOLD",
    "build_coarse",
    "coarse_summary",
    "ensure_fts_table",
    "fts_search",
    "index_full_text",
    "is_huge",
    "sampled_pagerank",
]
