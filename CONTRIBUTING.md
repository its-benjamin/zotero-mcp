# Contributing

Thanks for improving Zotero MCP. This project is a Python MCP server that connects Zotero to AI assistants through local Zotero or the Zotero Web API.

## Development setup

Use Python 3.10-3.13. Python 3.14 is not supported yet because some optional ML dependencies do not publish wheels for it.

```bash
uv sync --extra all --extra dev
```

If you are not using `uv`, install the package in editable mode with the development dependencies from `pyproject.toml`.

## Local checks

Run lint before opening a PR:

```bash
ruff check .
```

Run tests with:

```bash
uv run pytest tests/
```

For changes that touch semantic search, PDF extraction, or optional integrations, also test the relevant optional dependency group locally.

## Zotero setup for manual testing

For local mode, start Zotero desktop and enable:

`Settings > Advanced > Allow other applications on this computer to communicate with Zotero`

Use `ZOTERO_LOCAL=true` for local reads. Use `ZOTERO_API_KEY`, `ZOTERO_LIBRARY_ID`, and `ZOTERO_LIBRARY_TYPE` when testing Web API writes or hybrid mode.

## Pull request guidelines

- Keep public MCP tool names and arguments backward compatible unless the PR is explicitly a breaking change.
- Add or update tests for behavior changes.
- Update `README.md`, `CHANGELOG.md`, or docs when user-facing behavior changes.
- Avoid committing generated caches, build artifacts, local databases, or personal Zotero data.
