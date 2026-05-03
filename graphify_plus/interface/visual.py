"""SVG / PNG / DOT cluster renderer.

For a target community (Phase 4 result), emit a layout suitable for
vision-capable models. Output goes into the **target repo's**
``.graphify_plus/visuals/`` directory — never inside graphify-plus.

Visual encoding:

  - Node shape  → kind (rectangle = function, ellipse = class,
                  hexagon = endpoint, cylinder = dependency, note = module)
  - Node colour → layer (blue = ui, green = api, purple = database, …)
                  Phase 9 uses a small built-in palette; later phases
                  can read colours from rules.yaml.
  - Edge style  → kind (solid = call, dashed = import,
                  dotted = inferred / crosses_to)
  - Border     → ``thick`` for gatekeepers (high-betweenness — caller-supplied)

If the ``graphviz`` Python binding is available, render PNG + SVG
deterministically (set ``ordering=out``, ``newrank=true``). Otherwise
write a ``.dot`` source file the user can render with system graphviz.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import networkx as nx

_LAYER_COLOUR = {
    "ui": "#4f8ed8",
    "api": "#3aa55d",
    "database": "#a059c4",
    "service": "#d8a04f",
    "default": "#888888",
}

_KIND_SHAPE = {
    "function": "box",
    "method": "box",
    "class": "ellipse",
    "interface": "ellipse",
    "type": "ellipse",
    "module": "note",
    "endpoint": "hexagon",
    "dependency": "cylinder",
    "external": "plaintext",
}


@dataclass(frozen=True)
class RenderResult:
    dot_path: Path
    svg_path: Path | None
    png_path: Path | None


def _node_attrs(
    attrs: dict, layers: dict[str, list[str]] | None, gatekeepers: set[str]
) -> dict[str, str]:
    kind = attrs.get("kind") or "function"
    label = attrs.get("name") or attrs.get("qualified_name") or attrs.get("id") or ""
    shape = _KIND_SHAPE.get(kind, "box")
    layer = _layer_for(attrs.get("path") or "", layers or {})
    colour = _LAYER_COLOUR.get(layer or "default", _LAYER_COLOUR["default"])
    out = {
        "label": label,
        "shape": shape,
        "style": "filled",
        "fillcolor": colour,
        "fontcolor": "white" if layer else "#222222",
        "color": "#222222",
    }
    if attrs.get("id") in gatekeepers:
        out["penwidth"] = "3"
    return out


def _edge_attrs(data: dict) -> dict[str, str]:
    kind = (data or {}).get("kind") or ""
    if kind in {"calls", "references"}:
        style = "solid"
    elif kind in {"imports", "extends", "implements"}:
        style = "dashed"
    else:
        style = "dotted"
    return {"style": style, "label": kind, "fontsize": "8"}


def _layer_for(path: str, layers: dict[str, list[str]]) -> str | None:
    import fnmatch

    for name, globs in layers.items():
        if any(fnmatch.fnmatch(path, g) for g in globs):
            return name
    return None


def render_cluster(
    G: nx.MultiDiGraph,
    cluster_nodes: list[str],
    out_dir: Path,
    *,
    cluster_id: int,
    layers: dict[str, list[str]] | None = None,
    gatekeepers: set[str] | None = None,
) -> RenderResult:
    """Render the induced subgraph on ``cluster_nodes`` to SVG / PNG / DOT.

    ``out_dir`` should be inside the target repo (typically
    ``<target>/.graphify_plus/visuals/``). Files written:
    ``cluster_<id>.svg``, ``cluster_<id>.png`` (when graphviz binary is
    installed), ``cluster_<id>.dot`` (always).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    base = out_dir / f"cluster_{cluster_id}"
    sub = G.subgraph(cluster_nodes).copy()
    gks = gatekeepers or set()

    # Build the DOT body manually so we can write it deterministically
    # whether or not graphviz Python lib is present.
    lines: list[str] = [
        "digraph G {",
        "  graph [newrank=true, ordering=out];",
        '  node [fontname="Inter,Helvetica,Arial,sans-serif"];',
    ]
    for sid in sorted(sub.nodes):
        a = dict(sub.nodes[sid])
        a["id"] = sid
        attrs = _node_attrs(a, layers, gks)
        attr_str = ", ".join(f'{k}="{v}"' for k, v in sorted(attrs.items()))
        lines.append(f'  "{sid}" [{attr_str}];')
    for u, v, data in sorted(
        sub.edges(data=True), key=lambda t: (t[0], t[1], (t[2] or {}).get("kind") or "")
    ):
        attrs = _edge_attrs(data or {})
        attr_str = ", ".join(f'{k}="{v}"' for k, v in sorted(attrs.items()))
        lines.append(f'  "{u}" -> "{v}" [{attr_str}];')
    lines.append("}")
    dot_text = "\n".join(lines) + "\n"
    dot_path = base.with_suffix(".dot")
    dot_path.write_text(dot_text)

    svg_path: Path | None = None
    png_path: Path | None = None
    try:
        import graphviz  # type: ignore[import-untyped]

        src = graphviz.Source(
            dot_text, engine="dot", filename=str(base.name), directory=str(out_dir)
        )
        # graphviz auto-appends .svg / .png to filename
        try:
            src.format = "svg"
            src.render(cleanup=True)
            svg_path = base.with_suffix(".svg")
        except Exception:  # noqa: BLE001 — system 'dot' binary missing
            svg_path = None
        try:
            src.format = "png"
            src.render(cleanup=True)
            png_path = base.with_suffix(".png")
        except Exception:  # noqa: BLE001
            png_path = None
    except ImportError:
        pass

    return RenderResult(dot_path=dot_path, svg_path=svg_path, png_path=png_path)


__all__ = ["RenderResult", "render_cluster"]
