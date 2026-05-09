"""Layer 18 — embeddings, properly.

The strategy:

1. **Default-on, model-explicit** when the ``embeddings`` extra is
   installed. Pinned model name + version live in graph metadata so
   embeddings are reproducible and migration-aware.
2. **Embedding-augmented intent tools** — name-based search tries
   fuzzy string match first, falls back to embedding search if the
   score is below a threshold. The daemon picks; the user never has
   to specify the method.
3. **Reranking** — top-K from the graph (centrality + structure)
   gets reranked by embedding similarity to the query.
4. **Stale embedding detection** — when a node's text changes, its
   embedding is re-computed lazily on next access. The daemon tracks
   the staleness ratio.
5. **No external services required** — everything runs locally with
   the bundled (or detected) model. Larger models swap via config.

This module is *defensive*: if no embedding model is installed,
all calls return graceful "embeddings unavailable" hints. The intent
tools call ``rerank_or_passthrough`` so they never block on a missing
optional dep.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any

from .indexes import InMemoryGraph

log = logging.getLogger("graphify_plus.daemon.embeddings")

DEFAULT_MODEL = "all-MiniLM-L6-v2"  # ~22M params, broadly available


@dataclass
class EmbeddingState:
    available: bool
    model_name: str
    dim: int = 0
    reason: str = ""


_STATE: EmbeddingState | None = None
_MODEL: Any = None
_NODE_CACHE: dict[str, tuple[str, list[float]]] = {}  # sid → (text_hash, vec)


def state(*, model: str | None = None) -> EmbeddingState:
    """Probe for an embedding backend. Cached after first call."""
    global _STATE, _MODEL
    if _STATE is not None and (model is None or model == _STATE.model_name):
        return _STATE
    name = model or DEFAULT_MODEL
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore[import-not-found]

        _MODEL = SentenceTransformer(name)
        _STATE = EmbeddingState(available=True, model_name=name, dim=_MODEL.get_sentence_embedding_dimension())
    except ImportError:
        _STATE = EmbeddingState(
            available=False,
            model_name=name,
            reason="sentence-transformers not installed (pip install graphify-plus[embeddings])",
        )
    except Exception as exc:  # noqa: BLE001
        _STATE = EmbeddingState(
            available=False,
            model_name=name,
            reason=f"could not load {name}: {exc}",
        )
    return _STATE


def reset_state_for_tests(s: EmbeddingState | None) -> None:
    global _STATE, _NODE_CACHE
    _STATE = s
    _NODE_CACHE = {}


def _text_for_node(sym: dict[str, Any]) -> str:
    parts = [
        sym.get("qualified_name") or "",
        sym.get("name") or "",
        sym.get("signature") or "",
        sym.get("docstring") or "",
        sym.get("kind") or "",
    ]
    return " ".join(p for p in parts if p)


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def encode(texts: list[str]) -> list[list[float]] | None:
    s = state()
    if not s.available:
        return None
    vecs = _MODEL.encode(texts, normalize_embeddings=True, show_progress_bar=False)
    return [list(map(float, v)) for v in vecs]


def encode_node(graph: InMemoryGraph, sid: str) -> list[float] | None:
    sym = graph.by_id.get(sid)
    if not sym:
        return None
    text = _text_for_node(sym)
    h = _hash(text)
    cached = _NODE_CACHE.get(sid)
    if cached and cached[0] == h:
        return cached[1]
    enc = encode([text])
    if enc is None:
        return None
    vec = enc[0]
    _NODE_CACHE[sid] = (h, vec)
    return vec


def _cosine(a: list[float], b: list[float]) -> float:
    # Both expected to be L2-normalised by the model — dot product = cosine.
    return sum(x * y for x, y in zip(a, b, strict=False))


def search(graph: InMemoryGraph, query: str, *, top_k: int = 25) -> list[tuple[str, float]] | None:
    """Embedding-only similarity search over node text.

    Returns ``[(sid, score), ...]`` or None if embeddings are unavailable.
    """
    qv = encode([query])
    if qv is None:
        return None
    qvec = qv[0]
    scores: list[tuple[str, float]] = []
    for sid in graph.by_id:
        nv = encode_node(graph, sid)
        if nv is None:
            continue
        scores.append((sid, _cosine(qvec, nv)))
    scores.sort(key=lambda kv: -kv[1])
    return scores[:top_k]


def rerank_or_passthrough(
    graph: InMemoryGraph,
    query: str,
    candidates: list[dict[str, Any]],
    *,
    top_k: int | None = None,
) -> list[dict[str, Any]]:
    """If embeddings are available, rerank ``candidates`` by query
    similarity. Each candidate must carry a ``node_id``. The reranked
    score lands on a new ``rerank_score`` field; the original
    ``confidence`` is preserved. If embeddings aren't available, the
    candidate list is returned unchanged.
    """
    s = state()
    if not s.available:
        return candidates
    qv = encode([query])
    if qv is None:
        return candidates
    qvec = qv[0]
    scored: list[tuple[float, dict[str, Any]]] = []
    for c in candidates:
        sid = c.get("node_id")
        if not sid:
            scored.append((0.0, c))
            continue
        nv = encode_node(graph, sid)
        if nv is None:
            scored.append((0.0, c))
            continue
        scored.append((_cosine(qvec, nv), c))
    scored.sort(key=lambda kv: -kv[0])
    out: list[dict[str, Any]] = []
    for sc, c in scored:
        new_c = dict(c)
        new_c["rerank_score"] = round(sc, 4)
        out.append(new_c)
    return out[: top_k if top_k else len(out)]


def stale_ratio(graph: InMemoryGraph) -> float:
    """Fraction of nodes whose cached embedding text-hash no longer
    matches the current node text. Used by the freshness contract.
    """
    if not graph.by_id:
        return 0.0
    stale = 0
    for sid, sym in graph.by_id.items():
        cached = _NODE_CACHE.get(sid)
        if cached is None:
            stale += 1
            continue
        if cached[0] != _hash(_text_for_node(sym)):
            stale += 1
    return stale / len(graph.by_id)


__all__ = [
    "DEFAULT_MODEL",
    "EmbeddingState",
    "encode",
    "encode_node",
    "rerank_or_passthrough",
    "reset_state_for_tests",
    "search",
    "stale_ratio",
    "state",
]
