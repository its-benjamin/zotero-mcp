"""MCP Resources for Zotero library data.

Resources expose read-only data that models can browse without tool calls,
reducing token usage for common lookups.
"""

from fastmcp.exceptions import ResourceError

from zotero_mcp import client as _client
from zotero_mcp import utils as _utils
from zotero_mcp._app import mcp
from zotero_mcp.client import with_zotero_api_lock


@mcp.resource("zotero://library/info")
@with_zotero_api_lock
def library_info() -> dict:
    """Basic library information and item count."""
    try:
        zot = _client.get_zotero_client()
        override = _client.get_active_library()
        return {
            "library_type": override.get("library_type") or zot.library_type,
            "library_id": override.get("library_id") or zot.library_id,
            "local_mode": _utils.is_local_mode(),
        }
    except Exception as e:
        raise ResourceError(f"Could not fetch library info: {e}") from e


@mcp.resource("zotero://collections")
@with_zotero_api_lock
def collections_list() -> list[dict]:
    """All collections in the library with keys and names."""
    try:
        zot = _client.get_zotero_client()
        collections = zot.collections()
        return [
            {
                "key": c["key"],
                "name": c["data"].get("name", "Untitled"),
                "parent": c["data"].get("parentCollection") or None,
            }
            for c in collections
        ]
    except Exception as e:
        raise ResourceError(f"Could not fetch collections: {e}") from e


@mcp.resource("zotero://tags")
@with_zotero_api_lock
def tags_list() -> list[str]:
    """All tags used in the library."""
    try:
        zot = _client.get_zotero_client()
        tags = zot.everything(zot.tags())
        return sorted({t["tag"] for t in tags})
    except Exception as e:
        raise ResourceError(f"Could not fetch tags: {e}") from e


@mcp.resource("zotero://recent")
@with_zotero_api_lock
def recent_items() -> list[dict]:
    """10 most recently added items (key, title, type, date)."""
    try:
        zot = _client.get_zotero_client()
        zot.add_parameters(limit=10, sort="dateAdded", direction="desc", itemType="-attachment")
        items = zot.items()
        return [
            {
                "key": item["key"],
                "title": item["data"].get("title", "Untitled"),
                "type": item["data"].get("itemType"),
                "date": item["data"].get("date", ""),
            }
            for item in items
        ]
    except Exception as e:
        raise ResourceError(f"Could not fetch recent items: {e}") from e
