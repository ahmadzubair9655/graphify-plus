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

from pathlib import Path

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


def _format_node(
    s: Symbol,
    *,
    confidence: float = 1.0,
    coverage: dict[str, Any] | None = None,
    **extras: Any,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "node_id": s.get("id", ""),
        "label": _label_for(s),
        "source_file": s.get("path") or "",
        "line_number": _line_number_for(s),
        "snippet": _snippet_for(s),
        "confidence": confidence,
        "kind": s.get("kind") or "",
    }
    if coverage:
        # Round to one decimal to keep wire size small; "0.0%" is a
        # meaningful signal here, so don't drop zeros.
        out["coverage_pct"] = round(coverage.get("pct", 0.0) * 100, 1)
        out["coverage_lines"] = (
            f"{coverage.get('lines_covered', 0)}/{coverage.get('lines_total', 0)}"
        )
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
        _format_node(
            s,
            pagerank=round(pr.get(s.get("id", ""), 0.0), 6),
            coverage=graph.coverage.get(s.get("id", "")),
        )
        for s in symbols
    ]
    kept, more = _budget_clip(rows, budget)
    return {"results": kept, "more_available": more}


def whats_untested(graph: InMemoryGraph, args: dict[str, Any]) -> dict[str, Any]:
    """List symbols with low or zero coverage in a file/directory.

    Coverage is the single biggest "make Claude required" feature in the
    master plan (Layer 7.1). Without test execution at query time, this
    answers "which functions have no test signal?" in milliseconds.

    Args:
        path: file or directory prefix (default: whole repo)
        max_pct: maximum coverage % to include (default 10 — "essentially untested")
        budget_tokens: response budget
    """
    path = (args.get("path") or "").strip()
    max_pct = float(args.get("max_pct", 10.0))
    budget = int(args.get("budget_tokens") or DEFAULT_BUDGET_TOKENS)

    if not graph.coverage:
        return {
            "results": [],
            "more_available": 0,
            "extra": {
                "reason": (
                    "no coverage ingested — run "
                    "`pytest --cov --cov-report=xml` then "
                    "`gp daemon coverage ingest coverage.xml`"
                )
            },
        }

    # Filter to the requested scope.
    if not path or path in (".", "./", ""):
        candidate_ids = list(graph.by_id.keys())
    elif path in graph.by_path:
        candidate_ids = list(graph.by_path[path])
    else:
        prefix = path.rstrip("/") + "/"
        candidate_ids = []
        for p, ids in graph.by_path.items():
            if p == path or p.startswith(prefix):
                candidate_ids.extend(ids)

    rows: list[dict[str, Any]] = []
    for sid in candidate_ids:
        cov = graph.coverage.get(sid)
        if cov is None:
            continue  # no signal — different from "0% covered"
        pct = float(cov.get("pct", 0.0)) * 100
        if pct > max_pct:
            continue
        sym = graph.by_id.get(sid)
        if not sym:
            continue
        rows.append(
            _format_node(
                sym,
                coverage=cov,
                confidence=1.0 - (pct / 100.0),
            )
        )
    rows.sort(key=lambda r: (r.get("coverage_pct", 0.0), r["source_file"], r["line_number"]))
    kept, more = _budget_clip(rows, budget)
    return {"results": kept, "more_available": more, "extra": {"max_pct": max_pct}}


def coverage_for(graph: InMemoryGraph, args: dict[str, Any]) -> dict[str, Any]:
    """Coverage stats for a single symbol — {pct, lines_covered, lines_total}."""
    sid, ambig = _resolve_symbol(graph, args)
    if ambig is not None:
        return ambig
    sym = graph.by_id.get(sid)
    if sym is None:
        return {"results": [], "more_available": 0, "extra": {}}
    cov = graph.coverage.get(sid)
    if cov is None:
        return {
            "results": [_format_node(sym, coverage=None)],
            "more_available": 0,
            "extra": {"reason": "no coverage data for this symbol"},
        }
    return {
        "results": [_format_node(sym, coverage=cov)],
        "more_available": 0,
        "extra": {"coverage": cov},
    }


