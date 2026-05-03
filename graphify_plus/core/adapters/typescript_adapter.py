"""TypeScript / TSX LangAdapter."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from ._ts_common import parse_ts_like
from .base import Edge, LangAdapter, Symbol


class TypeScriptAdapter:
    language: ClassVar[str] = "typescript"
    file_globs: ClassVar[tuple[str, ...]] = ("*.ts", "*.tsx", "*.mts", "*.cts")

    def parse(self, path: Path, source: bytes) -> tuple[list[Symbol], list[Edge]]:
        grammar = "tsx" if path.suffix.lower() == ".tsx" else "typescript"
        return parse_ts_like(grammar, path, source, lang_label="typescript")


_check: LangAdapter = TypeScriptAdapter()  # type: ignore[abstract]
