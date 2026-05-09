# graphify-plus reference manual

The "how does this thing actually work" guide. Reading it once should be enough to extend graphify-plus deeply.

## 1. Mental model

graphify-plus is a **layer over the symbol graph**, not a replacement for grep / your IDE / your LSP. It answers structural questions in milliseconds with file:line grounding so its output substitutes for grep output in the rest of Claude's pipeline.

The shape is:

```
Source files → ingest (tree-sitter adapters) → symbols + edges
                                              ↓
                                          SQLite cache
                                              ↓
                                      InMemoryGraph (snapshot)
                                              ↓
                          ┌───────────────────┼────────────────────────┐
                          ↓                   ↓                        ↓
                   Daemon RPC          MCP server               CLI subcommands
                   (Unix socket)    (stdio for Claude)         (`gp daemon …`)
```

Every external surface — daemon, MCP, CLI, LSP — talks to the same `InMemoryGraph` shape.

## 2. The contract

Every tool response carries a uniform envelope:

```json
{
  "ok": true,
  "freshness": {"trust": "FRESH", "freshness_token": "abc123def456",
                "files_changed_since": 0, "stale_paths": [], "hint": ""},
  "receipt": {"op": "who_calls", "elapsed_ms": 0.5, "tokens": 380,
              "grep_equivalent": "~3 grep calls + 1 file read"},
  "results": [
    {"node_id": "abcd1234", "label": "auth.AuthService.login",
     "source_file": "auth.py", "line_number": 7,
     "snippet": "def login(self, user, password)",
     "confidence": 1.0, "kind": "method"}
  ],
  "more_available": 0,
  "extra": {…}
}
```

Three things are non-negotiable:

1. **file:line on every row** — Claude pipes straight into Read/Edit.
2. **Token-budgeted by default** — every handler clips to ~1500 tokens.
3. **Freshness envelope** — Claude reads `trust` and decides whether to trust the answer or hybrid-grep.

## 3. The data model

### Symbol

```python
class Symbol(TypedDict, total=False):
    id: str               # 16-hex sha256(path::qualified_name)[:16]
    kind: SymbolKind      # function | method | class | interface | type | const | module | export | endpoint
    name: str
    qualified_name: str   # 'auth.AuthService.login'
    path: str             # repo-relative POSIX
    span: tuple[int, int] # (start_line, end_line) — 1-based
    signature: str
    exported: bool
    docstring: str | None
    parent_id: str | None
    language: str
```

### Edge

```python
class Edge(TypedDict, total=False):
    src: str              # symbol_id (or unresolved string for placeholders)
    dst: str
    kind: EdgeKind        # calls | imports | extends | implements | references | exports | contains | jsx_render
    resolved: bool
    span: tuple[int, int] | None
    confidence: float
```

Unresolved-call placeholders (`self.login`, `s.login`) are added to the graph as `kind=external` nodes; the daemon's `placeholders_by_short_name` index maps them back to real symbols at query time.

### InMemoryGraph

The hot-path snapshot. Pre-computed on every daemon refresh:

| Index | Type | Purpose |
|---|---|---|
| `by_id` | `dict[str, Symbol]` | constant-time symbol lookup |
| `by_path` | `dict[str, list[str]]` | `whats_in` |
| `by_name` | `dict[str, list[str]]` | `find_by_name` exact path |
| `by_qname` | `dict[str, str]` | `find_by_name` exact qname |
| `sorted_qnames` | `list[(qname, sid)]` | binary-search prefix match |
| `out_neighbours` | `dict[str, list[(dst, kind, span)]]` | callees |
| `in_neighbours` | `dict[str, list[(src, kind, span)]]` | callers |
| `inverted_text` | `dict[str, set[str]]` | `find_by_concept` cheap path |
| `pagerank_top` | `list[(sid, score)]` | `whats_central` |
| `placeholders_by_short_name` | `dict[str, list[str]]` | unresolved-call rollup |
| `coverage` | `dict[str, dict]` | Layer 7.1 overlay |
| `cve_rows` / `cves_by_symbol` | overlay | Layer 9.1 |
| `sast_by_symbol` | overlay | Layer 9.2 |
| `ingest_nodes` / `ingest_refs` | overlay | Layer 6 |
| `cross_edges` | overlay | Layer 8 |

## 4. The contracts that matter

### Latency contract (Layer 12.2)

