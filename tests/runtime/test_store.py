from pathlib import Path

from graphify_plus.core.adapters import PythonAdapter
from graphify_plus.runtime.store import Store, cache_path


def test_store_roundtrip(tmp_path: Path):
    syms, edges = PythonAdapter().parse(
        Path("a.py"),
        b"def foo(): pass\nclass C:\n    def m(self): pass\n",
    )
    s = Store(cache_path(tmp_path))
    try:
        s.replace_all(syms, edges)
        assert {x["id"] for x in s.all_symbols()} == {x["id"] for x in syms}
        first = syms[0]
        assert s.get_symbol(first["id"]) == first
    finally:
        s.close()


def test_store_meta(tmp_path: Path):
    s = Store(cache_path(tmp_path))
    try:
        assert s.get_meta("foo") is None
        s.set_meta("foo", "bar")
        assert s.get_meta("foo") == "bar"
        s.set_meta("foo", "baz")
        assert s.get_meta("foo") == "baz"
    finally:
        s.close()


def test_skeleton_table(tmp_path: Path):
    s = Store(cache_path(tmp_path))
    try:
        s.put_skeleton("abc123", "@python function foo()")
        assert s.get_skeleton("abc123") == "@python function foo()"
        assert s.get_skeleton("missing") is None
    finally:
        s.close()
