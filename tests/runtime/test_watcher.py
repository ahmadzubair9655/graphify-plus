"""Live watcher tests.

Synchronisation rule: NEVER use ``time.sleep`` for watcher coordination.
Use ``Watcher.wait_for_path`` (threading.Event-backed) — the watcher
raises that event after the file's update has been fully applied.
"""

from __future__ import annotations

from pathlib import Path

from graphify_plus.core import cas
from graphify_plus.core.ingest import ingest
from graphify_plus.core.skeletonizer import skeletonize_all
from graphify_plus.runtime.store import Store, cache_path
from graphify_plus.runtime.watcher import Watcher


def _seed(repo: Path) -> None:
    """Run an initial ingest so the cache reflects the on-disk state."""
    res = ingest(repo, parallel=False)
    skeletons = skeletonize_all(res.symbols)
    s = Store(cache_path(repo))
    try:
        s.replace_all(res.symbols, res.edges)
        for sid, body in skeletons.items():
            h = cas.put(s, body)
            s.link_skeleton(sid, h)
    finally:
        s.close()


def test_watcher_picks_up_modification(tmp_path: Path):
    src = tmp_path / "a.py"
    src.write_text("def foo():\n    return 1\n")
    _seed(tmp_path)

    w = Watcher(tmp_path)
    updates: list[str] = []
    w.subscribe(lambda u: updates.append(u.path))
    w.start()
    try:
        # Edit: add a new function.
        src.write_text("def foo():\n    return 1\n\ndef bar():\n    return 2\n")
        assert w.wait_for_path("a.py", timeout=5.0), "watcher did not deliver event"
    finally:
        w.stop()

    assert "a.py" in updates
    s = Store(cache_path(tmp_path))
    try:
        qnames = {x["qualified_name"] for x in s.all_symbols()}
    finally:
        s.close()
    assert "a.foo" in qnames
    assert "a.bar" in qnames


def test_watcher_handles_deletion(tmp_path: Path):
    src = tmp_path / "doomed.py"
    src.write_text("def gone():\n    return 0\n")
    _seed(tmp_path)

    w = Watcher(tmp_path)
    w.start()
    try:
        src.unlink()
        assert w.wait_for_path("doomed.py", timeout=5.0)
    finally:
        w.stop()

    s = Store(cache_path(tmp_path))
    try:
        qnames = {x["qualified_name"] for x in s.all_symbols()}
    finally:
        s.close()
    assert "doomed.gone" not in qnames
    assert "doomed" not in qnames  # module symbol gone too


def test_watcher_debounces_rapid_writes(tmp_path: Path):
    src = tmp_path / "noisy.py"
    src.write_text("def a():\n    return 1\n")
    _seed(tmp_path)

    w = Watcher(tmp_path)
    count = {"n": 0}
    w.subscribe(lambda _u: count.__setitem__("n", count["n"] + 1))
    w.start()
    try:
        # Rapid burst of writes — debounce should coalesce.
        for i in range(5):
            src.write_text(f"def a():\n    return {i}\n")
        assert w.wait_for_path("noisy.py", timeout=5.0)
    finally:
        w.stop()

    # Debouncer should have collapsed the burst to ~1 callback.
    # (May fire 1–2 times depending on filesystem timing, but never 5.)
    assert 1 <= count["n"] <= 2
