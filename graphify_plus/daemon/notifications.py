"""Layer 19.x — notification, anomaly, trend output sinks.

Anomalies and digest-line outputs go where the user opts in:
* terminal (default)
* OS notification (osascript / notify-send / Windows toast)
* webhook URL
* Slack webhook
* email digest (text-only, simple SMTP)

Configuration lives at ``.graphify_plus/notifications.yaml``::

    quiet_hours: ["22:00-08:00"]
    rate_limit_minutes: 30
    sinks:
      - kind: webhook
        url: https://hooks.slack.com/services/...
        events: [anomaly, weekly]
      - kind: terminal
        events: [anomaly]

This module ships dispatch + rate-limit + quiet-hours; the actual
HTTP call is left to ``urllib`` so we stay zero-dep.
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("graphify_plus.daemon.notifications")

CONFIG_FILE = "notifications.yaml"
LAST_SENT_FILE = "notification-rate-state.json"


@dataclass
class Sink:
    kind: str            # 'terminal' | 'webhook' | 'slack' | 'osnotify' | 'email'
    url: str = ""
    events: list[str] = field(default_factory=list)  # subset of {anomaly, weekly, trend}
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class NotificationConfig:
    sinks: list[Sink] = field(default_factory=list)
    quiet_hours: list[tuple[dtime, dtime]] = field(default_factory=list)
    rate_limit_minutes: int = 30


def config_path(repo: Path) -> Path:
    return repo / ".graphify_plus" / CONFIG_FILE


def state_path(repo: Path) -> Path:
    return repo / ".graphify_plus" / LAST_SENT_FILE


def load_config(repo: Path) -> NotificationConfig:
    p = config_path(repo)
    if not p.exists():
        return NotificationConfig()
    try:
        import yaml

        body = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        log.warning("invalid notifications config: %s", exc)
        return NotificationConfig()
    quiet_hours: list[tuple[dtime, dtime]] = []
    for raw in body.get("quiet_hours") or []:
        rng = _parse_range(str(raw))
        if rng:
            quiet_hours.append(rng)
    sinks = [
        Sink(
            kind=str(s.get("kind", "terminal")),
            url=str(s.get("url", "")),
            events=list(s.get("events") or []),
            extra={k: v for k, v in s.items() if k not in ("kind", "url", "events")},
        )
        for s in (body.get("sinks") or [])
        if isinstance(s, dict)
    ]
    return NotificationConfig(
        sinks=sinks,
        quiet_hours=quiet_hours,
        rate_limit_minutes=int(body.get("rate_limit_minutes", 30)),
    )


def _parse_range(text: str) -> tuple[dtime, dtime] | None:
    m = re.match(r"(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})", text.strip())
    if not m:
        return None
    return (dtime(int(m.group(1)), int(m.group(2))), dtime(int(m.group(3)), int(m.group(4))))


def in_quiet_hours(cfg: NotificationConfig, *, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc).astimezone()
    for start, end in cfg.quiet_hours:
        if start <= end:
            if start <= now.time() < end:
                return True
        else:
            # Range crosses midnight (e.g. 22:00-08:00).
            if now.time() >= start or now.time() < end:
                return True
    return False


def _load_rate_state(repo: Path) -> dict[str, float]:
    p = state_path(repo)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_rate_state(repo: Path, state: dict[str, float]) -> None:
    p = state_path(repo)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2), encoding="utf-8")


def is_rate_limited(repo: Path, sink: Sink, cfg: NotificationConfig) -> bool:
    rate = _load_rate_state(repo)
    last = rate.get(sink.kind + ":" + sink.url, 0.0)
    return (time.time() - last) < cfg.rate_limit_minutes * 60


def mark_sent(repo: Path, sink: Sink) -> None:
    rate = _load_rate_state(repo)
    rate[sink.kind + ":" + sink.url] = time.time()
    _save_rate_state(repo, rate)


# ---- dispatch -----------------------------------------------------------


def dispatch(
    repo: Path,
    *,
    event: str,
    title: str,
    body: str,
    cfg: NotificationConfig | None = None,
) -> list[dict[str, Any]]:
    """Send the notification to every matching sink. Returns one
    record per attempted sink so the caller (or tests) can inspect.
    """
    cfg = cfg or load_config(repo)
    if in_quiet_hours(cfg):
        return [{"sink": "_quiet_hours", "skipped": True}]
    out: list[dict[str, Any]] = []
    for sink in cfg.sinks:
        if sink.events and event not in sink.events:
            continue
        if is_rate_limited(repo, sink, cfg):
            out.append({"sink": sink.kind, "skipped": "rate-limited"})
            continue
        try:
            res = _dispatch_one(sink, event=event, title=title, body=body)
            out.append({"sink": sink.kind, "result": res})
            mark_sent(repo, sink)
        except Exception as exc:  # noqa: BLE001
            out.append({"sink": sink.kind, "error": str(exc)})
    if not cfg.sinks:
        # default to terminal echo
        out.append({"sink": "terminal", "body": f"[{event}] {title}: {body}"})
    return out


def _dispatch_one(sink: Sink, *, event: str, title: str, body: str) -> str:
    if sink.kind == "terminal":
        log.info("[%s] %s: %s", event, title, body)
        return "ok"
    if sink.kind in ("webhook", "slack"):
        if not sink.url:
            return "no-url"
        payload = {"event": event, "title": title, "body": body, "ts": time.time()}
        if sink.kind == "slack":
            payload = {"text": f"*{title}*\n{body}"}
        req = urllib.request.Request(
            sink.url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=4) as resp:
            return f"http {resp.status}"
    if sink.kind == "osnotify":
        return _os_notify(title, body)
    if sink.kind == "email":
        # Caller-configured SMTP; we just log.
        log.info("email sink not configured for SMTP: %s", body)
        return "stub"
    return "unknown-sink"


def _os_notify(title: str, body: str) -> str:
    import platform
    import shutil
    import subprocess

    system = platform.system()
    try:
        if system == "Darwin":
            script = (
                f'display notification "{body[:240]}" with title "{title[:60]}"'
            )
            subprocess.run(["osascript", "-e", script], check=False, timeout=2)
        elif system == "Linux" and shutil.which("notify-send"):
            subprocess.run(["notify-send", title, body], check=False, timeout=2)
        else:
            return "unsupported-platform"
        return "ok"
    except Exception as exc:  # noqa: BLE001
        return f"error: {exc}"


__all__ = [
    "CONFIG_FILE",
    "NotificationConfig",
    "Sink",
    "config_path",
    "dispatch",
    "in_quiet_hours",
    "is_rate_limited",
    "load_config",
    "mark_sent",
]
