"""Tests for MCP capability matrix tool."""

import pytest

from zotero_mcp.tools.connectors import mcp_capabilities


class DummyContext:
    async def info(self, *_args, **_kwargs):
        return None

@pytest.mark.asyncio
async def test_mcp_capabilities_lists_supported_and_missing_features():
    result = await mcp_capabilities(ctx=DummyContext())

    assert "Tools" in result
    assert "Resource Templates" in result
    assert "Sampling" in result
    assert "Roots" in result
    assert "Apps / Generative UI" in result
    assert "Not implemented" in result
