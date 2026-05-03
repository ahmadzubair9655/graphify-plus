"""Semantic neighbourhood discovery.

Given a natural-language intent ("where is pricing calculated?"), return
the best-matching symbols.

Two-stage routing:

  1. **Louvain community detection** on the symbol graph (deterministic
     with a fixed seed). Communities are cached in SQLite so repeated
     queries don't re-run the algorithm.
  2. **BM25** over each community's symbol qualified-names + docstrings,
     scored as ``bm25_score × community_density_weight``.

An optional ``--with-embeddings`` mode (Feature 6 step 4) augments BM25
with sentence-transformers + per-community FAISS — only enabled when the
``embeddings`` extra is installed. Phase 4 ships the pure-python BM25
default; the embedding layer is a follow-up that can land any time after
this without needing a fresh phase.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

import networkx as nx
from rank_bm25 import BM25Okapi  # type: ignore[import-untyped]

from ..core.adapters import Symbol
from ..runtime.store import Store

LOUVAIN_SEED = 1337
TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*")


def _tokenize(text: str) -> list[str]:
    return [t.lower() for t in TOKEN_RE.findall(text or "")]


def _doc_for(sym: Symbol) -> str:
    parts = [
        sym.get("qualified_name") or "",
        sym.get("name") or "",
        sym.get("signature") or "",
        sym.get("docstring") or "",
        sym.get("kind") or "",
        sym.get("language") or "",
    ]
    return " ".join(p for p in parts if p)


@dataclass
class SymbolMatch:
    symbol: Symbol
    score: float
    community: int


def _undirected(G: nx.MultiDiGraph) -> nx.Graph:
    UG = nx.Graph()
    UG.add_nodes_from(G.nodes(data=True))
    for u, v in G.edges():
        if u != v and not UG.has_edge(u, v):
            UG.add_edge(u, v)
    return UG


def communities(store: Store, G: nx.MultiDiGraph) -> dict[str, int]:
    """Return ``{symbol_id: community_index}``. Cached in ``meta``."""
    cached = store.get_meta("communities_v1")
    if cached:
        return json.loads(cached)
    UG = _undirected(G)
    if not UG.nodes:
        return {}
    parts = nx.community.louvain_communities(UG, seed=LOUVAIN_SEED)
    parts_sorted = [sorted(p) for p in parts]
    parts_sorted.sort(key=lambda p: (-len(p), p[0] if p else ""))
    out: dict[str, int] = {}
    for idx, part in enumerate(parts_sorted):
        for sid in part:
            out[sid] = idx
    store.set_meta("communities_v1", json.dumps(out, sort_keys=True))
    return out


def find(store: Store, G: nx.MultiDiGraph, query: str, *, top_k: int = 8) -> list[SymbolMatch]:
    """BM25 over per-community indices, weighted by community density."""
    symbols = store.all_symbols()
    if not symbols:
        return []
    comm = communities(store, G)
    by_comm: dict[int, list[Symbol]] = {}
    for s in symbols:
        c = comm.get(s["id"], -1)
        by_comm.setdefault(c, []).append(s)

    # community density weight = log(1 + density) — prefer richer
    # communities slightly when scores are otherwise tied.
    UG = _undirected(G)
    weights: dict[int, float] = {}
    for c, members in by_comm.items():
        sub = UG.subgraph([s["id"] for s in members])
        if sub.number_of_nodes() <= 1:
            weights[c] = 1.0
            continue
        n = sub.number_of_nodes()
        m = sub.number_of_edges()
        density = (2 * m) / (n * (n - 1)) if n > 1 else 0.0
        from math import log1p

        weights[c] = 1.0 + log1p(density)

    q_tokens = _tokenize(query)
    matches: list[SymbolMatch] = []
    for c, members in by_comm.items():
        if not members:
            continue
        corpus = [_tokenize(_doc_for(s)) for s in members]
        if not any(corpus):
            continue
        bm25 = BM25Okapi(corpus)
        scores = bm25.get_scores(q_tokens)
        for s, score in zip(members, scores, strict=False):
            if score <= 0:
                continue
            matches.append(SymbolMatch(symbol=s, score=float(score) * weights[c], community=c))
    matches.sort(key=lambda m: (-m.score, m.symbol["qualified_name"]))
    return matches[:top_k]


__all__ = ["SymbolMatch", "communities", "find"]
