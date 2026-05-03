"""Language adapters — one per supported language."""

from __future__ import annotations

from fnmatch import fnmatch

from .base import Edge, LangAdapter, Symbol, make_symbol_id
from .csharp_adapter import CSharpAdapter
from .go_adapter import GoAdapter
from .java_adapter import JavaAdapter
from .javascript_adapter import JavaScriptAdapter
from .python_adapter import PythonAdapter
from .ruby_adapter import RubyAdapter
from .rust_adapter import RustAdapter
from .typescript_adapter import TypeScriptAdapter

ALL_ADAPTERS: tuple[LangAdapter, ...] = (
    PythonAdapter(),
    TypeScriptAdapter(),
    JavaScriptAdapter(),
    GoAdapter(),
    RustAdapter(),
    JavaAdapter(),
    RubyAdapter(),
    CSharpAdapter(),
)


def adapter_for(filename: str) -> LangAdapter | None:
    """Return the first adapter whose globs match the filename, else None."""
    for ad in ALL_ADAPTERS:
        for pat in ad.file_globs:
            if fnmatch(filename, pat):
                return ad
    return None


__all__ = [
    "ALL_ADAPTERS",
    "Edge",
    "LangAdapter",
    "Symbol",
    "adapter_for",
    "make_symbol_id",
    "PythonAdapter",
    "TypeScriptAdapter",
    "JavaScriptAdapter",
    "GoAdapter",
    "RustAdapter",
    "JavaAdapter",
    "RubyAdapter",
    "CSharpAdapter",
]
