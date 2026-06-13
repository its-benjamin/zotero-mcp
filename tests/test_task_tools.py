"""Tests for task-aware long-running tools."""

import asyncio

from zotero_mcp.server import mcp


def test_update_search_database_is_task_enabled():
    tools = asyncio.run(mcp.list_tools())
    tool = next(t for t in tools if t.name == "zotero_update_search_database")

    assert tool.task_config.mode != "forbidden"
