"""JSX-internal symbol tracing tests for the TS/JS adapter."""

from __future__ import annotations

from pathlib import Path

import networkx as nx

from graphify_plus.core.adapters import JavaScriptAdapter, TypeScriptAdapter
from graphify_plus.core.symbol_graph import build as build_graph
from graphify_plus.query.prune import find_dead_code


def _parse_tsx(src: bytes) -> tuple[list, list]:
    return TypeScriptAdapter().parse(Path("p.tsx"), src)


def _parse_jsx(src: bytes) -> tuple[list, list]:
    return JavaScriptAdapter().parse(Path("p.jsx"), src)


def _jsx_edges(edges):
    return [e for e in edges if e.get("kind") == "jsx_render"]


def _qname_edges(symbols, edges):
    by_id = {s["id"]: s["qualified_name"] for s in symbols}
    return {(by_id.get(e["src"], e["src"]), by_id.get(e["dst"], e["dst"])) for e in edges}


def test_jsx_simple_identifier_resolves_to_local_function():
    syms, edges = _parse_tsx(
        b"function Helper() { return null; }\n"
        b"function App() { return <Helper/>; }\n"
    )
    pairs = _qname_edges(syms, _jsx_edges(edges))
    assert ("p.App", "p.Helper") in pairs


def test_jsx_member_expression_resolves_to_nested_symbol():
    syms, edges = _parse_tsx(
        b"class Card { static Header() { return null; } }\n"
        b"function App() { return <Card.Header/>; }\n"
    )
    pairs = _qname_edges(syms, _jsx_edges(edges))
    assert ("p.App", "p.Card.Header") in pairs


def test_jsx_arrow_function_component_is_resolved():
    syms, edges = _parse_tsx(
        b"const Greeting = () => null;\n"
        b"function App() { return <Greeting/>; }\n"
    )
    pairs = _qname_edges(syms, _jsx_edges(edges))
    assert ("p.App", "p.Greeting") in pairs


def test_jsx_nested_component_inside_parent_resolves():
    """`function Child` defined inside `function Parent` should resolve when
    rendered as <Child/> inside Parent's body."""
    syms, edges = _parse_tsx(
        b"function Parent() {\n"
        b"  function Child() { return null; }\n"
        b"  return <Child/>;\n"
        b"}\n"
    )
    qnames = {s["qualified_name"] for s in syms}
    assert "p.Parent.Child" in qnames
    pairs = _qname_edges(syms, _jsx_edges(edges))
    assert ("p.Parent", "p.Parent.Child") in pairs


def test_jsx_unused_local_helper_is_still_dead():
    """Negative case: a helper that is declared but never rendered or called
    must still be flagged as dead code."""
    syms, edges = _parse_tsx(
        b"function GenuinelyDead() { return null; }\n"
        b"function App() { return null; }\n"
    )
    G = build_graph(syms, edges)
    dead = find_dead_code(G)
    by_qname_to_id = {s["qualified_name"]: s["id"] for s in syms}
    assert by_qname_to_id["p.GenuinelyDead"] in dead


def test_jsx_render_edge_keeps_component_alive_in_prune():
    syms, edges = _parse_tsx(
        b"function Helper() { return null; }\n"
        b"function App() { return <Helper/>; }\n"
    )
    G = build_graph(syms, edges)
    dead = find_dead_code(G)
    by_qname_to_id = {s["qualified_name"]: s["id"] for s in syms}
    # Helper is rendered by App via JSX → must NOT be flagged as dead.
    assert by_qname_to_id["p.Helper"] not in dead


def test_jsx_host_elements_do_not_emit_edges():
    """Lowercase tags like <div/> must never resolve to anything."""
    _, edges = _parse_tsx(b"function App() { return <div/>; }\n")
    assert _jsx_edges(edges) == []


def test_jsx_works_for_javascript_files_too():
    syms, edges = _parse_jsx(
        b"function Helper() { return null; }\n"
        b"function App() { return <Helper/>; }\n"
    )
    pairs = _qname_edges(syms, _jsx_edges(edges))
    assert ("p.App", "p.Helper") in pairs


def test_pure_ts_file_without_jsx_is_unaffected():
    """A .ts file with no JSX should produce zero jsx_render edges."""
    _, edges = TypeScriptAdapter().parse(
        Path("plain.ts"),
        b"export function add(a: number, b: number): number { return a + b; }\n",
    )
    assert _jsx_edges(edges) == []
