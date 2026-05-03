"""Generic tree-sitter symbol extractor driven by a per-language config.

Used by Go, Rust, Java, Ruby, C# adapters. Each adapter supplies a
``LangSpec`` mapping AST node kinds to symbol roles. This is intentionally
shallow (declaration-level only) — call-graph fidelity is best-effort.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from tree_sitter_languages import get_parser  # type: ignore[import-untyped]

from .base import Edge, Symbol, make_symbol_id


def _text(node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


@dataclass
class LangSpec:
    grammar: str
    label: str
    function_kinds: tuple[str, ...] = ()
    method_kinds: tuple[str, ...] = ()
    class_kinds: tuple[str, ...] = ()
    interface_kinds: tuple[str, ...] = ()
    type_kinds: tuple[str, ...] = ()
    import_kinds: tuple[str, ...] = ()
    name_field: str = "name"
    extra_signature_prefix: dict[str, str] = field(default_factory=dict)


def parse_generic(spec: LangSpec, path: Path, source: bytes) -> tuple[list[Symbol], list[Edge]]:
    parser = get_parser(spec.grammar)
    tree = parser.parse(source)
    rel_path = path.as_posix()
    module_qname = path.stem

    symbols: list[Symbol] = []
    edges: list[Edge] = []

    module_id = make_symbol_id(rel_path, module_qname)
    symbols.append(
        Symbol(
            id=module_id,
            kind="module",
            name=module_qname,
            qualified_name=module_qname,
            path=rel_path,
            span=(1, max(1, tree.root_node.end_point[0] + 1)),
            signature=f"// module {module_qname}",
            exported=True,
            docstring=None,
            parent_id=None,
            language=spec.label,
        )
    )

    def kind_of(t: str) -> str | None:
        if t in spec.function_kinds:
            return "function"
        if t in spec.method_kinds:
            return "method"
        if t in spec.class_kinds:
            return "class"
        if t in spec.interface_kinds:
            return "interface"
        if t in spec.type_kinds:
            return "type"
        return None

    def name_of(node) -> str | None:
        nm = node.child_by_field_name(spec.name_field)
        if nm is not None:
            return _text(nm, source)
        # fallback: first identifier-ish child
        for ch in node.children:
            if ch.type in {"identifier", "type_identifier", "field_identifier"}:
                return _text(ch, source)
        return None

    def walk(node, parent_qname: str, parent_id: str | None) -> None:
        t = node.type

        if t in spec.import_kinds:
            edges.append(
                Edge(
                    src=module_id,
                    dst=_text(node, source).strip(),
                    kind="imports",
                    resolved=False,
                    span=(node.start_point[0] + 1, node.end_point[0] + 1),
                )
            )
            return

        kind = kind_of(t)
        if kind is not None:
            nm = name_of(node)
            if nm:
                qname = f"{parent_qname}.{nm}"
                sid = make_symbol_id(rel_path, qname)
                prefix = spec.extra_signature_prefix.get(t, kind)
                symbols.append(
                    Symbol(
                        id=sid,
                        kind=kind,
                        name=nm,
                        qualified_name=qname,
                        path=rel_path,
                        span=(node.start_point[0] + 1, node.end_point[0] + 1),
                        signature=f"{prefix} {nm}",
                        exported=nm[:1].isupper() if spec.label == "go" else True,
                        docstring=None,
                        parent_id=parent_id,
                        language=spec.label,
                    )
                )
                if parent_id is not None:
                    edges.append(
                        Edge(src=parent_id, dst=sid, kind="contains", resolved=True, span=None)
                    )
                # recurse into class-likes for methods
                if kind == "class":
                    body = node.child_by_field_name("body")
                    if body is not None:
                        for ch in body.children:
                            walk(ch, qname, sid)
                    return

        for ch in node.children:
            walk(ch, parent_qname, parent_id)

    for ch in tree.root_node.children:
        walk(ch, module_qname, module_id)

    symbols.sort(key=lambda s: (s["path"], s["span"][0], s["qualified_name"]))
    edges.sort(
        key=lambda e: (
            e.get("src") or "",
            e.get("dst") or "",
            e.get("kind") or "",
            (e.get("span") or (0, 0))[0],
        )
    )
    return symbols, edges
