"""Unix-socket JSON-line RPC server.

Single-threaded accept loop, one worker thread per connection, line-delimited
JSON requests/responses. Every response is wrapped in the standard envelope:
``{ok, request_id, freshness, receipt, results, more_available, ...}``.

The daemon owns:

  - One ``InMemoryGraph`` snapshot held under ``self._snapshot_lock``
  - A ``Store`` opened against ``<repo>/.graphify_plus/cache.db``
  - A pid file at ``<repo>/.graphify_plus/daemon.pid``
  - A unix socket at ``<repo>/.graphify_plus/daemon.sock``

Refresh pattern (Layer 2.1 in the master plan): ``op="refresh"`` triggers
a full rebuild of the in-memory graph from the on-disk store. The watcher
(future work) will trigger this same op after debounced fs events. Until
then ``gp daemon refresh`` and a future watcher integration both call it.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from ..runtime.store import Store, cache_path
from .handlers import HANDLERS, DEFAULT_BUDGET_TOKENS
from .indexes import InMemoryGraph
from .protocol import (
    DAEMON_DIR,
    ERR_BAD_REQUEST,
    ERR_INTERNAL,
    ERR_NO_GRAPH,
    ERR_UNKNOWN_OP,
    pid_path,
    socket_path,
)
from .receipts import make_receipt
from .telemetry import append_event as telemetry_append

log = logging.getLogger("graphify_plus.daemon.server")

ACCEPT_BACKLOG = 16
RECV_BUF = 65536
MAX_LINE_BYTES = 4 * 1024 * 1024  # 4 MiB max request line — requests should be tiny


class DaemonServer:
    """Unix-socket RPC server holding a single ``InMemoryGraph`` snapshot."""

    def __init__(self, repo_root: Path, *, watch: bool = True):
        self.repo_root = repo_root.resolve()
        self.daemon_dir = self.repo_root / DAEMON_DIR
        self.daemon_dir.mkdir(parents=True, exist_ok=True)
        self.socket_path = socket_path(self.repo_root)
        self.pid_path = pid_path(self.repo_root)
        self._snapshot_lock = threading.Lock()
        self._snapshot: InMemoryGraph | None = None
        self._sock: socket.socket | None = None
        self._stop = threading.Event()
        self._workers: list[threading.Thread] = []
        self._refresh_lock = threading.Lock()
        self._want_watcher = watch
        self._watcher: Any = None
        # Coalesce watcher-driven refreshes into one rebuild even when many
        # files change in a burst (e.g. a `git pull`).
        self._refresh_pending = threading.Event()
        self._refresh_thread: threading.Thread | None = None

    # ---- lifecycle -------------------------------------------------------

    def load_initial(self) -> None:
        """Load the on-disk graph into memory. Called once at startup."""
        path = cache_path(self.repo_root)
        if not path.exists():
            raise RuntimeError(
                f"no graph cache at {path}. Run `gp init --repo {self.repo_root}` first."
            )
        store = Store(path)
        try:
            snap = InMemoryGraph.from_store(store, self.repo_root)
        finally:
            store.close()
        with self._snapshot_lock:
            self._snapshot = snap
        log.info(
            "daemon loaded: %d symbols, %d files, %.1fms",
            len(snap.by_id),
            len(snap.by_path),
            snap.stats.elapsed_ms if snap.stats else 0.0,
        )

    def refresh(self) -> dict[str, Any]:
        """Rebuild the in-memory snapshot from the on-disk cache.

        Cheap once incremental updates land in the watcher; for now this is
        the ``ingest -> rebuild`` path. Serialised under ``_refresh_lock``
        so concurrent ``refresh`` requests don't stampede.
        """
        with self._refresh_lock:
            store = Store(cache_path(self.repo_root))
            try:
                snap = InMemoryGraph.from_store(store, self.repo_root)
            finally:
                store.close()
            with self._snapshot_lock:
                self._snapshot = snap
            log.info("daemon refreshed: %d symbols", len(snap.by_id))
            return {
                "symbols": len(snap.by_id),
                "files": len(snap.by_path),
                "elapsed_ms": snap.stats.elapsed_ms if snap.stats else 0.0,
                "freshness_token": snap.freshness_token,
            }

    def serve_forever(self) -> None:
        """Bind, listen, and dispatch until ``stop()`` is called.

        The socket is bound on the (resolved) ``self.socket_path``. Stale
        sockets from a previous crashed run are unlinked. The pid file is
        written before listening so clients can detect liveness.
        """
        self._unlink_stale_socket()
        self.pid_path.write_text(str(os.getpid()))
        if self._want_watcher:
            self._start_watcher()
            self._start_refresh_loop()
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.bind(str(self.socket_path))
            os.chmod(self.socket_path, 0o600)
        except OSError:
            s.close()
            self._cleanup_pid()
            raise
        s.listen(ACCEPT_BACKLOG)
        s.settimeout(0.5)
        self._sock = s
        log.info("daemon listening on %s (pid=%d)", self.socket_path, os.getpid())
        try:
            while not self._stop.is_set():
                try:
                    conn, _ = s.accept()
                except socket.timeout:
                    continue
                except OSError:
                    if self._stop.is_set():
                        break
                    raise
                t = threading.Thread(
                    target=self._handle_connection,
                    args=(conn,),
                    daemon=True,
                )
                t.start()
                self._workers.append(t)
                self._workers = [w for w in self._workers if w.is_alive()]
        finally:
            self._teardown()

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass

    def _teardown(self) -> None:
        self._stop_watcher()
        try:
            if self._sock is not None:
                self._sock.close()
        except OSError:
            pass
        try:
            if self.socket_path.exists():
                self.socket_path.unlink()
        except OSError:
            pass
        self._cleanup_pid()

    # ---- watcher integration (Sprint 3) ---------------------------------

    def _start_watcher(self) -> None:
        """Subscribe the runtime watcher to drive incremental refresh.

        Failure to start the watcher is *not* fatal — the daemon still
        serves queries against the snapshot loaded at startup, but
        freshness will degrade as files change. We log loudly so the
        operator sees it in ``gp daemon status`` (via the build log).
        """
        try:
            from ..runtime.watcher import Watcher

            self._watcher = Watcher(self.repo_root)
            self._watcher.subscribe(lambda _update: self._signal_refresh())
            self._watcher.start()
            log.info("daemon watcher subscribed to %s", self.repo_root)
        except Exception as exc:  # noqa: BLE001
            log.warning("daemon watcher unavailable: %s", exc)
            self._watcher = None

    def _stop_watcher(self) -> None:
        if self._watcher is not None:
            try:
                self._watcher.stop()
            except Exception:  # noqa: BLE001
                pass
            self._watcher = None
        # Wake the refresh loop so it sees ``_stop`` and exits.
        self._refresh_pending.set()
        if self._refresh_thread is not None:
            self._refresh_thread.join(timeout=2.0)
            self._refresh_thread = None

    def _signal_refresh(self) -> None:
        """Watcher callback. Coalesces bursts — many file events in a
        short window all collapse into a single rebuild.
        """
        self._refresh_pending.set()

    def _start_refresh_loop(self) -> None:
        def _loop() -> None:
            while not self._stop.is_set():
                if not self._refresh_pending.wait(timeout=0.5):
                    continue
                self._refresh_pending.clear()
                if self._stop.is_set():
                    return
                # Tiny coalescing window — let further bursts accumulate.
                time.sleep(0.05)
                self._refresh_pending.clear()
                try:
                    self.refresh()
                except Exception:  # noqa: BLE001
                    log.exception("daemon refresh loop: rebuild failed")

        self._refresh_thread = threading.Thread(target=_loop, daemon=True)
        self._refresh_thread.start()

    def _cleanup_pid(self) -> None:
        try:
            if self.pid_path.exists():
                self.pid_path.unlink()
        except OSError:
            pass

    def _unlink_stale_socket(self) -> None:
        if not self.socket_path.exists():
            return
        # If a daemon is already running, we expect to fail bind() loudly.
        # If the socket is stale (no listener), unlink it so we can claim it.
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(0.5)
        try:
            s.connect(str(self.socket_path))
            s.close()
            raise RuntimeError(
                f"daemon already listening on {self.socket_path} — "
                f"run `gp daemon stop` first"
            )
        except (ConnectionRefusedError, FileNotFoundError, socket.timeout, OSError):
            try:
                self.socket_path.unlink()
            except OSError:
                pass
        finally:
            try:
                s.close()
            except OSError:
                pass

    # ---- per-connection --------------------------------------------------

    def _handle_connection(self, conn: socket.socket) -> None:
        try:
            with conn.makefile("rwb", buffering=0) as fp:
                while not self._stop.is_set():
                    line = fp.readline(MAX_LINE_BYTES + 1)
                    if not line:
                        return
                    if len(line) > MAX_LINE_BYTES:
                        self._send_error(fp, "", ERR_BAD_REQUEST, "request too large")
                        return
                    self._dispatch_line(fp, line)
        except (ConnectionResetError, BrokenPipeError):
            pass
        except Exception as exc:  # noqa: BLE001
            log.exception("connection handler crashed: %s", exc)
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _dispatch_line(self, fp: Any, line: bytes) -> None:
        try:
            req = json.loads(line)
        except json.JSONDecodeError as e:
            self._send_error(fp, "", ERR_BAD_REQUEST, f"invalid JSON: {e}")
            return
        if not isinstance(req, dict):
            self._send_error(fp, "", ERR_BAD_REQUEST, "request must be a JSON object")
            return
        request_id = str(req.get("request_id") or uuid.uuid4().hex[:12])
        op = req.get("op")
        args = req.get("args") or {}
        if not isinstance(op, str):
            self._send_error(fp, request_id, ERR_BAD_REQUEST, "missing 'op'")
            return
        if not isinstance(args, dict):
            self._send_error(fp, request_id, ERR_BAD_REQUEST, "'args' must be an object")
            return
        # Default budget if not provided.
        args.setdefault("budget_tokens", DEFAULT_BUDGET_TOKENS)

        # Built-in ops the handler registry doesn't own.
        if op == "ping":
            self._send(fp, {"request_id": request_id, "ok": True, "extra": {"pong": True}})
            return
        if op == "refresh":
            try:
                info = self.refresh()
                self._send(
                    fp,
                    {
                        "request_id": request_id,
                        "ok": True,
                        "freshness": self._freshness(),
                        "extra": info,
                    },
                )
            except Exception as exc:  # noqa: BLE001
                self._send_error(fp, request_id, ERR_INTERNAL, str(exc))
            return
        if op == "shutdown":
            self._send(fp, {"request_id": request_id, "ok": True, "extra": {"bye": True}})
            self.stop()
            return

        handler = HANDLERS.get(op)
        if handler is None:
            self._send_error(fp, request_id, ERR_UNKNOWN_OP, f"unknown op: {op!r}")
            return

        with self._snapshot_lock:
            snap = self._snapshot
        if snap is None:
            self._send_error(fp, request_id, ERR_NO_GRAPH, "graph not loaded")
            return

        t0 = time.perf_counter()
        try:
            payload = handler(snap, args)
        except Exception as exc:  # noqa: BLE001
            log.exception("handler %s crashed", op)
            self._send_error(fp, request_id, ERR_INTERNAL, str(exc))
            return
        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        # Handler-level error short-circuit.
        if "error" in payload:
            err = payload["error"]
            self._send_error(
                fp,
                request_id,
                err.get("code", "ERROR"),
                err.get("message", "error"),
                detail=err.get("detail"),
            )
            return

        results = payload.get("results", []) or []
        receipt = make_receipt(op, elapsed_ms, results, len(results))
        freshness = self._freshness()
        response: dict[str, Any] = {
            "request_id": request_id,
            "ok": True,
            "freshness": freshness,
            "receipt": receipt,
            "results": results,
            "more_available": payload.get("more_available", 0),
        }
        if "extra" in payload:
            response["extra"] = payload["extra"]
        self._send(fp, response)
        telemetry_append(
            self.repo_root,
            op,
            elapsed_ms=receipt.get("elapsed_ms", 0.0),
            tokens=receipt.get("tokens", 0),
            n_results=len(results),
            trust=freshness.get("trust", "?"),
            ok=True,
        )

    def _freshness(self) -> dict[str, Any]:
        with self._snapshot_lock:
            snap = self._snapshot
        if snap is None:
            return {"trust": "STALE_REBUILD_NEEDED", "freshness_token": ""}
        return snap.freshness()

    @staticmethod
    def _send(fp: Any, body: dict[str, Any]) -> None:
        try:
            line = json.dumps(body, separators=(",", ":")).encode("utf-8") + b"\n"
            fp.write(line)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_error(
        self,
        fp: Any,
        request_id: str,
        code: str,
        message: str,
        *,
        detail: dict[str, Any] | None = None,
    ) -> None:
        err: dict[str, Any] = {"code": code, "message": message}
        if detail:
            err["detail"] = detail
        freshness = self._freshness()
        self._send(
            fp,
            {
                "request_id": request_id,
                "ok": False,
                "error": err,
                "freshness": freshness,
            },
        )
        telemetry_append(
            self.repo_root,
            "error",
            elapsed_ms=0.0,
            tokens=0,
            n_results=0,
            trust=freshness.get("trust", "?"),
            ok=False,
            error_code=code,
        )


def run_server(repo_root: Path, *, watch: bool = True) -> int:
    """Foreground entry-point: load and serve until SIGINT."""
    import signal

    server = DaemonServer(repo_root, watch=watch)
    server.load_initial()

    def _on_sig(_signum: int, _frame: Any) -> None:
        server.stop()

    signal.signal(signal.SIGINT, _on_sig)
    signal.signal(signal.SIGTERM, _on_sig)
    try:
        server.serve_forever()
    finally:
        server._teardown()
    return 0


__all__ = ["DaemonServer", "run_server"]
