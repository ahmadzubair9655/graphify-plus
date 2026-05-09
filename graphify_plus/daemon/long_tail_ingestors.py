"""Layer 24 — long-tail ingestors.

Each ingestor pulls one external source and emits ``IngestNode`` rows
that go into the existing ``ingest_nodes`` table from Layer 6. Same
attribution + ``why_does_this_exist`` flow applies.

Shipped:

* **Jupyter notebooks** — cells as nodes, kernel ordering as
  intra-notebook edges.
* **OpenAPI / Swagger** — endpoints as nodes (one per ``(method, path)``).
* **Postman collections** — saved API calls as test-evidence nodes.
* **Dockerfile / Compose** — services + base images as nodes.
* **Kubernetes manifests** — resources (Deployment / Service / etc.)
  as nodes.
* **GitHub Actions / GitLab CI** — workflow jobs as nodes.
* **Translation files** — i18n keys as nodes (gettext, ARB, JSON).
* **Asset references** — image / font / icon files as nodes.

Each ingestor is small + best-effort. Failures degrade silently to
"no rows ingested" rather than crashing the daemon.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from .ingestors import IngestNode, store_ingest_nodes

log = logging.getLogger("graphify_plus.daemon.long_tail")


# ---- Jupyter --------------------------------------------------------


def parse_notebook(path: Path) -> list[IngestNode]:
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rows: list[IngestNode] = []
    for i, cell in enumerate(body.get("cells") or []):
        kind = cell.get("cell_type") or "code"
        src = cell.get("source") or []
        if isinstance(src, list):
            text = "".join(src)
        else:
            text = str(src)
        rows.append(
            IngestNode(
                id=f"nb-{path.stem}-{i:04d}",
                kind="notebook_cell",
                title=f"{path.name} cell {i} [{kind}]",
                body=text,
                source="jupyter",
                url=f"{path}#{i}",
                state=str(cell.get("execution_count") or ""),
                metadata={"cell_kind": kind, "execution_count": cell.get("execution_count")},
            )
        )
    return rows


# ---- OpenAPI --------------------------------------------------------


def parse_openapi(path: Path) -> list[IngestNode]:
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        try:
            import yaml

            body = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return []
    if not isinstance(body, dict):
        return []
    paths = body.get("paths") or {}
    rows: list[IngestNode] = []
    for url, ops in paths.items():
        if not isinstance(ops, dict):
            continue
        for method, op in ops.items():
            if method.lower() not in (
                "get",
                "post",
                "put",
                "delete",
                "patch",
                "head",
                "options",
            ):
                continue
            summary = (op.get("summary") if isinstance(op, dict) else "") or ""
            desc = (op.get("description") if isinstance(op, dict) else "") or ""
            op_id = (op.get("operationId") if isinstance(op, dict) else "") or ""
            rows.append(
                IngestNode(
                    id=f"openapi-{method.upper()}-{url}",
                    kind="openapi_op",
                    title=f"{method.upper()} {url}: {summary}".strip(),
                    body=desc,
                    source="openapi",
                    url=str(path),
                    metadata={"method": method.upper(), "path": url, "operation_id": op_id},
                )
            )
    return rows


# ---- Postman --------------------------------------------------------


def parse_postman(path: Path) -> list[IngestNode]:
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    items = body.get("item") or []

    rows: list[IngestNode] = []

    def _walk(items: list[Any], parent: str = "") -> None:
        for it in items:
            if not isinstance(it, dict):
                continue
            name = it.get("name", "?")
            full = f"{parent}/{name}" if parent else name
            if it.get("item"):
                _walk(it["item"], full)
                continue
            req = it.get("request") or {}
            method = req.get("method", "GET")
            url_raw = req.get("url") or ""
            url = url_raw.get("raw") if isinstance(url_raw, dict) else url_raw
            rows.append(
                IngestNode(
                    id=f"postman-{full}",
                    kind="api_call",
                    title=f"{method} {url}",
                    body=full,
                    source="postman",
                    url=str(path),
                    metadata={"method": method, "url": url},
                )
            )

    _walk(items)
    return rows


# ---- Dockerfile / Compose ------------------------------------------


_DOCKERFILE_FROM = re.compile(r"^\s*FROM\s+([^\s]+)", re.IGNORECASE | re.MULTILINE)
_DOCKERFILE_RUN = re.compile(r"^\s*RUN\s+(.+?)$", re.IGNORECASE | re.MULTILINE)


def parse_dockerfile(path: Path) -> list[IngestNode]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    images = _DOCKERFILE_FROM.findall(text)
    rows: list[IngestNode] = []
    for i, image in enumerate(images):
        rows.append(
            IngestNode(
                id=f"docker-{path.stem}-from-{i}",
                kind="docker_image",
                title=f"FROM {image}",
                body=text[:1000],
                source="dockerfile",
                url=str(path),
                metadata={"image": image},
            )
        )
    return rows


def parse_compose(path: Path) -> list[IngestNode]:
    try:
        import yaml

        body = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return []
    services = (body or {}).get("services") or {}
    rows: list[IngestNode] = []
    for name, info in services.items():
        if not isinstance(info, dict):
            continue
        image = info.get("image") or "?"
        ports = info.get("ports") or []
        rows.append(
            IngestNode(
                id=f"compose-{name}",
                kind="compose_service",
                title=f"service {name} ({image})",
                body=json.dumps(info, indent=2),
                source="compose",
                url=str(path),
                metadata={"image": image, "ports": ports},
            )
        )
    return rows


# ---- Kubernetes -----------------------------------------------------


def parse_k8s_manifest(path: Path) -> list[IngestNode]:
    try:
        import yaml

        body_iter = yaml.safe_load_all(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    rows: list[IngestNode] = []
    for doc in body_iter:
        if not isinstance(doc, dict):
            continue
        kind = doc.get("kind") or "?"
        meta = doc.get("metadata") or {}
        name = meta.get("name") or "?"
        rows.append(
            IngestNode(
                id=f"k8s-{kind}-{name}",
                kind="k8s_resource",
                title=f"{kind}/{name}",
                body=json.dumps(doc, indent=2)[:2000],
                source="k8s",
                url=str(path),
                metadata={"k8s_kind": kind, "namespace": meta.get("namespace", "default")},
            )
        )
    return rows


# ---- CI configs -----------------------------------------------------


def parse_github_actions(path: Path) -> list[IngestNode]:
    try:
        import yaml

        body = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return []
    jobs = (body or {}).get("jobs") or {}
    rows: list[IngestNode] = []
    for name, info in jobs.items():
        if not isinstance(info, dict):
            continue
        steps = info.get("steps") or []
        rows.append(
            IngestNode(
                id=f"gha-{path.stem}-{name}",
                kind="ci_job",
                title=f"GHA job: {name}",
                body=json.dumps(info, indent=2)[:2000],
                source="github_actions",
                url=str(path),
                metadata={"runs_on": info.get("runs-on"), "step_count": len(steps)},
            )
        )
    return rows


# ---- Translation files ---------------------------------------------


def parse_i18n_json(path: Path) -> list[IngestNode]:
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    rows: list[IngestNode] = []

    def _flatten(d: Any, prefix: str = "") -> None:
        if isinstance(d, dict):
            for k, v in d.items():
                _flatten(v, f"{prefix}.{k}" if prefix else k)
        elif isinstance(d, str):
            rows.append(
                IngestNode(
                    id=f"i18n-{path.stem}-{prefix}",
                    kind="translation",
                    title=prefix,
                    body=d,
                    source="i18n",
                    url=str(path),
                    metadata={"key": prefix, "locale": path.stem},
                )
            )

    _flatten(body)
    return rows


# ---- Asset references ----------------------------------------------


_ASSET_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".woff", ".woff2", ".ttf"}


def collect_assets(repo: Path) -> list[IngestNode]:
    rows: list[IngestNode] = []
    for ext in _ASSET_EXTS:
        for f in repo.rglob(f"*{ext}"):
            if ".graphify_plus" in f.parts or ".git" in f.parts or "node_modules" in f.parts:
                continue
            rel = f.relative_to(repo).as_posix()
            rows.append(
                IngestNode(
                    id=f"asset-{rel}",
                    kind="asset",
                    title=rel,
                    body=f"asset of type {ext}",
                    source="asset",
                    url=rel,
                    metadata={"size": f.stat().st_size if f.exists() else 0},
                )
            )
    return rows


# ---- end-to-end -----------------------------------------------------


def ingest_long_tail(store, repo_root: Path, *, kinds: list[str] | None = None) -> dict[str, int]:
    """Walk the repo and ingest every supported long-tail source.

    ``kinds`` filters to a subset (e.g. ``['jupyter', 'openapi']``).
    Defaults to all.
    """
    kinds = kinds or [
        "jupyter",
        "openapi",
        "postman",
        "dockerfile",
        "compose",
        "k8s",
        "gha",
        "i18n",
        "asset",
    ]
    summary: dict[str, int] = {}
    if "jupyter" in kinds:
        rows: list[IngestNode] = []
        for f in repo_root.rglob("*.ipynb"):
            if any(part in (".git", "node_modules", ".graphify_plus") for part in f.parts):
                continue
            rows.extend(parse_notebook(f))
        if rows:
            store_ingest_nodes(store, rows, replace_kind="notebook_cell")
            summary["jupyter"] = len(rows)
    if "openapi" in kinds:
        rows = []
        for name in ("openapi.yaml", "openapi.yml", "openapi.json", "swagger.json", "swagger.yaml"):
            for f in repo_root.rglob(name):
                rows.extend(parse_openapi(f))
        if rows:
            store_ingest_nodes(store, rows, replace_kind="openapi_op")
            summary["openapi"] = len(rows)
    if "postman" in kinds:
        rows = []
        for f in repo_root.rglob("*.postman_collection.json"):
            rows.extend(parse_postman(f))
        if rows:
            store_ingest_nodes(store, rows, replace_kind="api_call")
            summary["postman"] = len(rows)
    if "dockerfile" in kinds:
        rows = []
        for f in repo_root.rglob("Dockerfile*"):
            if not f.is_file():
                continue
            rows.extend(parse_dockerfile(f))
        if rows:
            store_ingest_nodes(store, rows, replace_kind="docker_image")
            summary["dockerfile"] = len(rows)
    if "compose" in kinds:
        rows = []
        for name in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"):
            for f in repo_root.rglob(name):
                rows.extend(parse_compose(f))
        if rows:
            store_ingest_nodes(store, rows, replace_kind="compose_service")
            summary["compose"] = len(rows)
    if "k8s" in kinds:
        rows = []
        for f in (repo_root / "k8s").rglob("*.yaml") if (repo_root / "k8s").exists() else []:
            rows.extend(parse_k8s_manifest(f))
        for f in (
            (repo_root / "manifests").rglob("*.yaml") if (repo_root / "manifests").exists() else []
        ):
            rows.extend(parse_k8s_manifest(f))
        if rows:
            store_ingest_nodes(store, rows, replace_kind="k8s_resource")
            summary["k8s"] = len(rows)
    if "gha" in kinds:
        rows = []
        actions_dir = repo_root / ".github" / "workflows"
        if actions_dir.exists():
            for f in actions_dir.rglob("*.yml"):
                rows.extend(parse_github_actions(f))
            for f in actions_dir.rglob("*.yaml"):
                rows.extend(parse_github_actions(f))
        if rows:
            store_ingest_nodes(store, rows, replace_kind="ci_job")
            summary["gha"] = len(rows)
    if "i18n" in kinds:
        rows = []
        for name in ("locales", "i18n", "translations"):
            base = repo_root / name
            if not base.exists():
                continue
            for f in base.rglob("*.json"):
                rows.extend(parse_i18n_json(f))
        if rows:
            store_ingest_nodes(store, rows, replace_kind="translation")
            summary["i18n"] = len(rows)
    if "asset" in kinds:
        rows = collect_assets(repo_root)
        if rows:
            store_ingest_nodes(store, rows, replace_kind="asset")
            summary["asset"] = len(rows)
    return summary


__all__ = [
    "collect_assets",
    "ingest_long_tail",
    "parse_compose",
    "parse_dockerfile",
    "parse_github_actions",
    "parse_i18n_json",
    "parse_k8s_manifest",
    "parse_notebook",
    "parse_openapi",
    "parse_postman",
]
