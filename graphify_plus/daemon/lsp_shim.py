"""Layer 17 — Language Server Protocol shim.

A thin LSP server that surfaces graph-derived information through the
LSP protocol every modern editor speaks. Hover gets PageRank +
test-coverage + dependents; codeLens marks public-API symbols and
untested functions; status-bar pill data ships as a custom request.

This is intentionally minimal — full LSP is a large surface and most
editors are happy with hover, definitions, and codeLens. We talk
JSON-RPC over stdio. Editors point at ``gp daemon lsp`` as the binary.

Internally we round-trip everything through ``DaemonClient`` so the
shim shares the daemon's snapshot rather than maintaining its own.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
from pathlib import Path
from typing import Any

from .client import DaemonClient, DaemonNotRunning

log = logging.getLogger("graphify_plus.daemon.lsp_shim")


# ---- JSON-RPC framing ---------------------------------------------------


def _read_message(stream) -> dict[str, Any] | None:
    headers: dict[str, str] = {}
    while True:
        line = stream.readline()
        if not line:
            return None
        line = line.decode("ascii") if isinstance(line, bytes) else line
        if line.strip() == "":
            break
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    n = int(headers.get("content-length", "0"))
    if not n:
        return None
    body = stream.read(n)
    if isinstance(body, bytes):
        body = body.decode("utf-8")
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return None


def _write_message(stream, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, separators=(",", ":"))
    raw = body.encode("utf-8")
    out = f"Content-Length: {len(raw)}\r\n\r\n".encode() + raw
    if hasattr(stream, "buffer"):
        stream.buffer.write(out)
        stream.buffer.flush()
    else:
        stream.write(out)
        stream.flush()


# ---- handler registry ---------------------------------------------------


class LSPServer:
    def __init__(self, repo_root: Path):
        self.repo = repo_root.resolve()
        self.client = DaemonClient(self.repo)
        self._open_files: dict[str, str] = {}
        self._stop = threading.Event()

    def serve(self, *, in_stream=None, out_stream=None) -> int:
        in_stream = in_stream or sys.stdin
        out_stream = out_stream or sys.stdout
        while not self._stop.is_set():
            msg = _read_message(in_stream)
            if msg is None:
                return 0
            method = msg.get("method")
            mid = msg.get("id")
            try:
                result = self._dispatch(method, msg.get("params") or {})
            except Exception as exc:  # noqa: BLE001
                log.exception("LSP method %s failed", method)
                if mid is not None:
                    _write_message(
                        out_stream,
                        {
                            "jsonrpc": "2.0",
                            "id": mid,
                            "error": {"code": -32603, "message": str(exc)},
                        },
                    )
                continue
            if mid is None:
                continue
            if result is None:
                _write_message(
                    out_stream, {"jsonrpc": "2.0", "id": mid, "result": None}
                )
            else:
                _write_message(
                    out_stream, {"jsonrpc": "2.0", "id": mid, "result": result}
                )
        return 0

    def _dispatch(self, method: str | None, params: dict[str, Any]) -> Any:
        if method == "initialize":
            return {
                "capabilities": {
                    "textDocumentSync": 1,
                    "hoverProvider": True,
                    "codeLensProvider": {"resolveProvider": False},
                    "definitionProvider": True,
                },
                "serverInfo": {"name": "graphify-plus", "version": "5.x"},
            }
        if method == "initialized":
            return None
        if method == "shutdown":
            return None
        if method == "exit":
            self._stop.set()
            return None
        if method == "textDocument/didOpen":
            d = params.get("textDocument") or {}
            self._open_files[d.get("uri", "")] = d.get("text", "")
            return None
        if method == "textDocument/didChange":
            d = params.get("textDocument") or {}
            changes = params.get("contentChanges") or []
            if changes:
                self._open_files[d.get("uri", "")] = changes[-1].get("text", "")
            return None
        if method == "textDocument/didClose":
            self._open_files.pop((params.get("textDocument") or {}).get("uri", ""), None)
            return None
        if method == "textDocument/hover":
            return self._hover(params)
        if method == "textDocument/codeLens":
            return self._code_lens(params)
        if method == "textDocument/publishDiagnostics":
            return None  # we push, not pull
        if method == "textDocument/inlineHint" or method == "textDocument/inlayHint":
            return self._inlay_hints(params)
        if method == "textDocument/definition":
            return self._definition(params)
        if method == "graphifyPlus/statusBar":
            return self._status_bar()
        return None

    # ---- handlers

    def _hover(self, params: dict[str, Any]) -> dict[str, Any] | None:
        path = self._uri_to_rel(params)
        line = (params.get("position") or {}).get("line", 0) + 1
        symbol = self._symbol_at(path, line)
        if not symbol:
            return None
        cov = symbol.get("coverage_pct")
        text = [f"**{symbol.get('label', '?')}** [{symbol.get('kind', '?')}]"]
        if symbol.get("snippet"):
            text.append(symbol["snippet"])
        if cov is not None:
            text.append(f"\nTest coverage: **{cov}%**")
        if symbol.get("pagerank"):
            text.append(f"PageRank: {symbol['pagerank']}")
        text.append("\n_(via graphify-plus)_")
        return {"contents": {"kind": "markdown", "value": "\n".join(text)}}

    def _inlay_hints(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Layer 17.2 — inline annotations: untested-warning gutter +
        public-API indicator. Returned as LSP InlayHint items."""
        path = self._uri_to_rel(params)
        try:
            resp = self.client.call("whats_in", {"path": path, "budget_tokens": 4000})
        except DaemonNotRunning:
            return []
        out: list[dict[str, Any]] = []
        for r in resp.get("results", []):
            label_parts: list[str] = []
            if r.get("coverage_pct") is not None and r["coverage_pct"] <= 10.0:
                label_parts.append(f"⚠ untested ({r['coverage_pct']}%)")
            if r.get("pagerank"):
                label_parts.append("· central")
            if not label_parts:
                continue
            line = max(int(r.get("line_number", 1)) - 1, 0)
            out.append(
                {
                    "position": {"line": line, "character": 0},
                    "label": "  ".join(label_parts),
                    "paddingRight": True,
                    "kind": 1,  # Type hint
                }
            )
        return out

    def _code_lens(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        path = self._uri_to_rel(params)
        try:
            resp = self.client.call("whats_in", {"path": path, "budget_tokens": 4000})
        except DaemonNotRunning:
            return []
        rows = resp.get("results", [])
        out: list[dict[str, Any]] = []
        for r in rows:
            line = max(int(r.get("line_number", 1)) - 1, 0)
            badges: list[str] = []
            if r.get("coverage_pct") is not None and r["coverage_pct"] <= 10.0:
                badges.append(f"untested ({r['coverage_pct']}%)")
            if r.get("pagerank"):
                badges.append("central")
            if not badges:
                continue
            out.append(
                {
                    "range": {
                        "start": {"line": line, "character": 0},
                        "end": {"line": line, "character": 0},
                    },
                    "command": {"title": "  ·  ".join(badges), "command": ""},
                }
            )
        return out

    def _definition(self, params: dict[str, Any]) -> list[dict[str, Any]] | None:
        path = self._uri_to_rel(params)
        line = (params.get("position") or {}).get("line", 0) + 1
        sym = self._symbol_at(path, line)
        if not sym:
            return None
        return [
            {
                "uri": (params.get("textDocument") or {}).get("uri", ""),
                "range": {
                    "start": {"line": max(int(sym.get("line_number", 1)) - 1, 0), "character": 0},
                    "end": {"line": max(int(sym.get("line_number", 1)) - 1, 0), "character": 0},
                },
            }
        ]

    def _status_bar(self) -> dict[str, Any]:
        try:
            resp = self.client.call("graph_stats")
            extra = resp.get("extra", {})
            fresh = resp.get("freshness", {})
            return {
                "running": True,
                "trust": fresh.get("trust", "?"),
                "symbols": extra.get("symbols", 0),
                "freshness_token": fresh.get("freshness_token", ""),
            }
        except DaemonNotRunning:
            return {"running": False}

    # ---- helpers

    def _uri_to_rel(self, params: dict[str, Any]) -> str:
        uri = (params.get("textDocument") or {}).get("uri") or ""
        if uri.startswith("file://"):
            absolute = Path(uri[len("file://") :])
            try:
                return absolute.relative_to(self.repo).as_posix()
            except ValueError:
                return absolute.as_posix()
        return uri

    def _symbol_at(self, path: str, line: int) -> dict[str, Any] | None:
        try:
            resp = self.client.call("whats_in", {"path": path, "budget_tokens": 4000})
        except DaemonNotRunning:
            return None
        best: dict[str, Any] | None = None
        for r in resp.get("results", []):
            ln = int(r.get("line_number", 0))
            if ln <= line:
                if best is None or ln > int(best.get("line_number", 0)):
                    best = r
        return best


def run_lsp(repo: Path) -> int:
    server = LSPServer(repo)
    return server.serve()


__all__ = ["LSPServer", "run_lsp"]
