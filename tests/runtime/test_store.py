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


def test_replace_all_dedupes_duplicate_symbol_ids(tmp_path: Path, caplog):
    syms = [
        {"id": "dup", "kind": "function", "name": "foo", "path": "a.ts", "span": (1, 1)},
        {"id": "unique", "kind": "function", "name": "bar", "path": "a.ts", "span": (2, 2)},
        {"id": "dup", "kind": "function", "name": "foo_v2", "path": "a.ts", "span": (3, 3)},
    ]
    s = Store(cache_path(tmp_path))
    try:
        with caplog.at_level("WARNING", logger="graphify_plus.runtime.store"):
            s.replace_all(syms, [])  # type: ignore[arg-type]
        ids = {x["id"] for x in s.all_symbols()}
        assert ids == {"dup", "unique"}
        # last-write-wins
        assert s.get_symbol("dup")["name"] == "foo_v2"  # type: ignore[index]
        assert any("duplicate symbol id" in r.message for r in caplog.records)
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
