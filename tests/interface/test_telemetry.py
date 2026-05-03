"""11.8 — telemetry is opt-in and never raises."""

from __future__ import annotations

from graphify_plus.interface import telemetry


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("GRAPHIFY_TELEMETRY_URL", raising=False)
    assert not telemetry.is_enabled()
    # Calling emit with no env should be a no-op.
    telemetry.emit("init", duration_ms=10, file_count=5)


def test_emit_swallows_errors(monkeypatch):
    monkeypatch.setenv("GRAPHIFY_TELEMETRY_URL", "http://127.0.0.1:1/never-listens")
    monkeypatch.setenv("GP_DEBUG", "0")
    # Should not raise even though the endpoint won't connect.
    telemetry.emit("init", duration_ms=10, file_count=5)


def test_bucket_thresholds():
    assert telemetry._bucket(0) == "<=10"
    assert telemetry._bucket(50) == "<=100"
    assert telemetry._bucket(500_000) == ">100000"
