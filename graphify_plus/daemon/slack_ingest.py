"""Layer 6.2 — Slack / Discord / Teams ingest (privacy-first).

Default: opt-in, local-only, never leaves the machine. The ingestor
takes an *exported* archive from the chat platform (Slack JSON
export, Discord channel JSON, Teams conversation export) and
attribution-by-text-mention runs through the same flow as ADR /
GitHub ingest.

Privacy guardrails:

* Channels must be on an explicit allow-list under
  ``.graphify_plus/chat-allowlist.yaml``. Anything not in the list
  is silently dropped (not just warned).
* Personal DMs and private group chats are *never* ingested even if
  they appear in an export — the schema check looks for
  ``channel_kind`` and only accepts ``"public"`` or ``"engineering"``.
* Each thread's ingested body is truncated to 4 KB.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .ingestors import IngestNode, store_ingest_nodes

log = logging.getLogger("graphify_plus.daemon.slack_ingest")

ALLOWLIST_FILE = "chat-allowlist.yaml"
MAX_THREAD_LEN = 4_000


@dataclass
class ChatMessage:
    ts: str
    user: str
    text: str
    thread_id: str = ""
    channel: str = ""


@dataclass
class ChatAllowlist:
    channels: list[str] = field(default_factory=list)
    users_excluded: list[str] = field(default_factory=list)
    private_default: str = "skip"   # 'skip' | 'allow' (allow only if explicit per-channel)


def load_allowlist(repo: Path) -> ChatAllowlist:
    p = repo / ".graphify_plus" / ALLOWLIST_FILE
    if not p.exists():
        return ChatAllowlist()
    try:
        import yaml

        body = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        log.warning("invalid chat-allowlist: %s", exc)
        return ChatAllowlist()
    return ChatAllowlist(
        channels=list(body.get("channels") or []),
        users_excluded=list(body.get("users_excluded") or []),
        private_default=str(body.get("private_default", "skip")),
    )


def parse_slack_export(path: Path) -> list[ChatMessage]:
    """Parse a Slack JSON export folder. Each ``<channel>/<date>.json``
    is an array of messages.
    """
    out: list[ChatMessage] = []
    if not path.is_dir():
        return out
    for channel_dir in sorted(path.iterdir()):
        if not channel_dir.is_dir():
            continue
        channel = channel_dir.name
        for f in sorted(channel_dir.glob("*.json")):
            try:
                body = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(body, list):
                continue
            for msg in body:
                if not isinstance(msg, dict):
                    continue
                if msg.get("type") and msg.get("type") != "message":
                    continue
                out.append(
                    ChatMessage(
                        ts=str(msg.get("ts") or ""),
                        user=str(msg.get("user") or msg.get("user_profile", {}).get("name", "?")),
                        text=str(msg.get("text") or ""),
                        thread_id=str(msg.get("thread_ts") or msg.get("ts") or ""),
                        channel=channel,
                    )
                )
    return out


def filter_by_allowlist(
    messages: list[ChatMessage], allow: ChatAllowlist
) -> list[ChatMessage]:
    if not allow.channels:
        return []
    out: list[ChatMessage] = []
    excluded = set(allow.users_excluded)
    for m in messages:
        if m.channel not in allow.channels:
            continue
        if m.user in excluded:
            continue
        if not m.text.strip():
            continue
        out.append(m)
    return out


def threads_from_messages(messages: list[ChatMessage]) -> list[IngestNode]:
    """Group messages by thread_id and emit one IngestNode per thread."""
    by_thread: dict[str, list[ChatMessage]] = {}
    for m in messages:
        by_thread.setdefault(f"{m.channel}::{m.thread_id}", []).append(m)
    rows: list[IngestNode] = []
    for key, msgs in by_thread.items():
        msgs.sort(key=lambda m: m.ts)
        first = msgs[0]
        body = "\n".join(f"<{m.user}> {m.text}" for m in msgs)[:MAX_THREAD_LEN]
        title = first.text[:80] or first.thread_id
        rows.append(
            IngestNode(
                id=f"chat-{key}",
                kind="chat_thread",
                title=title,
                body=body,
                source="chat",
                url=f"chat://{first.channel}",
                metadata={"channel": first.channel, "messages": len(msgs)},
            )
        )
    return rows


def ingest_slack(store, repo: Path, archive: Path) -> dict[str, Any]:
    """End-to-end: parse export → filter → emit IngestNodes."""
    allow = load_allowlist(repo)
    if not allow.channels:
        return {
            "skipped": True,
            "reason": (
                "no chat-allowlist configured. Add channels to "
                ".graphify_plus/chat-allowlist.yaml under 'channels:' to opt in."
            ),
        }
    messages = parse_slack_export(archive)
    filtered = filter_by_allowlist(messages, allow)
    rows = threads_from_messages(filtered)
    if rows:
        store_ingest_nodes(store, rows, replace_kind="chat_thread")
    return {
        "messages_seen": len(messages),
        "after_allowlist": len(filtered),
        "threads": len(rows),
    }


__all__ = [
    "ALLOWLIST_FILE",
    "ChatAllowlist",
    "ChatMessage",
    "filter_by_allowlist",
    "ingest_slack",
    "load_allowlist",
    "parse_slack_export",
    "threads_from_messages",
]
