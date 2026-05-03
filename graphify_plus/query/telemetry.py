"""Runtime telemetry ingest.

Accept OpenTelemetry-style spans as JSONL (one JSON object per line),
redact every payload through the privacy filter before storage,
aggregate by ``span.name`` into a JSON Schema fingerprint of the
attributes, and attach the result as ``data_shape`` to the matched
symbol.

Stdlib only — no opentelemetry-proto / ijson dependency. Phase 8 trades
arbitrary streaming OTLP support for a privacy-safe minimal core; the
schema is the load-bearing part, not the transport.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import networkx as nx

from ..interface.privacy import redact, redact_dict


@dataclass
class SpanShape:
    span_name: str
    sample_count: int = 0
    keys: dict[str, dict] = field(default_factory=dict)  # key -> {types, null_rate}


def _type_label(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "unknown"


def aggregate(jsonl_lines: Iterable[str]) -> dict[str, SpanShape]:
    shapes: dict[str, SpanShape] = {}
    for raw in jsonl_lines:
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = obj.get("name") or obj.get("span_name") or obj.get("operationName")
        if not name:
            continue
        attrs = obj.get("attributes") or obj.get("attrs") or {}
        if not isinstance(attrs, dict):
            continue
        attrs = redact_dict(attrs)
        shape = shapes.setdefault(name, SpanShape(span_name=name))
        shape.sample_count += 1
        for k, v in attrs.items():
            keyrec = shape.keys.setdefault(k, {"types": {}, "nulls": 0, "samples": 0})
            keyrec["samples"] += 1
            if v is None:
                keyrec["nulls"] += 1
            t = _type_label(v)
            keyrec["types"][t] = keyrec["types"].get(t, 0) + 1
    return shapes


def fingerprint(shape: SpanShape) -> dict:
    """Pure JSON-serialisable summary of a SpanShape."""
    return {
        "span_name": redact(shape.span_name),
        "samples": shape.sample_count,
        "keys": {
            k: {
                "types": sorted(v["types"].keys()),
                "null_rate": round(v["nulls"] / max(1, v["samples"]), 4),
            }
            for k, v in sorted(shape.keys.items())
        },
    }


def attach_to_graph(G: nx.MultiDiGraph, shapes: dict[str, SpanShape]) -> int:
    """For each node whose ``qualified_name`` matches (case-insensitive,
    treating both ``.`` and ``::`` as separators) a span name, attach
    ``data_shape`` as a fingerprint dict.
    """

    def _norm(s: str) -> str:
        return s.lower().replace("::", ".")

    by_norm = {_norm(name): shape for name, shape in shapes.items()}
    matched = 0
    for _sid, attrs in G.nodes(data=True):
        qn = (attrs or {}).get("qualified_name") or ""
        sh = by_norm.get(_norm(qn))
        if sh is None:
            continue
        attrs["data_shape"] = fingerprint(sh)
        matched += 1
    return matched


def ingest_file(path: Path, G: nx.MultiDiGraph) -> dict:
    """Ingest a JSONL file of spans into the graph. Returns a summary dict."""
    if not path.exists():
        return {"matched": 0, "skipped": True, "reason": f"missing {path}"}
    with path.open("r", errors="replace") as f:
        shapes = aggregate(f)
    matched = attach_to_graph(G, shapes)
    return {
        "matched": matched,
        "spans": len(shapes),
        "samples": sum(s.sample_count for s in shapes.values()),
    }


__all__ = ["SpanShape", "aggregate", "attach_to_graph", "fingerprint", "ingest_file"]
