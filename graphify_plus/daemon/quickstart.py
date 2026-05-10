"""Layer 5.3 — first-run UX. ``gp quickstart .`` runs the whole pipeline:

1. ``gp init`` if no cache
2. Build a fresh in-memory snapshot (warm the daemon)
3. Start the watcher daemon detached
4. Install the SKILL.md + pre-grep hook (Layer 4.2)
5. Drop default routing recipes
6. Print a one-screen "you can now ask Claude these things" tutorial
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

log = logging.getLogger("graphify_plus.daemon.quickstart")


def run_quickstart(repo: Path) -> dict[str, Any]:
    """Run the full first-run pipeline. Returns a structured summary of
    what happened so the CLI can render it.

    Each step is best-effort: a failed init falls through to a
    soft-warning instead of crashing.
    """
    from ..core import cas
    from ..core.ingest import ingest as ingest_repo
    from ..core.skeletonizer import skeletonize_all
    from ..runtime.store import Store, cache_path
    from .indexes import InMemoryGraph
    from .tutorial import install_recipes

    repo = repo.resolve()
    out: dict[str, Any] = {"repo": str(repo), "steps": []}

    cache = cache_path(repo)
    if not cache.exists():
        try:
            res = ingest_repo(repo, parallel=True)
            store = Store(cache)
            try:
                store.replace_all(res.symbols, res.edges)
                store.set_meta("repo_root", str(repo))
                skeletons = skeletonize_all(res.symbols)
                for sid, body in skeletons.items():
                    h = cas.put(store, body)
                    store.link_skeleton(sid, h)
            finally:
                store.close()
            out["steps"].append(
                {"step": "init", "files_parsed": res.files_parsed, "symbols": len(res.symbols)}
            )
        except Exception as exc:  # noqa: BLE001
            out["steps"].append({"step": "init", "error": str(exc)})
    else:
        out["steps"].append({"step": "init", "skipped": "cache already exists"})

    try:
        store = Store(cache)
        try:
            snap = InMemoryGraph.from_store(store, repo)
        finally:
            store.close()
        out["steps"].append(
            {
                "step": "snapshot",
                "symbols": len(snap.by_id),
                "files": len(snap.by_path),
                "build_ms": snap.stats.elapsed_ms if snap.stats else 0.0,
            }
        )
    except Exception as exc:  # noqa: BLE001
        out["steps"].append({"step": "snapshot", "error": str(exc)})

    # Install routing skill + pre-grep hook by reusing the existing
    # 'gp daemon install' code path so behaviour stays consistent.
    try:
        import shutil
        from importlib import resources

        template_root = resources.files("graphify_plus.daemon.templates")
        targets = {
            "SKILL.md": repo / ".claude" / "skills" / "graphify-plus" / "SKILL.md",
            "pre_grep_hook.py": repo / ".claude" / "hooks" / "pre_grep_hook.py",
        }
        installed: list[str] = []
        for src_name, dst in targets.items():
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():
                continue
            with resources.as_file(template_root / src_name) as src_path:
                shutil.copyfile(src_path, dst)
            if dst.suffix == ".py":
                dst.chmod(0o755)
            installed.append(str(dst))
        out["steps"].append({"step": "install-skill+hook", "installed": installed})
    except Exception as exc:  # noqa: BLE001
        out["steps"].append({"step": "install-skill+hook", "error": str(exc)})

    try:
        recipes = install_recipes(repo)
        out["steps"].append({"step": "recipes", "installed": len(recipes)})
    except Exception as exc:  # noqa: BLE001
        out["steps"].append({"step": "recipes", "error": str(exc)})

    # Capture today's adoption rate as the comparison baseline. Without
    # this every future `gp daemon adoption` is an absolute number with
    # no comparison — and the rewrite's whole premise is "this changed
    # Claude's behaviour vs the prior state." Set on every install for
    # free so the delta view is the default.
    try:
        from .adoption import adoption_report, write_baseline

        rep = adoption_report(repo)
        baseline_file = write_baseline(repo, rep, label="quickstart")
        out["steps"].append(
            {
                "step": "adoption-baseline",
                "label": "quickstart",
                "captured_rate": rep.adoption_rate,
                "path": str(baseline_file),
            }
        )
    except Exception as exc:  # noqa: BLE001
        out["steps"].append({"step": "adoption-baseline", "error": str(exc)})

    return out


def render_quickstart(out: dict[str, Any]) -> str:
    lines: list[str] = ["# graphify-plus quickstart"]
    lines.append(f"_Repo: {out.get('repo')}_")
    lines.append("")
    for step in out.get("steps", []):
        name = step.get("step")
        if "error" in step:
            lines.append(f"  ✗ {name:<22}  error: {step['error']}")
        elif "skipped" in step:
            lines.append(f"  · {name:<22}  skipped ({step['skipped']})")
        else:
            badges = []
            for k, v in step.items():
                if k == "step":
                    continue
                badges.append(f"{k}={v}")
            lines.append(f"  ✓ {name:<22}  {'  '.join(badges)}")
    lines.append("")
    lines.append("## You can now ask Claude things like:")
    lines.append("")
    lines.append("  • 'who calls AuthService.login?'  (graph: who_calls)")
    lines.append("  • 'what untested code did I just touch?'  (graph: review)")
    lines.append("  • 'walk me through this codebase'  (graph: onboard)")
    lines.append("  • 'plan adding rate limiting to all endpoints'  (graph: plan)")
    lines.append("")
    lines.append("Next steps:")
    lines.append("  • gp daemon start --detach    # warm in-memory cache")
    lines.append("  • gp daemon tutorial          # 10-question walkthrough")
    lines.append("  • gp daemon claude-md update  # write CLAUDE.md owned section")
    return "\n".join(lines)


__all__ = ["render_quickstart", "run_quickstart"]
