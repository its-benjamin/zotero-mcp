"""Every registered MCP tool must declare a `readOnlyHint` so AI clients
(Codex, Claude, ChatGPT) can distinguish read vs. write tools instead of
treating the whole server as read-only.
"""

from __future__ import annotations

import asyncio

import pytest

import zotero_mcp.tools  # noqa: F401  side-effect: registers all @mcp.tool
from zotero_mcp._app import mcp

EXPECTED_READ_ONLY: set[str] = {
    # retrieval
    "zotero_get_item_metadata",
    "zotero_get_item_fulltext",
    "zotero_extract_pdf_pages",
    "zotero_render_pdf_pages",
    "zotero_get_attachment_path",
    "zotero_get_collections",
    "zotero_get_collection_items",
    "zotero_get_item_children",
    "zotero_get_items_children",
    "zotero_get_tags",
    "zotero_list_libraries",
    "zotero_list_feeds",
    "zotero_get_feed_items",
    "zotero_get_recent",
    # search
    "zotero_search_items",
    "zotero_search_by_tag",
    "zotero_search_by_citation_key",
    "zotero_advanced_search",
    "zotero_semantic_search",
    "zotero_get_search_database_status",
    # annotations (read)
    "zotero_get_annotations",
    "zotero_get_notes",
    "zotero_search_notes",
    # write.py read helpers
    "zotero_search_collections",
    "zotero_get_pdf_outline",
    "zotero_find_duplicates",
    # connectors
    "search",
    "fetch",
    # scite
    "scite_enrich_item",
    "scite_enrich_search",
    "scite_check_retractions",
}

EXPECTED_WRITE: set[str] = {
    # annotations writes
    "zotero_create_note",
    "zotero_update_note",
    "zotero_delete_note",
    "zotero_create_annotation",
    "zotero_create_area_annotation",
    # collections / items writes
    "zotero_batch_update_tags",
    "zotero_create_collection",
    "zotero_manage_collections",
    "zotero_add_by_doi",
    "zotero_add_by_url",
    "zotero_add_by_isbn",
    "zotero_update_item",
    "zotero_delete_item",
    "zotero_merge_duplicates",
    "zotero_add_from_file",
    "zotero_move_item",
    "zotero_rename_tag",
    # search-side writes
    "zotero_update_search_database",
    # session / library state
    "zotero_switch_library",
}

EXPECTED_DESTRUCTIVE: set[str] = {
    "zotero_delete_note",
    "zotero_delete_item",
    "zotero_merge_duplicates",
    "zotero_rename_tag",
    "zotero_manage_collections",
}

@pytest.fixture(scope="module")
def registered_tools() -> dict[str, object]:
    tools = asyncio.run(mcp.list_tools())
    return {tool.name: tool for tool in tools}

def test_every_tool_declares_read_only_hint(registered_tools):
    missing = []
    for name, tool in registered_tools.items():
        annotations = getattr(tool, "annotations", None)
        if annotations is None or annotations.readOnlyHint is None:
            missing.append(name)
    assert missing == [], f"tools missing readOnlyHint: {sorted(missing)}"

def test_read_tools_marked_read_only(registered_tools):
    wrong = []
    for name in EXPECTED_READ_ONLY:
        tool = registered_tools.get(name)
        assert tool is not None, f"expected tool registered: {name}"
        if not getattr(tool.annotations, "readOnlyHint", None):
            wrong.append(name)
    assert wrong == [], f"read tools missing readOnlyHint=True: {sorted(wrong)}"

def test_write_tools_marked_not_read_only(registered_tools):
    wrong = []
    for name in EXPECTED_WRITE:
        tool = registered_tools.get(name)
        assert tool is not None, f"expected tool registered: {name}"
        if getattr(tool.annotations, "readOnlyHint", True) is not False:
            wrong.append(name)
    assert wrong == [], f"write tools must declare readOnlyHint=False: {sorted(wrong)}"

def test_destructive_tools_marked_destructive(registered_tools):
    wrong = []
    for name in EXPECTED_DESTRUCTIVE:
        tool = registered_tools.get(name)
        assert tool is not None
        if getattr(tool.annotations, "destructiveHint", None) is not True:
            wrong.append(name)
    assert wrong == [], f"destructive tools must declare destructiveHint=True: {sorted(wrong)}"
