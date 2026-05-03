"""LangAdapter protocol + Symbol / Edge TypedDicts.

Every language frontend (Python, TS/JS, Go, Rust, Java, Ruby, C#) implements
``LangAdapter`` and produces a deterministic ``(symbols, edges)`` pair from
a single source file. The orchestrator in ``core/ingest.py`` walks the repo,
dispatches each file to the matching adapter by glob, and merges results.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import ClassVar, Literal, Protocol, TypedDict, runtime_checkable

SymbolKind = Literal[
    "function",
    "method",
    "class",
    "interface",
    "type",
    "const",
    "module",
    "export",
    "endpoint",
]

EdgeKind = Literal[
    "calls",
    "imports",
    "extends",
    "implements",
    "references",
    "exports",
    "contains",
]


class Symbol(TypedDict, total=False):
    id: str
    kind: SymbolKind
    name: str
    qualified_name: str
    path: str
    span: tuple[int, int]
    signature: str
    exported: bool
    docstring: str | None
    parent_id: str | None
    language: str


class Edge(TypedDict, total=False):
    src: str
    dst: str
    kind: EdgeKind
    resolved: bool
    span: tuple[int, int] | None


def make_symbol_id(path: str, qualified_name: str) -> str:
    """Stable 16-hex-char id derived from POSIX path + qualified name.

    Never includes line numbers, mtimes, or RNG state — re-running ingest
    on an unchanged tree must produce identical ids.
    """
    norm = Path(path).as_posix()
    h = hashlib.sha256(f"{norm}::{qualified_name}".encode()).hexdigest()
    return h[:16]


@runtime_checkable
class LangAdapter(Protocol):
    language: ClassVar[str]
    file_globs: ClassVar[tuple[str, ...]]

    def parse(self, path: Path, source: bytes) -> tuple[list[Symbol], list[Edge]]: ...
