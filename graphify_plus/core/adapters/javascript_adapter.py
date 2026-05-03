"""JavaScript / JSX LangAdapter."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from ._ts_common import parse_ts_like
from .base import Edge, LangAdapter, Symbol


class JavaScriptAdapter:
    language: ClassVar[str] = "javascript"
    file_globs: ClassVar[tuple[str, ...]] = ("*.js", "*.jsx", "*.mjs", "*.cjs")

    def parse(self, path: Path, source: bytes) -> tuple[list[Symbol], list[Edge]]:
        return parse_ts_like("javascript", path, source, lang_label="javascript")


_check: LangAdapter = JavaScriptAdapter()  # type: ignore[abstract]
