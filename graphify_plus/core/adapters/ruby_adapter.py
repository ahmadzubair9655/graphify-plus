"""Ruby LangAdapter — best-effort, declaration-level only."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from ._generic import LangSpec, parse_generic
from .base import Edge, LangAdapter, Symbol

_SPEC = LangSpec(
    grammar="ruby",
    label="ruby",
    function_kinds=("method",),
    class_kinds=("class", "module"),
    extra_signature_prefix={"method": "def", "class": "class", "module": "module"},
)


class RubyAdapter:
    """Ruby support is best-effort — call edges and dynamic methods are not tracked."""

    language: ClassVar[str] = "ruby"
    file_globs: ClassVar[tuple[str, ...]] = ("*.rb",)

    def parse(self, path: Path, source: bytes) -> tuple[list[Symbol], list[Edge]]:
        return parse_generic(_SPEC, path, source)


_check: LangAdapter = RubyAdapter()  # type: ignore[abstract]
