"""Intent-typed tool handlers (Sprint 2 of the master plan).

Each handler answers exactly one question — named after the question, not
after a graph algorithm. They share a common output shape:

    {
        "node_id": str,
        "label": str,            # qualified_name when present, else name
        "source_file": str,      # POSIX, repo-relative
        "line_number": int,      # 1-based; 0 means "unknown"
        "snippet": str,          # short signature/docstring excerpt
        "confidence": float,     # 0..1, defaults to 1.0 for structural answers
        "kind": str,             # symbol kind
        ... handler-specific extras ...
    }

…so callers can pipe the response straight into Read/Edit without a second
round-trip. This is shortcoming #3 of the master plan: "the graph loses on
round-trips" because nodes don't include file:line. We fix that uniformly.

Token-budgeting (Layer 1.3): every handler accepts ``budget_tokens`` and
clips the result list to fit. The pre-clip count goes back as
``more_available`` so the caller can ask for the next page.
"""

from __future__ import annotations

import logging
from typing import Any

from ..core.adapters import Symbol
from .indexes import InMemoryGraph
from .receipts import estimate_tokens

log = logging.getLogger("graphify_plus.daemon.handlers")

DEFAULT_BUDGET_TOKENS = 1500
SNIPPET_MAX_CHARS = 240


# ---- result formatting ---------------------------------------------------


def _snippet_for(s: Symbol) -> str:
    sig = (s.get("signature") or "").strip()
    doc = (s.get("docstring") or "").strip()
    body = sig
    if doc:
        first_line = doc.splitlines()[0].strip()
        body = f"{sig}  — {first_line}" if sig else first_line
    if len(body) > SNIPPET_MAX_CHARS:
        body = body[: SNIPPET_MAX_CHARS - 1] + "…"
    return body


def _line_number_for(s: Symbol) -> int:
    span = s.get("span") or (0, 0)
    if isinstance(span, list | tuple) and span:
        return int(span[0])
    return 0


def _label_for(s: Symbol) -> str:
    return s.get("qualified_name") or s.get("name") or s.get("id", "")


def _format_node(s: Symbol, *, confidence: float = 1.0, **extras: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "node_id": s.get("id", ""),
        "label": _label_for(s),
        "source_file": s.get("path") or "",
        "line_number": _line_number_for(s),
        "snippet": _snippet_for(s),
        "confidence": confidence,
        "kind": s.get("kind") or "",
    }
    out.update(extras)
    return out


def _budget_clip(
    rows: list[dict[str, Any]], budget_tokens: int
) -> tuple[list[dict[str, Any]], int]:
    """Trim ``rows`` so the JSON-serialised list fits within ``budget_tokens``.

    Returns ``(kept, more_available)``. The check is done on the cumulative
    estimated-tokens count so the response shape is predictable.
    """
    if budget_tokens <= 0 or not rows:
        return rows, 0
    kept: list[dict[str, Any]] = []
    used = 0
    for row in rows:
        cost = estimate_tokens(row)
        if kept and used + cost > budget_tokens:
            break
        kept.append(row)
        used += cost
    return kept, max(0, len(rows) - len(kept))


# ---- handlers ------------------------------------------------------------


def whats_in(graph: InMemoryGraph, args: dict[str, Any]) -> dict[str, Any]:
    """List the nodes inside a file or module, ranked by structural weight.

    Input::

        {"path": "src/auth.py"}            # POSIX repo-relative
        {"path": "src/", "budget_tokens": 1500}    # also accepts directory prefix

    Ranking: PageRank when present, then symbol kind priority
    (module > class > function > const > everything else), then line number.
    """
    path = (args.get("path") or "").strip()
    budget = int(args.get("budget_tokens") or DEFAULT_BUDGET_TOKENS)
    if not path:
        return {"results": [], "more_available": 0, "extra": {"reason": "no path given"}}

    pr = {sid: score for sid, score in graph.pagerank_top}
    kind_order = {"module": 0, "class": 1, "interface": 1, "type": 2, "function": 3,
                  "method": 3, "const": 4}

    if path in graph.by_path:
        symbols = graph.symbols_in_path(path)
    elif path in (".", "./", ""):
        # Match every indexed file.
        symbols = []
        for p in graph.by_path:
            symbols.extend(graph.symbols_in_path(p))
    else:
        # Treat as a path *prefix* / directory.
        prefix = path.rstrip("/") + "/"
        symbols = []
        for p in graph.by_path:
            if p == path or p.startswith(prefix):
                symbols.extend(graph.symbols_in_path(p))

    def sort_key(s: Symbol) -> tuple[float, int, int]:
        return (
            -pr.get(s.get("id", ""), 0.0),
            kind_order.get(s.get("kind") or "", 9),
            _line_number_for(s),
        )

    symbols.sort(key=sort_key)
    rows = [
        _format_node(s, pagerank=round(pr.get(s.get("id", ""), 0.0), 6))
        for s in symbols
    ]
    kept, more = _budget_clip(rows, budget)
    return {"results": kept, "more_available": more}


