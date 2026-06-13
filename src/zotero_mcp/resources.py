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
async def library_info() -> dict:
    """Basic library information and item count."""
    try:
        zot = await _client.run_zotero_call(_client.get_zotero_client, operation="get_zotero_client")
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
async def collections_list() -> list[dict]:
    """All collections in the library with keys and names."""
    try:
        zot = await _client.run_zotero_call(_client.get_zotero_client, operation="get_zotero_client")
        collections = await _client.run_zotero_call(zot.collections, operation="zot.collections()")
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
async def tags_list() -> list[str]:
    """All tags used in the library."""
    try:
        zot = await _client.run_zotero_call(_client.get_zotero_client, operation="get_zotero_client")
        tags = await _client.run_zotero_call(lambda: zot.everything(zot.tags()), operation="zot.tags()")
        return sorted({t["tag"] for t in tags})
    except Exception as e:
        raise ResourceError(f"Could not fetch tags: {e}") from e


@mcp.resource("zotero://recent")
@with_zotero_api_lock
async def recent_items() -> list[dict]:
    """10 most recently added items (key, title, type, date)."""
    try:
        zot = await _client.run_zotero_call(_client.get_zotero_client, operation="get_zotero_client")

        def _fetch_items():
            zot.add_parameters(limit=10, sort="dateAdded", direction="desc", itemType="-attachment")
            return zot.items()

        items = await _client.run_zotero_call(_fetch_items, operation="zot.items(recent resource)")
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

# ---------------------------------------------------------------------------
# Resource templates (parameterized URIs)
# ---------------------------------------------------------------------------

@mcp.resource(
    "zotero://item/{item_key}",
    name="zotero_item",
    description="Single Zotero item metadata as JSON. Item key is 8 alphanumeric characters.",
    mime_type="application/json",
)
@with_zotero_api_lock
async def item_resource(item_key: str) -> dict:
    """Return JSON metadata for a single Zotero item by key.

    Browse-friendly alternative to ``zotero_get_item_metadata``: clients
    that resolved ``zotero://collections`` or ``zotero://recent`` can fan
    out into individual items by URI without spending a tool call.
    """
    try:
        zot = await _client.run_zotero_call(_client.get_zotero_client, operation="get_zotero_client")
        item = await _client.run_zotero_call(zot.item, item_key, operation=f"zot.item({item_key})")
        return item
    except Exception as e:
        raise ResourceError(f"Could not fetch item {item_key}: {e}") from e


@mcp.resource(
    "zotero://item/{item_key}/bibtex",
    name="zotero_item_bibtex",
    description="BibTeX citation for a Zotero item. Item key is 8 alphanumeric characters.",
    mime_type="text/plain",
)
@with_zotero_api_lock
async def item_bibtex_resource(item_key: str) -> str:
    """Return BibTeX citation for a single Zotero item by key."""
    try:
        zot = await _client.run_zotero_call(_client.get_zotero_client, operation="get_zotero_client")
        item = await _client.run_zotero_call(zot.item, item_key, operation=f"zot.item({item_key})")
        if not item:
            raise ResourceError(f"Item not found: {item_key}")
        return _client.generate_bibtex(item)
    except ResourceError:
        raise
    except Exception as e:
        raise ResourceError(f"Could not generate BibTeX for {item_key}: {e}") from e


@mcp.resource(
    "zotero://item/{item_key}/children",
    name="zotero_item_children",
    description="Children (notes, attachments, annotations) of a Zotero item.",
    mime_type="application/json",
)
@with_zotero_api_lock
async def item_children_resource(item_key: str) -> list[dict]:
    """Return notes/attachments/annotations attached to ``item_key``."""
    try:
        zot = await _client.run_zotero_call(_client.get_zotero_client, operation="get_zotero_client")
        children = await _client.run_zotero_call(zot.children, item_key, operation=f"zot.children({item_key})")
        return [
            {
                "key": c["key"],
                "type": c["data"].get("itemType"),
                "title": c["data"].get("title") or c["data"].get("filename") or "",
                "note": c["data"].get("note", "") if c["data"].get("itemType") == "note" else None,
            }
            for c in children
        ]
    except Exception as e:
        raise ResourceError(f"Could not fetch children for {item_key}: {e}") from e

@mcp.resource(
    "zotero://collection/{collection_key}/items",
    name="zotero_collection_items",
    description="Top-level items in a Zotero collection. Collection key is 8 alphanumeric characters.",
    mime_type="application/json",
)
@with_zotero_api_lock
async def collection_items_resource(collection_key: str) -> list[dict]:
    """Return top-level items contained in a collection."""
    try:
        zot = await _client.run_zotero_call(_client.get_zotero_client, operation="get_zotero_client")

        def _fetch():
            zot.add_parameters(limit=200, itemType="-attachment")
            return zot.collection_items_top(collection_key)

        items = await _client.run_zotero_call(_fetch, operation=f"zot.collection_items_top({collection_key})")
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
        raise ResourceError(f"Could not fetch items for collection {collection_key}: {e}") from e