def coverage_summary(graph: InMemoryGraph, args: dict[str, Any]) -> dict[str, Any]:
    """Repo-wide coverage roll-up: overall pct + worst-N files."""
    if not graph.coverage:
        return {
            "results": [],
            "more_available": 0,
            "extra": {"reason": "no coverage ingested"},
        }
    total_lc = 0
    total_lt = 0
    by_file: dict[str, list[int]] = {}  # path → [covered, total]
    for sid, cov in graph.coverage.items():
        sym = graph.by_id.get(sid)
        if not sym:
            continue
        path = sym.get("path") or ""
        bucket = by_file.setdefault(path, [0, 0])
        bucket[0] += int(cov.get("lines_covered", 0))
        bucket[1] += int(cov.get("lines_total", 0))
        total_lc += int(cov.get("lines_covered", 0))
        total_lt += int(cov.get("lines_total", 0))
    overall_pct = (total_lc / total_lt * 100.0) if total_lt else 0.0
    worst = sorted(
        (
            (path, lc, lt, (lc / lt * 100.0) if lt else 0.0)
            for path, (lc, lt) in by_file.items()
            if lt > 0
        ),
        key=lambda r: r[3],
    )[: int(args.get("top_k", 10))]
    return {
        "results": [],
        "more_available": 0,
        "extra": {
            "overall_pct": round(overall_pct, 1),
            "lines_covered": total_lc,
            "lines_total": total_lt,
            "files_with_signal": len(by_file),
            "worst_files": [
                {"path": p, "pct": round(pct, 1), "covered": lc, "total": lt}
                for p, lc, lt, pct in worst
            ],
        },
    }


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
    # Layer 18 — rerank with embeddings when available. If the cheap
    # path returned 0 hits, fall through to embedding-only search so a
    # well-installed user gets a real answer for fuzzy queries.
    if not rows:
        from .embeddings import search as embedding_search

        emb_hits = embedding_search(graph, query, top_k=top_k)
        if emb_hits:
            for sid, score in emb_hits:
                sym = graph.by_id.get(sid)
                if sym:
                    rows.append(
                        _format_node(
                            sym,
                            confidence=round(min(max(score, 0.0), 1.0), 3),
                            score=round(score, 3),
                            embedding=True,
                        )
                    )
    elif rows and not heavy:
        from .embeddings import rerank_or_passthrough

        rows = rerank_or_passthrough(graph, query, rows)
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


def gpl_query(graph: InMemoryGraph, args: dict[str, Any]) -> dict[str, Any]:
    """Run a GPL (Graphify-Plus Query Language) query.

    Layer 16. Args:
        query: GPL source. Required when ``nl`` not provided.
        nl: natural-language string; translated to GPL via the
            deterministic rule-based fallback. Returned as ``extra.nl_to_gpl``
            so the caller (and Claude) can confirm before re-running.
    """
    from .query_lang import QueryError, execute, parse, translate_nl

    src = (args.get("query") or "").strip()
    nl = (args.get("nl") or "").strip()
    nl_translation: str | None = None
    if nl and not src:
        src = translate_nl(nl)
        nl_translation = src
    if not src:
        return {
            "results": [],
            "more_available": 0,
            "extra": {"reason": "must pass `query` (GPL) or `nl` (natural language)"},
        }
    try:
        q = parse(src)
        result = execute(graph, q)
    except QueryError as exc:
        return {"error": {"code": "BAD_REQUEST", "message": str(exc)}}
    extra: dict[str, Any] = {
        "form": result.get("form"),
        "count": result.get("count", 0),
    }
    if nl_translation is not None:
        extra["nl_to_gpl"] = nl_translation
    if "count_by" in result:
        extra["count_by"] = result["count_by"]
    return {"results": result.get("rows", []), "more_available": 0, "extra": extra}


def cross_stack(graph: InMemoryGraph, args: dict[str, Any]) -> dict[str, Any]:
    """Cross-stack edges (HTTP boundary, DB schema) touching a symbol.

    Layer 8: returns inbound + outbound cross-stack edges so callers can
    answer questions like "which frontend code breaks if I rename this
    endpoint?" or "what code touches the users table?". With no
    arguments, returns repo-wide cross-edge stats.
    """
    sid_arg = (args.get("node") or args.get("node_id") or "").strip()
    if not sid_arg:
        # Repo-wide summary.
        by_kind: dict[str, int] = {}
        for e in graph.cross_edges:
            by_kind[e.get("kind", "?")] = by_kind.get(e.get("kind", "?"), 0) + 1
        return {
            "results": [],
            "more_available": 0,
            "extra": {"total": len(graph.cross_edges), "by_kind": by_kind},
        }
    sid, ambig = _resolve_symbol(graph, args)
    if ambig is not None:
        return ambig
    rows: list[dict[str, Any]] = []
    for e in graph.cross_edges_by_src.get(sid, []):
        rows.append({"direction": "out", **e})
    for e in graph.cross_edges_by_dst.get(sid, []):
        rows.append({"direction": "in", **e})
    if not rows:
        return {
            "results": [],
            "more_available": 0,
            "extra": {"reason": "no cross-stack edges for this symbol"},
        }
    budget = int(args.get("budget_tokens") or DEFAULT_BUDGET_TOKENS)
    kept, more = _budget_clip(rows, budget)
    return {"results": kept, "more_available": more, "extra": {"target": sid}}


