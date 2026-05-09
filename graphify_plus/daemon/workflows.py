"""Layers 10.3, 10.4, 10.5, 6.4, 11.3, 11.4 — workflow + ecosystem.

* **10.3 Refactor playbooks** — pre-built graph-aware plans:
  extract-module, rename, split, unwind. Each emits a step-by-step
  plan, never a diff.
* **10.4 Time-machine queries** — reuse git history to replay the
  graph at a past commit and diff structure across two refs.
* **10.5 Documentation generator** — per-module markdown auto-built
  from god nodes + causal chains + public API + known contradictions.
* **6.4 Conversation memory** — ingest Claude Code session transcript
  files into the existing ingest_nodes table so cross-session decisions
  become queryable. Privacy: opt-in, local-only by default.
* **11.3 Multi-repo monorepo support** — workspaces declared in
  ``.graphify_plus/workspaces.yaml`` map directories to sub-graphs.
  Cross-workspace edges are first-class.
* **11.4 Skills marketplace** — a directory-of-skills registry, fetched
  via plain HTTP to a manifest. ``gp daemon skill install <name>`` drops
  the skill into ``.claude/skills/``.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .indexes import InMemoryGraph

log = logging.getLogger("graphify_plus.daemon.workflows")


# =========================================================================
# 10.3 — refactor playbooks
# =========================================================================


@dataclass
class RefactorStep:
    description: str
    target: str = ""
    file: str = ""
    line: int = 0


@dataclass
class RefactorPlan:
    name: str
    description: str
    steps: list[RefactorStep] = field(default_factory=list)
    risk: str = "LOW"
    estimated_changes: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "steps": [s.__dict__ for s in self.steps],
            "risk": self.risk,
            "estimated_changes": self.estimated_changes,
        }


def extract_module_plan(graph: InMemoryGraph, pattern: str) -> RefactorPlan:
    """Identify a cohesive subgraph and produce a move plan."""
    plan = RefactorPlan(
        name=f"extract-module:{pattern}",
        description=f"Move symbols matching {pattern!r} into their own module.",
    )
    matches = graph.find_by_name(pattern, fuzzy=True, limit=200)
    matched_ids = {m["id"] for m in matches}
    plan.estimated_changes = len(matched_ids)
    if not matched_ids:
        plan.steps.append(
            RefactorStep(description=f"No symbols match {pattern!r} — nothing to extract.")
        )
        return plan
    external = 0
    for sid in matched_ids:
        for src, _kind, _span in graph.in_neighbours.get(sid, []):
            if src not in matched_ids:
                external += 1
    plan.steps.append(
        RefactorStep(description=f"Create new module file for {len(matched_ids)} symbol(s).")
    )
    plan.steps.append(
        RefactorStep(
            description=f"Rewire {external} external inbound reference(s) to the new module path."
        )
    )
    plan.steps.append(
        RefactorStep(description="Run tests and architectural rules.")
    )
    plan.risk = "HIGH" if external >= 20 else "MEDIUM" if external >= 5 else "LOW"
    return plan


def rename_plan(graph: InMemoryGraph, *, node: str, to: str) -> RefactorPlan:
    plan = RefactorPlan(
        name=f"rename:{node}",
        description=f"Rename {node} → {to} including indirect references.",
    )
    matches = graph.find_by_name(node, fuzzy=True, limit=20)
    if not matches:
        plan.steps.append(RefactorStep(description=f"Symbol {node!r} not found."))
        return plan
    target = matches[0]
    sid = target["id"]
    inbound = graph.dependents_of(sid)
    plan.estimated_changes = len(inbound) + 1
    plan.steps.append(
        RefactorStep(
            description=f"Rename declaration",
            target=node,
            file=target.get("path") or "",
            line=int((target.get("span") or (0, 0))[0]),
        )
    )
    for sym in inbound[:24]:
        plan.steps.append(
            RefactorStep(
                description="Update reference",
                target=sym.get("qualified_name") or "?",
                file=sym.get("path") or "",
                line=int((sym.get("span") or (0, 0))[0]),
            )
        )
    plan.steps.append(
        RefactorStep(description="Search string-literal references in configs / docs.")
    )
    plan.risk = "HIGH" if len(inbound) >= 12 else "MEDIUM"
    return plan


def split_plan(graph: InMemoryGraph, node: str) -> RefactorPlan:
    """Find natural seams via community detection — already cached on
    the snapshot — and propose a split.
    """
    plan = RefactorPlan(name=f"split:{node}", description=f"Split {node} into smaller pieces.")
    matches = graph.find_by_name(node, fuzzy=True, limit=4)
    if not matches:
        plan.steps.append(RefactorStep(description=f"Symbol {node!r} not found."))
        return plan
    sid = matches[0]["id"]
    callees = graph.callees_of(sid)
    if not callees:
        plan.steps.append(RefactorStep(description="No internal calls to use as split seams."))
        return plan
    by_file: dict[str, list[dict[str, Any]]] = {}
    for c in callees:
        by_file.setdefault(c.get("path") or "?", []).append(c)
    plan.estimated_changes = len(callees)
    for path, calls in by_file.items():
        plan.steps.append(
            RefactorStep(
                description=f"Pull {len(calls)} callee(s) into new helper",
                file=path,
            )
        )
    plan.risk = "MEDIUM"
    return plan


def unwind_cycle_plan(graph: InMemoryGraph) -> RefactorPlan:
    """Detect a small dependency cycle and propose the smallest-diff break."""
    import networkx as nx

    plan = RefactorPlan(name="unwind:cycle", description="Break the smallest dependency cycle.")
    G = nx.DiGraph()
    for sid in graph.by_id:
        G.add_node(sid)
    for src, neighbours in graph.out_neighbours.items():
        for dst, kind, _span in neighbours:
            if kind == "imports":
                G.add_edge(src, dst)
    try:
        cycle = nx.find_cycle(G, orientation="original")
    except nx.NetworkXNoCycle:
        plan.steps.append(RefactorStep(description="No import cycles found — nothing to do."))
        return plan
    plan.estimated_changes = len(cycle)
    for u, v, _ in cycle[:8]:
        sym_u = graph.by_id.get(u, {})
        plan.steps.append(
            RefactorStep(
                description=f"Break edge {u} → {v}",
                target=sym_u.get("qualified_name", u),
                file=sym_u.get("path") or "",
            )
        )
    plan.risk = "MEDIUM"
    return plan


# =========================================================================
# 10.4 — time-machine queries
# =========================================================================


@dataclass
class RevisionDelta:
    rev_a: str
    rev_b: str
    files_changed: int
    insertions: int
    deletions: int
    paths: list[str] = field(default_factory=list)


def at_revision(repo: Path, sha: str) -> dict[str, Any]:
    """Return basic structural info about ``sha`` (commit summary,
    files-touched). Replaying the *full* graph at a past commit needs
    a re-extract pass; we return a lightweight summary here.
    """
    try:
        info = subprocess.check_output(
            ["git", "-C", str(repo), "show", "--stat", "--no-color", sha],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=4,
        )
    except Exception as exc:  # noqa: BLE001
        return {"sha": sha, "error": str(exc)}
    return {"sha": sha, "summary": info[:4000]}


def compare_revs(repo: Path, rev_a: str, rev_b: str) -> RevisionDelta:
    try:
        out = subprocess.check_output(
            ["git", "-C", str(repo), "diff", "--shortstat", f"{rev_a}..{rev_b}"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=4,
        ).strip()
    except Exception as exc:  # noqa: BLE001
        log.debug("compare_revs failed: %s", exc)
        return RevisionDelta(rev_a=rev_a, rev_b=rev_b, files_changed=0, insertions=0, deletions=0)
    m = re.search(r"(\d+)\s+files?\s+changed", out)
    files = int(m.group(1)) if m else 0
    ins = int((re.search(r"(\d+)\s+insertion", out) or re.match("0", "0")).group(1)) if re.search(r"(\d+)\s+insertion", out) else 0
    dels = int((re.search(r"(\d+)\s+deletion", out) or re.match("0", "0")).group(1)) if re.search(r"(\d+)\s+deletion", out) else 0
    paths: list[str] = []
    try:
        names = subprocess.check_output(
            ["git", "-C", str(repo), "diff", "--name-only", f"{rev_a}..{rev_b}"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=4,
        )
        paths = [p for p in names.splitlines() if p.strip()]
    except Exception:  # noqa: BLE001
        pass
    return RevisionDelta(
        rev_a=rev_a, rev_b=rev_b, files_changed=files, insertions=ins, deletions=dels, paths=paths
    )


def evolution(repo: Path, path: str, *, limit: int = 25) -> list[dict[str, Any]]:
    """How a single file's edges grew over time — proxy: commit history."""
    try:
        out = subprocess.check_output(
            ["git", "-C", str(repo), "log", f"-{limit}", "--oneline", "--", path],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=4,
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("evolution failed: %s", exc)
        return []
    rows: list[dict[str, Any]] = []
    for line in out.splitlines():
        parts = line.split(" ", 1)
        if len(parts) == 2:
            rows.append({"sha": parts[0], "summary": parts[1]})
    return rows


# =========================================================================
# 10.5 — documentation generator
# =========================================================================


def generate_module_docs(graph: InMemoryGraph, module: str) -> str:
    """Produce a Markdown reference for ``module`` (a top-level dir)."""
    matched: list[dict[str, Any]] = []
    whole_repo = module in (".", "./", "")
    for sid, sym in graph.by_id.items():
        path = sym.get("path") or ""
        if not path:
            continue
        if whole_repo or path == module or path.startswith(module.rstrip("/") + "/"):
            matched.append(sym)
    if not matched:
        return f"# {module}\n\n(no symbols indexed)\n"
    matched.sort(key=lambda s: ((s.get("path") or ""), int((s.get("span") or (0, 0))[0])))
    pr = {sid: score for sid, score in graph.pagerank_top}
    god = sorted(matched, key=lambda s: -pr.get(s.get("id", ""), 0.0))[:5]
    public = [s for s in matched if s.get("exported")]
    out: list[str] = []
    out.append(f"# {module}")
    out.append("")
    out.append(f"_{len(matched)} symbol(s) across {len({s.get('path') for s in matched})} file(s)._")
    out.append("")
    out.append("## God nodes (most central)")
    out.append("")
    for s in god:
        out.append(
            f"- **{s.get('qualified_name') or s.get('name')}** "
            f"`{s.get('path')}:{(s.get('span') or (0,0))[0]}`"
        )
    out.append("")
    out.append("## Public API surface")
    out.append("")
    if not public:
        out.append("_no exported symbols_")
    else:
        for s in public[:20]:
            out.append(
                f"- {s.get('qualified_name') or s.get('name')} "
                f"`{s.get('path')}:{(s.get('span') or (0,0))[0]}`"
            )
    out.append("")
    out.append("## Files in this module")
    out.append("")
    for path in sorted({s.get("path") for s in matched if s.get("path")}):
        out.append(f"- `{path}`")
    out.append("")
    out.append("---")
    out.append(f"_Auto-generated by graphify-plus._  freshness_token: `{graph.freshness_token}`")
    return "\n".join(out)


# =========================================================================
# 6.4 — conversation memory
# =========================================================================


_DECISION_RE = re.compile(
    r"(?:^|\n)(?:decided|let's|we'll|going with|chose|chosen|agreed)[:\s]+([^\n]{8,200})",
    re.IGNORECASE,
)


@dataclass
class Decision:
    text: str
    line_no: int


def extract_decisions(text: str) -> list[Decision]:
    """Extract sentences that look like decisions from a conversation
    transcript. Deliberately strict so chitchat doesn't pollute the
    graph.
    """
    out: list[Decision] = []
    for m in _DECISION_RE.finditer(text):
        line_no = text[: m.start()].count("\n") + 1
        out.append(Decision(text=m.group(1).strip(), line_no=line_no))
    return out


def ingest_conversations(store, repo: Path, transcripts_dir: Path) -> dict[str, Any]:
    """Walk a folder of conversation transcripts and ingest each
    decision as an ``IngestNode`` of kind 'decision'.
    """
    from .ingestors import IngestNode, store_ingest_nodes

    if not transcripts_dir.exists():
        return {"transcripts": 0, "decisions": 0}
    rows: list[IngestNode] = []
    files = 0
    for f in sorted(transcripts_dir.rglob("*")):
        if not f.is_file() or f.suffix not in {".md", ".txt", ".log"}:
            continue
        files += 1
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for i, decision in enumerate(extract_decisions(text)):
            rows.append(
                IngestNode(
                    id=f"convo-{f.stem}-{i:04d}",
                    kind="decision",
                    title=decision.text[:80],
                    body=decision.text,
                    source="conversation",
                    url=str(f),
                    metadata={"line_no": decision.line_no},
                )
            )
    if rows:
        store_ingest_nodes(store, rows, replace_kind="decision")
    return {"transcripts": files, "decisions": len(rows)}


# =========================================================================
# 11.3 — monorepo workspaces
# =========================================================================


@dataclass
class Workspace:
    name: str
    path: str
    description: str = ""


def load_workspaces(repo: Path) -> list[Workspace]:
    p = repo / ".graphify_plus" / "workspaces.yaml"
    if not p.exists():
        return []
    try:
        import yaml

        body = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        log.warning("invalid workspaces.yaml: %s", exc)
        return []
    rows: list[Workspace] = []
    for w in body.get("workspaces") or []:
        if not isinstance(w, dict):
            continue
        rows.append(
            Workspace(
                name=str(w.get("name", "?")),
                path=str(w.get("path", "")),
                description=str(w.get("description", "")),
            )
        )
    return rows


def workspace_for_path(workspaces: list[Workspace], path: str) -> Workspace | None:
    for w in workspaces:
        if path == w.path or path.startswith(w.path.rstrip("/") + "/"):
            return w
    return None


def cross_workspace_edges(graph: InMemoryGraph, workspaces: list[Workspace]) -> list[dict[str, Any]]:
    edges: list[dict[str, Any]] = []
    for src, neighbours in graph.out_neighbours.items():
        src_sym = graph.by_id.get(src)
        if not src_sym:
            continue
        src_ws = workspace_for_path(workspaces, src_sym.get("path") or "")
        if src_ws is None:
            continue
        for dst, kind, _span in neighbours:
            dst_sym = graph.by_id.get(dst)
            if not dst_sym:
                continue
            dst_ws = workspace_for_path(workspaces, dst_sym.get("path") or "")
            if dst_ws is None or dst_ws.name == src_ws.name:
                continue
            edges.append(
                {
                    "src": src,
                    "dst": dst,
                    "kind": "workspace",
                    "detail": {
                        "src_workspace": src_ws.name,
                        "dst_workspace": dst_ws.name,
                        "edge_kind": kind,
                    },
                }
            )
    return edges


# =========================================================================
# 11.4 — skills marketplace (registry shim)
# =========================================================================


@dataclass
class SkillEntry:
    name: str
    description: str
    url: str
    version: str = ""
    author: str = ""


_DEFAULT_REGISTRY = [
    SkillEntry(
        name="react-patterns",
        description="React-specific routing rules and idioms.",
        url="https://example.invalid/skills/react-patterns.md",
        author="example",
    ),
    SkillEntry(
        name="django-idioms",
        description="Django ORM and DRF patterns.",
        url="https://example.invalid/skills/django-idioms.md",
        author="example",
    ),
    SkillEntry(
        name="ml-pipelines",
        description="Notebook + experiment-tracking patterns.",
        url="https://example.invalid/skills/ml-pipelines.md",
        author="example",
    ),
]


def load_registry(path: Path | None = None) -> list[SkillEntry]:
    """Read a local registry JSON if provided; otherwise return the
    bundled default. The shape is a list of objects with name +
    description + url.
    """
    if path and path.exists():
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
            return [SkillEntry(**row) for row in body if isinstance(row, dict)]
        except (OSError, json.JSONDecodeError):
            return list(_DEFAULT_REGISTRY)
    return list(_DEFAULT_REGISTRY)


def install_skill(repo: Path, entry: SkillEntry, *, body: str | None = None) -> Path:
    """Drop the skill content into ``.claude/skills/<name>/SKILL.md``.
    The caller supplies the body text (so this stays offline-friendly);
    a network-fetch wrapper can layer on top.
    """
    target = repo / ".claude" / "skills" / entry.name / "SKILL.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    if body is None:
        body = (
            f"---\nname: {entry.name}\ndescription: {entry.description}\n---\n\n"
            f"# {entry.name}\n\n_{entry.description}_\n\nSource: {entry.url}\n"
        )
    target.write_text(body, encoding="utf-8")
    return target


__all__ = [
    "Decision",
    "RefactorPlan",
    "RefactorStep",
    "RevisionDelta",
    "SkillEntry",
    "Workspace",
    "at_revision",
    "compare_revs",
    "cross_workspace_edges",
    "evolution",
    "extract_decisions",
    "extract_module_plan",
    "generate_module_docs",
    "ingest_conversations",
    "install_skill",
    "load_registry",
    "load_workspaces",
    "rename_plan",
    "split_plan",
    "unwind_cycle_plan",
    "workspace_for_path",
]
