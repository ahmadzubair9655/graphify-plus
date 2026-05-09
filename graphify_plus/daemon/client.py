"""Lightweight blocking client for the daemon.

Round-trips one JSON-line request and returns the parsed response. Designed
for the MCP wrapper and the CLI — not a long-lived connection-pooled client.
A short-lived connection per call is fine because Unix-socket connect is
~50µs and the daemon's whole pitch is sub-100ms responses.
"""

from __future__ import annotations

import json
import os
import socket
import time
import uuid
from pathlib import Path
from typing import Any

from .protocol import pid_path, socket_path


class DaemonNotRunning(RuntimeError):
    """Raised when no daemon is listening on the expected socket."""


class DaemonError(RuntimeError):
    """Raised when the daemon returns ``{ok: false}``."""

    def __init__(self, code: str, message: str, detail: dict[str, Any] | None = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.detail = detail or {}


class DaemonClient:
    """Blocking, single-call client.

    Use::

        client = DaemonClient(repo_root)
        resp = client.call("who_calls", {"node": "AuthService.login"})
        for row in resp["results"]:
            ...
    """

    def __init__(self, repo_root: Path, *, timeout_s: float = 5.0):
        self.repo_root = repo_root.resolve()
        self.socket_path = socket_path(self.repo_root)
        self.pid_path = pid_path(self.repo_root)
        self.timeout_s = timeout_s

    def is_running(self) -> bool:
        """Cheap liveness probe — checks socket + pid + ping."""
        if not self.socket_path.exists():
            return False
        pid = self._read_pid()
        if pid and not self._pid_alive(pid):
            return False
        try:
            self._raw_call({"op": "ping"})
        except Exception:  # noqa: BLE001
            return False
        return True

    def call(
        self, op: str, args: dict[str, Any] | None = None, *, request_id: str | None = None
    ) -> dict[str, Any]:
        req = {
            "op": op,
            "args": args or {},
            "request_id": request_id or uuid.uuid4().hex[:12],
        }
        resp = self._raw_call(req)
        if not resp.get("ok"):
            err = resp.get("error") or {}
            raise DaemonError(
                err.get("code", "ERROR"),
                err.get("message", "unknown error"),
                err.get("detail"),
            )
        return resp

    def shutdown(self) -> None:
        try:
            self.call("shutdown")
        except Exception:  # noqa: BLE001
            pass

    # ---- internals -------------------------------------------------------

    def _raw_call(self, req: dict[str, Any]) -> dict[str, Any]:
        if not self.socket_path.exists():
            raise DaemonNotRunning(f"no daemon socket at {self.socket_path}")
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout_s)
        try:
            s.connect(str(self.socket_path))
        except (ConnectionRefusedError, FileNotFoundError) as exc:
            s.close()
            raise DaemonNotRunning(str(exc)) from exc
        try:
            line = json.dumps(req, separators=(",", ":")).encode("utf-8") + b"\n"
            s.sendall(line)
            buf = bytearray()
            deadline = time.monotonic() + self.timeout_s
            while True:
                if time.monotonic() > deadline:
                    raise TimeoutError("daemon did not respond before timeout")
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf.extend(chunk)
                if b"\n" in chunk:
                    break
            line_in, _, _ = bytes(buf).partition(b"\n")
            if not line_in:
                raise RuntimeError("daemon returned empty response")
            return json.loads(line_in)
        finally:
            try:
                s.close()
            except OSError:
                pass

    def _read_pid(self) -> int | None:
        try:
            return int(self.pid_path.read_text().strip())
        except (OSError, ValueError):
            return None

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True


__all__ = ["DaemonClient", "DaemonError", "DaemonNotRunning"]