def why_does_this_exist(graph: InMemoryGraph, args: dict[str, Any]) -> dict[str, Any]:
    """Return ADRs / issues / PRs that mention the target symbol.

    Layer 6.1 + 6.3: turns the structural "this function looks weird"
    question into the answer: "PR #847 added it as a fix for issue
    #812 (Stripe webhook race condition)." Limited to the cheap path
    — body-text matching on names, since deeper attribution requires
    actually following commit-to-symbol traces.
    """
    sid, ambig = _resolve_symbol(graph, args)
    if ambig is not None:
        return ambig
    nodes = graph.ingest_refs.get(sid, [])
    if not nodes:
        return {
            "results": [],
            "more_available": 0,
            "extra": {
                "reason": (
                    "no issue / PR / ADR mentions this symbol — try "
                    "`gp daemon ingest github` or `gp daemon ingest adr`"
                ),
            },
        }
    rows: list[dict[str, Any]] = []
    for node in nodes:
        rows.append(
            {
                "node_id": node.get("id"),
                "kind": node.get("kind"),
                "title": node.get("title"),
                "url": node.get("url"),
                "state": node.get("state"),
                "source": node.get("source"),
                "snippet": (node.get("body") or "")[:200],
            }
        )
    rows.sort(key=lambda r: (r.get("kind", ""), r.get("node_id", "")))
    budget = int(args.get("budget_tokens") or DEFAULT_BUDGET_TOKENS)
    kept, more = _budget_clip(rows, budget)
    return {"results": kept, "more_available": more, "extra": {"target": sid}}


def whats_vulnerable(graph: InMemoryGraph, args: dict[str, Any]) -> dict[str, Any]:
    """List CVEs reachable from the application code.

    Returns one row per symbol that imports a vulnerable package, with
    the CVE list inlined. Master plan Layer 9.1: "CVE-2025-12345 in
    `requests` is reached via `auth_service.fetch_token`."
    """
    if not graph.cve_rows:
        return {
            "results": [],
            "more_available": 0,
            "extra": {
                "reason": (
                    "no audit data ingested — run `pip-audit -f json -o audit.json` "
                    "then `gp daemon security ingest audit.json`"
                )
            },
        }
    severity_floor = (args.get("severity") or "").upper()
    min_rank = _SEVERITY_RANK.get(severity_floor, 0)
    budget = int(args.get("budget_tokens") or DEFAULT_BUDGET_TOKENS)
    rows: list[dict[str, Any]] = []
    for sid, cves in graph.cves_by_symbol.items():
        sym = graph.by_id.get(sid)
        if not sym:
            continue
        applicable = [
            c for c in cves if _SEVERITY_RANK.get((c.get("severity") or "").upper(), 0) >= min_rank
        ]
        if not applicable:
            continue
        rows.append(
            _format_node(
                sym,
                vulnerabilities=applicable,
                worst_severity=max(
                    (c["severity"] for c in applicable),
                    key=lambda s: _SEVERITY_RANK.get(s, 0),
                    default="UNKNOWN",
                ),
            )
        )
    rows.sort(
        key=lambda r: (-_SEVERITY_RANK.get(r.get("worst_severity", "UNKNOWN"), 0), r["label"])
    )
    kept, more = _budget_clip(rows, budget)
    return {
        "results": kept,
        "more_available": more,
        "extra": {
            "total_cves": len(graph.cve_rows),
            "reachable_symbols": len(graph.cves_by_symbol),
        },
    }


