"""Rust LangAdapter — tree-sitter, declaration-level."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from ._generic import LangSpec, parse_generic
from .base import Edge, LangAdapter, Symbol

_SPEC = LangSpec(
    grammar="rust",
    label="rust",
    function_kinds=("function_item",),
    class_kinds=("struct_item", "enum_item", "impl_item"),
    interface_kinds=("trait_item",),
    type_kinds=("type_item",),
    import_kinds=("use_declaration",),
    extra_signature_prefix={
        "function_item": "fn",
        "struct_item": "struct",
        "enum_item": "enum",
        "impl_item": "impl",
        "trait_item": "trait",
        "type_item": "type",
    },
)


class RustAdapter:
    language: ClassVar[str] = "rust"
    file_globs: ClassVar[tuple[str, ...]] = ("*.rs",)

    def parse(self, path: Path, source: bytes) -> tuple[list[Symbol], list[Edge]]:
        return parse_generic(_SPEC, path, source)


_check: LangAdapter = RustAdapter()  # type: ignore[abstract]
