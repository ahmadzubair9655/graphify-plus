"""Layer 16 — GPL: Graphify-Plus Query Language v1.

A small Cypher-flavoured language for code graphs:

* ``MATCH (n:Function)-[:calls*1..3]->(:Function {label: "x"})
   WHERE n.test_coverage < 0.5
   RETURN n.label, n.source_file, n.line_number
   ORDER BY n.degree DESC
   LIMIT 10``
* ``FIND nodes WHERE confidence < 0.6 AND extractor_kind = "LLM"
   COUNT BY source_file``

Read-only by design: writebacks go through a separate API. Big v2
features (multi-graph joins, subqueries) are out of scope.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .indexes import InMemoryGraph

_TOKEN_RE = re.compile(
    r"""
    \s*(?:
        (?P<STRING>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')
      | (?P<NUMBER>\d+(?:\.\d+)?)
      | (?P<IDENT>[A-Za-z_][A-Za-z0-9_]*)
      | (?P<OP>->|<-|==|>=|<=|!=|\*\.\.|\.\.|[(){}\[\]:.,*=<>!|\-])
    )
    """,
    re.VERBOSE,
)


class QueryError(RuntimeError):
    """Raised on a malformed GPL query."""


def _tokenise(src: str) -> list[tuple[str, str]]:
    pos = 0
    tokens: list[tuple[str, str]] = []
    while pos < len(src):
        m = _TOKEN_RE.match(src, pos)
        if not m or m.end() == pos:
            if src[pos].isspace():
                pos += 1
                continue
            raise QueryError(f"Unexpected character {src[pos]!r} at offset {pos}")
        for kind in ("STRING", "NUMBER", "IDENT", "OP"):
            if m.group(kind) is not None:
                tokens.append((kind, m.group(kind)))
                break
        pos = m.end()
    return tokens


@dataclass
class Predicate:
    field: str
    op: str
    value: Any

    def evaluate(self, row: dict[str, Any]) -> bool:
        cur = row.get(self.field)
        if cur is None and self.op not in ("==", "!="):
            return False
        try:
            if self.op == "==":
                return cur == self.value
            if self.op == "!=":
                return cur != self.value
            if self.op == "<":
                return cur < self.value
            if self.op == "<=":
                return cur <= self.value
            if self.op == ">":
                return cur > self.value
            if self.op == ">=":
                return cur >= self.value
        except TypeError:
            return False
        return False


@dataclass
class MatchClause:
    var: str = "n"
    label: str = ""


@dataclass
class EdgeClause:
    kinds: list[str] = field(default_factory=list)
    min_hops: int = 1
    max_hops: int = 1


@dataclass
class Query:
    form: str
    src: MatchClause = field(default_factory=MatchClause)
    edge: EdgeClause | None = None
    dst: MatchClause | None = None
    predicates: list[Predicate] = field(default_factory=list)
    return_fields: list[str] = field(default_factory=list)
    order_by: tuple[str, bool] | None = None
    limit: int = 0
    count_by: str = ""


class _Parser:
    def __init__(self, tokens: list[tuple[str, str]]) -> None:
        self.tokens = tokens
        self.pos = 0

    def peek(self) -> tuple[str | None, str | None]:
        if self.pos < len(self.tokens):
            return self.tokens[self.pos]
        return (None, None)

    def take(self) -> tuple[str, str]:
        if self.pos >= len(self.tokens):
            raise QueryError("Unexpected end of query")
        t = self.tokens[self.pos]
        self.pos += 1
        return t

    def expect(self, kind: str, value: str | None = None) -> tuple[str, str]:
        t = self.take()
        if t[0] != kind or (
            value is not None and (t[1] or "").upper() != value.upper()
        ):
            raise QueryError(f"Expected {value or kind}, got {t[1]!r}")
        return t

    # ---- top-level

    def parse(self) -> Query:
        head = (self.peek()[1] or "").upper()
        if head == "MATCH":
            return self._match()
        if head == "FIND":
            return self._find()
        raise QueryError(f"Unsupported query head: {head!r}")

    # ---- node and edge

    def _node(self) -> MatchClause:
        self.expect("OP", "(")
        var = ""
        label = ""
        first = self.take()
        if first[0] == "IDENT":
            var = first[1]
            nxt = self.take()
        else:
            nxt = first
        if nxt[0] == "OP" and nxt[1] == ":":
            label = self.take()[1]
            nxt = self.take()
        if nxt[0] != "OP" or nxt[1] != ")":
            raise QueryError(f"Expected ')' to close node, got {nxt[1]!r}")
        return MatchClause(var=var or "n", label=label)

    def _edge(self) -> EdgeClause:
        sym = self.take()
        if sym[1] not in ("-", "<-"):
            raise QueryError(f"Expected '-' or '<-', got {sym[1]!r}")
        self.expect("OP", "[")
        self.expect("OP", ":")
        kinds = [self.take()[1]]
        while self.peek()[1] == "|":
            self.take()
            kinds.append(self.take()[1])
        min_hops, max_hops = 1, 1
        nxt = self.take()
        if nxt[0] == "OP" and nxt[1] == "*":
            a_tok = self.take()
            if a_tok[0] != "NUMBER":
                raise QueryError("Expected hop count after '*'")
            min_hops = int(float(a_tok[1]))
            sep = self.take()
            if sep[0] == "OP" and sep[1] == "..":
                b_tok = self.take()
                if b_tok[0] != "NUMBER":
                    raise QueryError("Expected upper hop count")
                max_hops = int(float(b_tok[1]))
                nxt = self.take()
            else:
                max_hops = min_hops
                nxt = sep
        if not (nxt[0] == "OP" and nxt[1] == "]"):
            raise QueryError(f"Expected ']' to close edge spec, got {nxt[1]!r}")
        self.expect("OP", "->")
        return EdgeClause(kinds=kinds, min_hops=min_hops, max_hops=max_hops)

    # ---- predicates and projection

    def _predicates(self) -> list[Predicate]:
        out: list[Predicate] = []
        while True:
            field_name = self._qualified_ident()
            op_tok = self.take()
            if op_tok[0] != "OP":
                raise QueryError(f"Expected comparator, got {op_tok[1]!r}")
            op = op_tok[1]
            if op == "=":
                op = "=="
            if op not in ("==", "!=", "<", "<=", ">", ">="):
                raise QueryError(f"Unsupported operator: {op}")
            value = self._value()
            out.append(Predicate(field=field_name, op=op, value=value))
            if (self.peek()[1] or "").upper() == "AND":
                self.take()
                continue
            break
        return out

    def _qualified_ident(self) -> str:
        tok = self.take()
        if tok[0] != "IDENT":
            raise QueryError(f"Expected identifier, got {tok[1]!r}")
        name = tok[1]
        if self.peek()[0] == "OP" and self.peek()[1] == ".":
            self.take()
            name = self.take()[1]
        return name

    def _value(self) -> Any:
        tok = self.take()
        sign = 1
        if tok[0] == "OP" and tok[1] == "-":
            sign = -1
            tok = self.take()
        if tok[0] == "STRING":
            if sign != 1:
                raise QueryError("unary minus before string")
            return tok[1][1:-1].encode("utf-8").decode("unicode_escape")
        if tok[0] == "NUMBER":
            n = float(tok[1]) if "." in tok[1] else int(tok[1])
            return sign * n
        if tok[0] == "IDENT":
            if tok[1].lower() == "true":
                return True
            if tok[1].lower() == "false":
                return False
            return tok[1]
        raise QueryError(f"Unexpected value: {tok[1]!r}")

    def _return_fields(self) -> list[str]:
        out: list[str] = [self._qualified_ident()]
        while self.peek()[0] == "OP" and self.peek()[1] == ",":
            self.take()
            out.append(self._qualified_ident())
        return out

    def _match(self) -> Query:
        self.expect("IDENT", "MATCH")
        src = self._node()
        edge: EdgeClause | None = None
        dst: MatchClause | None = None
        if self.peek()[0] == "OP" and self.peek()[1] in ("-", "<-"):
            edge = self._edge()
            dst = self._node()
        predicates: list[Predicate] = []
        if (self.peek()[1] or "").upper() == "WHERE":
            self.take()
            predicates = self._predicates()
        self.expect("IDENT", "RETURN")
        fields = self._return_fields()
        order_by: tuple[str, bool] | None = None
        if (self.peek()[1] or "").upper() == "ORDER":
            self.take()
            self.expect("IDENT", "BY")
            ord_field = self._qualified_ident()
            desc = False
            up = (self.peek()[1] or "").upper()
            if up == "DESC":
                self.take()
                desc = True
            elif up == "ASC":
                self.take()
            order_by = (ord_field, desc)
        limit = 0
        if (self.peek()[1] or "").upper() == "LIMIT":
            self.take()
            n_tok = self.take()
            if n_tok[0] != "NUMBER":
                raise QueryError("LIMIT requires a number")
            limit = int(float(n_tok[1]))
        return Query(
            form="MATCH",
            src=src,
            edge=edge,
            dst=dst,
            predicates=predicates,
            return_fields=fields,
            order_by=order_by,
            limit=limit,
        )

    def _find(self) -> Query:
        self.expect("IDENT", "FIND")
        self.expect("IDENT", "nodes")
        self.expect("IDENT", "WHERE")
        predicates = self._predicates()
        count_by = ""
        if (self.peek()[1] or "").upper() == "COUNT":
            self.take()
            self.expect("IDENT", "BY")
            count_by = self._qualified_ident()
        return Query(
            form="FIND",
            predicates=predicates,
            return_fields=["label", "source_file", "line_number"],
            count_by=count_by,
        )


def parse(src: str) -> Query:
    return _Parser(_tokenise(src)).parse()


# ---- execution ----------------------------------------------------------


def _row_for(graph: InMemoryGraph, sid: str) -> dict[str, Any]:
    sym = graph.by_id.get(sid)
    if not sym:
        return {}
    span = sym.get("span") or (0, 0)
    cov = graph.coverage.get(sid)
    return {
        "id": sid,
        "label": sym.get("qualified_name") or sym.get("name") or sid,
        "qualified_name": sym.get("qualified_name") or "",
        "name": sym.get("name") or "",
        "kind": sym.get("kind") or "",
        "source_file": sym.get("path") or "",
        "line_number": int(span[0]) if span else 0,
        "end_line": int(span[1]) if span and len(span) > 1 else 0,
        "degree": len(graph.out_neighbours.get(sid, []))
        + len(graph.in_neighbours.get(sid, [])),
        "test_coverage": float(cov.get("pct", 0.0)) if cov else 0.0,
        "exported": 1 if sym.get("exported") else 0,
        "confidence": 1.0,
    }


def _matches_clause(row: dict[str, Any], clause: MatchClause) -> bool:
    if clause.label and (row.get("kind") or "").lower() != clause.label.lower():
        return False
    return True


def execute(graph: InMemoryGraph, query: Query) -> dict[str, Any]:
    if query.form == "FIND":
        rows = []
        for sid in graph.by_id:
            row = _row_for(graph, sid)
            if all(p.evaluate(row) for p in query.predicates):
                rows.append(row)
        if query.count_by:
            counts: dict[Any, int] = {}
            for r in rows:
                key = r.get(query.count_by, "?")
                counts[key] = counts.get(key, 0) + 1
            return {
                "form": "FIND",
                "count_by": query.count_by,
                "rows": [
                    {"value": k, "count": v}
                    for k, v in sorted(counts.items(), key=lambda kv: -kv[1])
                ],
            }
        return _project(rows, query)

    src_rows: list[dict[str, Any]] = []
    for sid in graph.by_id:
        row = _row_for(graph, sid)
        if not _matches_clause(row, query.src):
            continue
        if query.predicates and not all(p.evaluate(row) for p in query.predicates):
            continue
        src_rows.append(row)
    if query.edge is None:
        return _project(src_rows, query)
    out_rows: list[dict[str, Any]] = []
    for src in src_rows:
        for tgt in _reachable(graph, src["id"], query.edge, query.dst):
            row = dict(src)
            for k, v in tgt.items():
                row[f"dst_{k}"] = v
            out_rows.append(row)
    return _project(out_rows, query)


def _reachable(
    graph: InMemoryGraph,
    src: str,
    edge: EdgeClause,
    dst: MatchClause | None,
) -> list[dict[str, Any]]:
    seen: set[str] = {src}
    frontier: list[tuple[str, int]] = [(src, 0)]
    out: list[dict[str, Any]] = []
    while frontier:
        node, depth = frontier.pop(0)
        if depth >= edge.max_hops:
            continue
        for dst_sid, kind, _span in graph.out_neighbours.get(node, []):
            if edge.kinds and kind not in edge.kinds:
                continue
            if dst_sid in seen:
                continue
            seen.add(dst_sid)
            new_depth = depth + 1
            row = _row_for(graph, dst_sid)
            if not row:
                continue
            if dst is None or _matches_clause(row, dst):
                if new_depth >= edge.min_hops:
                    out.append(row)
            if new_depth < edge.max_hops:
                frontier.append((dst_sid, new_depth))
    return out


def _project(rows: list[dict[str, Any]], query: Query) -> dict[str, Any]:
    if query.order_by:
        f, desc = query.order_by
        rows = sorted(rows, key=lambda r: r.get(f, 0) or 0, reverse=bool(desc))
    limit = query.limit or 200
    return {
        "form": query.form,
        "count": len(rows),
        "returned": min(len(rows), limit),
        "rows": [{f: r.get(f) for f in query.return_fields} for r in rows[:limit]],
    }


# ---- NL → GPL ------------------------------------------------------------

NL_HINTS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"untested|no tests?", re.IGNORECASE), "test_coverage < 0.1"),
    (re.compile(r"public api|exported", re.IGNORECASE), "exported == 1"),
    (re.compile(r"low coverage|under \d+%", re.IGNORECASE), "test_coverage < 0.5"),
    (re.compile(r"\bfunctions?\b", re.IGNORECASE), "kind == 'function'"),
    (re.compile(r"\bclasses?\b", re.IGNORECASE), "kind == 'class'"),
]


def translate_nl(text: str) -> str:
    """Crude rule-based NL → GPL fallback. Real production uses an LLM."""
    parts = [frag for pat, frag in NL_HINTS if pat.search(text)]
    if not parts:
        parts = ["confidence < 0.6"]
    return f"FIND nodes WHERE {' AND '.join(parts)}"


__all__ = [
    "EdgeClause",
    "MatchClause",
    "Predicate",
    "Query",
    "QueryError",
    "execute",
    "parse",
    "translate_nl",
]
