"""Same-file `references` edge extraction for the TS/JS adapter."""

from __future__ import annotations

from pathlib import Path

from graphify_plus.core.adapters import TypeScriptAdapter
from graphify_plus.core.symbol_graph import build as build_graph
from graphify_plus.query.prune import find_dead_code


def _parse(src: bytes, name: str = "p.ts"):
    return TypeScriptAdapter().parse(Path(name), src)


def _refs_pairs(symbols, edges):
    by_id = {s["id"]: s["qualified_name"] for s in symbols}
    return {
        (by_id.get(e["src"], e["src"]), by_id.get(e["dst"], e["dst"]))
        for e in edges
        if e.get("kind") == "references"
    }


def _calls_pairs(symbols, edges):
    by_id = {s["id"]: s["qualified_name"] for s in symbols}
    return {
        (by_id.get(e["src"], e["src"]), by_id.get(e["dst"], e["dst"]))
        for e in edges
        if e.get("kind") == "calls"
    }


def test_function_reference_inside_array_literal():
    """The AudienceSplitter pattern: functions stored in a const tuple."""
    syms, edges = _parse(
        b"function A() { return null; }\n"
        b"function B() { return null; }\n"
        b"const SCENES = [A, B] as const;\n"
    )
    pairs = _refs_pairs(syms, edges)
    assert ("p", "p.A") in pairs
    assert ("p", "p.B") in pairs


def test_function_reference_inside_object_literal_value():
    syms, edges = _parse(
        b"function handleClick() {}\n"
        b"const handlers = { onClick: handleClick };\n"
    )
    pairs = _refs_pairs(syms, edges)
    assert ("p", "p.handleClick") in pairs


def test_function_passed_as_argument_emits_reference():
    syms, edges = _parse(
        b"function callback() {}\n"
        b"function subscribe(cb: () => void) { cb(); }\n"
        b"function init() { subscribe(callback); }\n"
    )
    # `subscribe(callback)` — `subscribe` is the call, `callback` is the
    # argument and should produce a `references` edge.
    pairs = _refs_pairs(syms, edges)
    assert ("p.init", "p.callback") in pairs
    # And the call itself still produces a `calls` edge.
    assert ("p.init", "p.subscribe") in _calls_pairs(syms, edges)


def test_function_returned_from_another_function():
    syms, edges = _parse(
        b"function helper() {}\n"
        b"function getHelper() { return helper; }\n"
    )
    assert ("p.getHelper", "p.helper") in _refs_pairs(syms, edges)


def test_call_expression_does_not_double_emit_reference():
    """`foo()` should emit a `calls` edge but NOT a `references` edge for
    the same identifier — that would double-count and confuse audit."""
    syms, edges = _parse(
        b"function foo() {}\n"
        b"function caller() { return foo(); }\n"
    )
    refs = _refs_pairs(syms, edges)
    calls = _calls_pairs(syms, edges)
    assert ("p.caller", "p.foo") in calls
    assert ("p.caller", "p.foo") not in refs


def test_import_statement_does_not_emit_self_reference():
    """`import { foo } from "./x"` introduces `foo` as a local binding —
    that's a declaration, not a use. No references edge."""
    syms, edges = _parse(
        b"import { foo } from './x';\n"
        b"function alive() {}\n"
    )
    pairs = _refs_pairs(syms, edges)
    # `foo` shouldn't reference any local symbol (it's an import binding,
    # and there's no same-file `foo` to resolve to anyway).
    assert not any(dst.endswith(".foo") for _, dst in pairs)


def test_function_declaration_name_does_not_emit_self_reference():
    """The `helper` identifier in `function helper() {...}` is the symbol
    being declared, not a reference to itself."""
    syms, edges = _parse(
        b"function helper() { return 1; }\n"
    )
    pairs = _refs_pairs(syms, edges)
    assert ("p", "p.helper") not in pairs
    assert ("p.helper", "p.helper") not in pairs


def test_member_expression_property_is_not_a_reference():
    """In `obj.prop`, `prop` is property access — only `obj` (the
    leftmost identifier of the chain) counts as a same-file reference."""
    syms, edges = _parse(
        b"const obj = { prop: 1 };\n"
        b"function read() { return obj.prop; }\n"
    )
    # `obj` should be referenced (resolves to the const). `prop` should
    # not produce a phantom edge to anything named `prop` in this file.
    pairs = _refs_pairs(syms, edges)
    # No same-file `prop` symbol exists, but verify we don't even try
    # to resolve property names.
    assert not any(dst.endswith(".prop") for _, dst in pairs)


def test_prune_rescues_function_used_only_as_value():
    """Integration: a fixture with one truly-dead function and one
    function used only as an array element. After references extraction,
    only the truly-dead one should be flagged."""
    syms, edges = _parse(
        b"function rescuedByRef() { return null; }\n"
        b"function genuinelyDead() { return null; }\n"
        b"const ARR = [rescuedByRef];\n"
        b"export function useArr() { return ARR; }\n",
        name="p.tsx",
    )
    G = build_graph(syms, edges)
    dead = find_dead_code(G)
    by_qname = {s["qualified_name"]: s["id"] for s in syms}
    assert by_qname["p.genuinelyDead"] in dead
    assert by_qname["p.rescuedByRef"] not in dead


def test_references_edge_carries_lower_confidence_than_calls():
    syms, edges = _parse(
        b"function helper() {}\n"
        b"const ARR = [helper];\n"
    )
    refs = [e for e in edges if e.get("kind") == "references"]
    assert refs
    for e in refs:
        assert e.get("confidence") == 0.5
        assert e.get("confidence_score") == 0.5
