# Installing graphify-plus

graphify-plus is a Python package. Pick whichever installer best fits
your environment.

## One-line install (recommended)

### `uv` (fastest)

```bash
uv tool install graphify-plus
```

`uv tool install` puts the binary on your PATH in an isolated env. Update later with `uv tool update graphify-plus`.

### `pipx` (isolated venv per tool)

```bash
pipx install graphify-plus
```

`pipx upgrade graphify-plus` later. This is what most teams settle on.

### `pip` (global / venv)

```bash
pip install graphify-plus
```

If you want the optional extras (embeddings, MCP server, REST):

```bash
pip install 'graphify-plus[embeddings,mcp,serve]'
```

### Homebrew (macOS / Linuxbrew) — coming soon

Tap not yet published. Track [issue #N](https://github.com/ahmadzubair9655/graphify-plus/issues) for progress.

## After install

```bash
gp --help                 # see the full subcommand surface
gp daemon quickstart      # init + warm + install skill + recipes
gp daemon tutorial        # 10-question walkthrough
gp daemon diagnose        # is everything healthy?
```

If you're integrating with Claude Code:

```bash
gp daemon install         # drops .claude/skills/graphify-plus/SKILL.md
                          # and .claude/hooks/pre_grep_hook.py
```

## Updating

```bash
uv tool update graphify-plus       # if installed via uv
pipx upgrade graphify-plus         # if via pipx
pip install -U graphify-plus       # if via pip
```

`gp daemon version-check` shows whether a newer release exists. The check honours `GP_NO_UPDATE_CHECK=1` for air-gapped setups.

## Air-gapped / offline

* Set `GP_NO_UPDATE_CHECK=1` to skip the daily PyPI ping.
* Configure `.graphify_plus/llm-routing.yaml` with `llm_free: true` to disable all LLM extraction.
* See `gp daemon privacy --dry-run-network` for every endpoint the current configuration would touch.

## Optional extras

| Extra | Adds |
|---|---|
| `embeddings` | `sentence-transformers` for embedding rerank (Layer 18) |
| `mcp` | MCP SDK; required for `gp mcp` |
| `serve` | `uvicorn` + `fastapi`; required for `gp serve` REST server |
| `full` | every extra |

## Uninstalling

```bash
uv tool uninstall graphify-plus
# or pipx uninstall, or pip uninstall
```

`graphify-plus` is local-only — uninstalling removes the binary. Per-repo `.graphify_plus/` directories are not touched (delete those manually if you want).
