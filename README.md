# graphify-plus

A tree-sitter-based code-graph analyser for TypeScript, JavaScript, Python, Go, Rust, Java, Ruby, and C#. Builds a symbol graph from a repository on disk, then runs adversarial audit probes, drift checks, dead-code analysis, and graph diffs against it.

This is the v5 line. The pre-v4 product was an enhancement layer over [safishamsi/graphify](https://github.com/safishamsi/graphify); see [`README.legacy.md`](README.legacy.md) for that earlier shape. Current graphify-plus is self-contained — it does not depend on graphify.

---

## The daemon (Layers 1–28 of the master plan)

The fast path: a long-running local daemon that answers structural
questions in sub-millisecond P50 with file:line on every row.

```bash
gp daemon quickstart                     # init + warm + install skill + recipes
gp daemon start --detach                 # bring up the daemon
gp daemon query who_calls -a node=AuthService.login --receipt
gp daemon plan "add rate limiting to all endpoints"
gp daemon coverage ingest coverage.xml   # then `gp daemon coverage untested`
gp daemon review --base main             # PR co-pilot output
gp daemon onboard                        # guided-tour walkthrough
gp daemon diagnose                       # one-screen ops status
```

`gp daemon --help` lists the full ~80 subcommands. See
[`REFERENCE.md`](REFERENCE.md) for the data model + extension guide,
[`ROADMAP.md`](ROADMAP.md) for layer-by-layer status, and
[`examples/`](examples/) for runnable scripts.

The MCP server (`gp mcp`) exposes 31 tools so Claude can call them
directly. The LSP shim (`gp daemon lsp`) surfaces hover / codeLens /
inlay-hint info every modern editor speaks.

## What graphify-plus is NOT

The mark of a mature project is publishing what it explicitly will not do.

- **graphify-plus is not a database.** No transactions, no high-availability, no replication. The graph is an artifact, not a system of record.
- **graphify-plus is not a security tool.** SAST/CVE overlays inform; they don't audit. A real security review is still required.
- **graphify-plus does not replace your IDE.** Symbol resolution, auto-completion, jump-to-definition, hover-to-type stay with the LSP. The graph is *additional* context, not a substitute.
- **graphify-plus does not run your code.** Runtime ingest is opt-in and consumes traces produced elsewhere.
- **graphify-plus is not your team's wiki.** It can ingest and link, but the canonical decision still lives in the source artifact.
- **graphify-plus is not real-time.** Watcher latency exists. Architectural rule violations are detected on the next build, not at the keystroke.

---

## Install

```bash
pip install graphify-plus
```

Python 3.10+ required (per `pyproject.toml`'s `requires-python = ">=3.10"`). Core runtime deps: `networkx>=3.0`, `tree-sitter>=0.21,<0.22`, `tree-sitter-languages>=1.10`, `click>=8.1`, `orjson>=3.10`, `numpy`, `scipy`.

The `tree-sitter-languages` wheel bundles grammars for every supported language — no separate compiler step.

---

## Quickstart

```bash
# 1. Build a symbol graph for the current repo.
gp init --repo .

# 2. Audit the graph — adversarial probes graded A–F, plus an overall trust score.
gp audit .graphify_plus/graph_symbols.jsonl

# 3. List dead-code candidates worth deleting (high-confidence only).
gp prune --repo . --min-confidence 0.7

# 4. Compare two snapshots (e.g. for a code review or CI gate).
gp diff old.jsonl new.jsonl
```

`gp init` writes `.graphify_plus/cache.db` (SQLite) and `.graphify_plus/graph_symbols.jsonl` into the target repo. Both files are deterministic — re-running `init` on an unchanged tree produces byte-identical output.

A real `gp audit` run on a tiny fixture:

```
# Adversarial Audit Report

## Trust score: **67 / 100** — verify findings before relying on the graph

**Graph:** 4 nodes, 3 edges (undirected)

## [B] Overall: **B** (weighted score 2.671)

**Skipped probes** (excluded from trust score — known reasons):
- `confidence_drift`: SKIP — need >=3 INFERRED edges with confidence_score (got 0)

## 1. Edge deletion stability - Grade A
_After deleting 5% of edges across 5 runs, 100% of community structure was preserved on average._
```

---

## CLI reference

26 subcommands. Run `gp <command> --help` for per-command options. Both `gp` and `graphify-plus` work as the entry point.

**Graph construction**

| Command | What it does |
|---|---|
| `init` | Walk a repo, parse with tree-sitter, write the symbol cache and JSONL. |
| `enrich git \| lockfile \| telemetry` | Overlay non-structural information onto the graph. |
| `vacuum` | Compact and clean the SQLite cache. |
| `watch` | Incremental file-watcher reconciliation. |

**Analysis**

| Command | What it does |
|---|---|
| `audit` | Adversarial probes → A–F grades + 0–100 trust score. |
| `prune` | Dead-code candidates, classified by category and confidence. |
| `find` | Semantic symbol/file finder (BM25 over per-community indices). |
| `explain` | Explain a node or edge — provenance, confidence, neighbours. |
| `blast-radius` | What breaks if a symbol changes. |
| `matrix` | Render a community to SVG/PNG/DOT. |
| `drift check \| watch` | Evaluate `.graphify_plus/rules.yaml` against current graph. |
| `simulate` | Simulate a plan execution against the graph. |
| `guardrails` | Evaluate proposed edits against architectural rules. |

**Output**

| Command | What it does |
|---|---|
| `diff` | Diff two graph snapshots; markdown / JSON / text output. |
| `claude-md` | Manage `CLAUDE.md` files. |
| `sync-docs` | Keep docs aligned with code. |
| `skeleton` | Manage the skeleton CAS store. |
| `context` | Token-budgeted context framing. |
| `visual` | Visualise a community / subgraph. |

**Workflow**

| Command | What it does |
|---|---|
| `session` | Manage capture/replay sessions. |
| `coordinate` | Multi-agent coordination. |
| `stitch` | Stitch sub-graphs into one. |
| `plan` | Generate a change plan from intent. |

**Infrastructure**

| Command | What it does |
|---|---|
| `mcp` | Run as an MCP server. |
| `serve` | Run REST/web server. |
| `doctor` | Diagnose the workspace ('something feels off'). |

---

## Audit

`gp audit` runs ten probes against the graph. Each grades A–F; the overall trust score is a weighted average of those that ran. Skipped probes are surfaced with their reason rather than silently dropped.

| Probe | What it measures |
|---|---|
| `edge_deletion_stability` | Community-structure resilience to random edge removal. |
| `confidence_drift` | Coefficient of variation across `INFERRED` edge confidences. |
| `rename_sensitivity` | High-degree nodes whose label is embedded in their edge evidence. |
| `structural_fragility` | Articulation points (cut vertices) as a fraction of all nodes. |
| `lonely_inferred_edges` | `INFERRED` edges with no shared neighbour for corroboration. |
| `modularity_quality` | Newman Q for the community partition. |
| `centrality_drift` | Agreement across degree / betweenness / pagerank top-K. |
| `unresolved_imports` | Fraction of import edges that resolved to a real symbol. |
| `phantom_symbols` | Symbols emitted by a failed adapter parse. |
| `low_confidence_ratio` | Share of edges below a confidence threshold. |

The output is a markdown report by default; `--json-output PATH` writes the full machine-readable record. `--fail-below B` makes the command exit non-zero when the overall grade slips, suitable for a CI gate. `--only <probe>` runs a single probe.

**Known limitation:** `confidence_drift` currently always SKIPs because adapters emit numeric confidence values rather than the `"INFERRED"` string sentinel the probe expects. Tracked in 5.1.0 known limitations; the other nine probes grade.

---

## Dead-code analysis

`gp prune` reports symbols the budgeter should exclude from any framed context — and, for callers doing actual cleanup work, candidates worth deleting from the codebase. Each candidate is classified by a likely category and a confidence score from 0.0 to 1.0, so callers can filter out the false-positive-prone buckets and focus on high-signal candidates.

```bash
gp init --repo .                            # build the symbol graph
gp prune --repo . --json                    # all candidates, classified
gp prune --repo . --min-confidence 0.7      # high-confidence only
```

**Categories:**

| Category | Confidence | What it means |
|---|---|---|
| `plausibly_dead` | ≈0.85 | Module-level symbol with no inbound edges and no false-positive heuristic match. The bucket worth deleting. |
| `prop_type` | ≈0.4 | TS `interface`/`type` ending in `*Props` / `*Properties`. Often used only as a type annotation that the graph doesn't track. |
| `unknown` | ≈0.5 | No rule matched — review by hand. |
| `jsx_internal` | ≈0.15 | Nested function inside a JSX-bearing parent. Often a real handler that dynamic dispatch hides. |
| `reducer_case` | ≈0.15 | `onX` / `handleX` / `*Reducer` / `*Handler` name shape. Usually wired up by string keys. |
| `test_internal` | ≈0.15 | Lives in `__tests__/` or `*.test`/`*.spec` files. |
| `pytest_fixture` | ≈0.15 | `conftest.py` or `fixture_*` / `setup_*` Python helpers. |
| `dunder_method` | ≈0.15 | `__dunder__` names in `.py` files. |

`--min-confidence 0.7` returns only the `plausibly_dead` bucket — the list to actually act on. The lower-confidence buckets are surfaced for inspection but not recommended for blind deletion.

JSON output adds a `dead_classified` array carrying each candidate's qualified name, file path, kind, language, category, and confidence.

---

## Drift rules

`gp drift check` evaluates a YAML rules file at `<repo>/.graphify_plus/rules.yaml` against the current graph and reports violations. `gp drift watch` runs the same check continuously, driven by the file watcher.

```yaml
# .graphify_plus/rules.yaml
layers:
  ui:        ["frontend/**", "src/components/**"]
  api:       ["backend/api/**", "service/**"]
  database:  ["backend/db/**", "**/migrations/**"]

rules:
  - id: no-ui-to-db
    description: UI must not import from the database layer.
    forbid_edge:
      from_layer: ui
      to_layer: database
```

The same rules engine powers `gp guardrails` (evaluate a *proposed* edit before it lands) and the shadow-graph dry-run. A drift run with zero violations grades A; from there, grade degrades with violation count. Reports land at `.graphify_plus/drift_report.md` in the target repo.

---

## Edge kinds

For TypeScript and JavaScript the adapter resolves five intra-file relationships during `gp init`. The latter three are what saves a real symbol from looking dead in the prune output:

- **`contains`** — module → top-level symbol; class → method.
- **`imports`** — module → import target (often unresolved across files).
- **`jsx_render`** — `<Component />` references resolve to the function or component declaration in the same file (handles identifier, member-expression, arrow-function, and nested-component patterns). Confidence 0.7.
- **`calls`** — `foo(x)`, `Ns.foo(x)`, calls inside template literal substitutions, and calls inside JSX expression containers all resolve to the same-file callee. Confidence 0.7.
- **`references`** — bare identifier references (functions stored in `const SCENES = [Foo, Bar]`, passed as `subscribe(callback)`, returned as values, assigned as object-literal values) resolve to the same-file target. Confidence 0.5 — noisier than calls but the signal that catches dynamic-dispatch patterns.

Cross-file resolution is intentionally out of scope for these passes. Cross-file calls remain in the unresolved-imports bucket and are graded by the `unresolved_imports` audit probe.

---

## Architecture overview

`gp init` parses each source file with tree-sitter (one adapter per language under `core/adapters/`), merges per-file `(symbols, edges)` into a deterministic graph, persists it to a SQLite cache plus a JSONL sidecar, and computes Louvain communities so the audit and semantic-search layers have something to grade against. Everything else (`prune`, `audit`, `find`, `drift`, `diff`, `serve`) reads from that cache.

```
graphify_plus/
├── core/                 # tree-sitter adapters + graph construction
│   ├── adapters/         # one per language (ts, js, py, go, rust, java, ruby, c#)
│   ├── ingest.py         # repo walker + per-file parse isolation
│   ├── symbol_graph.py   # adapters' (symbols, edges) → networkx MultiDiGraph
│   └── skeletonizer.py   # CAS-backed code skeletons
├── audit/                # 10 adversarial probes + CLI
├── query/                # prune, semantic find, blast-radius, drift, …
├── runtime/              # SQLite store, watcher, sessions, rules engine
├── interface/            # CLI dispatcher (cli/), MCP server, REST + web UI
└── review/               # graph diff for code review
```

---

## Tests

```bash
pytest -q
```

336 tests collected on a clean install; runs in ~10s. Five test files (`test_privacy`, `test_rest_server`, `test_budget`, `test_overlay`, `test_session`) require optional dev dependencies (`hypothesis` etc.) and error at collection time without them — pytest will report the count if those are excluded with `--ignore`.

A version-sync guard test (`tests/test_version_sync.py`) asserts `pyproject.toml`'s `[project].version` matches `graphify_plus.__version__`, so the two can't drift silently.

---

## Relationship to graphify

The project name and earliest versions originated as an enhancement layer for [safishamsi/graphify](https://github.com/safishamsi/graphify). That architecture is preserved in [`README.legacy.md`](README.legacy.md). The current product is independent — no `graphify` import, no shared graph format, no shared CLI surface.

---

## Licence

MIT
