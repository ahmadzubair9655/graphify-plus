"""Go LangAdapter — tree-sitter, declaration-level."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from ._generic import LangSpec, parse_generic
from .base import Edge, LangAdapter, Symbol

_SPEC = LangSpec(
    grammar="go",
    label="go",
    function_kinds=("function_declaration",),
    method_kinds=("method_declaration",),
    class_kinds=("type_declaration",),  # struct/interface live under type_declaration
    import_kinds=("import_declaration",),
    extra_signature_prefix={
        "function_declaration": "func",
        "method_declaration": "func",
        "type_declaration": "type",
    },
)


class GoAdapter:
    language: ClassVar[str] = "go"
    file_globs: ClassVar[tuple[str, ...]] = ("*.go",)

    def parse(self, path: Path, source: bytes) -> tuple[list[Symbol], list[Edge]]:
        return parse_generic(_SPEC, path, source)


_check: LangAdapter = GoAdapter()  # type: ignore[abstract]