def who_calls(graph: InMemoryGraph, args: dict[str, Any]) -> dict[str, Any]:
    """Direct callers (or referencers) of a node.

    Input::

        {"node": "auth.AuthService.login"}
        {"node_id": "abcdef0123456789"}        # also accepts raw symbol id

    Resolution order: (1) raw id; (2) exact qualified_name; (3) ``find_by_name``
    fuzzy resolution. If multiple symbols match the name, returns
    ``{"error": "AMBIGUOUS", ...}`` so the caller can re-query with a more
    specific name.
    """
    sid, ambig = _resolve_symbol(graph, args)
    if ambig is not None:
        return ambig
    budget = int(args.get("budget_tokens") or DEFAULT_BUDGET_TOKENS)
    callers = graph.callers_of(sid)
    rows = [_format_node(s) for s in callers]
    kept, more = _budget_clip(rows, budget)
    return {"results": kept, "more_available": more, "extra": {"target": sid}}


def whos_called_by(graph: InMemoryGraph, args: dict[str, Any]) -> dict[str, Any]:
    """Direct callees of a node — the inverse of who_calls."""
    sid, ambig = _resolve_symbol(graph, args)
    if ambig is not None:
        return ambig
    budget = int(args.get("budget_tokens") or DEFAULT_BUDGET_TOKENS)
    callees = graph.callees_of(sid)
    rows = [_format_node(s) for s in callees]
    kept, more = _budget_clip(rows, budget)
    return {"results": kept, "more_available": more, "extra": {"target": sid}}


def what_depends_on(graph: InMemoryGraph, args: dict[str, Any]) -> dict[str, Any]:
    """Anyone with an inbound edge to the node — calls, refs, imports,
    extends, implements. The dependents view used by refactoring.
    """
    sid, ambig = _resolve_symbol(graph, args)
    if ambig is not None:
        return ambig
    budget = int(args.get("budget_tokens") or DEFAULT_BUDGET_TOKENS)
    dependents = graph.dependents_of(sid)
    rows = [_format_node(s) for s in dependents]
    kept, more = _budget_clip(rows, budget)
    return {"results": kept, "more_available": more, "extra": {"target": sid}}


def what_does_this_depend_on(
    graph: InMemoryGraph, args: dict[str, Any]
) -> dict[str, Any]:
    """Outbound dependencies of the node."""
    sid, ambig = _resolve_symbol(graph, args)
    if ambig is not None:
        return ambig
    budget = int(args.get("budget_tokens") or DEFAULT_BUDGET_TOKENS)
    deps = graph.dependencies_of(sid)
    rows = [_format_node(s) for s in deps]
    kept, more = _budget_clip(rows, budget)
    return {"results": kept, "more_available": more, "extra": {"target": sid}}


def find_by_name(graph: InMemoryGraph, args: dict[str, Any]) -> dict[str, Any]:
    """Fuzzy label match. Confidence reflects exact-vs-prefix-vs-suffix.

    Input::

        {"label": "AuthService"}
        {"label": "auth", "fuzzy": true}
    """
    label = (args.get("label") or args.get("query") or "").strip()
    fuzzy = bool(args.get("fuzzy", True))
    budget = int(args.get("budget_tokens") or DEFAULT_BUDGET_TOKENS)
    if not label:
        return {"results": [], "more_available": 0, "extra": {"reason": "no label given"}}
    matches = graph.find_by_name(label, fuzzy=fuzzy, limit=200)
    rows: list[dict[str, Any]] = []
    ql = label.lower()
    for s in matches:
        qname = (s.get("qualified_name") or "").lower()
        name = (s.get("name") or "").lower()
        if qname == ql or name == ql:
            confidence = 1.0
        elif qname.startswith(ql) or name.startswith(ql):
            confidence = 0.85
        elif qname.endswith("." + ql):
            confidence = 0.75
        else:
            confidence = 0.6
        rows.append(_format_node(s, confidence=confidence))
    kept, more = _budget_clip(rows, budget)
    return {"results": kept, "more_available": more}


