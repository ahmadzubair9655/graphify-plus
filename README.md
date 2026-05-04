# graphify-plus

> Drop-in enhancement layer for [safishamsi/graphify](https://github.com/safishamsi/graphify).  
> Point it at your `graphify-out/graph.json` and it runs. No changes to graphify required.

```bash
pip install graphify-plus
graphify-plus enhance ./graphify-out/graph.json
```

graphify already does the hard part — it turns a folder of code, docs, papers, images, and video into a queryable knowledge graph. graphify-plus runs after that and adds the question graphify doesn't answer: **what should you not trust about this graph, and what would break if you changed it?**

---

## What graphify-plus adds

### v2 — Core enhancements (7 modules)

| Module | What it does |
|---|---|
| `temporal` | Git-aware temporal edges: `first_seen`, `last_modified`, `commit_author`, `supersedes`. One bulk `git log` call regardless of corpus size. |
| `reconcile` | After `graphify merge-graphs`, finds semantically identical nodes from different repos and adds `cross_repo_equivalent_to` edges. Auto-detects namespaces from file paths. |
| `causal` | "What forced this node to exist?" Adds `caused_by / mandated_by / required_by / response_to / evolved_from` edges. Targets high-interest nodes (security/deprecated keywords, rationale_for neighbours) not just high-degree ones. |
| `contradict` | Detects contradictions between AST-extracted and LLM-inferred edges. Builds exclusive pairs dynamically from your graph's actual relation types. Flags docstring/implementation conflicts. |
| `budget` | Importance-ranked subgraph extraction. Nodes scored by degree centrality, hop distance, edge confidence, and temporal recency. Fuzzy label+id matching for hash-format node IDs. |
| `writeback` | MCP write tools: `annotate_node`, `correct_edge`, `flag_ambiguous`. Append-only `corrections.jsonl` ledger with idempotency guards and conflict detection. |
| `report_json` | Machine-readable `report.json` with a 0–100 **health score** (A–F) from 5 weighted signals. Structured warnings. PreToolUse hook injection helper. |

### v3 — Advanced features (5 production, 5 prototype/stub)

| Module | Status | What it does |
|---|---|---|
| `audit` | **production** | 7 adversarial probes → weighted A–F grade. See [Audit](#adversarial-audit). |
| `counterfactual` | **production** | "What breaks if I delete this node?" Impact simulation with severity scoring. |
| `verification` | **production** | Multi-agent voting — N models extract, disagreements tagged `DISPUTED`. |
| `review` | **production** | Graph diff for code review. Markdown/JSON/text output. GitHub Action included. |
| `selfhealing` | **production** | Scans for stale issues, proposes corrections. Tracks confidence drift over time. |
| `multimodal` | prototype | Image manifest + text-similarity retrieval. CLIP hook available for callers. |
| `live` | prototype | File-watcher daemon, debounced rebuild, pollable delta event queue. |
| `federation` | prototype | Cross-team graph query routing. Transport/auth are pluggable hooks. |
| `local` | stub | Interface for on-device extraction. Similarity edges work with `sentence-transformers`. |
| `personal` | stub | Notes and shell history ingest work. Browser/Slack/meetings are stubs with design docs. |

---

## Install

```bash
# Core (no graphify dependency — works standalone on any graph.json)
pip install graphify-plus

# Bundled with graphify itself
pip install "graphify-plus[full]"

# With local embedding support (sentence-transformers for similarity edges)
pip install "graphify-plus[embeddings]"

# Development
pip install "graphify-plus[dev]"
```

**Requirements:** Python 3.10+. Core dependency: `networkx>=3.0` only.

---

## Quickstart

### The three CLI commands

```bash
# 1. Enhance an existing graph
graphify-plus enhance ./graphify-out/graph.json

# 2. Audit the graph (7 adversarial probes)
graphify-plus audit ./graphify-out/graph_enhanced.json

# 3. Diff two snapshots (for code review or CI)
graphify-plus diff old/graph.json new/graph.json
```

### Enhance — Python API

```python
from graphify_plus import enhance_existing_graph

result = enhance_existing_graph(
    graph_json_path="./graphify-out/graph.json",
    corpus_root="./my-repo",   # for git temporal enrichment
    verbose=True,
)

print(result["report"]["health"]["summary"])
# Health 81/100 (B) — main drag: 3 contradictions
```

After running, the following files appear in `graphify-out/`:

| File | Contents |
|---|---|
| `graph_enhanced.json` | Original graph + all new node attributes and edge types |
| `report.json` | Machine-readable: health score, warnings, god nodes, causal chains |
| `corrections.jsonl` | Append-only MCP writeback ledger |

---

## Adversarial audit

The audit answers: *what should you not trust about this graph?*

```bash
graphify-plus audit ./graphify-out/graph_enhanced.json
```

```
Graph: 312 nodes, 891 edges (undirected)

[B] Overall: B (weighted score 2.85)

1. Edge deletion stability — Grade A
   After deleting 5% of edges across 5 runs, 97% of community
   structure was preserved on average.

2. Confidence drift — Grade B (CV=0.18)
   INFERRED edges have mean confidence 0.71 +/- 0.13

3. Rename sensitivity — Grade A
   0 high-degree nodes have edges referencing their label.

4. Structural fragility — Grade C
   4 cut vertices (1.3% of nodes). Moderate bottlenecks.
     - AuthService (47°)
     - RequestRouter (31°)

5. Lonely INFERRED edges — Grade D
   12 of 47 INFERRED edges have no structural corroboration.

6. Modularity quality — Grade A (Q=0.42)
   Strong community structure.

7. Centrality drift — Grade B
   Top-10 agreement across metrics: 80%
```

**Probes:**

| Probe | What it measures | Grade threshold |
|---|---|---|
| Edge deletion stability | Community structure resilience to random edge removal | Overlap A≥90% |
| Confidence drift | Coefficient of variation across INFERRED scores | CV A<0.1 |
| Rename sensitivity | Nodes whose label is embedded in their edge evidence | Worst-node fragility |
| Structural fragility | Articulation points (cut vertices) | Cut ratio A<5% |
| Lonely INFERRED edges | INFERRED edges with no shared neighbour for corroboration | Ratio A<10% |
| Modularity quality | Newman Q for the community partition | Q A≥0.4 |
| Centrality drift | Agreement between degree/betweenness/pagerank top-K | Overlap A≥90% |

**CLI options:**

```bash
graphify-plus audit graph.json --fail-below B         # exit 1 if grade below B (CI)
graphify-plus audit graph.json --only structural_fragility lonely_inferred_edges
graphify-plus audit graph.json --skip-expensive       # skips betweenness (slow on large graphs)
graphify-plus audit graph.json --json-output audit.json --quiet
graphify-plus audit graph.json --iterations 10 --seed 123
```

---

## Dead-code analysis

`gp prune` reports symbols the budgeter should exclude from any framed
context — and, for callers doing actual cleanup work, candidates worth
deleting from the codebase. Each candidate is classified by a likely
category and a confidence score from 0.0 to 1.0, so callers can filter
out the false-positive-prone buckets and focus on high-signal
candidates.

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

`--min-confidence 0.7` returns only the `plausibly_dead` bucket — the
list to actually act on. The lower-confidence buckets are surfaced for
inspection but not recommended for blind deletion.

JSON output adds a `dead_classified` array carrying each candidate's
qualified name, file path, kind, language, category, and confidence.

### Edge kinds resolved within a file

For TypeScript and JavaScript the adapter resolves three intra-file
relationships during `gp init`. Each one is what saves a real symbol
from looking dead in the prune output:

- **`jsx_render`** — `<Component />` references resolve to the
  function or component declaration in the same file (handles
  identifier, member-expression, arrow-function, and nested-component
  patterns). Confidence 0.7.
- **`calls`** — `foo(x)`, `Ns.foo(x)`, calls inside template literal
  substitutions, and calls inside JSX expression containers all
  resolve to the same-file callee. Confidence 0.7.
- **`references`** — bare identifier references (functions stored in
  `const SCENES = [Foo, Bar]`, passed as `subscribe(callback)`,
  returned as values, assigned as object-literal values) resolve to
  the same-file target. Confidence 0.5 — noisier than calls but the
  signal that catches dynamic-dispatch patterns.

Cross-file resolution is intentionally out of scope for these passes.
Cross-file calls remain in the unresolved-imports bucket and are
graded by the `unresolved_imports` audit probe.

---

## Graph diff for code review

```bash
graphify-plus diff old/graph.json new/graph.json
```

```
## Graph diff [CRITICAL]

312 -> 308 nodes (-4), 891 -> 879 edges (-12)
Health score: 81 (B) -> 73 (B) (-8)

### 1 causal chain(s) orphaned
- `OAuth2Migration` -[caused_by]-> `legacy_auth_check`

### - 4 node(s) removed
- `legacy_auth_check` (was 8°) [CRITICAL]
- `deprecated_token_cache` (was 5°) [HIGH]
- `unused_helper` (was 1°) [LOW]
```

**Per-change severity:**

| Severity | Conditions |
|---|---|
| `CRITICAL` | Removed node had degree ≥10, or had a causal edge |
| `HIGH` | Removed node had degree ≥5 |
| `MEDIUM` | Removed node had degree ≥2, or contradictions increased |
| `LOW` | Isolated node removed, or minor structural change |

**CLI options:**

```bash
# Include health-score delta from report.json files
graphify-plus diff old.json new.json \
  --old-report old-report.json \
  --new-report new-report.json

# CI: fail PR if overall severity exceeds HIGH
graphify-plus diff old.json new.json --fail-above HIGH

# Output formats
graphify-plus diff old.json new.json --format json --output diff.json
graphify-plus diff old.json new.json --format text   # plain text for CI logs
graphify-plus diff old.json new.json --format markdown  # default (for PR comments)
```

### GitHub Action

Copy `.github/workflows/graph-diff.yml` from this repo into your project. It will:

1. Build a graphify graph on the PR head
2. Build a graphify graph on the base branch
3. Run `graphify-plus diff` between them
4. Post the diff as a PR comment
5. Fail the PR if severity exceeds `HIGH`

```yaml
# .github/workflows/graph-diff.yml
# Already in the repo — copy to your project as-is.
```

---

## MCP write-back

Add write tools to your graphify MCP server so the assistant can improve the graph during sessions:

```python
from graphify_plus import handle_write_tool, apply_corrections, WRITE_TOOL_SCHEMAS

# In your MCP serve.py — add alongside existing read tools:
if tool_name in ("annotate_node", "correct_edge", "flag_ambiguous"):
    return handle_write_tool(tool_name, tool_input, G, corrections_path)
```

The three write tools:

- **`annotate_node(node_id, note)`** — attach a note. Idempotent: same note twice = one record.
- **`correct_edge(source_id, target_id, new_relation)`** — fix a relation type. Conflict-safe.
- **`flag_ambiguous(node_id, reason)`** — mark for human review.

Apply accumulated corrections at rebuild time:

```bash
graphify-plus enhance ./graphify-out/graph.json --apply-corrections
# Applied 7 corrections. Skipped 2 (already applied).
```

---

## Counterfactual simulation

```python
from graphify_plus import simulate_change

result = simulate_change(G, node_id="auth_service", change_type="delete")

print(result["severity_level"])       # CRITICAL
print(result["direct_dependents"])    # [{id, label, source_file}, ...]
print(result["broken_causal_chains"]) # causal edges that become orphaned
print(result["recommendations"])      # ["Update 4 dependent nodes before removing..."]
```

Change types: `delete`, `rename`, `deprecate`, `split`, `merge`.

```python
# What happens if I revert this commit?
from graphify_plus import simulate_commit_revert
result = simulate_commit_revert(G, commit_hash="abc1234")
print(result["summary"])
# Reverting abc1234 would remove 3 node(s) and 5 edge(s), orphaning 2 additional node(s).
```

---

## Multi-agent verification

```python
from graphify_plus import call_with_voting

result = call_with_voting(
    text=source_code,
    extract_fns={
        "claude": claude_extract,
        "gpt4":   gpt4_extract,
        "gemini": gemini_extract,
    },
    agreement_threshold=2,  # at least 2 of 3 must agree
)

# result["edges"] has confidence_class: HIGH | DISPUTED | MINORITY
# DISPUTED edges carry result["dissenting_opinions"] for manual review
```

---

## Self-healing proposals

```python
from graphify_plus import generate_proposals, format_digest

proposals = generate_proposals(
    G,
    history_path="graphify-out/history.jsonl",  # optional confidence history
)
print(format_digest(proposals))
```

```
Self-healing digest
5 proposals (high: 2, medium: 2, low: 1)

Unresolved Contradiction (1)
- AuthService → TokenValidator: AST and LLM extraction disagree...
  Options: accept_AST, accept_LLM, remove_both, flag_ambiguous

Weak Causal Edge (1)
- OAuth2Migration → legacy_auth_check: confidence 0.42...
  Options: verify, remove, downgrade_relation
```

Track confidence drift over multiple runs:

```python
from graphify_plus.selfhealing.proposals import record_confidence_snapshot
record_confidence_snapshot(G, history_path="graphify-out/history.jsonl")
# Call this after each enhance run; drift proposals appear after 3+ snapshots.
```

---

## Token-budgeted retrieval

```python
from graphify_plus import extract_budgeted_subgraph
from graphify_plus.budget import find_root_ids_for_query

# Fuzzy matching — works even with hash-format node IDs
roots = find_root_ids_for_query(G, "auth flow", top_n=5)
context = extract_budgeted_subgraph(G, root_ids=roots, token_budget=1500)
# Returns ranked subgraph string ready to inject into a prompt.
# Overflow nodes get: "... 14 additional nodes cut by budget"
```

Node scoring weights: centrality (40%) + hop distance (25%) + confidence (20%) + temporal recency (15%).

---

## Health score

A single 0–100 number computed from five weighted signals:

| Signal | Weight | Measures |
|---|---|---|
| Trust ratio (EXTRACTED / total edges) | 30 pts | How much came from deterministic AST vs LLM inference |
| Contradiction penalty | 25 pts | Number of CONTRADICTS edges detected |
| Temporal coverage | 20 pts | Fraction of nodes with git history |
| Causal coverage | 15 pts | Fraction of god nodes with causal edges |
| Ambiguity penalty | 10 pts | Number of flagged-ambiguous nodes |

```
Health 81/100 (B) — main drag: 3 contradictions
Health 94/100 (A)
Health 47/100 (D) — main drags: low trust ratio (31% EXTRACTED), 11 contradictions
```

---

## Directed graph support

graphify supports `--directed` mode. graphify-plus respects it throughout:

- `budget.py` uses `successors/predecessors` BFS for directed traversal
- `contradict.py` maintains separate `(a→b)` and `(b→a)` edge pairs
- `causal.py` traverses upstream via `predecessors` for context gathering
- `graph_enhanced.json` records the `_directed` flag so it round-trips cleanly

---

## Project structure

```
graphify_plus/
├── __init__.py          # full public surface
├── __main__.py          # CLI dispatcher (enhance / audit / diff)
│
├── budget.py            # token-budgeted retrieval
├── causal.py            # causal chain extraction
├── contradict.py        # contradiction detection
├── pipeline.py          # orchestrator
├── reconcile.py         # cross-repo reconciliation
├── report_json.py       # health score + report.json
├── temporal.py          # git-aware temporal edges
├── writeback.py         # MCP write tools + corrections ledger
│
├── audit/
│   ├── probe.py         # 7 adversarial probes (production)
│   └── cli.py           # audit CLI
│
├── review/
│   ├── graph_diff.py    # graph diff engine (production)
│   └── cli.py           # diff CLI
│
├── counterfactual/
│   └── simulate.py      # impact simulation (production)
│
├── verification/
│   └── voting.py        # multi-agent vote-extract (production)
│
├── selfhealing/
│   └── proposals.py     # correction proposals (production)
│
├── multimodal/
│   └── retriever.py     # image retrieval (prototype)
│
├── live/
│   └── daemon.py        # file-watcher daemon (prototype)
│
├── federation/
│   └── coordinator.py   # query routing (prototype)
│
├── local/
│   └── extractor.py     # on-device extraction (stub)
│
└── personal/
    └── sources.py       # personal sources (stub)

tests/
├── test_enhancements.py      # 39 tests — v2 core modules
├── test_v3.py                # 41 tests — v3 feature coverage
├── test_audit_production.py  # 56 tests — audit edge cases
└── test_diff_production.py   # 36 tests — diff edge cases

.github/
└── workflows/
    └── graph-diff.yml        # PR comment + CI gate
```

---

## Tests

```bash
pytest tests/ -v        # 172 tests, all passing, 0.54s
pytest tests/ -q        # quiet mode
pytest tests/test_audit_production.py -v   # audit only
pytest tests/test_diff_production.py  -v   # diff only
```

Coverage includes: empty/single-node/disconnected/multigraph/directed graphs, unicode labels, numeric node IDs, malformed JSON inputs, CLI exit codes, threshold gating, JSON serialisability, and RNG determinism.

---

## Relationship to graphify

graphify-plus is a fan project. It is not affiliated with or endorsed by [safishamsi](https://github.com/safishamsi). It reads `graphifyy` (the official PyPI package)'s output format and is designed to be forward-compatible with graphify's `graph.json` schema.

If graphify ships any of these features natively, the corresponding graphify-plus module becomes redundant and can be dropped without affecting anything else.

---

## Licence

MIT
