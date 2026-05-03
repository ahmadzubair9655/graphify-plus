"""TypeScript / JavaScript path-alias resolution.

Reads ``tsconfig.json`` / ``tsconfig.*.json`` / ``jsconfig.json`` at the
repo root, parses ``compilerOptions.paths`` + ``compilerOptions.baseUrl``,
and exposes :func:`resolve_alias_imports` which post-processes ingest's
import edges:

  - Extracts the literal source string from each TS/JS import edge's
    ``dst`` (which currently holds the whole ``import ... from '...';``
    statement).
  - If the source begins with a configured alias (``@/...``,
    ``~/...``, anything in ``paths``), expands it to a repo-relative
    path and tries to find an indexed module symbol with that path.
  - On success, rewrites the edge: ``dst`` becomes the resolved
    module symbol id, ``resolved=True``, and ``confidence`` is bumped
    from ``CONF_FALLBACK`` to ``CONF_RESOLVED``.

This fixes Bug 5: every TS import using ``@/`` aliases came back as
``resolved=false`` because the parser worked file-by-file with no view
of the repo's ``tsconfig.json``. The unresolved-imports audit probe
scored F on every TS project, the trust score was artificially low,
and the layer-rule engine became a no-op (it only walks resolved
edges). Resolving aliases here fixes all three.

JSON-with-comments is the canonical ``tsconfig`` format. ``json5`` is
not a base dep; we use a tiny stripper that removes ``//`` line
comments and ``/* ... */`` block comments before ``json.loads`` —
adequate for tsconfig files in practice.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .adapters.base import CONF_RESOLVED, Edge, Symbol

# ---------- comment-stripping JSON loader ----------------------------------

_LINE_COMMENT = re.compile(r"//[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_TRAILING_COMMA = re.compile(r",(\s*[}\]])")


def _strip_jsonc(text: str) -> str:
    text = _BLOCK_COMMENT.sub("", text)
    text = _LINE_COMMENT.sub("", text)
    text = _TRAILING_COMMA.sub(r"\1", text)
    return text


def _load_jsonc(path: Path) -> dict | None:
    try:
        raw = path.read_text(errors="replace")
    except OSError:
        return None
    try:
        return json.loads(_strip_jsonc(raw))
    except json.JSONDecodeError:
        return None


# ---------- resolver -------------------------------------------------------


@dataclass(frozen=True)
class AliasMap:
    """Resolved configuration. ``base_dir`` is repo-relative."""

    base_dir: str  # e.g. "src" or "" if no baseUrl
    # alias prefix (without trailing wildcard) -> list of repo-relative
    # destination prefixes. e.g. {"@/": ["src/"]}
    prefixes: dict[str, list[str]]

    def is_empty(self) -> bool:
        return not self.prefixes and not self.base_dir


def _normalise_paths_entry(
    pattern: str,
    targets: list[str],
    config_dir: Path,
    repo_root: Path,
    base_dir: str,
) -> tuple[str, list[str]] | None:
    """Translate one tsconfig ``paths`` entry into a (prefix, [targets]).

    ``pattern`` looks like ``@/*`` or ``@app/utils/*``. ``targets`` are
    relative to the tsconfig file's directory and resolved against
    ``compilerOptions.baseUrl`` if set.
    """
    if not pattern.endswith("*"):
        return None
    prefix = pattern[:-1]  # "@/*" -> "@/"
    repo = repo_root.resolve()

    out: list[str] = []
    for tgt in targets:
        if not tgt.endswith("*"):
            continue
        tgt_prefix = tgt[:-1]  # "src/*" -> "src/"
        # Targets resolve relative to the tsconfig directory + baseUrl.
        anchor = config_dir
        if base_dir:
            anchor = (config_dir / base_dir).resolve()
        candidate = (anchor / tgt_prefix).resolve()
        try:
            rel = candidate.relative_to(repo)
        except ValueError:
            continue
        rel_str = rel.as_posix()
        if rel_str and not rel_str.endswith("/"):
            rel_str += "/"
        out.append(rel_str)
    if not out:
        return None
    return prefix, out


def _config_files(repo: Path) -> list[Path]:
    out: list[Path] = []
    for name in ("tsconfig.json", "jsconfig.json"):
        p = repo / name
        if p.is_file():
            out.append(p)
    out.extend(sorted(repo.glob("tsconfig.*.json")))
    return out


def load_alias_map(repo: Path) -> AliasMap:
    """Read every tsconfig/jsconfig at repo root and merge their paths.

    Later configs override earlier ones for the same alias prefix.
    """
    repo = repo.resolve()
    prefixes: dict[str, list[str]] = {}
    base_dir = ""
    for cfg in _config_files(repo):
        data = _load_jsonc(cfg)
        if not data:
            continue
        compiler_opts = data.get("compilerOptions") or {}
        cfg_base = compiler_opts.get("baseUrl") or ""
        # baseUrl is recorded once — last config wins.
        if cfg_base:
            anchor = (cfg.parent / cfg_base).resolve()
            try:
                base_dir = anchor.relative_to(repo).as_posix()
            except ValueError:
                base_dir = ""
        paths_map = compiler_opts.get("paths") or {}
        if not isinstance(paths_map, dict):
            continue
        for pattern, targets in paths_map.items():
            if not isinstance(pattern, str) or not isinstance(targets, list):
                continue
            entry = _normalise_paths_entry(
                pattern,
                [t for t in targets if isinstance(t, str)],
                cfg.parent,
                repo,
                cfg_base,
            )
            if entry:
                prefixes[entry[0]] = entry[1]
    return AliasMap(base_dir=base_dir, prefixes=prefixes)


# ---------- import edge extraction ----------------------------------------

# Capture the source string from `import ... from 'X';` / `import('X')`.
_FROM_RE = re.compile(r"""from\s+['"]([^'"]+)['"]""")
_BARE_IMPORT_RE = re.compile(r"""^import\s+['"]([^'"]+)['"]""")
_DYNAMIC_IMPORT_RE = re.compile(r"""import\(\s*['"]([^'"]+)['"]\s*\)""")


def _extract_source(import_text: str) -> str | None:
    for rx in (_FROM_RE, _BARE_IMPORT_RE, _DYNAMIC_IMPORT_RE):
        m = rx.search(import_text)
        if m:
            return m.group(1)
    return None


# ---------- candidate path expansion --------------------------------------

# File suffixes the resolver tries when looking up a symbol by import.
# The first match wins; ordering matters.
_TS_SUFFIXES = (
    ".ts",
    ".tsx",
    ".mts",
    ".cts",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    "/index.ts",
    "/index.tsx",
    "/index.js",
    "/index.jsx",
)


def _candidates(rel_path: str) -> list[str]:
    out: list[str] = [rel_path]
    for suf in _TS_SUFFIXES:
        out.append(rel_path + suf)
    return out


def _resolve(
    source: str,
    from_path: str,
    aliases: AliasMap,
    module_index: dict[str, str],
) -> str | None:
    """Return the resolved symbol id, or ``None`` if unresolvable."""
    if source.startswith("."):
        # Relative import — collapse "../foo" / "./foo" against from_path.
        parent_parts = Path(from_path).parent.parts
        src_parts = source.split("/")
        stack: list[str] = list(parent_parts)
        for part in src_parts:
            if part in ("", "."):
                continue
            if part == "..":
                if stack:
                    stack.pop()
                continue
            stack.append(part)
        target = "/".join(stack)
        for cand in _candidates(target):
            if cand in module_index:
                return module_index[cand]
        return None

    # Alias prefix match (longest first).
    for prefix in sorted(aliases.prefixes, key=len, reverse=True):
        if source.startswith(prefix):
            tail = source[len(prefix) :]
            for dest in aliases.prefixes[prefix]:
                base = dest + tail
                for cand in _candidates(base):
                    if cand in module_index:
                        return module_index[cand]
            return None  # prefix matched but file missing — leave unresolved

    # baseUrl bare-specifier resolution: tsconfig allows `import x from "lib/foo"`
    # to mean `<baseUrl>/lib/foo`.
    if aliases.base_dir:
        base = aliases.base_dir.rstrip("/") + "/" + source if aliases.base_dir else source
        for cand in _candidates(base):
            if cand in module_index:
                return module_index[cand]

    return None


# ---------- public entrypoint ---------------------------------------------


def resolve_alias_imports(
    repo: Path,
    symbols: list[Symbol],
    edges: list[Edge],
) -> dict:
    """Mutate ``edges`` in place: rewrite TS/JS alias imports to resolved
    module ids. Returns a small summary dict.
    """
    aliases = load_alias_map(repo)

    # Build path -> module symbol id lookup for TS/JS modules only.
    module_index: dict[str, str] = {}
    for s in symbols:
        if s.get("kind") != "module":
            continue
        if s.get("language") not in {"typescript", "javascript"}:
            continue
        path = s.get("path") or ""
        if path:
            module_index[path] = s["id"]

    if not module_index:
        return {"resolved": 0, "considered": 0, "alias_prefixes": list(aliases.prefixes)}

    # id -> path lookup for the source-side of each import edge.
    src_path: dict[str, str] = {}
    for s in symbols:
        if s.get("language") in {"typescript", "javascript"} and s.get("kind") == "module":
            src_path[s["id"]] = s.get("path") or ""

    considered = 0
    resolved = 0
    for e in edges:
        if e.get("kind") != "imports":
            continue
        if e.get("resolved"):
            continue
        from_path = src_path.get(e.get("src") or "")
        if not from_path:
            continue
        considered += 1
        text = e.get("dst") or ""
        source = _extract_source(text) if isinstance(text, str) else None
        if not source:
            continue
        target_id = _resolve(source, from_path, aliases, module_index)
        if target_id is None:
            continue
        e["dst"] = target_id
        e["resolved"] = True
        e["confidence"] = CONF_RESOLVED
        resolved += 1

    return {
        "resolved": resolved,
        "considered": considered,
        "alias_prefixes": list(aliases.prefixes),
        "base_dir": aliases.base_dir,
    }


__all__ = ["AliasMap", "load_alias_map", "resolve_alias_imports"]
