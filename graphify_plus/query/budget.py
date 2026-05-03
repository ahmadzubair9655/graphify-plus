"""Token-budgeted framing for graph context.

Given a ``Partition`` and a token budget, pack the most informative
skeletons into the budget.

Greedy ordering:

  1. All gatekeepers (high-betweenness on the induced subgraph) — these
     are non-negotiable; without them, downstream code paths are unclear.
  2. Path skeletons (shortest entry→target route) in topological order.
  3. 1-hop neighbours sorted by importance proxy (kind weight × name
     length tie-break, descending).

When the budget is tight, neighbours fall back to **truncated skeletons**
(signature only, no body-omitted line, no docstring). When still over,
emit a single ``« N additional nodes cut by budget »`` marker.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ..core.adapters import Symbol
from ..runtime.store import Store
from .partition import Partition

# Token counting --------------------------------------------------------

try:  # tiktoken is a base dep, but degrade gracefully if it can't load.
    import tiktoken

    _ENC = tiktoken.get_encoding("cl100k_base")

    def count_tokens(s: str) -> int:
        return len(_ENC.encode(s))
except Exception:  # noqa: BLE001 — fallback path

    def count_tokens(s: str) -> int:
        # Rough char→token ratio ~ 3.5 for English+code mix.
        return max(1, len(s) // 4)


# Importance heuristic --------------------------------------------------

_KIND_WEIGHT: dict[str, int] = {
    "module": 6,
    "class": 5,
    "interface": 5,
    "function": 4,
    "method": 4,
    "type": 3,
    "const": 2,
    "export": 1,
    "endpoint": 5,
    "external": 0,
}


def _importance(sym: Symbol) -> tuple[int, int]:
    return (_KIND_WEIGHT.get(sym.get("kind") or "", 1), len(sym.get("name") or ""))


def _truncate(skeleton: str) -> str:
    """Drop body-omitted line + docstring, keep header + signature only."""
    lines = skeleton.splitlines()
    if len(lines) <= 2:
        return skeleton.rstrip("\n") + "\n"
    return "\n".join(lines[:2]) + "\n"


# Public API ------------------------------------------------------------


@dataclass
class BudgetedContext:
    text: str
    manifest: list[str]  # ordered list of skeleton hashes included
    truncated_count: int
    cut_count: int
    tokens: int


def _skeleton_for(store: Store, sid: str) -> tuple[str | None, str | None]:
    """Return ``(body, hash)`` for ``sid``, or ``(None, None)`` if not stored."""
    body = store.get_skeleton_for_symbol(sid)
    if body is None:
        return None, None
    row = store.conn.execute(
        "SELECT hash FROM symbol_skeletons WHERE symbol_id = ?", (sid,)
    ).fetchone()
    return body, (row[0] if row else None)


def frame_to_budget(
    partition: Partition, store: Store, *, max_tokens: int = 4000
) -> BudgetedContext:
    """Pack ``partition`` into ``max_tokens`` of skeleton text.

    Token count of the returned text is bounded by ``max_tokens`` plus a
    5% slack accounting for the cut marker; never under-counts.
    """
    out_chunks: list[str] = []
    manifest: list[str] = []
    used = 0
    truncated_count = 0
    cut_count = 0

    # 1) Resolve symbols + skeletons in priority order.
    seen: set[str] = set()
    ordered_ids: list[tuple[str, str]] = []  # (sid, role)

    for sid in partition.gatekeepers:
        if sid not in seen:
            seen.add(sid)
            ordered_ids.append((sid, "gatekeeper"))
    for sid in partition.path:
        if sid not in seen:
            seen.add(sid)
            ordered_ids.append((sid, "path"))

    # Sort neighbours by importance, then id for stability.
    neigh_ranked: list[tuple[Symbol, str]] = []
    for sid in partition.neighbours:
        if sid in seen:
            continue
        node = partition.induced.nodes.get(sid)
        if node is None:
            continue
        neigh_ranked.append((dict(node), sid))  # type: ignore[arg-type]
    neigh_ranked.sort(key=lambda pair: (-_importance(pair[0])[0], pair[1]))
    for _, sid in neigh_ranked:
        ordered_ids.append((sid, "neighbour"))
        seen.add(sid)

    # 2) Greedy fill.
    for sid, role in ordered_ids:
        body, h = _skeleton_for(store, sid)
        if body is None:
            continue
        full_tokens = count_tokens(body)
        if used + full_tokens <= max_tokens:
            out_chunks.append(body.rstrip("\n"))
            if h:
                manifest.append(h)
            used += full_tokens
            continue

        # Try truncated form for neighbours.
        if role == "neighbour":
            tr = _truncate(body)
            tr_tokens = count_tokens(tr)
            if used + tr_tokens <= max_tokens:
                out_chunks.append(tr.rstrip("\n"))
                if h:
                    manifest.append(h)
                used += tr_tokens
                truncated_count += 1
                continue

        cut_count += 1

    # 3) Cut marker if anything got dropped.
    if cut_count:
        marker = f"« {cut_count} additional nodes cut by budget »"
        out_chunks.append(marker)
        used += count_tokens(marker)

    text = "\n\n".join(out_chunks) + "\n"
    return BudgetedContext(
        text=text,
        manifest=manifest,
        truncated_count=truncated_count,
        cut_count=cut_count,
        tokens=used,
    )


def manifest_for_path(store: Store, paths: Sequence[str]) -> list[str]:
    """Helper: build a partition-equivalent input from a list of file paths.
    Used by ``gp context --files`` (Phase 3 simple mode) — Phase 4 will
    add intent-driven entry points.
    """
    ids: list[str] = []
    for p in paths:
        for s in store.symbols_in_path(p):
            ids.append(s["id"])
    return ids


__all__ = ["BudgetedContext", "count_tokens", "frame_to_budget", "manifest_for_path"]
