from pathlib import Path

from graphify_plus.core.adapters import PythonAdapter


def parse(src: str):
    return PythonAdapter().parse(Path("a.py"), src.encode())


def test_module_function_class_method():
    syms, edges = parse(
        "def hello(x: int) -> int:\n"
        '    """Greet."""\n'
        "    return x + 1\n"
        "\n"
        "class Foo(Base):\n"
        "    def bar(self):\n"
        "        return hello(1)\n"
    )
    by_qname = {s["qualified_name"]: s for s in syms}
    assert "a" in by_qname and by_qname["a"]["kind"] == "module"
    assert "a.hello" in by_qname and by_qname["a.hello"]["kind"] == "function"
    assert by_qname["a.hello"]["signature"].startswith("def hello(")
    assert "a.Foo" in by_qname and by_qname["a.Foo"]["kind"] == "class"
    assert by_qname["a.Foo"]["signature"] == "class Foo(Base)"
    assert "a.Foo.bar" in by_qname and by_qname["a.Foo.bar"]["kind"] == "method"

    # extends edge from Foo -> Base (unresolved by name)
    assert any(e.get("kind") == "extends" and e.get("dst") == "Base" for e in edges)
    # contains edge from class to method
    assert any(
        e.get("kind") == "contains" and e.get("dst") == by_qname["a.Foo.bar"]["id"] for e in edges
    )


def test_imports_and_dunder_all():
    syms, edges = parse(
        "from foo import bar\n"
        "import baz\n"
        "__all__ = ['hello']\n"
        "def hello(): pass\n"
        "def _private(): pass\n"
    )
    by_qname = {s["qualified_name"]: s for s in syms}
    assert any(e.get("kind") == "imports" for e in edges)
    assert by_qname["a.hello"]["exported"] is True
    assert by_qname["a._private"]["exported"] is False


def test_deterministic_ids():
    s1, _ = parse("def foo(): pass\n")
    s2, _ = parse("def foo(): pass\n")
    assert {s["id"] for s in s1} == {s["id"] for s in s2}


def test_signature_strips_whitespace():
    syms, _ = parse("def f(\n    a: int,\n    b: int,\n) -> int:\n    return a + b\n")
    f = next(s for s in syms if s["qualified_name"] == "a.f")
    # multi-line params collapsed to single line
    assert "\n" not in f["signature"]
    assert f["signature"].startswith("def f(")
