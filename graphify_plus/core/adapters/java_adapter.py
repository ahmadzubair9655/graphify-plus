"""Java LangAdapter — tree-sitter, declaration-level."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from ._generic import LangSpec, parse_generic
from .base import Edge, LangAdapter, Symbol

_SPEC = LangSpec(
    grammar="java",
    label="java",
    function_kinds=(),
    method_kinds=("method_declaration", "constructor_declaration"),
    class_kinds=("class_declaration", "enum_declaration"),
    interface_kinds=("interface_declaration",),
    import_kinds=("import_declaration",),
    extra_signature_prefix={
        "method_declaration": "method",
        "constructor_declaration": "constructor",
        "class_declaration": "class",
        "enum_declaration": "enum",
        "interface_declaration": "interface",
    },
)


class JavaAdapter:
    language: ClassVar[str] = "java"
    file_globs: ClassVar[tuple[str, ...]] = ("*.java",)

    def parse(self, path: Path, source: bytes) -> tuple[list[Symbol], list[Edge]]:
        return parse_generic(_SPEC, path, source)


_check: LangAdapter = JavaAdapter()  # type: ignore[abstract]
