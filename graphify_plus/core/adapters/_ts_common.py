"""Shared TS/JS extraction logic — both adapters point at the same engine
with different tree-sitter grammar names and globs."""

from __future__ import annotations

from pathlib import Path

from tree_sitter_languages import get_parser  # type: ignore[import-untyped]

from .base import CONF_EXACT, CONF_FALLBACK, CONF_RESOLVED, Edge, Symbol, make_symbol_id

JSX_NODE_TYPES = {"jsx_self_closing_element", "jsx_opening_element"}
JSX_CONTAINER_TYPES = {"jsx_element", "jsx_fragment"}
_FUNCTION_DECL_TYPES = {
    "function_declaration",
    "function_signature",
    "generator_function_declaration",
}
_FUNCTION_EXPR_TYPES = {
    "arrow_function",
    "function",
    "function_expression",
    "generator_function",
}


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


def _file_has_jsx(root) -> bool:
    stack = [root]
    while stack:
        n = stack.pop()
        if n.type in JSX_NODE_TYPES or n.type in JSX_CONTAINER_TYPES:
            return True
        stack.extend(n.children)
    return False


def _jsx_element_name_parts(node, source: bytes) -> list[str] | None:
    """Return the component name as a list of identifier parts, or None.

    Handles three cases:
      - simple identifier: <Foo />            -> ["Foo"]
      - member expression: <Foo.Bar.Baz />    -> ["Foo", "Bar", "Baz"]
      - namespace name (xml-style):    -> None (skipped)
    Lowercase-leading names (host elements like <div/>) are returned as-is;
    callers usually filter them out.
    """
    name_node = None
    for ch in node.children:
        if ch.type in {"identifier", "member_expression", "nested_identifier"}:
            name_node = ch
            break
        if ch.type == "jsx_namespace_name":
            return None
    if name_node is None:
        return None
    if name_node.type == "identifier":
        return [_text(name_node, source)]
    parts: list[str] = []

    def collect(n) -> None:
        if n.type in {"identifier", "property_identifier"}:
            parts.append(_text(n, source))
        elif n.type in {"member_expression", "nested_identifier"}:
            for c in n.children:
                if c.type in {"identifier", "property_identifier", "member_expression", "nested_identifier"}:
                    collect(c)

    collect(name_node)
    return parts or None


def _resolve_jsx_render_edges(
    root,
    source: bytes,
    *,
    module_qname: str,
    module_id: str,
    symbols: list[Symbol],
) -> list[Edge]:
    """Walk the AST again, this time descending into function bodies, and
    emit JSX_RENDER edges from each enclosing function/component to any
    same-file symbol it renders via JSX.
    """
    by_qname: dict[str, str] = {s["qualified_name"]: s["id"] for s in symbols}
    edges: list[Edge] = []
    seen: set[tuple[str, str, int]] = set()

    def emit(src_id: str, dst_id: str, line: int) -> None:
        key = (src_id, dst_id, line)
        if key in seen or src_id == dst_id:
            return
        seen.add(key)
        edges.append(
            Edge(
                src=src_id,
                dst=dst_id,
                kind="jsx_render",
                resolved=True,
                span=(line, line),
                confidence=CONF_RESOLVED,
            )
        )

    def resolve(parts: list[str], scope_chain: list[str]) -> str | None:
        if not parts:
            return None
        head = parts[0]
        if not head or not head[0].isalpha() or head[0].islower():
            # host element like <div/> or <svg/> — never user-defined here
            return None
        for prefix in scope_chain:
            head_qname = f"{prefix}.{head}" if prefix else head
            if head_qname not in by_qname:
                continue
            if len(parts) == 1:
                return by_qname[head_qname]
            full = ".".join([head_qname] + parts[1:])
            if full in by_qname:
                return by_qname[full]
        return None

    def walk(node, scope_chain: list[str], enclosing_id: str) -> None:
        t = node.type
        next_scope = scope_chain
        next_enclosing = enclosing_id

        # Track function-declaration entry into a new scope.
        if t in _FUNCTION_DECL_TYPES:
            nm = node.child_by_field_name("name")
            if nm is not None:
                fname = _text(nm, source)
                fqname = f"{scope_chain[0]}.{fname}"
                if fqname in by_qname:
                    next_scope = [fqname] + scope_chain
                    next_enclosing = by_qname[fqname]

        # const Foo = (...) => ... / function expression assigned to a name.
        elif t == "variable_declarator":
            nm = node.child_by_field_name("name")
            val = node.child_by_field_name("value")
            if nm is not None and val is not None and val.type in _FUNCTION_EXPR_TYPES:
                vname = _text(nm, source)
                vqname = f"{scope_chain[0]}.{vname}"
                if vqname in by_qname:
                    next_scope = [vqname] + scope_chain
                    next_enclosing = by_qname[vqname]

        elif t in {"method_definition", "method_signature"}:
            nm = node.child_by_field_name("name")
            if nm is not None:
                mname = _text(nm, source)
                # methods are scoped under their class qname which is
                # scope_chain[0] when we entered the class body.
                mqname = f"{scope_chain[0]}.{mname}"
                if mqname in by_qname:
                    next_scope = [mqname] + scope_chain
                    next_enclosing = by_qname[mqname]

        elif t == "class_declaration":
            nm = node.child_by_field_name("name")
            if nm is not None:
                cname = _text(nm, source)
                cqname = f"{scope_chain[0]}.{cname}"
                if cqname in by_qname:
                    next_scope = [cqname] + scope_chain

        if t in JSX_NODE_TYPES:
            parts = _jsx_element_name_parts(node, source)
            if parts:
                target = resolve(parts, next_scope)
                if target is not None:
                    emit(next_enclosing, target, node.start_point[0] + 1)

        for ch in node.children:
            walk(ch, next_scope, next_enclosing)

    walk(root, [module_qname], module_id)
    return edges


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
                fname = _text(nm, source)
                fid = add_symbol(
                    node,
                    fname,
                    "function",
                    _signature_of_function(node, source),
                    parent_id,
                    parent_qname,
                )
                # Recurse into the function body so nested function/component
                # declarations (a common React pattern) become symbols too.
                body = node.child_by_field_name("body")
                if body is not None:
                    new_qname = f"{parent_qname}.{fname}"
                    for ch in body.children:
                        walk(ch, new_qname, fid)
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
                        vname = _text(nm, source)
                        vid = add_symbol(
                            ch,
                            vname,
                            "function",
                            f"const {vname} = ...",
                            parent_id,
                            parent_qname,
                        )
                        body = val.child_by_field_name("body")
                        if body is not None:
                            new_qname = f"{parent_qname}.{vname}"
                            if body.type == "statement_block":
                                for sub in body.children:
                                    walk(sub, new_qname, vid)
                            else:
                                # arrow expression body, e.g. () => <Foo/>
                                walk(body, new_qname, vid)
            return

        for ch in node.children:
            walk(ch, parent_qname, parent_id)

    for ch in tree.root_node.children:
        walk(ch, module_qname, module_id)

    # JSX-internal symbol resolution: <Foo /> in the same file resolves to a
    # function/component defined in this file. Without this pass, JSX-only
    # internal helpers look unused and get flagged as dead.
    if _file_has_jsx(tree.root_node):
        edges.extend(
            _resolve_jsx_render_edges(
                tree.root_node,
                source,
                module_qname=module_qname,
                module_id=module_id,
                symbols=symbols,
            )
        )

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
