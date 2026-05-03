# Cache compatibility matrix

The graphify-plus on-disk cache (`<repo>/.graphify_plus/cache.db`) is
versioned via the `meta.schema_version` row. A tool refusing to open a
cache from a newer version raises `GP-CACHE-VERSION-MISMATCH`; an older
version triggers a registered migration if one exists, otherwise the
same error.

## Matrix

| Tool version | Cache schema_version | Notes |
|---|---|---|
| 3.5.0 | 1 | Phase 5 cut |
| 4.0.0 | 1 | Phase 10 — MCP server, claude-md |
| 4.1.0 | 1 | Resilience foundation. `quick_check` on open, quarantine corrupt files. |
| 4.2.0 | 1 | Confidence layer. `Edge.confidence` carried in the JSON column — no DDL change, no migration. Caches written by ≤ 4.1.0 read fine; missing `confidence` defaults to 0.2 (FALLBACK) until re-ingested. |
| 4.3.0 | 1 | Trust score banner, drift JSONL log. JSONL log lives at `.graphify_plus/confidence_drift.log`; no SQLite change. |
| 4.4.0 | 1 | Operational hardening: repo-level lock, `gp vacuum`, watcher reconciliation. No schema change. |

## Forward compatibility rules

1. **Edge fields** — adding optional fields to the `Edge` JSON dict is
   a non-breaking change. Old caches read missing fields as
   per-field defaults; readers must use `.get(field, default)`.
2. **New SQL tables** — additive only, gated behind `IF NOT EXISTS` in
   `SCHEMA`. Bumps `schema_version` only when an existing row layout
   changes.
3. **Schema bump policy** — never bump for purely additive changes.
   Bump only when a) an existing column changes type or semantics, or
   b) a tool can no longer read older caches correctly.

## What to do on `GP-CACHE-VERSION-MISMATCH`

```
graphify-plus init --repo <path> --force
```

This re-ingests from source. The old cache is moved to
`cache.db.bak` first; you can `gp vacuum` it later.
