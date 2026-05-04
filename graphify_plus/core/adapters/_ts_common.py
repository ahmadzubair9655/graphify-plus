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


def _call_expression_name_parts(node, source: bytes) -> list[str] | None:
    """Return the callee name as identifier parts, or None when the call
    cannot be statically resolved without cross-file knowledge.

    Handles:
      - identifier callee:        ``foo(x)``        -> ["foo"]
      - member-expression callee: ``Ns.foo(x)``     -> ["Ns", "foo"]
      - nested member access:     ``A.B.foo()``     -> ["A", "B", "foo"]
    Returns None for call subjects we don't try to resolve (super(), this.x(),
    new-expressions, optional-chained calls on computed properties, etc.).
    """
    fn_node = node.child_by_field_name("function")
    if fn_node is None:
        return None
    if fn_node.type == "identifier":
        return [_text(fn_node, source)]
    if fn_node.type in {"member_expression", "nested_identifier"}:
        parts: list[str] = []

        def collect(n) -> bool:
            if n.type == "identifier":
                parts.append(_text(n, source))
                return True
            if n.type == "property_identifier":
                parts.append(_text(n, source))
                return True
            if n.type in {"member_expression", "nested_identifier"}:
                obj = n.child_by_field_name("object")
                prop = n.child_by_field_name("property")
                if obj is None or prop is None:
                    # Subscript / computed property — give up.
                    return False
                if not collect(obj):
                    return False
                return collect(prop)
            # `this`, `super`, computed `[expr]`, parenthesised expressions,
            # call_expressions (chained calls) — out of scope for static
            # resolution. Bail.
            return False

        if not collect(fn_node):
            return None
        return parts or None
    return None


_DECL_NAME_PARENTS: frozenset[str] = frozenset({
    "function_declaration",
    "function_signature",
    "generator_function_declaration",
    "method_definition",
    "method_signature",
    "class_declaration",
    "interface_declaration",
    "type_alias_declaration",
    "enum_declaration",
    "abstract_class_declaration",
    "abstract_method_signature",
    "variable_declarator",
    "required_parameter",
    "optional_parameter",
    "rest_pattern",
    "shorthand_property_identifier_pattern",
    "labeled_statement",
})

_REF_BLOCKING_ANCESTORS: frozenset[str] = frozenset({
    # Import / export bindings — not references, declarations.
    "import_statement",
    "import_clause",
    "import_specifier",
    "namespace_import",
    "named_imports",
    # Destructuring binding contexts — identifiers here are introducing
    # new local names, not referring to existing ones.
    "object_pattern",
    "array_pattern",
})

_JSX_NAME_PARENTS: frozenset[str] = frozenset({
    "jsx_opening_element",
    "jsx_closing_element",
    "jsx_self_closing_element",
})


def _is_reference_position(node) -> bool:
    """True if the identifier node is in a value-reference position
    (worth emitting a `references` edge for) rather than a declaration,
    import, JSX name, member-access property, or call-expression
    function position."""
    p = node.parent
    if p is None:
        return False
    pt = p.type

    # Declaration positions: identifier IS the name being introduced.
    if pt in _DECL_NAME_PARENTS:
        nm = p.child_by_field_name("name")
        if nm is not None and nm.id == node.id:
            return False

    # Property side of `obj.prop` — not a same-file reference.
    # (Object side IS a reference and we let it through.)
    if pt == "member_expression":
        prop = p.child_by_field_name("property")
        if prop is not None and prop.id == node.id:
            return False

    # JSX element name — handled by jsx_render.
    if pt in _JSX_NAME_PARENTS:
        return False

    # Function position of a call_expression — handled by calls.
    if pt == "call_expression":
        fn = p.child_by_field_name("function")
        if fn is not None and fn.id == node.id:
            return False

    # Walk up: any ancestor in the blocking set means we're inside a
    # declaration/binding context (import_specifier, destructuring
    # pattern, etc).
    a = p
    while a is not None:
        if a.type in _REF_BLOCKING_ANCESTORS:
            return False
        # Don't walk past the enclosing function — saves time and avoids
        # false positives from unrelated outer constructs.
        if a.type in _FUNCTION_DECL_TYPES or a.type in _FUNCTION_EXPR_TYPES:
            break
        a = a.parent

    return True


