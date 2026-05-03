from pathlib import Path

from graphify_plus.core.adapters import JavaScriptAdapter, TypeScriptAdapter


def test_typescript_function_class_export():
    syms, edges = TypeScriptAdapter().parse(
        Path("b.ts"),
        b"export function add(a: number, b: number): number { return a + b; }\n"
        b"export class Foo { method(): void {} }\n"
        b"import { x } from './y';\n",
    )
    by_qname = {s["qualified_name"]: s for s in syms}
    assert "b" in by_qname and by_qname["b"]["kind"] == "module"
    assert by_qname["b.add"]["kind"] == "function"
    assert by_qname["b.add"]["exported"] is True
    assert by_qname["b.Foo"]["kind"] == "class"
    assert by_qname["b.Foo.method"]["kind"] == "method"
    assert any(e.get("kind") == "imports" for e in edges)


def test_javascript_arrow_const():
    syms, _ = JavaScriptAdapter().parse(
        Path("c.js"),
        b"export const greet = (name) => 'hi ' + name;\n",
    )
    by_qname = {s["qualified_name"]: s for s in syms}
    assert "c.greet" in by_qname
    assert by_qname["c.greet"]["kind"] == "function"