| graph size | P50 | P99 | RAM |
|---|---|---|---|
| 5k nodes | 20ms | 100ms | 300MB |
| 30k nodes | 50ms | 250ms | 1GB |
| 100k nodes | 100ms | 500ms | 3GB |

Each tier has a `gp daemon perfcheck` regression test.

### Freshness contract (Layer 2)

* `FRESH` — graph reflects HEAD exactly.
* `LIVE_AHEAD` — uncommitted edits applied incrementally.
* `STALE_FILES` — N files modified since last rebuild.
* `STALE_REBUILD_NEEDED` — schema changed; full rebuild needed.

When trust degrades, Layer 2.3 hybrid mode augments responses with grep hits in stale files only.

### Privacy contract (Layer 12.1)

Default: nothing leaves the machine. Run `gp daemon privacy --dry-run-network` to enumerate every outbound endpoint.

### Schema versioning (Layer 21)

Every artifact carries `_schema_version`. The compatibility window is N-2 → N. Forward and backward migrations are registered via `register_migration(from_v, to_v)`.

## 5. Extending graphify-plus

### Adding a new daemon op

1. Write a handler in `daemon/handlers.py`:

   ```python
   def my_op(graph: InMemoryGraph, args: dict[str, Any]) -> dict[str, Any]:
       return {"results": [...], "more_available": 0}
   ```

2. Register in `HANDLERS = {…, "my_op": my_op}`.
3. Add an MCP wrapper in `interface/mcp_server.py` if Claude should call it.
4. Add a `gp daemon …` CLI command.
5. Add tests under `tests/daemon/`.

### Adding an overlay (coverage-style)

1. Add a SQLite table + parser + ingest function in a new module.
2. Wire `load_<overlay>` into `InMemoryGraph.from_store`.
3. Add a `_format_node` extension point so existing handlers surface the new field automatically.

### Adding a plugin (Layer 11.2)

```toml
# pyproject.toml
[project.entry-points."graphify_plus.plugins"]
my_plugin = "my_package.plugin:register"
```

```python
# my_package/plugin.py
from graphify_plus.daemon.plugins import PluginRegistry

def register(reg: PluginRegistry) -> None:
    reg.register_handler("my_op", my_handler)
    reg.register_ingestor("my_format", my_ingestor)
```

## 6. Failure modes

| Scenario | Behaviour |
|---|---|
| Daemon crashes | MCP / CLI fall back to in-process build (slower, still works) |
| Cache corrupt | Quarantined as `cache.db.corrupt.<ts>`; rebuild required |
| Watcher floods | Coalesces with 50ms window; surfaces `STALE_REBUILD_NEEDED` if backlog grows unbounded |
| LLM extractor down | AST extraction continues; LLM-flagged edges drop with reduced trust |
| Plugin raises in `register()` | Logged at warning; daemon keeps starting |
| Embeddings model missing | `find_by_concept` falls through to text search; no rerank |

## 7. Testing graphify-plus itself

Built-in benchmark harness:

```bash
gp daemon benchmark              # runs the built-in tiny corpus
gp daemon benchmark --corpus my-corpus.json
```

Performance regression check:

```bash
gp daemon perfcheck --samples 500
```

Operational health:

```bash
gp daemon diagnose
```

## 8. The honest limitations

graphify-plus is bad at:

* Exact-string searches across all files including comments / docs / vendored code → **grep wins**.
* Symbol resolution / jump-to-definition / hover-to-type → **your LSP wins**.
* Live runtime state and stack inspection → **debugger wins**.
* Editing code itself → graphify-plus *informs* the edit; you (or Claude) do it.
* Real-time keystroke-level feedback → watcher latency is real (~250ms debounce + parse).

## 9. Glossary

* **god node** — a high-PageRank symbol; touching it has wide blast radius.
* **causal chain** — sequence of related decisions (issue → PR → commit → symbol).
* **freshness token** — 12-hex hash that changes whenever the graph changes; cheap idempotency key.
* **placeholder** — unresolved edge target (`self.x`); rolled up to its real symbol via short-name match.
* **provenance** — (extractor, source_sha, prompt_hash, confidence) per node/edge so every claim is reproducible.
* **hybrid grep** — augmented response when the graph is stale: grep over the stale files only.

---

_If you find this manual unclear, that's a bug in the manual, not in your reading. Open an issue._