def find_by_concept(graph: InMemoryGraph, args: dict[str, Any]) -> dict[str, Any]:
    """Concept / natural-language search.

    Backed by the daemon's pre-computed inverted-text index for sub-millisecond
    response. Heavier BM25 + community weighting (``query.semantic.find``) is
    available via ``args["heavy"] = True`` for cases where the cheap path
    misses — but the cheap path covers ~90% of intent.
    """
    query = (args.get("query") or args.get("intent") or "").strip()
    budget = int(args.get("budget_tokens") or DEFAULT_BUDGET_TOKENS)
    heavy = bool(args.get("heavy", False))
    top_k = int(args.get("top_k", 25))
    if not query:
        return {"results": [], "more_available": 0, "extra": {"reason": "no query given"}}

    if heavy:
        # Reuse the existing community-weighted BM25 path. We fall back
        # gracefully if it can't be constructed (e.g. fresh empty graph).
        rows = _heavy_concept_search(graph, query, top_k)
    else:
        ranked = graph.text_search(query, limit=top_k)
        max_score = max((s for _, s in ranked), default=1.0) or 1.0
        rows = [
            _format_node(sym, confidence=round(score / max_score, 3), score=round(score, 3))
            for sym, score in ranked
        ]
    kept, more = _budget_clip(rows, budget)
    return {"results": kept, "more_available": more}


def whats_central(graph: InMemoryGraph, args: dict[str, Any]) -> dict[str, Any]:
    """Top-N central nodes by PageRank.

    Useful for onboarding ("show me the most important things to read first")
    and for ranking other handler results.
    """
    budget = int(args.get("budget_tokens") or DEFAULT_BUDGET_TOKENS)
    top_k = int(args.get("top_k", 25))
    rows: list[dict[str, Any]] = []
    for sid, score in graph.pagerank_top[:top_k]:
        sym = graph.by_id.get(sid)
        if not sym:
            continue
        rows.append(_format_node(sym, pagerank=round(score, 6)))
    kept, more = _budget_clip(rows, budget)
    return {"results": kept, "more_available": more}


def plan(graph: InMemoryGraph, args: dict[str, Any]) -> dict[str, Any]:
    """Graph-grounded edit plan for a natural-language task description.

    Wraps :func:`graphify_plus.daemon.planner.make_plan` so the same
    deterministic pipeline is reachable from the daemon, the CLI, and
    MCP. Returns the plan as the ``extra`` payload — not as ``results``
    — because the plan is a structured object, not a list of nodes.
    """
    from .planner import make_plan as _make_plan

    task = (args.get("task") or args.get("query") or "").strip()
    if not task:
        return {
            "results": [],
            "more_available": 0,
            "extra": {"reason": "no task given"},
        }
    p = _make_plan(graph, task, top_k=int(args.get("top_k", 12)))
    return {"results": [], "more_available": 0, "extra": {"plan": p.to_dict()}}


def graph_stats(graph: InMemoryGraph, args: dict[str, Any]) -> dict[str, Any]:
    """Summary stats — used by ``gp daemon status`` and tests."""
    stats = graph.stats
    return {
        "results": [],
        "more_available": 0,
        "extra": {
            "symbols": len(graph.by_id),
            "edges_out": sum(len(v) for v in graph.out_neighbours.values()),
            "files": len(graph.by_path),
            "pagerank_top_n": len(graph.pagerank_top),
            "inverted_terms": len(graph.inverted_text),
            "build_elapsed_ms": stats.elapsed_ms if stats else 0.0,
            "freshness_token": graph.freshness_token,
        },
    }


# ---- internal helpers ----------------------------------------------------


