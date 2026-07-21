# AGENTS.md

## Project

Python MCP server connecting Zotero to AI assistants. Single package at `src/zotero_mcp/`. Not a monorepo.

- **Entry point**: `zotero-mcp serve` → `cli.py` → `server.py` → `_app.py` (FastMCP instance)
- **Tool registration**: `tools/` directory, each module uses `@mcp.tool` decorator. Importing `tools/__init__.py` registers all tools via side-effect, then applies progressive disclosure via `tool_mode.apply_tool_mode()`.
- **Tool mode**: `ZOTERO_MCP_TOOL_MODE=meta|core|full` (default **meta**). Meta exposes `zotero_search_tools` / `zotero_get_tool_schema` / `zotero_call_tool` only; hidden tools stay registered and run via `local_provider`. See `tool_mode.py` + `tools/meta_tools.py`.
- **Version**: `src/zotero_mcp/_version.py` (currently 0.7.0)

## Commands

```bash
# Lint (the single most common check — run after every edit batch)
ruff check src/

# Format check
ruff format --check src/

# Fix lint + format
ruff check --fix src/ && ruff format src/

# Tests (single file)
pytest tests/test_foo.py -v

# Tests (full suite)
pytest -q

# Tests (parallel, mirrors CI)
pytest -q -n auto

# CI mirror (what GitHub Actions runs)
bash scripts/ci-local.sh

# Install for development
uv sync --extra all --extra dev
```

## CI pipeline

`.github/workflows/release.yml` runs on push/PR:
1. `ruff check .`
2. `ruff format --check .`
3. `pytest --ignore=tests/test_lifespan.py` across Python 3.10, 3.11, 3.12

**`test_lifespan.py` is always skipped in CI** — it requires a live Zotero instance.

## Linting rules

`pyproject.toml [tool.ruff]`:
- `target-version = "py310"` — write Python 3.10+ compatible code
- `line-length = 120`
- `select = ["E", "F", "W", "I", "UP"]` — errors, pyflakes, warnings, isort, pyupgrade
- `ignore = ["E501"]` — long lines allowed

Pre-commit also runs `pyupgrade --py310-plus` and `ruff-format`.

## Architecture

```
src/zotero_mcp/
├── _app.py          # FastMCP instance + server lifespan
├── server.py        # Re-exports everything for tests (server.X pattern)
├── client.py        # Zotero client wrapper, API lock, run_zotero_call()
├── tools/
│   ├── _helpers.py  # Shared utils (normalize, CrossRef map, PDF attach)
│   ├── retrieval.py # Read-only tools (get_item, get_collections, etc.)
│   ├── write.py     # Mutation tools (add, update, delete, batch ops)
│   ├── search.py    # Search tools (keyword, semantic, advanced)
│   ├── annotations.py # Annotation/note CRUD
│   ├── connectors.py   # ChatGPT/connector search
│   ├── read_pdf.py     # PDF page rendering
│   └── scite.py        # Optional Scite enrichment
├── local_db.py      # Local Zotero SQLite reader (FTS5)
├── semantic_search.py # ChromaDB semantic search
├── cache.py         # TTL caches (items, collections, tags, annotations)
├── rate_limiter.py  # Per-provider rate limiting
├── config.py        # get_config_path() — use this, not hardcoded paths
└── cli.py           # CLI commands (serve, setup, update-db, build-fts, etc.)
```

**Key pattern**: `server.py` re-exports all tool functions so tests can do `from zotero_mcp.server import func_name` and monkeypatch via `zotero_mcp.server`.

## Testing

- **Fixtures**: `conftest.py` provides `DummyContext` (no-op MCP context), `FakeZotero` (minimal pyzotero stub), `fake_zot` and `dummy_ctx` fixtures.
- **Autouse cleanup**: `_clear_runtime_caches` fixture clears search cache, client singleton, and all TTL caches between tests. Tests that monkeypatch must not rely on cached state.
- **`skip_on_ci` marker**: Tests using `tmp_path` that fail on GitHub Actions should use `@skip_on_ci`.
- **Timeout**: 30s default per test (`pytest-timeout`).
- **Mocking pattern**: Patch at `zotero_mcp.server.func_name` or `zotero_mcp.client.get_zotero_client`.

## Concurrency model

- **API lock**: `_zotero_api_lock` (RLock) serializes all Zotero API access. Decorate tool functions with `@with_zotero_api_lock`.
- **`run_zotero_call()`**: Wraps blocking `asyncio.to_thread()` with 30s timeout. Use this, not bare `asyncio.to_thread()`, for all Zotero API calls in tools.
- **Rate limiting**: `rate_limiter.py` provides per-provider limiters. Use `rate_limited_get()`/`rate_limited_post()` for external APIs.

## Caching

`cache.py` provides TTL caches: items (300s), children (300s), collections (600s), tags (600s), annotations (120s). Call `invalidate_all_caches()` after write operations.

## Optional dependencies

Groups: `semantic` (ChromaDB, embeddings), `pdf` (PyMuPDF, ebooklib), `scite`, `paddleocr`, `all`, `dev`.

## Gotchas

- **`_handle_write_response` is async** — missing `await` returns a coroutine (always truthy), silently ignoring failures. This is a recurring bug pattern.
- **`@with_zotero_api_lock` makes async functions appear sync** to FastMCP — don't use `task=True` on decorated functions.
- **Config paths**: Use `get_config_path()` from `config.py`. There are still hardcoded `Path.home() / ".config" / "zotero-mcp"` in some files.
- **Python 3.14**: Not supported yet — some ML deps lack wheels. Target 3.10-3.13.
- **Zotero local API** (`localhost:23119`): GET-only, no auth. Must be enabled in Zotero preferences.
- **Test imports**: Always import from `zotero_mcp.server`, not directly from tool modules, to match the monkeypatching surface.
