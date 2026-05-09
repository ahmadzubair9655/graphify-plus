"""Tests for the plugin architecture (Layer 11.2)."""

from __future__ import annotations

from click.testing import CliRunner

from graphify_plus.daemon.plugins import (
    PluginRegistry,
    discover,
    get_registry,
    reset_registry_for_tests,
)
from graphify_plus.interface.cli.daemon_cmd import daemon_cmd


def test_register_handler_records_metadata() -> None:
    reg = PluginRegistry()
    reg._current = "myplugin"
    reg.register_handler("custom_op", lambda g, a: {"results": [], "more_available": 0})
    assert "custom_op" in reg.handlers
    assert "handler:custom_op" in reg.metadata["myplugin"]


def test_register_ingestor_records_metadata() -> None:
    reg = PluginRegistry()
    reg._current = "myplugin"
    reg.register_ingestor("widgets", lambda **_kw: {"ok": True})
    assert "widgets" in reg.ingestors
    assert "ingestor:widgets" in reg.metadata["myplugin"]


def test_first_registration_wins() -> None:
    reg = PluginRegistry()
    reg._current = "first"
    reg.register_handler("op", lambda g, a: {"results": ["first"]})
    reg._current = "second"
    reg.register_handler("op", lambda g, a: {"results": ["second"]})
    assert reg.handlers["op"](None, {})["results"] == ["first"]


def test_discover_no_entry_points_returns_empty_registry(monkeypatch) -> None:
    """When no plugins are installed under the entry-point group,
    discover() returns an empty registry without raising."""

    class _FakeEntryPoints:
        def __iter__(self):
            return iter([])

    def _fake_eps(*args, **kwargs):
        return _FakeEntryPoints()

    monkeypatch.setattr("graphify_plus.daemon.plugins.entry_points", _fake_eps)
    reset_registry_for_tests(None)
    reg = discover()
    assert reg.handlers == {}
    assert reg.ingestors == {}


def test_discover_swallows_broken_plugin(monkeypatch) -> None:
    class _Bad:
        name = "bad"

        def load(self):
            raise RuntimeError("boom")

    class _Good:
        name = "good"

        def load(self):
            def register(reg):
                reg.register_handler(
                    "good_op",
                    lambda g, a: {"results": [], "more_available": 0},
                )

            return register

    class _FakeEPs:
        def __iter__(self):
            return iter([_Bad(), _Good()])

    monkeypatch.setattr(
        "graphify_plus.daemon.plugins.entry_points", lambda *a, **kw: _FakeEPs()
    )
    reset_registry_for_tests(None)
    reg = discover()
    assert "good_op" in reg.handlers
    # Bad plugin doesn't crash discovery.


def test_plugin_handler_merged_into_registry() -> None:
    """The plugin's handler is reachable from the daemon's HANDLERS
    after the merge step at startup."""
    fake = PluginRegistry()
    fake.handlers["plugin_op"] = lambda g, a: {"results": ["plugin"], "more_available": 0}
    reset_registry_for_tests(fake)

    # Re-import handlers to trigger the merge.
    import importlib

    import graphify_plus.daemon.handlers as h

    importlib.reload(h)
    try:
        assert "plugin_op" in h.HANDLERS
    finally:
        reset_registry_for_tests(None)
        importlib.reload(h)


def test_built_in_handlers_win_over_plugins() -> None:
    fake = PluginRegistry()
    fake.handlers["whats_in"] = lambda g, a: {"results": ["plugin override"]}
    reset_registry_for_tests(fake)

    import importlib

    import graphify_plus.daemon.handlers as h

    importlib.reload(h)
    try:
        # The built-in is unchanged: it returns rows with structure, not
        # the plugin sentinel.
        assert h.HANDLERS["whats_in"] is h.whats_in
    finally:
        reset_registry_for_tests(None)
        importlib.reload(h)


def test_cli_plugin_list_no_plugins() -> None:
    reset_registry_for_tests(PluginRegistry())
    runner = CliRunner()
    result = runner.invoke(daemon_cmd, ["plugin", "list"])
    assert result.exit_code == 0, result.output
    assert "no plugins" in result.output
    reset_registry_for_tests(None)


def test_cli_plugin_list_with_plugins() -> None:
    reg = PluginRegistry()
    reg._current = "smart_energie"
    reg.register_handler("boiler_for", lambda g, a: {"results": [], "more_available": 0})
    reset_registry_for_tests(reg)
    runner = CliRunner()
    result = runner.invoke(daemon_cmd, ["plugin", "list"])
    assert result.exit_code == 0, result.output
    assert "smart_energie" in result.output
    assert "boiler_for" in result.output
    reset_registry_for_tests(None)
