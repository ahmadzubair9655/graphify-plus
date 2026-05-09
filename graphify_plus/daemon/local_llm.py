"""Layer 22 — local LLM and air-gapped operation.

Detection of local LLM backends:

* **Ollama** — set ``OLLAMA_BASE_URL`` (defaults to ``http://localhost:11434``).
* **llama.cpp / llama-cpp-python** — ``LLAMA_CPP_BASE_URL``.
* **MLX** (Apple) — ``MLX_BASE_URL``.

When detected, the semantic-extraction pipeline routes through the local
endpoint. ``LLM-free mode`` drops every feature that requires extraction
but everything structural still works.

Hybrid local/cloud routing: per-file rules in
``.graphify_plus/llm-routing.yaml`` say which paths go local. Path-based
allow patterns override default cloud routing.
"""

from __future__ import annotations

import logging
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("graphify_plus.daemon.local_llm")


@dataclass
class LLMBackend:
    name: str  # 'ollama' | 'llama.cpp' | 'mlx' | 'cloud' | 'none'
    url: str
    model: str = ""
    available: bool = False
    reason: str = ""


@dataclass
class RoutingRule:
    pattern: str  # glob against the *repo-relative* path
    backend: str  # 'local' | 'cloud' | 'skip'


@dataclass
class LLMRouting:
    default: str = "cloud"
    rules: list[RoutingRule] = field(default_factory=list)
    llm_free: bool = False


def detect_backends() -> list[LLMBackend]:
    """Probe environment + well-known endpoints. Returns *every* backend
    seen (whether available or not) so the operator can debug missing
    ones in ``gp daemon diagnose``.
    """
    out: list[LLMBackend] = []
    candidates = [
        ("ollama", os.environ.get("OLLAMA_BASE_URL") or "http://localhost:11434"),
        ("llama.cpp", os.environ.get("LLAMA_CPP_BASE_URL") or ""),
        ("mlx", os.environ.get("MLX_BASE_URL") or ""),
    ]
    for name, url in candidates:
        if not url:
            out.append(LLMBackend(name=name, url="", reason="not configured"))
            continue
        ok, why = _probe(url)
        out.append(
            LLMBackend(
                name=name,
                url=url,
                available=ok,
                reason=why,
                model=os.environ.get(f"{name.upper().replace('.', '_')}_MODEL", ""),
            )
        )
    return out


def _probe(url: str, *, timeout: float = 0.8) -> tuple[bool, str]:
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if 200 <= resp.status < 500:
                return True, "ok"
            return False, f"http {resp.status}"
    except urllib.error.URLError as exc:
        return False, str(exc.reason)
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


# ---- routing --------------------------------------------------------


def load_routing(repo: Path) -> LLMRouting:
    """Read ``.graphify_plus/llm-routing.yaml`` if present."""
    p = repo / ".graphify_plus" / "llm-routing.yaml"
    if not p.exists():
        return LLMRouting()
    try:
        import yaml

        body = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        log.warning("invalid routing config: %s", exc)
        return LLMRouting()
    rules = [
        RoutingRule(pattern=str(r.get("pattern", "")), backend=str(r.get("backend", "cloud")))
        for r in (body.get("rules") or [])
        if r.get("pattern")
    ]
    return LLMRouting(
        default=str(body.get("default", "cloud")),
        rules=rules,
        llm_free=bool(body.get("llm_free", False)),
    )


def route_for_path(routing: LLMRouting, path: str) -> str:
    """Return ``'local' | 'cloud' | 'skip'`` for the given path."""
    if routing.llm_free:
        return "skip"
    for rule in routing.rules:
        if _glob_match(rule.pattern, path):
            return rule.backend
    return routing.default


def _glob_match(pattern: str, path: str) -> bool:
    """Lightweight glob: ``*`` matches anything; otherwise exact prefix
    or substring match. ``*foo*`` matches anywhere in the path.
    """
    if pattern in ("*", "**"):
        return True
    if "*" in pattern:
        # Translate to regex once (escape the rest of the pattern).
        regex = re.escape(pattern).replace(r"\*", ".*")
        return re.search(regex, path) is not None
    return path == pattern or path.startswith(pattern.rstrip("/") + "/") or path.endswith(pattern)


def deterministic_seed(prompt: str) -> int:
    import hashlib

    return int(hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:8], 16)


__all__ = [
    "LLMBackend",
    "LLMRouting",
    "RoutingRule",
    "deterministic_seed",
    "detect_backends",
    "load_routing",
    "route_for_path",
]
