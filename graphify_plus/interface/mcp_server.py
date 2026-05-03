"""Model Context Protocol server.

Exposes Graphify-Plus capabilities as MCP tools so an MCP-aware AI
client (Claude Code, Cursor, etc.) can call them directly without
shelling out to the CLI.

Tools exposed (input schemas mirror the corresponding ``gp`` CLI):

  gp_init, gp_plan, gp_context, gp_skeleton, gp_find, gp_simulate,
  gp_guardrails, gp_audit, gp_diff, gp_blast_radius, gp_coordinate,
  gp_visual

Outputs are token-budgeted to ≤ 8 000 tokens by default. Stdio
transport. Run via ``python -m graphify_plus.interface.mcp_server``
or as a subcommand: ``graphify-plus mcp``.

The ``mcp`` SDK is an optional dependency installed via the ``[mcp]``
extra. We import it lazily so the module is safe to import without it
(useful for tooling that just inspects the manifest).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..core.symbol_graph import build as build_graph
from ..query.budget import frame_to_budget
from ..query.partition import compute_partition
from ..query.semantic import find as semantic_find
from ..runtime.store import Store, cache_path

# ---------- tool implementations (independent of the MCP SDK) -----------


def _open(repo: Path) -> Store:
    if not cache_path(repo).exists():
        raise RuntimeError(
            f"No cache at {cache_path(repo)}. Run 'graphify-plus init --repo {repo}' first."
        )
    return Store(cache_path(repo))


def tool_init(args: dict[str, Any]) -> dict[str, Any]:
    from ..core import cas
    from ..core.ingest import ingest
    from ..core.skeletonizer import skeletonize_all

    repo = Path(args["repo"]).resolve()
    parallel = bool(args.get("parallel", True))
    res = ingest(repo, parallel=parallel)
    skeletons = skeletonize_all(res.symbols)
    store = Store(cache_path(repo))
    try:
        store.replace_all(res.symbols, res.edges)
        store.set_meta("repo_root", str(repo))
        for sid, body in skeletons.items():
            h = cas.put(store, body)
            store.link_skeleton(sid, h)
    finally:
        store.close()
    return {
        "files_parsed": res.files_parsed,
        "files_skipped": res.files_skipped,
        "symbols": len(res.symbols),
        "edges": len(res.edges),
    }


def tool_context(args: dict[str, Any]) -> dict[str, Any]:
    repo = Path(args["repo"]).resolve()
    target = args["target"]
    entry = args.get("entry")
    max_tokens = int(args.get("max_tokens", 8000))
    store = _open(repo)
    try:
        target_id = target if store.get_symbol(target) else None
        if target_id is None:
            matches = store.find_symbols_by_qualified_name(target)
            if not matches:
                raise RuntimeError(f"No symbol matches: {target}")
            if len(matches) > 1:
                raise RuntimeError(
                    f"Ambiguous target {target!r} ({len(matches)} matches) — "
                    "use a fully qualified name."
                )
            target_id = matches[0]["id"]
        if entry:
            entry_syms = store.symbols_in_path(entry)
            if not entry_syms:
                raise RuntimeError(f"No symbols at entry {entry}")
            entry_id = entry_syms[0]["id"]
        else:
            same_path = store.symbols_in_path(store.get_symbol(target_id)["path"] or "")
            entry_id = next(
                (s["id"] for s in same_path if s.get("kind") == "module"),
                target_id,
            )
        G = build_graph(store.all_symbols(), store.all_edges())
        partition = compute_partition(G, entry_id, target_id)
        framed = frame_to_budget(partition, store, max_tokens=max_tokens)
    finally:
        store.close()
    return {
        "text": framed.text,
        "tokens": framed.tokens,
        "manifest_hashes": framed.manifest,
        "truncated": framed.truncated_count,
        "cut": framed.cut_count,
    }


def tool_skeleton(args: dict[str, Any]) -> dict[str, Any]:
    repo = Path(args["repo"]).resolve()
    store = _open(repo)
    try:
        if "file" in args:
            syms = store.symbols_in_path(args["file"])
            chunks = [b for s in syms if (b := store.get_skeleton_for_symbol(s["id"]))]
            return {"text": "\n\n".join(chunks)}
        target = args["target"]
        body = store.get_skeleton_for_symbol(target)
        if body:
            return {"text": body}
        matches = store.find_symbols_by_qualified_name(target)
        if not matches:
            raise RuntimeError(f"No symbol matches: {target}")
        body = store.get_skeleton_for_symbol(matches[0]["id"])
        return {"text": body or ""}
    finally:
        store.close()


def tool_find(args: dict[str, Any]) -> dict[str, Any]:
    repo = Path(args["repo"]).resolve()
    intent = args["intent"]
    top_k = int(args.get("top_k", 8))
    store = _open(repo)
    try:
        G = build_graph(store.all_symbols(), store.all_edges())
        matches = semantic_find(store, G, intent, top_k=top_k)
    finally:
        store.close()
    return {
        "matches": [
            {
                "qualified_name": m.symbol.get("qualified_name"),
                "kind": m.symbol.get("kind"),
                "path": m.symbol.get("path"),
                "score": round(m.score, 4),
                "community": m.community,
            }
            for m in matches
        ]
    }


def tool_simulate(args: dict[str, Any]) -> dict[str, Any]:
    from ..query.shadow import simulate
    from ..runtime.overlay import empty
    from .cli.safety_cmds import _load_ruleset

    repo = Path(args["repo"]).resolve()
    store = _open(repo)
    try:
        G = build_graph(store.all_symbols(), store.all_edges())
        overlay = empty(G)
        for spec in args.get("add_edge", []):
            kind = spec.get("kind", "calls")
            overlay.add_edge(spec["src"], spec["dst"], kind=kind)
        for spec in args.get("remove_edge", []):
            overlay.remove_edge(spec["src"], spec["dst"], kind=spec.get("kind"))
        result = simulate(overlay, _load_ruleset(repo))
    finally:
        store.close()
    return {
        "grade": result.grade,
        "summary": result.summary,
        "violations": [
            {
                "rule_id": v.rule_id,
                "severity": v.severity,
                "src": v.src,
                "dst": v.dst,
                "kind": v.kind,
                "message": v.message,
            }
            for v in result.violations
        ],
    }


def tool_guardrails(args: dict[str, Any]) -> dict[str, Any]:
    from ..query.guardrails import evaluate_payload
    from .cli.safety_cmds import _load_ruleset

    repo = Path(args["repo"]).resolve()
    store = _open(repo)
    try:
        G = build_graph(store.all_symbols(), store.all_edges())
        decision = evaluate_payload(store, G, args["payload"], _load_ruleset(repo))
    finally:
        store.close()
    return {
        "block": decision.block,
        "rule_id": decision.rule_id,
        "suggestion": decision.suggestion,
    }


def tool_blast_radius(args: dict[str, Any]) -> dict[str, Any]:
    from ..query.blast_radius import trace as blast_trace

    repo = Path(args["repo"]).resolve()
    store = _open(repo)
    try:
        G = build_graph(store.all_symbols(), store.all_edges())
        report = blast_trace(G, args["package"])
    finally:
        store.close()
    return {
        "package": report.package,
        "suggestion": report.suggestion,
        "hits": [
            {"qualified_name": h.qualified_name, "path": h.path, "depth": h.depth}
            for h in report.hits
        ],
    }


# ---------- registry ----------------------------------------------------


TOOLS: dict[str, Callable[[dict], dict]] = {
    "gp_init": tool_init,
    "gp_context": tool_context,
    "gp_skeleton": tool_skeleton,
    "gp_find": tool_find,
    "gp_simulate": tool_simulate,
    "gp_guardrails": tool_guardrails,
    "gp_blast_radius": tool_blast_radius,
}


def manifest() -> dict[str, Any]:
    """Static manifest written to ``<target>/.graphify_plus/mcp.json`` by
    ``gp init`` so MCP clients can autodiscover the tool surface."""
    return {
        "version": 1,
        "transport": "stdio",
        "tools": [
            {
                "name": name,
                "description": (fn.__doc__ or name).strip().splitlines()[0] if fn.__doc__ else name,
            }
            for name, fn in TOOLS.items()
        ],
    }


# ---------- stdio entry point -------------------------------------------


async def _serve_async() -> None:  # pragma: no cover — exercised only in production
    """Run the MCP server over stdio, requires ``mcp`` extra."""
    try:
        from mcp.server import Server
        from mcp.server.stdio import stdio_server
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "MCP SDK not installed. Install with: pip install 'graphify-plus[mcp]'"
        ) from e

    server = Server("graphify-plus")

    @server.list_tools()  # type: ignore[misc]
    async def _list_tools():
        from mcp.types import Tool

        return [
            Tool(
                name=name, description=(fn.__doc__ or name).strip(), inputSchema={"type": "object"}
            )
            for name, fn in TOOLS.items()
        ]

    @server.call_tool()  # type: ignore[misc]
    async def _call_tool(name: str, arguments: dict):
        from mcp.types import TextContent

        if name not in TOOLS:
            return [TextContent(type="text", text=json.dumps({"error": f"unknown tool {name}"}))]
        try:
            result = TOOLS[name](arguments or {})
        except Exception as e:  # noqa: BLE001
            return [TextContent(type="text", text=json.dumps({"error": str(e)}))]
        return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]

    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main() -> int:  # pragma: no cover
    import asyncio

    asyncio.run(_serve_async())
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "TOOLS",
    "manifest",
    "tool_init",
    "tool_context",
    "tool_skeleton",
    "tool_find",
    "tool_simulate",
    "tool_guardrails",
    "tool_blast_radius",
]
