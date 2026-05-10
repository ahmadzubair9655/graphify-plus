# Privacy contract — `grep-events.jsonl`

This file is the load-bearing measurement surface for graphify-plus.
Without it, the rewrite has no way to demonstrate that Claude's tool
choice changed. So it ships with explicit privacy guarantees and is
intentionally boring to maintain.

**TL;DR**: local-only, never shipped, never sent over the network,
deletable at any time without breaking anything.

---

## What's in it

One JSON object per line, written by the pre-grep hook on every
Grep / Glob tool call it observes:

```json
{
  "ts": "2026-05-09T11:42:13.001234+00:00",
  "pattern": "AuthService",
  "nudged": true,
  "bareword": true,
  "daemon_running": true,
  "fresh": true,
  "matched_node_count": 12
}
```

`pattern` is truncated to 80 characters. Nothing else from the grep
payload is captured.

## What's NOT in it

By design, never:

- File contents.
- File paths searched.
- Symbol names beyond the literal grep pattern.
- The grep results themselves.
- Any user identifier (no username, no project name, no host).
- Any token, key, or secret — even if present in the pattern.
- Timestamps with sub-millisecond resolution that could correlate
  cross-session.

The matching index lookup the hook performs is *server-side* against
the daemon's in-memory snapshot; the *response* (whether a node
matched) is summarised to a count, not a list of names.

## Where it lives

- Path: `<repo>/.graphify_plus/grep-events.jsonl`
- Owner: the user invoking `gp`. File mode is 0644 (user-writable;
  group/world readable for the same reason the rest of
  `.graphify_plus/` is — it's a build artifact, not a secret).

## Where it does NOT go

- It is **never** uploaded.
- It is **never** included in `gp daemon diagnose` output.
- It is **never** referenced in any HTTP request the daemon makes.
- It is **never** baked into a Docker image, package, or release
  artifact.
- The optional opt-in HTTP telemetry exporter
  (`GRAPHIFY_TELEMETRY_URL`) does **not** read this file. Telemetry
  upload covers the existing graph-tool call counter only.

If any of those statements becomes false in a future change, that is
a privacy regression and the change should be reverted.

## Deleting it

Safe at any time:

```bash
rm <repo>/.graphify_plus/grep-events.jsonl
```

The next grep observed by the hook recreates the file. The only
side-effect is that `gp daemon adoption` reports zero events for the
window that just got cleared.

## Rotation

The hook never rotates this file itself. If the file grows large
enough to be inconvenient, delete it. A future enhancement may
add automatic rotation (truncate to last N events, write to dated
archives), but this is not currently implemented.

## Inspecting

Use `gp daemon adoption [--window-hours N]` for the aggregated view.
For raw events:

```bash
jq . <repo>/.graphify_plus/grep-events.jsonl
```

## Why this file matters

The thesis of v6.0 is that Claude picks the graph over grep on
structural questions. Without the denominator (grep observations)
the project has no way to verify or falsify that claim. This file
is the denominator. The numerator (graph tool calls) lives in
`telemetry.jsonl` and has the same privacy properties.

Make this file boring. Don't add fields. Don't move it.

---

_See [`RISKS.md`](../RISKS.md) for the post-merge failure modes
related to telemetry and adoption measurement._