@mcp.resource(
    "zotero://tag/{tag_name}/items",
    name="zotero_tag_items",
    description="Top-level items tagged with the given tag (URL-decoded).",
    mime_type="application/json",
)
@with_zotero_api_lock
async def tag_items_resource(tag_name: str) -> list[dict]:
    """Return top-level items tagged with ``tag_name``."""
    try:
        zot = await _client.run_zotero_call(_client.get_zotero_client, operation="get_zotero_client")

        def _fetch():
            zot.add_parameters(limit=200, tag=tag_name, itemType="-attachment")
            return zot.top()

        items = await _client.run_zotero_call(_fetch, operation=f"zot.top(tag={tag_name})")
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
        raise ResourceError(f"Could not fetch items for tag {tag_name}: {e}") from e


@mcp.resource("zotero://library/stats")
@with_zotero_api_lock
async def library_stats() -> dict:
    """Library-wide statistics: item counts by type, tag count, collection count."""
    try:
        zot = await _client.run_zotero_call(_client.get_zotero_client, operation="get_zotero_client")

        # Get item counts by type using paginated fetch (limited to first 500 for performance)
        def _fetch_sample():
            zot.add_parameters(itemType="-attachment -note -annotation", limit=500, sort="dateAdded", direction="desc")
            return zot.items()

        items = await _client.run_zotero_call(_fetch_sample, operation="zot.items(stats_sample)")

        # Compute stats from sample
        from collections import Counter

        type_counter: Counter[str] = Counter()
        for item in (items or []):
            type_counter[item.get("data", {}).get("itemType", "unknown")] += 1

        # Get collection and tag counts
        collections = await _client.run_zotero_call(zot.collections, operation="zot.collections(stats)")
        collection_count = len(collections) if collections else 0

        tags = await _client.run_zotero_call(zot.tags, operation="zot.tags(stats)")
        tag_count = len(tags) if tags else 0

        return {
            "total_items_sampled": sum(type_counter.values()),
            "item_types_sample": dict(type_counter.most_common()),
            "collections": collection_count,
            "tags": tag_count,
            "note": "Item type counts are based on the 500 most recently added items.",
        }
    except Exception as e:
        raise ResourceError(f"Could not fetch library stats: {e}") from e


@mcp.resource("zotero://recent/annotations")
@with_zotero_api_lock
async def recent_annotations() -> list[dict]:
    """10 most recently added annotations across the library."""
    try:
        zot = await _client.run_zotero_call(_client.get_zotero_client, operation="get_zotero_client")

        def _fetch():
            zot.add_parameters(limit=10, sort="dateAdded", direction="desc", itemType="annotation")
            return zot.items()

        items = await _client.run_zotero_call(_fetch, operation="zot.items(recent_annotations)")
        return [
            {
                "key": item["key"],
                "type": item["data"].get("annotationType", ""),
                "text": item["data"].get("text", "")[:200],
                "comment": item["data"].get("comment", "")[:200],
                "color": item["data"].get("color", ""),
                "parentItem": item["data"].get("parentItem", ""),
            }
            for item in items
        ]
    except Exception as e:
        raise ResourceError(f"Could not fetch recent annotations: {e}") from e


@mcp.resource("zotero://recent/modified")
@with_zotero_api_lock
async def recently_modified() -> list[dict]:
    """10 most recently modified items in the library."""
    try:
        zot = await _client.run_zotero_call(_client.get_zotero_client, operation="get_zotero_client")

        def _fetch():
            zot.add_parameters(limit=10, sort="dateModified", direction="desc", itemType="-attachment -note -annotation")
            return zot.items()

        items = await _client.run_zotero_call(_fetch, operation="zot.items(recently_modified)")
        return [
            {
                "key": item["key"],
                "title": item["data"].get("title", "Untitled"),
                "type": item["data"].get("itemType"),
                "date": item["data"].get("date", ""),
                "dateModified": item["data"].get("dateModified", ""),
            }
            for item in items
        ]
    except Exception as e:
        raise ResourceError(f"Could not fetch recently modified items: {e}") from e


@mcp.resource("zotero://saved-searches")
@with_zotero_api_lock
async def saved_searches_resource() -> list[dict]:
    """All saved searches in the library with their conditions."""
    try:
        from zotero_mcp.local_db import get_local_zotero_reader

        reader = get_local_zotero_reader()
        if not reader:
            return [{"error": "Saved searches are only available in local mode (ZOTERO_LOCAL=true)."}]
        try:
            searches = reader.get_saved_searches()
            return [
                {
                    "key": s["key"],
                    "name": s["name"],
                    "conditions": s["conditions"],
                }
                for s in searches
            ]
        finally:
            reader.close()
    except Exception as e:
        raise ResourceError(f"Could not fetch saved searches: {e}") from e
