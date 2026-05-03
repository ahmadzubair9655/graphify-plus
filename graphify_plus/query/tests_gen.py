"""Edge-centric test scaffolds.

For each edge ``(A, B, kind)`` in a target subgraph, emit a test
template in the appropriate language/framework, detected from the file
paths involved. The test asserts the contract implied by the edge —
``calls`` → "A's caller invokes B and gets back …", ``imports`` → "A
exposes B at its expected path", ``crosses_to`` → "A's frontend hits
B's endpoint with a payload that matches the OpenAPI schema".

Intentionally minimal — these are *scaffolds* the developer fills in.
No network, no mocking framework choice baked in.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import networkx as nx
from jinja2 import Environment, select_autoescape


@dataclass(frozen=True)
class TestScaffold:
    edge_kind: str
    src_qname: str
    dst_qname: str
    language: str  # python | typescript | javascript | go | other
    body: str


_ENV = Environment(autoescape=select_autoescape(["html", "xml"]))

_TEMPLATES = {
    "python": (
        "def test_{slug}() -> None:\n"
        '    """{kind}: {src} → {dst}\n'
        "\n"
        "    Auto-generated edge scaffold. Fill in the assertions.\n"
        '    """\n'
        "    # arrange: build inputs that exercise the {kind} edge\n"
        "    # act:     call the source path so it reaches the target\n"
        "    # assert:  the target was invoked with expected args / returned shape\n"
        '    raise AssertionError("scaffold not yet implemented")\n'
    ),
    "typescript": (
        "import {{ describe, it, expect }} from 'vitest';\n"
        "\n"
        "describe('{src} → {dst} ({kind})', () => {{\n"
        "  it('exercises the edge', () => {{\n"
        "    // arrange: build inputs that exercise the {kind} edge\n"
        "    // act:     call the source path so it reaches the target\n"
        "    // assert:  the target was invoked with expected args / returned shape\n"
        "    expect.fail('scaffold not yet implemented');\n"
        "  }});\n"
        "}});\n"
    ),
    "javascript": (
        "const {{ describe, it, expect }} = require('vitest');\n"
        "\n"
        "describe('{src} → {dst} ({kind})', () => {{\n"
        "  it('exercises the edge', () => {{\n"
        "    expect.fail('scaffold not yet implemented');\n"
        "  }});\n"
        "}});\n"
    ),
    "go": (
        "package generated\n"
        "\n"
        'import "testing"\n'
        "\n"
        "func Test_{slug}(t *testing.T) {{\n"
        "    // {kind}: {src} → {dst}\n"
        '    t.Skip("scaffold not yet implemented")\n'
        "}}\n"
    ),
    "default": ("// {kind}: {src} → {dst}\n// scaffold not yet implemented\n"),
}


def _slug(s: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in s).strip("_")


def _language_for(language: str | None) -> str:
    if language in {"python", "typescript", "javascript", "go"}:
        return language
    return "default"


def render(scaffold_input: dict[str, str]) -> str:
    """Render the templated body using a Python format-string fallback
    (Jinja is overkill for these single-paragraph scaffolds; we use it
    only for autoescape consistency with downstream callers).
    """
    template = _TEMPLATES.get(scaffold_input["language"], _TEMPLATES["default"])
    return template.format(**scaffold_input)


def for_subgraph(
    G: nx.MultiDiGraph, target_paths: Iterable[str] | None = None
) -> list[TestScaffold]:
    """Yield one TestScaffold per edge in the (optionally path-scoped) subgraph."""
    paths = set(target_paths) if target_paths else None
    out: list[TestScaffold] = []
    seen: set[tuple[str, str, str]] = set()
    for u, v, data in G.edges(data=True):
        kind = (data or {}).get("kind") or ""
        if kind in {"contains", "exports", "depends_on"}:
            continue  # purely structural — no test contract
        u_attrs = G.nodes.get(u) or {}
        v_attrs = G.nodes.get(v) or {}
        if paths is not None and (
            u_attrs.get("path") not in paths and v_attrs.get("path") not in paths
        ):
            continue
        src_q = u_attrs.get("qualified_name") or u
        dst_q = v_attrs.get("qualified_name") or v
        key = (src_q, dst_q, kind)
        if key in seen:
            continue
        seen.add(key)
        lang = _language_for(u_attrs.get("language") or v_attrs.get("language"))
        body = render(
            {
                "language": lang,
                "kind": kind,
                "src": src_q,
                "dst": dst_q,
                "slug": _slug(f"{src_q}_to_{dst_q}_{kind}"),
            }
        )
        out.append(
            TestScaffold(edge_kind=kind, src_qname=src_q, dst_qname=dst_q, language=lang, body=body)
        )
    out.sort(key=lambda s: (s.language, s.src_qname, s.dst_qname, s.edge_kind))
    return out


def write_scaffolds(scaffolds: list[TestScaffold], out_dir: Path) -> list[Path]:
    """Write each scaffold to a per-edge file under ``out_dir``.
    Filename: ``test_<slug>.<ext>`` (or ``<slug>_test.go`` for Go).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for sc in scaffolds:
        slug = _slug(f"{sc.src_qname}_to_{sc.dst_qname}_{sc.edge_kind}")
        if sc.language == "python":
            name = f"test_{slug}.py"
        elif sc.language in {"typescript", "javascript"}:
            ext = "ts" if sc.language == "typescript" else "js"
            name = f"{slug}.test.{ext}"
        elif sc.language == "go":
            name = f"{slug}_test.go"
        else:
            name = f"test_{slug}.txt"
        path = out_dir / name
        path.write_text(sc.body)
        written.append(path)
    return written


__all__ = ["TestScaffold", "for_subgraph", "render", "write_scaffolds"]
