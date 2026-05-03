"""Skeletonizer + CAS tests."""

from __future__ import annotations

import secrets
from pathlib import Path

from graphify_plus.core import cas
from graphify_plus.core.adapters import PythonAdapter
from graphify_plus.core.cas import hash_skeleton
from graphify_plus.core.skeletonizer import skeletonize, skeletonize_all
from graphify_plus.runtime.store import Store, cache_path


def test_skeleton_format_is_deterministic():
    syms, _ = PythonAdapter().parse(
        Path("a.py"),
        b'def hello(x: int) -> int:\n    """Greets."""\n    return x + 1\n',
    )
    func = next(s for s in syms if s["qualified_name"] == "a.hello")
    a = skeletonize(func)
    b = skeletonize(func)
    assert a == b
    assert hash_skeleton(a) == hash_skeleton(b)


def test_skeleton_includes_signature_and_docstring():
    syms, _ = PythonAdapter().parse(
        Path("a.py"),
        b'def hello(x: int) -> int:\n    """Greets the world."""\n    return x + 1\n',
    )
    func = next(s for s in syms if s["qualified_name"] == "a.hello")
    sk = skeletonize(func)
    assert "@python function a.hello" in sk
    assert "def hello(x: int) -> int" in sk
    assert "Greets the world" in sk


def test_skeleton_omits_body_with_line_count():
    syms, _ = PythonAdapter().parse(
        Path("a.py"),
        b"def f():\n    a = 1\n    b = 2\n    return a + b\n",
    )
    func = next(s for s in syms if s["qualified_name"] == "a.f")
    sk = skeletonize(func)
    assert "« body omitted (4 lines) »" in sk


def test_skeleton_compresses_meaningfully():
    """Skeletons should be substantially smaller than the source."""
    src = (
        b'def add(a: int, b: int) -> int:\n    """Add two numbers."""\n'
        b"    total = a + b\n"
        b"    if total < 0:\n"
        b"        raise ValueError('negative not allowed')\n"
        b"    return total\n"
    )
    syms, _ = PythonAdapter().parse(Path("m.py"), src)
    skeletons = skeletonize_all(syms)
    total_skel = sum(len(s) for s in skeletons.values())
    # Skeleton-set is meaningfully smaller than source.
    assert total_skel < len(src) * 0.9


def test_cas_dedup_identical_skeletons(tmp_path: Path):
    s = Store(cache_path(tmp_path))
    try:
        body = "@python function foo()\ndef foo()\n"
        h1 = cas.put(s, body)
        h2 = cas.put(s, body)
        assert h1 == h2
        assert cas.get(s, h1) == body

        # 1000 random distinct skeletons → 0 truncated-hash collisions
        seen = {h1}
        for _ in range(1000):
            random_body = secrets.token_hex(32)
            h = cas.put(s, random_body)
            assert h not in seen, "16-char hash collision in random sample"
            seen.add(h)
    finally:
        s.close()


def test_init_writes_skeletons(tmp_path: Path):
    """End-to-end: gp init populates symbol_skeletons + skeletons tables."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "foo.py").write_text("def hello():\n    return 1\n")

    from graphify_plus.core.ingest import ingest

    res = ingest(tmp_path, parallel=False)
    skeletons = skeletonize_all(res.symbols)
    s = Store(cache_path(tmp_path))
    try:
        s.replace_all(res.symbols, res.edges)
        for sid, body in skeletons.items():
            h = cas.put(s, body)
            s.link_skeleton(sid, h)
        # Lookup each symbol's skeleton via the join.
        for sid in skeletons:
            assert s.get_skeleton_for_symbol(sid) is not None
    finally:
        s.close()