def whats_risky(graph: InMemoryGraph, args: dict[str, Any]) -> dict[str, Any]:
    """SAST findings — symbols with attached static-analysis warnings.

    Layer 9.2: integrates Bandit / Semgrep output into the same graph.
    The structural-risk check from `simulate` keeps its own command;
    this is the *security-risk* counterpart.
    """
    if not graph.sast_by_symbol:
        return {
            "results": [],
            "more_available": 0,
            "extra": {
                "reason": (
                    "no SAST findings ingested — run `bandit -f json -o sast.json` or "
                    "`semgrep --json > sast.json`, then "
                    "`gp daemon security ingest-sast sast.json`"
                )
            },
        }
    severity_floor = (args.get("severity") or "").upper()
    min_rank = _SEVERITY_RANK.get(severity_floor, 0)
    budget = int(args.get("budget_tokens") or DEFAULT_BUDGET_TOKENS)
    rows: list[dict[str, Any]] = []
    for sid, findings in graph.sast_by_symbol.items():
        sym = graph.by_id.get(sid)
        if not sym:
            continue
        applicable = [
            f for f in findings if _SEVERITY_RANK.get((f.get("severity") or "").upper(), 0) >= min_rank
        ]
        if not applicable:
            continue
        rows.append(
            _format_node(
                sym,
                sast_findings=applicable,
                worst_severity=max(
                    (f["severity"] for f in applicable),
                    key=lambda s: _SEVERITY_RANK.get(s, 0),
                    default="UNKNOWN",
                ),
                n_findings=len(applicable),
            )
        )
    rows.sort(
        key=lambda r: (-_SEVERITY_RANK.get(r.get("worst_severity", "UNKNOWN"), 0), -r["n_findings"])
    )
    kept, more = _budget_clip(rows, budget)
    return {"results": kept, "more_available": more}


_SEVERITY_RANK = {
    "CRITICAL": 50,
    "HIGH": 40,
    "ERROR": 40,
    "MEDIUM": 30,
    "WARNING": 25,
    "MODERATE": 30,
    "LOW": 20,
    "INFO": 10,
    "UNKNOWN": 5,
    "": 0,
}


def session_digest(graph: InMemoryGraph, args: dict[str, Any]) -> dict[str, Any]:
    """Post-session digest — Layer 13.3.

    Composes review + coverage + rules into a single end-of-session
    summary suitable for pasting into a PR description.
    """
    from .session_digest import make_digest as _make_digest

    since = (args.get("since") or "main").strip()
    head = (args.get("head") or "HEAD").strip()
    diff_text = args.get("diff_text")
    digest = _make_digest(graph, since=since, head=head, diff_text=diff_text)
    return {"results": [], "more_available": 0, "extra": {"digest": digest.to_dict()}}


def onboard(graph: InMemoryGraph, args: dict[str, Any]) -> dict[str, Any]:
    """Onboarding walkthrough — Sprint 10.2.

    Returns a structured tour: top central nodes, well-tested exemplars,
    and one representative symbol per top-level module. Renderers turn
    this into the Markdown a new contributor reads on day one.
    """
    from .onboarding import make_plan as _make_onboarding

    persona = (args.get("persona") or "engineer").strip()
    plan = _make_onboarding(graph, persona=persona)
    return {"results": [], "more_available": 0, "extra": {"onboarding": plan.to_dict()}}


def review(graph: InMemoryGraph, args: dict[str, Any]) -> dict[str, Any]:
    """PR review co-pilot — Sprint 10.1.

    Composes the touched-symbols set, central-touched, untested-touched,
    rules violations, and blast radius into a single structured report
    that callers can post as a PR comment. ``args`` accepts ``base`` and
    ``head`` git refs (defaults: ``main`` and ``HEAD``); pass
    ``diff_text`` directly to skip the git invocation (test-friendly).
    """
    from .review import make_review as _make_review

    base = (args.get("base") or "main").strip()
    head = (args.get("head") or "HEAD").strip()
    diff_text = args.get("diff_text")
    rev = _make_review(graph, base=base, head=head, diff_text=diff_text)
    return {"results": [], "more_available": 0, "extra": {"review": rev.to_dict()}}


