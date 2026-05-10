"""In-memory graph daemon — Sprint 1 of the master plan.

A long-running local process that holds the symbol graph in RAM with
pre-computed indexes. MCP tools and the CLI talk to this daemon over a
Unix domain socket so query latency drops from "load + traverse + serialize"
(hundreds of ms) to "look up the index" (sub-20ms P50).

Public API
----------

    from graphify_plus.daemon import (
        InMemoryGraph,    # the indexed graph held by the daemon
        DaemonServer,     # Unix-socket RPC server
        DaemonClient,     # lightweight blocking client
        socket_path, pid_path,
    )

The intent-typed tools live in ``handlers.py``. Each returns a structured
response with ``{node_id, label, source_file, line_number, snippet, confidence}``
and a freshness envelope so callers can decide whether to trust the answer.
"""

from __future__ import annotations

from .client import DaemonClient, DaemonNotRunning
from .indexes import InMemoryGraph
from .protocol import FreshnessTrust, pid_path, socket_path
from .server import DaemonServer

__all__ = [
    "DaemonClient",
    "DaemonNotRunning",
    "DaemonServer",
    "FreshnessTrust",
    "InMemoryGraph",
    "pid_path",
    "socket_path",
]