def _resolve_call_and_jsx_edges(
    root,
    source: bytes,
    *,
    module_qname: str,
    module_id: str,
    symbols: list[Symbol],
) -> list[Edge]:
    """Walk the AST descending into function bodies and emit two edge kinds:

      - ``jsx_render`` from an enclosing component to any same-file symbol
        it renders via ``<Component />``.
      - ``calls`` from an enclosing function/method to any same-file symbol
        it invokes via ``foo()`` or ``Ns.foo()``.

    Both kinds use the same scope-chain resolution: walk innermost-first,
    looking for a symbol whose qualified name matches ``{prefix}.{head}``
    (and any subsequent ``.parts``). Cross-file resolution is intentionally
    out of scope — unresolved calls stay unresolved (the imports graph and
    audit's unresolved_imports probe handle that). Confidence is
    ``CONF_RESOLVED`` (0.7) for both.
    """
    by_qname: dict[str, str] = {s["qualified_name"]: s["id"] for s in symbols}
    edges: list[Edge] = []
    seen: set[tuple[str, str, str, int]] = set()

    def emit(kind: str, src_id: str, dst_id: str, line: int) -> None:
        key = (kind, src_id, dst_id, line)
        if key in seen or src_id == dst_id:
            return
        seen.add(key)
        # `references` edges are noisier than calls/jsx_render — they
        # include things like passing a function as an argument or storing
        # it in a const tuple, which are real liveness signals but also
        # easier to over-emit. Lower confidence reflects that.
        conf = 0.5 if kind == "references" else CONF_RESOLVED
        edge: Edge = Edge(  # type: ignore[typeddict-item]
            src=src_id,
            dst=dst_id,
            kind=kind,  # type: ignore[arg-type]
            resolved=True,
            span=(line, line),
            confidence=conf,
        )
        # `confidence_score` is a numeric attribute the audit's
        # confidence_drift probe reads; keep it in sync with `confidence`
        # so probes that filter by it see consistent values.
        edge["confidence_score"] = conf  # type: ignore[typeddict-unknown-key]
        edges.append(edge)

    def resolve(parts: list[str], scope_chain: list[str], *, jsx: bool) -> str | None:
        if not parts:
            return None
        head = parts[0]
        if jsx and (not head or not head[0].isalpha() or head[0].islower()):
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
                target = resolve(parts, next_scope, jsx=True)
                if target is not None:
                    emit("jsx_render", next_enclosing, target, node.start_point[0] + 1)

        elif t == "call_expression":
            parts = _call_expression_name_parts(node, source)
            if parts:
                target = resolve(parts, next_scope, jsx=False)
                if target is not None:
                    emit("calls", next_enclosing, target, node.start_point[0] + 1)

        elif t == "identifier" and _is_reference_position(node):
            name = _text(node, source)
            target = resolve([name], next_scope, jsx=False)
            if target is not None:
                emit("references", next_enclosing, target, node.start_point[0] + 1)

        for ch in node.children:
            walk(ch, next_scope, next_enclosing)

    walk(root, [module_qname], module_id)
    return edges


# Back-compat alias — older imports still reference the JSX-only name.
_resolve_jsx_render_edges = _resolve_call_and_jsx_edges


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
    # Same-file call resolution always runs for TS/JS files; JSX render
    # resolution is folded into the same pass and only emits when JSX
    # elements are present in the tree. One walk, two edge kinds.
    edges.extend(
        _resolve_call_and_jsx_edges(
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
