"""C# LangAdapter — best-effort, declaration-level only."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from ._generic import LangSpec, parse_generic
from .base import Edge, LangAdapter, Symbol

_SPEC = LangSpec(
    grammar="c_sharp",
    label="csharp",
    method_kinds=("method_declaration", "constructor_declaration"),
    class_kinds=(
        "class_declaration",
        "struct_declaration",
        "record_declaration",
        "enum_declaration",
    ),
    interface_kinds=("interface_declaration",),
    import_kinds=("using_directive",),
    extra_signature_prefix={
        "method_declaration": "method",
        "constructor_declaration": "constructor",
        "class_declaration": "class",
        "struct_declaration": "struct",
        "record_declaration": "record",
        "enum_declaration": "enum",
        "interface_declaration": "interface",
    },
)


class CSharpAdapter:
    """C# support is best-effort — partial classes and reflection are not tracked."""

    language: ClassVar[str] = "csharp"
    file_globs: ClassVar[tuple[str, ...]] = ("*.cs",)

    def parse(self, path: Path, source: bytes) -> tuple[list[Symbol], list[Edge]]:
        return parse_generic(_SPEC, path, source)


_check: LangAdapter = CSharpAdapter()  # type: ignore[abstract]
