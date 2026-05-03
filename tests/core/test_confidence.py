"""11.2 — confidence layer: every adapter emits ``confidence`` per edge."""

from __future__ import annotations

from pathlib import Path

from graphify_plus.core.adapters.base import (
    CONF_EXACT,
    CONF_FALLBACK,
    CONF_RESOLVED,
)
from graphify_plus.core.adapters.python_adapter import PythonAdapter
from graphify_plus.core.adapters.typescript_adapter import TypeScriptAdapter
from graphify_plus.core.adapters.go_adapter import GoAdapter


def _confidences_by_kind(edges):
    out: dict[str, set[float]] = {}
    for e in edges:
        out.setdefault(e["kind"], set()).add(e.get("confidence"))
    return out


def test_python_adapter_emits_confidence_per_edge_kind():
    src = b"""
import os
from foo import bar

class Cat(Animal):
    def meow(self):
        return os.path.join("a", "b")

__all__ = ["Cat"]
"""
    syms, edges = PythonAdapter().parse(Path("cat.py"), src)
    assert all("confidence" in e for e in edges), "every edge must carry confidence"
    by_kind = _confidences_by_kind(edges)
    assert by_kind["contains"] == {CONF_EXACT}
    assert by_kind["imports"] == {CONF_FALLBACK}
    assert by_kind["extends"] == {CONF_FALLBACK}
    assert by_kind["calls"] == {CONF_FALLBACK}
    assert by_kind["exports"] == {CONF_RESOLVED}


def test_typescript_adapter_emits_confidence():
    src = b"""
import {x} from 'foo';
export class C {
  m() {}
}
"""
    _syms, edges = TypeScriptAdapter().parse(Path("a.ts"), src)
    assert all("confidence" in e for e in edges)
    by_kind = _confidences_by_kind(edges)
    assert by_kind.get("contains") == {CONF_EXACT}
    assert by_kind.get("imports") == {CONF_FALLBACK}


def test_go_adapter_emits_confidence():
    src = b"""package main
import "fmt"
type Cat struct{}
func (c Cat) Meow() { fmt.Println("hi") }
"""
    _syms, edges = GoAdapter().parse(Path("cat.go"), src)
    assert all("confidence" in e for e in edges)
    by_kind = _confidences_by_kind(edges)
    assert by_kind.get("imports") == {CONF_FALLBACK}
