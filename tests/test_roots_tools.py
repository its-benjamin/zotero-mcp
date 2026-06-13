"""Tests for MCP roots helpers."""

import pytest

from zotero_mcp.tools.retrieval import list_client_roots


class RootsContext:
    async def info(self, *_args, **_kwargs):
        return None

    async def warning(self, *_args, **_kwargs):
        return None

    async def error(self, *_args, **_kwargs):
        return None

    async def list_roots(self):
        return [
            type("Root", (), {"uri": "file:///repo", "name": "repo"})(),
            {"uri": "file:///notes", "name": "notes"},
        ]

@pytest.mark.asyncio
async def test_list_client_roots_formats_roots():
    result = await list_client_roots(ctx=RootsContext())

    assert "file:///repo" in result
    assert "repo" in result
    assert "file:///notes" in result

@pytest.mark.asyncio
async def test_list_client_roots_handles_unsupported_clients():
    class NoRootsContext(RootsContext):
        async def list_roots(self):
            raise RuntimeError("roots unsupported")

    result = await list_client_roots(ctx=NoRootsContext())

    assert "Roots are not available" in result