def rules_check(graph: InMemoryGraph, args: dict[str, Any]) -> dict[str, Any]:
    """Run the architectural-drift rules from ``.graphify_plus/rules.yaml``.

    Each violation is returned as a structured row with ``rule_id``,
    ``severity``, the human-readable message, and — when the rule
    targets a specific edge — the source and destination labels with
    file:line. Returns ``{rule_count: 0}`` if no ruleset is configured
    (not an error: many repos run without rules).

    Args:
        rules_path: optional explicit path; defaults to
            ``<repo>/.graphify_plus/rules.yaml``.
    """
    import networkx as nx

    from ..runtime.overlay import empty as empty_overlay
    from ..runtime.rules import RuleSet, evaluate, grade

    repo = graph.repo_root
    rules_arg = args.get("rules_path")
    rules_path = Path(rules_arg) if rules_arg else (repo / ".graphify_plus" / "rules.yaml")
    if not rules_path.exists():
        return {
            "results": [],
            "more_available": 0,
            "extra": {"rule_count": 0, "reason": f"no ruleset at {rules_path}"},
        }

    try:
        import yaml  # PyYAML — already a hard dep
    except ImportError:
        return {
            "results": [],
            "more_available": 0,
            "extra": {"reason": "PyYAML not installed"},
        }
    try:
        ruleset = RuleSet.from_dict(yaml.safe_load(rules_path.read_text()) or {})
    except Exception as exc:  # noqa: BLE001
        return {
            "error": {
                "code": "BAD_REQUEST",
                "message": f"could not parse rules.yaml: {exc}",
            }
        }

    # Materialise a minimal NetworkX view from the snapshot.
    G: nx.MultiDiGraph = nx.MultiDiGraph()
    for sid, sym in graph.by_id.items():
        G.add_node(sid, **sym)
    for src, neighbours in graph.out_neighbours.items():
        for dst, kind, span in neighbours:
            if dst not in G:
                G.add_node(dst, id=dst, kind="external", path="")
            G.add_edge(src, dst, kind=kind, span=span)

    overlay = empty_overlay(G)
    violations = evaluate(overlay, ruleset)
    rows: list[dict[str, Any]] = []
    for v in violations:
        row: dict[str, Any] = {
            "rule_id": v.rule_id,
            "severity": v.severity,
            "message": v.message,
        }
        if v.src and v.src in graph.by_id:
            sym = graph.by_id[v.src]
            row["src"] = {
                "node_id": v.src,
                "label": _label_for(sym),
                "source_file": sym.get("path") or "",
                "line_number": _line_number_for(sym),
            }
        elif v.src:
            row["src"] = {"node_id": v.src, "label": v.src, "source_file": "", "line_number": 0}
        if v.dst and v.dst in graph.by_id:
            sym = graph.by_id[v.dst]
            row["dst"] = {
                "node_id": v.dst,
                "label": _label_for(sym),
                "source_file": sym.get("path") or "",
                "line_number": _line_number_for(sym),
            }
        elif v.dst:
            row["dst"] = {"node_id": v.dst, "label": v.dst, "source_file": "", "line_number": 0}
        if v.kind:
            row["kind"] = v.kind
        rows.append(row)

    grade_letter = grade(list(violations))
    budget = int(args.get("budget_tokens") or DEFAULT_BUDGET_TOKENS)
    kept, more = _budget_clip(rows, budget)
    return {
        "results": kept,
        "more_available": more,
        "extra": {
            "rule_count": len(ruleset.rules),
            "violation_count": len(violations),
            "errors": sum(1 for v in violations if v.severity == "error"),
            "warnings": sum(1 for v in violations if v.severity == "warning"),
            "grade": grade_letter,
            "rules_path": str(rules_path),
        },
    }


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
    # Sprint 8 — test-coverage overlay handlers:
    "whats_untested": whats_untested,
    "coverage_for": coverage_for,
    "coverage_summary": coverage_summary,
    # Sprint 9.4 — architectural-drift rules:
    "rules_check": rules_check,
    # Sprint 10.1 — PR review co-pilot:
    "review": review,
    # Sprint 10.2 — onboarding walkthrough:
    "onboard": onboard,
    # Layer 13.3 — post-session digest:
    "session_digest": session_digest,
    # Layer 9 — security overlays:
    "whats_vulnerable": whats_vulnerable,
    "whats_risky": whats_risky,
    # Layer 6 — external-source ingest:
    "why_does_this_exist": why_does_this_exist,
    # Layer 8 — cross-stack edges:
    "cross_stack": cross_stack,
    # Layer 16 — query language:
    "gpl_query": gpl_query,
    "graph_stats": graph_stats,
}


# Layer 11.2 — third-party plugins. Discovery happens lazily on first
# import so test packages can patch ``reset_registry_for_tests`` before
# the daemon comes up. Plugin-provided handlers are merged into the
# main HANDLERS dict but never override built-ins (first registration
# wins, with a warning logged).
def _merge_plugin_handlers() -> None:
    try:
        from .plugins import get_registry

        for op, fn in get_registry().handlers.items():
            if op in HANDLERS:
                continue  # built-in always wins
            HANDLERS[op] = fn
    except Exception as exc:  # noqa: BLE001
        import logging as _logging

        _logging.getLogger("graphify_plus.daemon.handlers").debug(
            "plugin merge failed: %s", exc
        )


_merge_plugin_handlers()


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
