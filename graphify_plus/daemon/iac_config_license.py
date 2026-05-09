"""Layers 8.3, 8.4, 9.3 — IaC, config/feature flags, license.

Each is a thin layer on top of the existing ``cross_edges`` /
``ingest_nodes`` tables.

* **8.3 IaC edges** — Terraform ``resource`` blocks and Kubernetes
  manifests already covered by long_tail_ingestors. Here we add the
  *consumer* link: code that references a resource name gets a
  cross-edge of kind ``iac``.
* **8.4 Config / feature flag drift** — walk env-var / feature-flag
  references in code; check against deployment manifests already
  ingested. Drift = referenced-in-code but absent-from-manifest, or
  vice-versa.
* **9.3 License overlay** — parse package-level licenses from
  ``pyproject.toml`` / ``package.json`` etc. ``license_audit()``
  flags incompatible combinations.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.adapters import Symbol
from ..runtime.store import Store

log = logging.getLogger("graphify_plus.daemon.iac_config_license")


# ---- 8.3 IaC edges ------------------------------------------------------


_TERRAFORM_RESOURCE_RE = re.compile(
    r'resource\s+"(?P<type>[^"]+)"\s+"(?P<name>[^"]+)"', re.MULTILINE
)


@dataclass
class IaCResource:
    type: str
    name: str
    file: str
    line: int = 0


def detect_terraform_resources(repo: Path) -> list[IaCResource]:
    out: list[IaCResource] = []
    for f in repo.rglob("*.tf"):
        if any(p in (".terraform", "node_modules", ".graphify_plus") for p in f.parts):
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in _TERRAFORM_RESOURCE_RE.finditer(text):
            line = text[: m.start()].count("\n") + 1
            out.append(
                IaCResource(
                    type=m.group("type"),
                    name=m.group("name"),
                    file=f.relative_to(repo).as_posix(),
                    line=line,
                )
            )
    return out


def synthesise_iac_edges(
    resources: list[IaCResource], symbols: list[Symbol], repo: Path
) -> list[dict[str, Any]]:
    """Emit cross_edges of kind 'iac' from code symbols that reference
    the resource name (best-effort substring match in the file body).
    """
    edges: list[dict[str, Any]] = []
    by_path: dict[str, list[Symbol]] = {}
    for s in symbols:
        path = s.get("path") or ""
        if path:
            by_path.setdefault(path, []).append(s)
    for res in resources:
        for path, syms in by_path.items():
            if not path.endswith((".py", ".ts", ".tsx", ".js", ".go", ".rs", ".java", ".rb")):
                continue
            full = repo / path
            try:
                text = full.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if res.name not in text:
                continue
            line = text.find(res.name)
            line_num = text[:line].count("\n") + 1 if line >= 0 else 1
            owner = None
            for s in sorted(
                syms,
                key=lambda s: ((s.get("span") or (0, 0))[1] - (s.get("span") or (0, 0))[0]) or 1_000_000,
            ):
                span = s.get("span") or (0, 0)
                if int(span[0]) <= line_num <= int(span[1]):
                    owner = s
                    break
            if owner is None:
                continue
            edges.append(
                {
                    "src": owner["id"],
                    "dst": f"iac:{res.type}.{res.name}",
                    "kind": "iac",
                    "detail": {
                        "resource_type": res.type,
                        "resource_name": res.name,
                        "manifest_file": res.file,
                        "manifest_line": res.line,
                        "consumer_file": path,
                    },
                }
            )
    return edges


# ---- 8.4 config / feature flag drift -----------------------------------


_ENV_REF = re.compile(
    r'\b(?:os\.environ|process\.env|System\.getenv)\s*[\.\[]\s*[\'"]?(?P<name>[A-Z][A-Z0-9_]+)'
)
_FF_REF = re.compile(r'\b(?:feature_flag|launchdarkly|flags\.is_enabled)\s*\(\s*[\'"]([^\'"]+)')


@dataclass
class ConfigKey:
    key: str
    kind: str             # 'env' | 'feature_flag'
    file: str
    line: int


def detect_config_keys(repo: Path) -> list[ConfigKey]:
    out: list[ConfigKey] = []
    for f in repo.rglob("*"):
        if not f.is_file() or any(p in (".git", "node_modules", ".graphify_plus") for p in f.parts):
            continue
        if f.suffix not in {".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".java", ".rb"}:
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = f.relative_to(repo).as_posix()
        for m in _ENV_REF.finditer(text):
            out.append(
                ConfigKey(
                    key=m.group("name"),
                    kind="env",
                    file=rel,
                    line=text[: m.start()].count("\n") + 1,
                )
            )
        for m in _FF_REF.finditer(text):
            out.append(
                ConfigKey(
                    key=m.group(1),
                    kind="feature_flag",
                    file=rel,
                    line=text[: m.start()].count("\n") + 1,
                )
            )
    return out


def detect_declared_env(repo: Path) -> set[str]:
    """Best-effort: read .env / .env.example / docker-compose / k8s
    manifests for declared env-var names.
    """
    declared: set[str] = set()
    for name in (".env", ".env.example", ".env.sample"):
        p = repo / name
        if p.exists():
            try:
                for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                    if "=" in line and not line.strip().startswith("#"):
                        declared.add(line.split("=", 1)[0].strip())
            except OSError:
                continue
    for compose in repo.rglob("docker-compose*.yml"):
        try:
            for m in re.finditer(r'^\s*([A-Z][A-Z0-9_]+)\s*:', compose.read_text(encoding="utf-8")):
                declared.add(m.group(1))
        except OSError:
            continue
    return declared


def detect_drift(repo: Path) -> dict[str, Any]:
    keys = detect_config_keys(repo)
    declared = detect_declared_env(repo)
    referenced = {k.key for k in keys if k.kind == "env"}
    return {
        "referenced_in_code": sorted(referenced),
        "declared_in_manifests": sorted(declared),
        "referenced_but_undeclared": sorted(referenced - declared),
        "declared_but_unused": sorted(declared - referenced),
        "feature_flags": sorted({k.key for k in keys if k.kind == "feature_flag"}),
    }


# ---- 9.3 license overlay -----------------------------------------------


_GPL_FAMILY = {"GPL", "GPLv2", "GPLv3", "AGPL", "AGPL-3.0", "GPL-3.0", "GPL-2.0"}
_PERMISSIVE = {"MIT", "BSD", "Apache-2.0", "ISC", "Unlicense", "0BSD"}


@dataclass
class PackageLicense:
    package: str
    license: str
    source: str   # 'pyproject' | 'package.json' | 'requirements'

    def is_strong_copyleft(self) -> bool:
        return any(self.license.upper().startswith(g.upper()) for g in _GPL_FAMILY)


def detect_python_licenses(repo: Path) -> list[PackageLicense]:
    out: list[PackageLicense] = []
    pyproj = repo / "pyproject.toml"
    if pyproj.exists():
        try:
            text = pyproj.read_text(encoding="utf-8")
        except OSError:
            return out
        m = re.search(r'license\s*=\s*\{?\s*text\s*=\s*"([^"]+)"', text)
        if m:
            out.append(
                PackageLicense(package=repo.name, license=m.group(1), source="pyproject")
            )
    return out


def detect_npm_licenses(repo: Path) -> list[PackageLicense]:
    out: list[PackageLicense] = []
    pj = repo / "package.json"
    if pj.exists():
        try:
            body = json.loads(pj.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return out
        if "license" in body:
            out.append(
                PackageLicense(
                    package=body.get("name", repo.name),
                    license=str(body.get("license") or ""),
                    source="package.json",
                )
            )
    return out


def license_audit(repo: Path) -> dict[str, Any]:
    pkgs = detect_python_licenses(repo) + detect_npm_licenses(repo)
    issues: list[dict[str, Any]] = []
    own_license = pkgs[0].license if pkgs else ""
    own_permissive = own_license.upper() in {p.upper() for p in _PERMISSIVE}
    for p in pkgs[1:]:
        if own_permissive and p.is_strong_copyleft():
            issues.append(
                {
                    "package": p.package,
                    "license": p.license,
                    "issue": "strong copyleft inside permissive project",
                }
            )
    return {
        "packages": [p.__dict__ for p in pkgs],
        "issues": issues,
    }


__all__ = [
    "ConfigKey",
    "IaCResource",
    "PackageLicense",
    "detect_config_keys",
    "detect_declared_env",
    "detect_drift",
    "detect_npm_licenses",
    "detect_python_licenses",
    "detect_terraform_resources",
    "license_audit",
    "synthesise_iac_edges",
]
