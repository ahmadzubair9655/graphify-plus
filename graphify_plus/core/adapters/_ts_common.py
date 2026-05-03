"""Shared TS/JS extraction logic — both adapters point at the same engine
with different tree-sitter grammar names and globs."""

from __future__ import annotations

from pathlib import Path

from tree_sitter_languages import get_parser  # type: ignore[import-untyped]

from .base import CONF_EXACT, CONF_FALLBACK, Edge, Symbol, make_symbol_id


def _text(node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _is_exported(node) -> bool:
    """A declaration is exported if its parent is `export_statement`."""
    p = node.parent
    while p is not None:
        if p.type == "export_statement":
            return True
        if p.type in {"program", "statement_block"}:
            return False
        p = p.parent
    return False


def _signature_of_function(node, source: bytes) -> str:
    name_node = node.child_by_field_name("name")
    name = _text(name_node, source) if name_node else "<anonymous>"
    params = node.child_by_field_name("parameters")
    ret = node.child_by_field_name("return_type")
    params_s = _text(params, source) if params else "()"
    params_s = " ".join(params_s.split())
    out = f"function {name}{params_s}"
    if ret is not None:
        out += " " + _text(ret, source).strip()
    return out


def parse_ts_like(
    language_name: str, path: Path, source: bytes, *, lang_label: str
) -> tuple[list[Symbol], list[Edge]]:
    parser = get_parser(language_name)
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
            language=lang_label,
        )
    )

    def add_symbol(
        node, name: str, kind: str, sig: str, parent_id: str | None, parent_qname: str
    ) -> str:
        qname = f"{parent_qname}.{name}"
        sid = make_symbol_id(rel_path, qname)
        symbols.append(
            Symbol(
                id=sid,
                kind=kind,
                name=name,
                qualified_name=qname,
                path=rel_path,
                span=(node.start_point[0] + 1, node.end_point[0] + 1),
                signature=sig,
                exported=_is_exported(node),
                docstring=None,
                parent_id=parent_id,
                language=lang_label,
            )
        )
        if parent_id is not None:
            edges.append(
                Edge(
                    src=parent_id,
                    dst=sid,
                    kind="contains",
                    resolved=True,
                    span=None,
                    confidence=CONF_EXACT,
                )
            )
        return sid

    def walk(node, parent_qname: str, parent_id: str | None) -> None:
        t = node.type

        if t in {"function_declaration", "function_signature", "generator_function_declaration"}:
            nm = node.child_by_field_name("name")
            if nm is not None:
                add_symbol(
                    node,
                    _text(nm, source),
                    "function",
                    _signature_of_function(node, source),
                    parent_id,
                    parent_qname,
                )
            return

        if t == "class_declaration":
            nm = node.child_by_field_name("name")
            if nm is None:
                return
            cname = _text(nm, source)
            cid = add_symbol(node, cname, "class", f"class {cname}", parent_id, parent_qname)
            body = node.child_by_field_name("body")
            if body is not None:
                for ch in body.children:
                    if ch.type in {"method_definition", "method_signature"}:
                        mn = ch.child_by_field_name("name")
                        if mn is not None:
                            mname = _text(mn, source)
                            mqname = f"{parent_qname}.{cname}.{mname}"
                            mid = make_symbol_id(rel_path, mqname)
                            symbols.append(
                                Symbol(
                                    id=mid,
                                    kind="method",
                                    name=mname,
                                    qualified_name=mqname,
                                    path=rel_path,
                                    span=(ch.start_point[0] + 1, ch.end_point[0] + 1),
                                    signature=f"method {mname}",
                                    exported=False,
                                    docstring=None,
                                    parent_id=cid,
                                    language=lang_label,
                                )
                            )
                            edges.append(
                                Edge(
                                    src=cid,
                                    dst=mid,
                                    kind="contains",
                                    resolved=True,
                                    span=None,
                                    confidence=CONF_EXACT,
                                )
                            )
            return

        if t in {"interface_declaration", "type_alias_declaration"}:
            nm = node.child_by_field_name("name")
            if nm is not None:
                kind = "interface" if t == "interface_declaration" else "type"
                add_symbol(
                    node,
                    _text(nm, source),
                    kind,
                    f"{kind} {_text(nm, source)}",
                    parent_id,
                    parent_qname,
                )
            return

        if t in {"import_statement", "import_clause"}:
            edges.append(
                Edge(
                    src=module_id,
                    dst=_text(node, source).strip(),
                    kind="imports",
                    resolved=False,
                    span=(node.start_point[0] + 1, node.end_point[0] + 1),
                    confidence=CONF_FALLBACK,
                )
            )
            return

        if t == "lexical_declaration" or t == "variable_declaration":
            for ch in node.children:
                if ch.type == "variable_declarator":
                    nm = ch.child_by_field_name("name")
                    val = ch.child_by_field_name("value")
                    if (
                        nm is not None
                        and val is not None
                        and val.type
                        in {
                            "arrow_function",
                            "function",
                            "function_expression",
                        }
                    ):
                        add_symbol(
                            ch,
                            _text(nm, source),
                            "function",
                            f"const {_text(nm, source)} = ...",
                            parent_id,
                            parent_qname,
                        )
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
