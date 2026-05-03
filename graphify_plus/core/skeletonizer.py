"""Deterministic, language-agnostic skeleton generator.

A *skeleton* is the smallest representation of a symbol that preserves
enough information for an AI to reason about its API surface — signature,
kind, exports, first-sentence docstring — while omitting the body.

Format (deterministic, line-oriented):

    @<language> <kind> <qualified_name>
    <signature>
      « body omitted (<line_count> lines) »
    [docstring (first sentence)]
    [exports: <comma-list>]   # only at module-level if any

Skeletons are content-addressed via SHA-256 (truncated to 16 hex chars).
Two symbols whose skeletons are byte-identical share one CAS row — common
for boilerplate forms, generated CRUD handlers, identical React props
shells across files.

Property: ``skeletonize(s) == skeletonize(s)`` for every symbol — re-running
ingest on an unchanged tree must produce identical CAS hashes.
"""

from __future__ import annotations

from collections.abc import Iterable

from .adapters import Symbol


def _first_sentence(s: str | None) -> str | None:
    if not s:
        return None
    s = s.strip()
    if not s:
        return None
    for sep in (". ", "\n"):
        if sep in s:
            return s.split(sep, 1)[0].strip().rstrip(".")
    return s.rstrip(".")


def _line_count(symbol: Symbol) -> int:
    span = symbol.get("span") or (1, 1)
    return max(0, int(span[1]) - int(span[0]) + 1)


def skeletonize(symbol: Symbol, *, exports: Iterable[str] | None = None) -> str:
    """Return the canonical skeleton string for ``symbol``.

    Output is deterministic: identical inputs always produce byte-identical
    output. No timestamps, no IDs, no path-to-string formatting variation.
    """
    lang = symbol.get("language") or "unknown"
    kind = symbol.get("kind") or "symbol"
    qname = symbol.get("qualified_name") or symbol.get("name") or "<unknown>"
    sig = " ".join((symbol.get("signature") or "").split())
    lines: list[str] = [f"@{lang} {kind} {qname}", sig]

    body_lines = _line_count(symbol)
    if body_lines > 1 and kind in {"function", "method", "class"}:
        lines.append(f"  « body omitted ({body_lines} lines) »")

    doc = _first_sentence(symbol.get("docstring"))
    if doc:
        lines.append(doc)

    if kind == "module" and exports:
        ex = sorted({e for e in exports if e})
        if ex:
            lines.append("exports: " + ", ".join(ex))

    return "\n".join(lines) + "\n"


def skeletonize_all(
    symbols: list[Symbol],
    *,
    exports_by_module: dict[str, list[str]] | None = None,
) -> dict[str, str]:
    """Return ``{symbol_id: skeleton_string}`` for every symbol.

    ``exports_by_module`` lets callers pass module-level export lists so
    the module skeleton renders an ``exports:`` line.
    """
    out: dict[str, str] = {}
    eb = exports_by_module or {}
    for s in symbols:
        sid = s.get("id")
        if not sid:
            continue
        ex = eb.get(sid) or eb.get(s.get("qualified_name") or "")
        out[sid] = skeletonize(s, exports=ex)
    return out


__all__ = ["skeletonize", "skeletonize_all"]
