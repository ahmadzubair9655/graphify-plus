"""Python LangAdapter — tree-sitter backed."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

# tree_sitter_languages is the bundled-grammar shim.
from tree_sitter_languages import get_parser  # type: ignore[import-untyped]

from .base import Edge, LangAdapter, Symbol, make_symbol_id


def _text(node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _first_line(s: str) -> str:
    s = s.strip()
    if not s:
        return s
    for sep in (". ", "\n"):
        if sep in s:
            return s.split(sep, 1)[0].strip().rstrip(".")
    return s


def _docstring(body_node, source: bytes) -> str | None:
    """Return the first-statement docstring of a body block, if any."""
    if body_node is None:
        return None
    for child in body_node.children:
        if child.type == "expression_statement" and child.child_count > 0:
            inner = child.children[0]
            if inner.type == "string":
                raw = _text(inner, source)
                # strip quotes
                for q in ('"""', "'''", '"', "'"):
                    if raw.startswith(q) and raw.endswith(q) and len(raw) >= 2 * len(q):
                        return raw[len(q) : -len(q)].strip() or None
                return raw.strip() or None
            return None
        if child.is_named:
            return None
    return None


def _signature(node, source: bytes) -> str:
    """One-line canonical signature: 'def name(params) -> return_type'."""
    name_node = node.child_by_field_name("name")
    params = node.child_by_field_name("parameters")
    ret = node.child_by_field_name("return_type")
    name = _text(name_node, source) if name_node else "<anonymous>"
    params_s = _text(params, source) if params else "()"
    params_s = " ".join(params_s.split())
    out = f"def {name}{params_s}"
    if ret is not None:
        out += f" -> {_text(ret, source).strip()}"
    return out


class PythonAdapter:
    language: ClassVar[str] = "python"
    file_globs: ClassVar[tuple[str, ...]] = ("*.py", "*.pyi")

    def __init__(self) -> None:
        self._parser = get_parser("python")

    def parse(self, path: Path, source: bytes) -> tuple[list[Symbol], list[Edge]]:
        tree = self._parser.parse(source)
        rel_path = path.as_posix()
        module_qname = path.stem

        symbols: list[Symbol] = []
        edges: list[Edge] = []

        # Module-level synthetic symbol — anchors imports and module-level consts.
        module_id = make_symbol_id(rel_path, module_qname)
        symbols.append(
            Symbol(
                id=module_id,
                kind="module",
                name=module_qname,
                qualified_name=module_qname,
                path=rel_path,
                span=(1, max(1, tree.root_node.end_point[0] + 1)),
                signature=f"# module {module_qname}",
                exported=True,
                docstring=_docstring(tree.root_node, source),
                parent_id=None,
                language=self.language,
            )
        )

        all_exports: set[str] = set()

        def walk(node, parent_qname: str, parent_id: str | None) -> None:
            t = node.type
            if t == "function_definition":
                name_node = node.child_by_field_name("name")
                if name_node is None:
                    return
                fname = _text(name_node, source)
                qname = f"{parent_qname}.{fname}"
                kind = "method" if parent_id != module_id else "function"
                sid = make_symbol_id(rel_path, qname)
                body = node.child_by_field_name("body")
                symbols.append(
                    Symbol(
                        id=sid,
                        kind=kind,
                        name=fname,
                        qualified_name=qname,
                        path=rel_path,
                        span=(node.start_point[0] + 1, node.end_point[0] + 1),
                        signature=_signature(node, source),
                        exported=not fname.startswith("_"),
                        docstring=_first_line(_docstring(body, source) or ""),
                        parent_id=parent_id,
                        language=self.language,
                    )
                )
                if parent_id is not None:
                    edges.append(
                        Edge(src=parent_id, dst=sid, kind="contains", resolved=True, span=None)
                    )
                # walk body for calls
                _collect_calls(body, sid)
                return

            if t == "class_definition":
                name_node = node.child_by_field_name("name")
                if name_node is None:
                    return
                cname = _text(name_node, source)
                qname = f"{parent_qname}.{cname}"
                cid = make_symbol_id(rel_path, qname)
                body = node.child_by_field_name("body")
                # bases
                superclasses = node.child_by_field_name("superclasses")
                base_strs = []
                if superclasses is not None:
                    for ch in superclasses.children:
                        if ch.is_named:
                            base_strs.append(_text(ch, source))
                sig = f"class {cname}"
                if base_strs:
                    sig += "(" + ", ".join(base_strs) + ")"
                symbols.append(
                    Symbol(
                        id=cid,
                        kind="class",
                        name=cname,
                        qualified_name=qname,
                        path=rel_path,
                        span=(node.start_point[0] + 1, node.end_point[0] + 1),
                        signature=sig,
                        exported=not cname.startswith("_"),
                        docstring=_first_line(_docstring(body, source) or ""),
                        parent_id=parent_id,
                        language=self.language,
                    )
                )
                if parent_id is not None:
                    edges.append(
                        Edge(src=parent_id, dst=cid, kind="contains", resolved=True, span=None)
                    )
                for base in base_strs:
                    edges.append(Edge(src=cid, dst=base, kind="extends", resolved=False, span=None))
                if body is not None:
                    for ch in body.children:
                        walk(ch, qname, cid)
                return

            if t == "import_statement" or t == "import_from_statement":
                target = _text(node, source).strip()
                edges.append(
                    Edge(
                        src=module_id,
                        dst=target,
                        kind="imports",
                        resolved=False,
                        span=(node.start_point[0] + 1, node.end_point[0] + 1),
                    )
                )
                return

            if (
                t == "expression_statement"
                and node.parent is not None
                and node.parent.type == "module"
            ):
                # Detect __all__ = [...]
                if node.child_count > 0:
                    inner = node.children[0]
                    if inner.type == "assignment":
                        left = inner.child_by_field_name("left")
                        right = inner.child_by_field_name("right")
                        if (
                            left is not None
                            and _text(left, source).strip() == "__all__"
                            and right is not None
                        ):
                            for ch in right.children:
                                if ch.type == "string":
                                    raw = _text(ch, source).strip("'\"")
                                    all_exports.add(raw)
                return

            for ch in node.children:
                walk(ch, parent_qname, parent_id)

        def _collect_calls(body_node, owner_id: str) -> None:
            if body_node is None:
                return
            cursor = body_node.walk()
            visited = False
            stack = [body_node]
            while stack:
                n = stack.pop()
                if n.type == "call":
                    fn = n.child_by_field_name("function")
                    if fn is not None:
                        target = _text(fn, source)
                        edges.append(
                            Edge(
                                src=owner_id,
                                dst=target,
                                kind="calls",
                                resolved=False,
                                span=(n.start_point[0] + 1, n.end_point[0] + 1),
                            )
                        )
                for ch in n.children:
                    stack.append(ch)
            del cursor, visited  # silence linters

        # Walk top-level
        for ch in tree.root_node.children:
            walk(ch, module_qname, module_id)

        # Mark __all__ exports
        if all_exports:
            for sym in symbols:
                if sym["name"] in all_exports:
                    sym["exported"] = True
                    edges.append(
                        Edge(src=module_id, dst=sym["id"], kind="exports", resolved=True, span=None)
                    )

        # Deterministic sort
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


# Static type assertion (for mypy)
_check: LangAdapter = PythonAdapter()  # type: ignore[abstract]
