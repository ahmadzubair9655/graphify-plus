from __future__ import annotations

import json
from pathlib import Path

import pytest

from graphify_plus.interface.errors import (
    CacheCorrupt,
    CacheLocked,
    GraphifyError,
    InternalError,
    handle,
    render_for_stderr,
    require_cache,
    write_debug,
)


def test_subclass_codes_and_exits_are_unique():
    seen_codes: set[str] = set()
    seen_exits: set[int] = set()
    for cls in (
        CacheCorrupt,
        CacheLocked,
        InternalError,
    ):
        assert cls.code not in seen_codes
        assert cls.exit_code not in seen_exits
        seen_codes.add(cls.code)
        seen_exits.add(cls.exit_code)


def test_render_includes_code_message_and_remediation():
    err = CacheCorrupt(
        "cache.db quick_check failed",
        remediation="Run 'graphify-plus init --force --repo /tmp/x'.",
        context={"path": "/tmp/x/.graphify_plus/cache.db"},
    )
    out = render_for_stderr(err)
    assert "GP-CACHE-CORRUPT" in out
    assert "What to try next:" in out
    assert "Run 'graphify-plus init --force --repo /tmp/x'." in out


def test_handle_known_error_returns_exit_code(tmp_path: Path, capsys):
    err = CacheLocked(
        "another writer present",
        context={"holder_pid": 12345},
    )
    code = handle(err, repo=tmp_path)
    captured = capsys.readouterr()
    assert "GP-CACHE-LOCKED" in captured.err
    assert code == CacheLocked.exit_code


def test_handle_writes_debug_log(tmp_path: Path, capsys):
    err = CacheCorrupt("boom", context={"k": "v"})
    handle(err, repo=tmp_path)
    log_path = tmp_path / ".graphify_plus" / "debug.log"
    assert log_path.exists()
    last = log_path.read_text().strip().splitlines()[-1]
    payload = json.loads(last)
    assert payload["code"] == "GP-CACHE-CORRUPT"
    assert payload["message"] == "boom"
    assert payload["context"] == {"k": "v"}


def test_handle_unknown_exception_wraps_as_internal(tmp_path: Path, capsys):
    try:
        raise RuntimeError("kaboom")
    except RuntimeError as e:
        code = handle(e, repo=tmp_path)
    captured = capsys.readouterr()
    assert "GP-INTERNAL-ERROR" in captured.err
    assert "kaboom" in captured.err
    assert code == InternalError.exit_code


def test_handle_debug_emits_traceback(tmp_path: Path, capsys):
    try:
        raise RuntimeError("kaboom")
    except RuntimeError as e:
        handle(e, repo=tmp_path, debug=True)
    captured = capsys.readouterr()
    assert "Traceback" in captured.err


def test_require_cache_raises_when_absent(tmp_path: Path):
    with pytest.raises(GraphifyError) as exc:
        require_cache(tmp_path / "missing")
    assert "init" in exc.value.remediation


def test_require_cache_passes_when_present(tmp_path: Path):
    (tmp_path / ".graphify_plus").mkdir()
    (tmp_path / ".graphify_plus" / "cache.db").write_text("")
    require_cache(tmp_path)  # no exception


def test_write_debug_is_best_effort_on_oserror(tmp_path: Path, monkeypatch):
    # If the directory cannot be created, write_debug must NOT raise.
    monkeypatch.setattr(Path, "mkdir", lambda *a, **kw: (_ for _ in ()).throw(OSError()))
    write_debug(tmp_path, {"code": "X"})
