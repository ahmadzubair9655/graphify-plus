"""Layer 2.3 — hybrid mode: graph + grep on stale paths.

The master plan's killer feature: when the graph is stale on a
particular file, transparently fall back to grep *for that file
only* and merge the results. Claude sees a unified response with
each row labelled by source.

The implementation is deliberately simple — when freshness reports
``STALE_FILES``, we run a one-shot regex over each stale path and
synthesise extra rows that look like normal handler output rows
(with ``source: "grep"`` so callers can distinguish).
"""

from __future__ import annotations

import logging
import re
from typing import Any

from .indexes import InMemoryGraph

log = logging.getLogger("graphify_plus.daemon.hybrid_grep")

MAX_GREP_HITS_PER_FILE = 8
MAX_LINE_LENGTH = 200


def grep_stale_paths(
    graph: InMemoryGraph,
    pattern: str,
    *,
    max_hits_per_file: int = MAX_GREP_HITS_PER_FILE,
) -> list[dict[str, Any]]:
    """Run a literal-string scan over every stale path declared by the
    snapshot's freshness envelope. Returns one row per match in the
    canonical handler-row shape::

        {
            "node_id": "",                # empty — grep, not a node
            "label": "<line>",
            "source_file": <path>,
            "line_number": <line>,
            "snippet": <line text>,
            "confidence": 0.5,
            "kind": "grep",
            "source": "grep"
        }
    """
    fresh = graph.freshness()
    stale_paths = list(fresh.get("stale_paths") or [])
    if not stale_paths:
        return []
    try:
        rgx = re.compile(re.escape(pattern), re.IGNORECASE)
    except re.error:
        return []
    rows: list[dict[str, Any]] = []
    for rel in stale_paths:
        full = graph.repo_root / rel
        if not full.exists() or not full.is_file():
            continue
        try:
            text = full.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        hits = 0
        for i, line in enumerate(text.splitlines(), start=1):
            if hits >= max_hits_per_file:
                break
            if not rgx.search(line):
                continue
            snippet = line.strip()
            if len(snippet) > MAX_LINE_LENGTH:
                snippet = snippet[:MAX_LINE_LENGTH] + "…"
            rows.append(
                {
                    "node_id": "",
                    "label": snippet[:80],
                    "source_file": rel,
                    "line_number": i,
                    "snippet": snippet,
                    "confidence": 0.5,
                    "kind": "grep",
                    "source": "grep",
                }
            )
            hits += 1
    return rows


def hybrid_merge(
    graph_rows: list[dict[str, Any]],
    grep_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Append grep rows after graph rows; tag each graph row with
    ``source: "graph"`` so callers can distinguish.
    """
    out: list[dict[str, Any]] = []
    for row in graph_rows:
        new = dict(row)
        new.setdefault("source", "graph")
        out.append(new)
    out.extend(grep_rows)
    return out


def maybe_augment(
    graph: InMemoryGraph,
    rows: list[dict[str, Any]],
    *,
    pattern: str | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Augment ``rows`` with grep hits over stale paths when:

    * freshness reports STALE_FILES
    * a meaningful pattern was provided (non-empty after stripping)

    Returns ``(merged_rows, info)`` where info carries telemetry the
    handler can surface (n_grep_hits, stale_paths).
    """
    fresh = graph.freshness()
    info: dict[str, Any] = {
        "trust": fresh.get("trust", "FRESH"),
        "stale_paths": list(fresh.get("stale_paths") or []),
        "n_grep_hits": 0,
    }
    trust = fresh.get("trust") or ""
    if not pattern or trust not in ("STALE_FILES", "STALE_REBUILD_NEEDED"):
        return rows, info
    grep = grep_stale_paths(graph, pattern.strip())
    info["n_grep_hits"] = len(grep)
    return hybrid_merge(rows, grep), info


__all__ = [
    "MAX_GREP_HITS_PER_FILE",
    "grep_stale_paths",
    "hybrid_merge",
    "maybe_augment",
]
