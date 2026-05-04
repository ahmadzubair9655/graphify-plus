"""Same-file `calls` edge extraction for the TS/JS adapter."""

from __future__ import annotations

from pathlib import Path

from graphify_plus.core.adapters import JavaScriptAdapter, TypeScriptAdapter
from graphify_plus.core.symbol_graph import build as build_graph
from graphify_plus.query.prune import find_dead_code


def _parse_ts(src: bytes, name: str = "p.ts"):
    return TypeScriptAdapter().parse(Path(name), src)


def _parse_tsx(src: bytes):
    return TypeScriptAdapter().parse(Path("p.tsx"), src)


def _calls_pairs(symbols, edges):
    by_id = {s["id"]: s["qualified_name"] for s in symbols}
    return {
        (by_id.get(e["src"], e["src"]), by_id.get(e["dst"], e["dst"]))
        for e in edges
        if e.get("kind") == "calls"
    }


def test_direct_call_resolves_within_same_file():
    syms, edges = _parse_ts(
        b"function helper(x: number): number { return x * 2; }\n"
        b"function caller(): number { return helper(3); }\n"
    )
    assert ("p.caller", "p.helper") in _calls_pairs(syms, edges)


def test_forward_reference_call_resolves():
    """JS hoisting: a function declared later in source order is callable
    from above (function declarations are hoisted). The resolver should
    find it regardless of source order."""
    syms, edges = _parse_ts(
        b"function caller(): number { return helper(3); }\n"
        b"function helper(x: number): number { return x * 2; }\n"
    )
    assert ("p.caller", "p.helper") in _calls_pairs(syms, edges)


def test_member_expression_call_resolves_when_object_is_a_known_symbol():
    """`Ns.helper()` where `Ns` is itself a same-file class (so `Ns.helper`
    qualifies as a known qname). Object literals (`const obj = {fn:...}`)
    are intentionally not added to the symbol map and remain unresolved."""
    syms, edges = _parse_ts(
        b"class Ns { static helper(): number { return 1; } }\n"
        b"function caller(): number { return Ns.helper(); }\n"
    )
    pairs = _calls_pairs(syms, edges)
    assert ("p.caller", "p.Ns.helper") in pairs


def test_call_inside_template_literal_substitution_resolves():
    syms, edges = _parse_ts(
        b"function fmt(x: number): string { return String(x); }\n"
        b"function caller(): string { return `value=${fmt(3)}`; }\n"
    )
    assert ("p.caller", "p.fmt") in _calls_pairs(syms, edges)


def test_call_inside_jsx_expression_container_resolves():
    syms, edges = _parse_tsx(
        b"function fmt(x: number): string { return String(x); }\n"
        b"function App(): JSX.Element { return <div>{fmt(3)}</div>; }\n"
    )
    assert ("p.App", "p.fmt") in _calls_pairs(syms, edges)


def test_unresolved_cross_module_call_does_not_emit_false_edge():
    """Imported `parseInt` is NOT a same-file symbol — the resolver must
    not invent an edge to a coincidentally-named local symbol if any
    existed. With no local `parseInt`, no calls edge is emitted at all."""
    syms, edges = _parse_ts(
        b"import { parseInt } from './elsewhere';\n"
        b"function caller(): number { return parseInt('3'); }\n"
    )
    pairs = _calls_pairs(syms, edges)
    # No same-file `parseInt` symbol exists; resolver must not fabricate one.
    assert not any(dst.endswith(".parseInt") for _, dst in pairs)


def test_prune_no_longer_flags_locally_called_helper():
    """Integration: a fixture with one truly dead module-level function
    and one locally-called helper. After calls extraction, only the
    truly-dead one should appear in the dead set."""
    syms, edges = _parse_ts(
        b"function helper(x: number): number { return x * 2; }\n"
        b"function caller(): number { return helper(3); }\n"
        b"function genuinelyDead(): number { return 99; }\n"
    )
    G = build_graph(syms, edges)
    dead = find_dead_code(G)
    by_qname = {s["qualified_name"]: s["id"] for s in syms}
    assert by_qname["p.genuinelyDead"] in dead
    assert by_qname["p.helper"] not in dead
    assert by_qname["p.caller"] not in dead


def test_javascript_files_get_calls_extraction_too():
    syms, edges = JavaScriptAdapter().parse(
        Path("p.js"),
        b"function helper(x) { return x * 2; }\n"
        b"function caller() { return helper(3); }\n",
    )
    assert ("p.caller", "p.helper") in _calls_pairs(syms, edges)


def test_calls_edge_carries_confidence_score_attribute():
    """The audit's confidence_drift probe reads `confidence_score`. We
    write it alongside the existing numeric `confidence` field so future
    work that promotes adapter edges to INFERRED can grade against it."""
    syms, edges = _parse_ts(
        b"function helper(): number { return 1; }\n"
        b"function caller(): number { return helper(); }\n"
    )
    calls = [e for e in edges if e.get("kind") == "calls"]
    assert calls
    for e in calls:
        assert e.get("confidence") == 0.7
        assert e.get("confidence_score") == 0.7