def _resolve_symbol(
    graph: InMemoryGraph, args: dict[str, Any]
) -> tuple[str, dict[str, Any] | None]:
    """Returns ``(symbol_id, error_payload_or_None)``. Pattern lets handlers
    say ``sid, ambig = _resolve_symbol(...); if ambig: return ambig``.
    """
    raw_id = (args.get("node_id") or "").strip()
    if raw_id and raw_id in graph.by_id:
        return raw_id, None
    label = (args.get("node") or args.get("label") or "").strip()
    if not label:
        return "", {
            "error": {
                "code": "BAD_REQUEST",
                "message": "must provide 'node' (label) or 'node_id'",
            }
        }
    matches = graph.find_by_name(label, fuzzy=True, limit=8)
    if not matches:
        return "", {
            "error": {
                "code": "NOT_FOUND",
                "message": f"no symbol matches {label!r}",
            }
        }
    if len(matches) > 1:
        # Prefer exact qname / name matches if any.
        ql = label.lower()
        exact = [
            s
            for s in matches
            if (s.get("qualified_name") or "").lower() == ql
            or (s.get("name") or "").lower() == ql
        ]
        if len(exact) == 1:
            return exact[0]["id"], None
        return "", {
            "error": {
                "code": "AMBIGUOUS",
                "message": (
                    f"{len(matches)} symbols match {label!r} — "
                    f"use a more specific qualified_name"
                ),
                "detail": {
                    "candidates": [
                        {
                            "node_id": s["id"],
                            "label": _label_for(s),
                            "source_file": s.get("path", ""),
                            "line_number": _line_number_for(s),
                        }
                        for s in matches[:8]
                    ]
                },
            }
        }
    return matches[0]["id"], None


def _heavy_concept_search(
    graph: InMemoryGraph, query: str, top_k: int
) -> list[dict[str, Any]]:
    """Fallback to the existing BM25 + community path. Operates on the
    *symbols already in memory* (no SQLite roundtrip) by reconstructing a
    minimal NetworkX view.
    """
    try:
        import networkx as nx
        from rank_bm25 import BM25Okapi
        from .indexes import _safe_doc, _tokenize
    except Exception:  # noqa: BLE001
        return []

    if not graph.by_id:
        return []

    # Minimal multidigraph for community read.
    G = nx.MultiDiGraph()
    for sid, s in graph.by_id.items():
        G.add_node(sid, **s)
    for src, neighbours in graph.out_neighbours.items():
        for dst, kind, span in neighbours:
            if dst not in G:
                G.add_node(dst, id=dst, kind="external")
            G.add_edge(src, dst, kind=kind, span=span)

    comm = graph.communities or {}
    by_comm: dict[int, list[Symbol]] = {}
    for sid, s in graph.by_id.items():
        c = comm.get(sid, -1)
        by_comm.setdefault(c, []).append(s)

    q_tokens = _tokenize(query)
    if not q_tokens:
        return []
    matches: list[tuple[Symbol, float]] = []
    for _c, members in by_comm.items():
        corpus = [_tokenize(_safe_doc(s)) for s in members]
        if not any(corpus):
            continue
        bm25 = BM25Okapi(corpus)
        scores = bm25.get_scores(q_tokens)
        for s, score in zip(members, scores, strict=False):
            if score <= 0:
                continue
            matches.append((s, float(score)))
    matches.sort(key=lambda kv: -kv[1])
    matches = matches[:top_k]
    max_score = max((sc for _, sc in matches), default=1.0) or 1.0
    return [
        _format_node(s, confidence=round(sc / max_score, 3), score=round(sc, 3))
        for s, sc in matches
    ]


# ---- registry ------------------------------------------------------------

HANDLERS = {
    "whats_in": whats_in,
    "who_calls": who_calls,
    "whos_called_by": whos_called_by,
    "what_depends_on": what_depends_on,
    "what_does_this_depend_on": what_does_this_depend_on,
    "find_by_name": find_by_name,
    "find_by_concept": find_by_concept,
    "whats_central": whats_central,
    "plan": plan,
    "graph_stats": graph_stats,
}


__all__ = [
    "DEFAULT_BUDGET_TOKENS",
    "HANDLERS",
    "find_by_concept",
    "find_by_name",
    "graph_stats",
    "what_depends_on",
    "what_does_this_depend_on",
    "whats_central",
    "whats_in",
    "who_calls",
    "whos_called_by",
]
