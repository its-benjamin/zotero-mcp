"""Progressive tool disclosure (meta / core / full modes)."""

from __future__ import annotations

import json

import pytest

from zotero_mcp import tool_mode
from zotero_mcp._app import mcp
from zotero_mcp.tool_mode import (
    CORE_TOOL_NAMES,
    META_TOOL_NAMES,
    apply_tool_mode,
    call_internal_tool,
    catalog_tools,
    get_tool_mode,
    get_tool_schema,
    pack_for_tool,
)


@pytest.fixture(autouse=True)
def _restore_full_mode_after_test(monkeypatch):
    """Keep other tests unaffected: restore full tool visibility after each test.

    Most unit tests call Python functions directly (not tools/list), but
    restoring full mode avoids surprising shared FastMCP state.
    """
    yield
    monkeypatch.setenv("ZOTERO_MCP_TOOL_MODE", "full")
    apply_tool_mode(mcp)


@pytest.mark.asyncio
async def test_meta_mode_keeps_resources_and_prompts(monkeypatch):
    """Regression: enable(only=True) used to wipe resources/prompts."""
    monkeypatch.setenv("ZOTERO_MCP_TOOL_MODE", "meta")
    apply_tool_mode(mcp)

    resources = await mcp.list_resources()
    prompts = await mcp.list_prompts()
    assert len(resources) > 0
    assert len(prompts) > 0


def test_get_tool_mode_aliases(monkeypatch):
    monkeypatch.setenv("ZOTERO_MCP_TOOL_MODE", "progressive")
    assert get_tool_mode() == "meta"
    monkeypatch.setenv("ZOTERO_MCP_TOOL_MODE", "lite")
    assert get_tool_mode() == "core"
    monkeypatch.setenv("ZOTERO_MCP_TOOL_MODE", "classic")
    assert get_tool_mode() == "full"
    monkeypatch.setenv("ZOTERO_MCP_TOOL_MODE", "nope")
    assert get_tool_mode() == "meta"


def test_pack_for_tool():
    assert pack_for_tool("zotero_search_items") == "search"
    assert pack_for_tool("zotero_add_by_doi") == "write"
    assert pack_for_tool("scite_enrich_item") == "scite"
    assert pack_for_tool("totally_unknown_tool") == "other"


@pytest.mark.asyncio
async def test_meta_mode_lists_only_meta_tools(monkeypatch):
    monkeypatch.setenv("ZOTERO_MCP_TOOL_MODE", "meta")
    apply_tool_mode(mcp)

    listed = await mcp.list_tools()
    names = {t.name for t in listed}
    assert names == set(META_TOOL_NAMES)


@pytest.mark.asyncio
async def test_core_mode_lists_core_plus_meta(monkeypatch):
    monkeypatch.setenv("ZOTERO_MCP_TOOL_MODE", "core")
    apply_tool_mode(mcp)

    listed = await mcp.list_tools()
    names = {t.name for t in listed}
    assert META_TOOL_NAMES <= names
    assert CORE_TOOL_NAMES <= names
    # Full catalog is much larger
    all_tools = await mcp.local_provider.list_tools()
    assert len(names) < len(all_tools)


@pytest.mark.asyncio
async def test_full_mode_lists_many_tools(monkeypatch):
    monkeypatch.setenv("ZOTERO_MCP_TOOL_MODE", "full")
    apply_tool_mode(mcp)

    listed = await mcp.list_tools()
    names = {t.name for t in listed}
    assert "zotero_search_items" in names
    assert "zotero_add_by_doi" in names
    assert len(names) >= 80


@pytest.mark.asyncio
async def test_catalog_and_schema(monkeypatch):
    monkeypatch.setenv("ZOTERO_MCP_TOOL_MODE", "meta")
    apply_tool_mode(mcp)

    catalog = await catalog_tools(mcp, query="add doi", limit=10)
    assert catalog["returned"] >= 1
    names = [t["name"] for t in catalog["tools"]]
    assert "zotero_add_by_doi" in names

    schema = await get_tool_schema(mcp, "zotero_add_by_doi")
    assert schema["name"] == "zotero_add_by_doi"
    assert "inputSchema" in schema
    props = schema["inputSchema"].get("properties") or {}
    assert "doi" in props or any("doi" in k.lower() for k in props)


@pytest.mark.asyncio
async def test_call_hidden_tool(monkeypatch):
    """Hidden tools remain callable via local_provider (meta path)."""
    monkeypatch.setenv("ZOTERO_MCP_TOOL_MODE", "meta")
    apply_tool_mode(mcp)

    # Use a pure helper-style tool that does not need a live Zotero if we
    # mock… Prefer get_item_types which hits the API. Instead call a tool
    # that validates args and fails predictably without network if possible.
    # zotero_get_item_types needs client — mock at local_provider level by
    # calling a tool with missing required args and checking error path.
    result = await call_internal_tool(mcp, "zotero_add_by_doi", {})
    # Missing doi should produce an error string or raise-handled message
    assert result is not None
    text = result if isinstance(result, str) else json.dumps(result)
    assert "doi" in text.lower() or "error" in text.lower() or "required" in text.lower()


@pytest.mark.asyncio
async def test_call_unknown_tool_suggests(monkeypatch):
    monkeypatch.setenv("ZOTERO_MCP_TOOL_MODE", "meta")
    apply_tool_mode(mcp)

    result = await call_internal_tool(mcp, "zotero_add_by_d0i", {})
    assert isinstance(result, str)
    assert "unknown tool" in result.lower()
    assert "zotero_add_by_doi" in result


@pytest.mark.asyncio
async def test_refuse_meta_recursion(monkeypatch):
    monkeypatch.setenv("ZOTERO_MCP_TOOL_MODE", "meta")
    apply_tool_mode(mcp)

    result = await call_internal_tool(mcp, "zotero_call_tool", {"name": "x"})
    assert isinstance(result, str)
    assert "refusing" in result.lower()


@pytest.mark.asyncio
async def test_meta_tools_registered():
    # Regardless of mode after fixture restore, tools exist on local provider
    for name in META_TOOL_NAMES:
        tool = await mcp.local_provider.get_tool(name)
        assert tool is not None, name


def test_score_empty_query_lists():
    # empty query should match everything with positive score
    assert tool_mode._score_match("", "zotero_search_items", "Search library", "search") > 0
