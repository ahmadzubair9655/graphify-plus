"""Lockfile dependency layer.

Parsers for common lockfiles, attaching a ``dependency`` sub-graph to
the symbol graph. Edges of kind ``depends_on`` connect the repo's
synthetic root node to each declared package.

Supported (best-effort, focused on the most common):

  - npm package-lock.json (v3 ``packages`` map)
  - Python requirements.txt (one pinned/unpinned line per row)
  - poetry.lock (``[[package]]`` blocks — minimal TOML reader, stdlib)

Cargo / yarn / pnpm parsers can land later without changing the
``DependencyEdge`` shape.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

import networkx as nx

from ..core.adapters import make_symbol_id


@dataclass(frozen=True)
class DependencyEdge:
    package: str
    version: str | None
    ecosystem: str  # npm, pypi, cargo, ...
    source: str  # lockfile path


# ---------- parsers ----------------------------------------------------


def parse_npm(path: Path) -> list[DependencyEdge]:
    try:
        data = json.loads(path.read_text(errors="replace"))
    except Exception:  # noqa: BLE001
        return []
    out: list[DependencyEdge] = []
    pkgs = data.get("packages") or {}
    for pkg_path, info in pkgs.items():
        if not pkg_path or pkg_path == "":
            continue
        # node_modules/<name> or node_modules/@scope/<name>
        m = re.match(r"node_modules/((?:@[^/]+/)?[^/]+)$", pkg_path)
        if not m:
            continue
        out.append(
            DependencyEdge(
                package=m.group(1),
                version=info.get("version"),
                ecosystem="npm",
                source=path.as_posix(),
            )
        )
    return out


def parse_requirements_txt(path: Path) -> list[DependencyEdge]:
    out: list[DependencyEdge] = []
    for raw in path.read_text(errors="replace").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        # name[extras]==version | name>=version | bare name
        m = re.match(r"([A-Za-z0-9._\-]+)(?:\[[^\]]*\])?\s*(?:[<>=!~]+\s*([^\s;]+))?", line)
        if not m:
            continue
        out.append(
            DependencyEdge(
                package=m.group(1).lower(),
                version=m.group(2),
                ecosystem="pypi",
                source=path.as_posix(),
            )
        )
    return out


def parse_poetry_lock(path: Path) -> list[DependencyEdge]:
    """Minimal TOML reader — only [[package]] blocks with name/version.
    Avoids a tomllib dependency for older Pythons; adequate for our use.
    """
    out: list[DependencyEdge] = []
    text = path.read_text(errors="replace")
    blocks = re.split(r"^\s*\[\[package\]\]\s*$", text, flags=re.MULTILINE)
    for blk in blocks[1:]:
        nm = re.search(r'name\s*=\s*"([^"]+)"', blk)
        ver = re.search(r'version\s*=\s*"([^"]+)"', blk)
        if not nm:
            continue
        out.append(
            DependencyEdge(
                package=nm.group(1).lower(),
                version=ver.group(1) if ver else None,
                ecosystem="pypi",
                source=path.as_posix(),
            )
        )
    return out


def discover_lockfiles(repo: Path) -> list[Path]:
    out: list[Path] = []
    for name in ("package-lock.json", "requirements.txt", "poetry.lock"):
        out.extend(repo.glob(name))
        out.extend(repo.glob(f"**/{name}"))
    seen: set[Path] = set()
    deduped: list[Path] = []
    for p in out:
        rp = p.resolve()
        if rp in seen:
            continue
        seen.add(rp)
        deduped.append(p)
    return sorted(deduped)


_PARSERS = {
    "package-lock.json": parse_npm,
    "requirements.txt": parse_requirements_txt,
    "poetry.lock": parse_poetry_lock,
}


def parse_all(repo: Path) -> list[DependencyEdge]:
    out: list[DependencyEdge] = []
    for path in discover_lockfiles(repo):
        parser = _PARSERS.get(path.name)
        if parser is None:
            continue
        out.extend(parser(path))
    return out


# ---------- enrichment -------------------------------------------------


def _dep_node_id(edge: DependencyEdge) -> str:
    return make_symbol_id(f"dep::{edge.ecosystem}", edge.package)


def attach_to_graph(repo: Path, G: nx.MultiDiGraph) -> dict:
    """Add a 'dependency' node per package and 'depends_on' edges from a
    synthetic ``repo:<name>`` root (always one root per repo).
    """
    deps = parse_all(repo)
    if not deps:
        return {"packages": 0, "skipped": True}

    repo_root_id = make_symbol_id("repo::", repo.name or "root")
    if not G.has_node(repo_root_id):
        G.add_node(
            repo_root_id,
            id=repo_root_id,
            kind="module",
            name=repo.name,
            qualified_name=f"repo::{repo.name}",
            path="",
            span=(0, 0),
            signature=f"# repo {repo.name}",
            exported=True,
            docstring=None,
            parent_id=None,
            language="repo",
        )

    for d in deps:
        nid = _dep_node_id(d)
        if not G.has_node(nid):
            G.add_node(
                nid,
                id=nid,
                kind="dependency",
                name=d.package,
                qualified_name=f"dep::{d.ecosystem}::{d.package}",
                path=d.source,
                span=(0, 0),
                signature=f"{d.ecosystem}:{d.package}@{d.version or '*'}",
                exported=True,
                docstring=None,
                parent_id=None,
                language="dep",
                ecosystem=d.ecosystem,
                version=d.version,
            )
        G.add_edge(
            repo_root_id,
            nid,
            kind="depends_on",
            resolved=True,
            confidence=1.0,
            span=None,
        )
    return {"packages": len(deps)}


__all__ = [
    "DependencyEdge",
    "attach_to_graph",
    "discover_lockfiles",
    "parse_all",
    "parse_npm",
    "parse_poetry_lock",
    "parse_requirements_txt",
]
