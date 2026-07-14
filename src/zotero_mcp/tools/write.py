"""Write / mutation tool functions for the Zotero MCP server."""

import asyncio
import json
import os
import posixpath
import re
import tempfile
import xml.etree.ElementTree as ET
from typing import Annotated, Any, Literal, cast

import requests
from mcp.types import ToolAnnotations
from pydantic import Field

from zotero_mcp import citation_import as _citation_import
from zotero_mcp import client as _client
from zotero_mcp import utils as _utils
from zotero_mcp._app import mcp
from zotero_mcp._context import Context
from zotero_mcp.client import with_zotero_api_lock
from zotero_mcp.rate_limiter import rate_limit, rate_limited_get
from zotero_mcp.tools import _helpers

# Accessed as _helpers.X so that monkeypatch/mock on the module attribute works.
CROSSREF_TYPE_MAP = _helpers.CROSSREF_TYPE_MAP


def _resolve_collections_arg(
    read_zot,
    collections,
    ctx,
    *,
    create_missing: bool = False,
    write_zot=None,
) -> list[str]:
    """Normalize the caller's ``collections`` argument and resolve every spec
    (key, name, or '/'-path) to a live collection key.

    Raises ValueError with a user-facing message on unknown or ambiguous
    specs — callers should fail the add *before* creating an item, so a typo
    can't produce an unfiled or invisibly-filed item.
    """
    specs = _helpers._normalize_str_list_input(collections, "collections")
    if not specs:
        return []
    return _helpers.resolve_collection_specs(
        read_zot, specs,
        create_missing=create_missing, write_zot=write_zot, ctx=ctx,
    )


def _collections_status(coll_keys: list[str], missing: list[str]) -> str:
    """Render the post-create collection-membership state for tool output."""
    if not coll_keys:
        return "My Library (no collection)"
    if missing:
        return (
            f"Filed in {sorted(set(coll_keys) - set(missing))}; "
            f"FAILED to file in {missing}"
        )
    return f"Filed in {coll_keys}"


_IF_EXISTS_VALUES = ("duplicate", "file", "skip")


def _converge_existing_item(write_zot, item, coll_keys, tags, ctx) -> dict:
    """Additively converge an existing item to the requested state.

    Adds the item to any of *coll_keys* it isn't in yet and attaches any of
    *tags* it doesn't carry yet. Never removes anything. Returns a summary
    dict: ``{"key", "title", "colls_added", "colls_already", "colls_failed",
    "tags_added", "tags_failed"}``.
    """
    item_key = item.get("key")
    data = item.get("data", {})
    title = data.get("title") or "(untitled)"

    current_colls = set(data.get("collections") or [])
    to_add = [k for k in coll_keys if k not in current_colls]
    already = [k for k in coll_keys if k in current_colls]

    tag_list = _helpers._normalize_str_list_input(tags, "tags")
    current_tags = {t.get("tag") for t in data.get("tags") or []}
    tags_to_add = [t for t in tag_list if t not in current_tags]

    tags_failed = False
    if tags_to_add:
        # Update tags first, on our fetched copy (current version); the
        # collection backstop below re-fetches, so it sees the new version.
        item["data"]["tags"] = (data.get("tags") or []) + [
            {"tag": t} for t in tags_to_add
        ]
        try:
            resp = write_zot.update_item(item)
            tags_failed = not _helpers._handle_write_response(resp, ctx)
        except Exception as e:
            tags_failed = True
            if ctx is not None:
                ctx.warning(f"Could not add tags to {item_key}: {e}")

    colls_failed = _helpers.ensure_collection_membership(
        write_zot, item_key, to_add, ctx=ctx
    )
    colls_added = [k for k in to_add if k not in colls_failed]

    return {
        "key": item_key,
        "title": title,
        "colls_added": colls_added,
        "colls_already": already,
        "colls_failed": colls_failed,
        "tags_added": [] if tags_failed else tags_to_add,
        "tags_failed": tags_failed,
    }


def _handle_existing_item(write_zot, existing, coll_keys, tags, if_exists,
                          matched_by, ctx) -> str:
    """Render the if_exists='file'/'skip' outcome for a single-item add tool.

    The report keeps the ``Item key: `KEY``` line that callers (and
    add_from_file's key extraction) rely on.
    """
    item = existing[0]
    item_key = item.get("key")
    title = item.get("data", {}).get("title") or "(untitled)"

    note = ""
    if len(existing) > 1:
        other = [i.get("key") for i in existing[1:]]
        note = (
            f"\n\nNote: {len(existing)} items match ({other} besides the one "
            "used); consider zotero_find_duplicates / zotero_merge_duplicates."
        )

    header = (
        f"Already in library: **{title}** (`{item_key}`, matched by {matched_by})\n\n"
        f"Item key: `{item_key}`\n"
    )

    if if_exists == "skip":
        return header + "No changes made (if_exists='skip')." + note

    summary = _converge_existing_item(write_zot, item, coll_keys, tags, ctx)

    lines = []
    coll_bits = []
    if summary["colls_added"]:
        coll_bits.append(f"added to {summary['colls_added']}")
    if summary["colls_failed"]:
        coll_bits.append(f"FAILED to add to {summary['colls_failed']}")
    if summary["colls_already"]:
        coll_bits.append(f"already in {summary['colls_already']}")
    if coll_keys:
        lines.append("Collections: " + "; ".join(coll_bits))
    if summary["tags_added"]:
        lines.append(f"Tags: added {summary['tags_added']}")
    elif summary["tags_failed"]:
        lines.append("Tags: FAILED to update")

    if not lines:
        lines.append("Nothing to change — item already in the requested state.")

    return header + "\n".join(lines) + note


@mcp.tool(
    name="zotero_batch_update_tags",
    description="Batch update tags across multiple items matching a search query or tag filter.",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False),
)
@with_zotero_api_lock
async def batch_update_tags(
    query: str = "",
    add_tags: list[str] | str | None = None,
    remove_tags: list[str] | str | None = None,
    tag: str | list[str] | None = None,
    limit: int | str = 50,
    *,
    ctx: Context,
) -> str:
    """
    Batch update tags across multiple items matching a search query or tag filter.

    Args:
        query: Search query to find items to update (text search)
        add_tags: List of tags to add to matched items (can be list or JSON string)
        remove_tags: List of tags to remove from matched items (can be list or JSON string)
        tag: Filter by existing tag name (e.g., "test" finds items with that exact tag).
             When provided alongside query, both filters are applied (AND).
        limit: Maximum number of items to process
        ctx: MCP context

    Returns:
        Summary of the batch update
    """
    try:
        if not query and not tag:
            return "Error: Must provide a search query and/or tag filter"

        if not add_tags and not remove_tags:
            return "Error: You must specify either tags to add or tags to remove"

        try:
            add_tags = _helpers._normalize_str_list_input(add_tags, "add_tags")
            remove_tags = _helpers._normalize_str_list_input(remove_tags, "remove_tags")
        except ValueError as validation_error:
            return f"Error: {validation_error}"

        if not add_tags and not remove_tags:
            return "Error: After parsing, no valid tags were provided to add or remove"

        await ctx.info(f"Batch updating tags for items matching '{query}'")
        zot = _client.get_zotero_client()

        # Use shared hybrid-mode helper for correct library override propagation
        try:
            _, write_zot = _helpers._get_write_client(ctx)
        except ValueError as e:
            return str(e)

        limit = _helpers._normalize_limit(limit, default=50)

        # Normalize tag parameter: accept string, list, or JSON string
        if tag is not None:
            if isinstance(tag, list):
                # Pyzotero expects comma-separated tags for AND filtering
                tag = " || ".join(str(t).strip() for t in tag if str(t).strip())
            elif isinstance(tag, str):
                tag = tag.strip()
                # Handle JSON string like '["test"]'
                try:
                    import json

                    parsed = json.loads(tag)
                    if isinstance(parsed, list):
                        tag = " || ".join(str(t).strip() for t in parsed if str(t).strip())
                    elif isinstance(parsed, str):
                        tag = parsed.strip()
                except (json.JSONDecodeError, ValueError):
                    pass  # Use as-is
            if not tag:
                tag = None

        # Search for items matching the query and/or tag filter
        params: dict[str, Any] = {"limit": limit}
        if query:
            params["q"] = query
        if tag:
            params["tag"] = tag

        def _fetch_items():
            zot.add_parameters(**params)
            return zot.items()

        items = await _client.run_zotero_call(_fetch_items, operation="zot.items(batch_update_tags)")

        if not items:
            filter_desc = []
            if query:
                filter_desc.append(f"query '{query}'")
            if tag:
                filter_desc.append(f"tag '{tag}'")
            return f"No items found matching {' and '.join(filter_desc) or 'the given filters'}"

        # Initialize counters
        updated_count = 0
        skipped_count = 0
        added_tag_counts = {tag: 0 for tag in (add_tags or [])}
        removed_tag_counts = {tag: 0 for tag in (remove_tags or [])}

        # Process each item
        for item in items:
            # Skip attachments if they were included in the results
            if item["data"].get("itemType") == "attachment":
                skipped_count += 1
                continue

            # Get current tags
            current_tags = item["data"].get("tags", [])
            current_tag_values = {t["tag"] for t in current_tags}

            # Track if this item needs to be updated
            needs_update = False

            # Process tags to remove
            if remove_tags:
                new_tags = []
                for tag_obj in current_tags:
                    tag = tag_obj["tag"]
                    if tag in remove_tags:
                        removed_tag_counts[tag] += 1
                        needs_update = True
                    else:
                        new_tags.append(tag_obj)
                current_tags = new_tags
                # Refresh the set of current tag values after removal
                current_tag_values = {t["tag"] for t in current_tags}

            # Process tags to add
            if add_tags:
                for tag in add_tags:
                    if tag and tag not in current_tag_values:
                        current_tags.append({"tag": tag})
                        added_tag_counts[tag] += 1
                        needs_update = True

            # Update the item if needed
            if needs_update:
                try:
                    item_key = item.get("key", "unknown")

                    # If writing via web API, re-fetch the item from web to get
                    # the correct version number for the update
                    if write_zot is not zot:
                        try:
                            web_item = await _client.run_zotero_call(
                                write_zot.item, item_key, operation=f"write_zot.item({item_key})"
                            )
                            web_item["data"]["tags"] = current_tags
                            await ctx.info(f"Updating item {item_key} via web API with tags: {current_tags}")
                            result = await _client.run_zotero_call(
                                write_zot.update_item, web_item, operation=f"write_zot.update_item({item_key})"
                            )
                        except Exception as e:
                            await ctx.error(f"Failed to fetch/update item {item_key} via web API: {str(e)}")
                            skipped_count += 1
                            continue
                    else:
                        item["data"]["tags"] = current_tags
                        await ctx.info(f"Updating item {item_key} with tags: {current_tags}")
                        result = await _client.run_zotero_call(
                            write_zot.update_item, item, operation=f"write_zot.update_item({item_key})"
                        )

                    if await _helpers._handle_write_response(result, ctx):
                        updated_count += 1
                    else:
                        await ctx.error(f"Update may have failed for item {item_key}: {result}")
                        skipped_count += 1
                except Exception as e:
                    await ctx.error(f"Failed to update item {item.get('key', 'unknown')}: {str(e)}")
                    # Continue with other items instead of failing completely
                    skipped_count += 1
            else:
                skipped_count += 1

        if updated_count:
            await _helpers._notify_library_changed(ctx)

        # Format the response
        response = ["# Batch Tag Update Results", ""]
        response.append(f"Query: '{query}'")
        response.append(f"Items processed: {len(items)}")
        response.append(f"Items updated: {updated_count}")
        response.append(f"Items skipped: {skipped_count}")

        if add_tags:
            response.append("\n## Tags Added")
            for tag, count in added_tag_counts.items():
                response.append(f"- `{tag}`: {count} items")

        if remove_tags:
            response.append("\n## Tags Removed")
            for tag, count in removed_tag_counts.items():
                response.append(f"- `{tag}`: {count} items")

        return "\n".join(response)

    except Exception as e:
        error_msg = str(e)
        suggestion = ""
        if "timeout" in error_msg.lower():
            suggestion = " Try reducing the number of items or using a more specific query."
        elif "connection" in error_msg.lower():
            suggestion = " Check if Zotero is running and web API credentials are configured."
        elif "permission" in error_msg.lower() or "read-only" in error_msg.lower():
            suggestion = " You may need write access. Check ZOTERO_API_KEY and ZOTERO_LIBRARY_ID."
        await ctx.error(f"Error in batch tag update: {error_msg}")
        return f"Error in batch tag update: {error_msg}{suggestion}"


def _apply_extra_edits(
    extra: str,
    set_keys: dict[str, str],
    remove_keys: list[str],
    replace: bool,
) -> tuple[str, bool]:
    """Apply `Key: value` line edits to an Extra field value.

    Extra is treated as newline-separated lines; lines of the form
    "Key: value" are matched by the text before the first colon,
    case-insensitively. Free-form lines (no colon) are never touched.

    Returns:
        (new_extra, changed)
    """
    def line_key(line: str) -> str | None:
        head, sep, _ = line.partition(":")
        return head.strip().lower() if sep else None

    original = extra or ""

    if replace:
        new_extra = "\n".join(f"{k}: {v}" for k, v in set_keys.items())
        return new_extra, new_extra != original

    lines = original.splitlines()

    if remove_keys:
        remove = {k.strip().lower() for k in remove_keys if k.strip()}
        lines = [ln for ln in lines if line_key(ln) not in remove]

    for key, value in (set_keys or {}).items():
        target = key.strip().lower()
        new_line = f"{key}: {value}"
        out = []
        replaced = False
        for ln in lines:
            if line_key(ln) == target:
                # Replace the first matching line in place; drop duplicates.
                if not replaced:
                    out.append(new_line)
                    replaced = True
            else:
                out.append(ln)
        if not replaced:
            out.append(new_line)
        lines = out

    new_extra = "\n".join(lines)
    return new_extra, new_extra != original


@mcp.tool(
    name="zotero_batch_update_extra",
    description=(
        "Upsert and/or remove `Key: value` lines in the Extra field across "
        "multiple items in one call — the batch counterpart of "
        "zotero_update_item for Extra-field metadata (Better BibTeX "
        "citation keys, tex.* fields, CSL variables). "
        "item_keys: list of item keys to edit (or a JSON-encoded list "
        "string). set_keys: mapping of key→value lines to upsert (or a "
        "JSON object string); an existing line with the same key is "
        "replaced in place, otherwise the line is appended. "
        "remove_keys: list of key names whose lines are deleted. "
        "replace: when true, rebuild Extra from set_keys only, dropping "
        "every other line (incompatible with remove_keys). "
        "Keys are matched by their `key:` prefix, case-insensitively; "
        "free-form lines without a colon are preserved. Items needing no "
        "change, attachments/notes/annotations, and unknown keys are "
        "skipped (counted in the summary). "
        "Requires a writable library (web API key or hybrid mode) — fails "
        "in local-only mode. "
        "Example: zotero_batch_update_extra(item_keys=['ABCD1234', "
        "'EFGH5678'], set_keys={'tex.otscore': '2'}, "
        "remove_keys=['tex.draft'])."
    )
)
@with_zotero_api_lock
def batch_update_extra(
    item_keys: list[str] | str | None = None,
    set_keys: dict[str, str] | str | None = None,
    remove_keys: list[str] | str | None = None,
    replace: bool | str = False,
    *,
    ctx: Context
) -> str:
    """
    Batch update Extra-field key lines across multiple items.

    Args:
        item_keys: Item keys to edit (list or JSON-encoded list string)
        set_keys: Mapping of key→value lines to upsert (dict or JSON object string)
        remove_keys: Key names whose lines are deleted (list or JSON string)
        replace: When true, rebuild Extra from set_keys only
        ctx: MCP context

    Returns:
        Summary of the batch update
    """
    try:
        try:
            item_keys = _helpers._normalize_str_list_input(item_keys, "item_keys")
            remove_keys = _helpers._normalize_str_list_input(remove_keys, "remove_keys")
        except ValueError as validation_error:
            return f"Error: {validation_error}"

        if not item_keys:
            return "Error: Must provide item_keys to update"

        if isinstance(set_keys, str):
            try:
                set_keys = json.loads(set_keys)
            except json.JSONDecodeError:
                return "Error: set_keys must be a mapping of key→value strings"
        if set_keys is None:
            set_keys = {}
        if not isinstance(set_keys, dict):
            return "Error: set_keys must be a mapping of key→value strings"
        set_keys = {
            str(k).strip(): str(v).strip()
            for k, v in set_keys.items()
            if str(k).strip()
        }

        if isinstance(replace, str):
            replace = replace.strip().lower() in ("true", "1", "yes")
        replace = bool(replace)

        if not set_keys and not remove_keys and not replace:
            return "Error: Must specify set_keys, remove_keys, or replace"
        if replace and remove_keys:
            return "Error: replace=True is incompatible with remove_keys"

        ctx.info(f"Batch updating Extra field for {len(item_keys)} item(s)")
        zot = _client.get_zotero_client()

        try:
            _, write_zot = _helpers._get_write_client(ctx)
        except ValueError as e:
            return str(e)

        updated_count = 0
        skipped_count = 0

        for item_key in item_keys:
            try:
                item = zot.item(item_key)
            except Exception as e:
                ctx.error(f"Failed to fetch item {item_key}: {str(e)}")
                skipped_count += 1
                continue
            if not item:
                skipped_count += 1
                continue

            if item["data"].get("itemType") in ("attachment", "note", "annotation"):
                skipped_count += 1
                continue

            extra = item["data"].get("extra", "") or ""
            new_extra, changed = _apply_extra_edits(
                extra, set_keys, remove_keys, replace
            )
            if not changed:
                skipped_count += 1
                continue

            try:
                # If writing via web API, re-fetch the item from web to get
                # the correct version number for the update
                if write_zot is not zot:
                    web_item = write_zot.item(item_key)
                    web_item["data"]["extra"] = new_extra
                    result = write_zot.update_item(web_item)
                else:
                    item["data"]["extra"] = new_extra
                    result = write_zot.update_item(item)

                if _helpers._handle_write_response(result, ctx):
                    updated_count += 1
                else:
                    ctx.error(f"Update may have failed for item {item_key}: {result}")
                    skipped_count += 1
            except Exception as e:
                ctx.error(f"Failed to update item {item_key}: {str(e)}")
                skipped_count += 1

        response = ["# Batch Extra Update Results", ""]
        response.append(f"Items processed: {len(item_keys)}")
        response.append(f"Items updated: {updated_count}")
        response.append(f"Items skipped: {skipped_count}")

        if set_keys:
            response.append("\n## Keys Set")
            for key, value in set_keys.items():
                response.append(f"- `{key}: {value}`")
        if remove_keys:
            response.append("\n## Keys Removed")
            for key in remove_keys:
                response.append(f"- `{key}`")
        if replace:
            response.append("\nExtra field fully replaced from set_keys.")

        return "\n".join(response)

    except Exception as e:
        ctx.error(f"Error in batch extra update: {str(e)}")
        return f"Error in batch extra update: {str(e)}"


@mcp.tool(
    name="zotero_create_collection",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False),
    description=(
        "Create a new collection (project/folder) in your Zotero library. "
        "To create a subcollection, pass parent_collection (not parent_key) as either "
        "a collection key (8-character string like 'KMMQDFQ4') or a collection name. "
        "Use zotero_search_collections to find collection keys."
    ),
)
@with_zotero_api_lock
async def create_collection(name: str, parent_collection: str | None = None, *, ctx: Context) -> str:
    try:
        read_zot, write_zot = _helpers._get_write_client(ctx)
    except ValueError as e:
        return str(e)

    try:
        await ctx.info(f"Creating collection '{name}'")

        # Resolve parent_collection name if it doesn't look like a key
        parent_key = parent_collection
        if parent_collection and not re.match(r"^[A-Z0-9]{8}$", parent_collection):
            try:
                keys = await _helpers._resolve_collection_names(read_zot, [parent_collection], ctx=ctx)
                parent_key = keys[0] if keys else None
            except ValueError as e:
                return f"Error resolving parent collection: {e}"

        coll_data = {"name": name}
        if parent_key:
            coll_data["parentCollection"] = parent_key
        else:
            coll_data["parentCollection"] = False  # type: ignore[assignment]

        result = await _client.run_zotero_call(
            write_zot.create_collections, [coll_data], operation="write_zot.create_collections"
        )

        if isinstance(result, dict) and result.get("success"):
            coll_key = next(iter(result["success"].values()))
            parent_info = f" under parent '{parent_collection}'" if parent_collection else ""
            await _helpers._notify_library_changed(ctx)
            return f'Successfully created collection "{name}"{parent_info}\n\nCollection key: `{coll_key}`'
        return f"Failed to create collection: {result}"

    except Exception as e:
        error_msg = str(e)
        suggestion = ""
        if "permission" in error_msg.lower() or "read-only" in error_msg.lower():
            suggestion = " You may need write access. Check ZOTERO_API_KEY and ZOTERO_LIBRARY_ID."
        elif "connection" in error_msg.lower():
            suggestion = " Check if Zotero is running and web API credentials are configured."
        await ctx.error(f"Error creating collection: {error_msg}")
        return f"Error creating collection: {error_msg}{suggestion}"


@mcp.tool(
    name="zotero_search_collections",
    description="Search for collections by name to find their keys.",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
)
@with_zotero_api_lock
async def search_collections(query: str, *, ctx: Context) -> str:
    try:
        zot = await _client.run_zotero_call(_client.get_zotero_client, operation="get_zotero_client")
        await ctx.info(f"Searching collections for '{query}'")

        # Use cache if available
        from zotero_mcp.cache import get_collections_cache

        cache = get_collections_cache()
        collections = cache.get("all_collections")
        if collections is None:
            collections = _helpers._paginate(zot.collections)
            cache.set("all_collections", collections or [])
        if not collections:
            return "No collections found in your Zotero library."

        words = query.lower().split()
        matching = [c for c in collections if all(w in c.get("data", {}).get("name", "").lower() for w in words)]

        if not matching:
            return f"No collections found matching '{query}'"

        lines = [f"# Collections matching '{query}'", ""]
        for i, coll in enumerate(matching, 1):
            name = coll["data"].get("name", "Unnamed")
            key = coll["key"]
            parent_key = coll["data"].get("parentCollection")
            lines.append(f"## {i}. {name}")
            lines.append(f"**Key:** `{key}`")
            if parent_key:
                try:
                    parent = await _client.run_zotero_call(
                        zot.collection, parent_key, operation=f"zot.collection({parent_key})"
                    )
                    lines.append(f"**Parent:** {parent['data'].get('name', parent_key)}")
                except Exception:
                    lines.append(f"**Parent key:** {parent_key}")
            lines.append("")

        return "\n".join(lines)

    except Exception as e:
        await ctx.error(f"Error searching collections: {e}")
        return f"Error searching collections: {e}"


@mcp.tool(
    name="zotero_manage_collections",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False),
    description=(
        "Add or remove one or more items from collections. "
        'item_keys must be an ARRAY of item keys, e.g. ["KEY1", "KEY2"] — not a single string. '
        "add_to and remove_from accept arrays of collection keys, names, or "
        "'/'-separated paths (resolved and validated automatically; unknown, "
        "trashed, or ambiguous specs fail before anything is changed). "
        "Use zotero_search_items to find item keys and zotero_search_collections to find collection keys."
    ),
)
@with_zotero_api_lock
async def manage_collections(
    item_keys: list[str] | str,
    add_to: list[str] | str | None = None,
    remove_from: list[str] | str | None = None,
    *,
    ctx: Context,
) -> str:
    try:
        read_zot, write_zot = _helpers._get_write_client(ctx)
    except ValueError as e:
        return str(e)

    try:
        keys = _helpers._normalize_str_list_input(item_keys, "item_keys")
        add_specs = _helpers._normalize_str_list_input(add_to, "add_to")
        remove_specs = _helpers._normalize_str_list_input(remove_from, "remove_from")

        if not keys:
            return "Error: No item keys provided."
        if not add_specs and not remove_specs:
            return "Error: Must specify add_to and/or remove_from."

        # Resolve specs (keys, names, or '/'-paths) to live collection keys
        # before doing any work. Resolution also validates existence — Zotero
        # will happily accept add/remove against a trashed collection, leaving
        # items parented under an invisible bucket so the caller sees
        # "success" but nothing renders in the desktop client (#233).
        try:
            add_colls = _helpers.resolve_collection_specs(read_zot, add_specs, ctx=ctx)
            remove_colls = _helpers.resolve_collection_specs(read_zot, remove_specs, ctx=ctx)
        except ValueError as e:
            return f"Error: {e}"

        results = []

        # Cache item fetches to avoid repeated API calls for the same key
        item_cache = {}

        async def _get_item(key):
            if key not in item_cache:
                item_cache[key] = await _client.run_zotero_call(write_zot.item, key, operation=f"write_zot.item({key})")
            return item_cache[key]

        for coll_key in add_colls:
            for item_key in keys:
                item_dict = await _get_item(item_key)
                resp = await _client.run_zotero_call(
                    write_zot.addto_collection, coll_key, item_dict, operation=f"write_zot.addto_collection({coll_key})"
                )
                if await _helpers._handle_write_response(resp, ctx):
                    results.append(f"Added {item_key} to {coll_key}")
                    # Invalidate cache — version changed after addto_collection
                    item_cache.pop(item_key, None)
                else:
                    results.append(f"Failed to add {item_key} to {coll_key}")

        for coll_key in remove_colls:
            for item_key in keys:
                item_dict = await _get_item(item_key)
                resp = await _client.run_zotero_call(
                    write_zot.deletefrom_collection,
                    coll_key,
                    item_dict,
                    operation=f"write_zot.deletefrom_collection({coll_key})",
                )
                if await _helpers._handle_write_response(resp, ctx):
                    results.append(f"Removed {item_key} from {coll_key}")
                    item_cache.pop(item_key, None)
                else:
                    results.append(f"Failed to remove {item_key} from {coll_key}")

        return "\n".join(results)

    except ValueError as e:
        return f"Input error: {e}"
    except Exception as e:
        await ctx.error(f"Error managing collections: {e}")
        return f"Error managing collections: {e}"


@mcp.tool(
    name="zotero_add_by_doi",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True),
    description=(
        "Add an item to the active Zotero library by DOI, resolving rich "
        "metadata (title, creators, journal, year, abstract) from "
        "CrossRef. "
        "Use this as the FIRST choice when the user gives you a DOI — "
        "cleaner metadata than zotero_add_by_url. For arXiv IDs or raw "
        "URLs use zotero_add_by_url; for a local PDF use "
        "zotero_add_from_file. "
        "doi: the DOI string (with or without the '10.' prefix, with or "
        "without a leading 'https://doi.org/'). "
        "collections: optional list of collection keys, names, or "
        "'/'-separated paths (e.g. '_project/topic') — resolved and "
        "validated before the item is created; unknown or ambiguous "
        "specs fail the call with suggestions instead of producing an "
        "unfiled item. "
        "tags: optional list of tag strings to attach. "
        "if_exists: 'duplicate' (default) always creates a new item; "
        "'file' makes the call idempotent — when an item with this DOI "
        "already exists it is reused, filed into any missing collections "
        "and given any missing tags (nothing is ever removed); 'skip' "
        "leaves an existing match untouched. "
        "create_missing_collections: when True, collection specs that "
        "don't resolve are created (including path chains) instead of "
        "failing. "
        "attach_mode: 'auto' (default) downloads a PDF if CrossRef links "
        "one and storage is available; 'none' skips PDF download; "
        "'required' fails if no PDF can be attached. PDF uploads may fail "
        "on the Zotero cloud free-tier 300MB quota — metadata still lands "
        "even when the upload fails. "
        "Requires a writable library (web API key or hybrid mode); fails "
        "in local-only mode. Remember to run zotero_update_search_database "
        "afterwards to make the new item searchable semantically. "
        "Example: zotero_add_by_doi(doi='10.1145/3708319', "
        "collections=['9SU943GB'], tags=['MCP'])."
    ),
)
@with_zotero_api_lock
async def add_by_doi(
    doi: str,
    collections: list[str] | str | None = None,
    tags: list[str] | str | None = None,
    attach_mode: str = "auto",
    if_exists: Literal["duplicate", "file", "skip"] = "duplicate",
    create_missing_collections: bool = False,
    *,
    ctx: Context,
) -> str:
    try:
        read_zot, write_zot = _helpers._get_write_client(ctx)
    except ValueError as e:
        return str(e)

    try:
        if if_exists not in _IF_EXISTS_VALUES:
            return f"Error: if_exists must be one of {_IF_EXISTS_VALUES}."
        normalized = _helpers._normalize_doi(doi)
        if not normalized:
            return f"Error: '{doi}' does not appear to be a valid DOI."

        # Resolve collection specs (keys/names/paths) BEFORE any network or
        # write work — a bad spec must not produce an unfiled item.
        try:
            coll_keys = _resolve_collections_arg(
                read_zot,
                collections,
                ctx,
                create_missing=create_missing_collections,
                write_zot=write_zot,
            )
        except ValueError as e:
            return f"Error: {e}"

        if if_exists != "duplicate":
            existing = _helpers.find_existing_items(read_zot, doi=normalized, ctx=ctx)
            if existing:
                return _handle_existing_item(
                    write_zot,
                    existing,
                    coll_keys,
                    tags,
                    if_exists,
                    matched_by=f"DOI {normalized}",
                    ctx=ctx,
                )

        await ctx.info(f"Fetching metadata for DOI: {normalized}")

        resp = rate_limited_get(
            "crossref",
            f"https://api.crossref.org/works/{normalized}",
            headers={
                "User-Agent": "zotero-mcp/1.0 (https://github.com/ehawkin/zotero-mcp)",
                "Accept": "application/json",
            },
            timeout=15,
        )

        if resp.status_code == 404:
            return f"DOI not found on CrossRef: {normalized}"
        resp.raise_for_status()

        cr = resp.json().get("message", {})

        # Determine Zotero item type
        cr_type = cr.get("type", "")
        zot_type = CROSSREF_TYPE_MAP.get(cr_type, "document")

        # Get valid fields from item template
        template = await _client.run_zotero_call(
            write_zot.item_template, zot_type, operation=f"write_zot.item_template({zot_type})"
        )
        item_data = dict(template)

        # Map fields
        title_list = cr.get("title", [])
        if title_list and "title" in item_data:
            item_data["title"] = title_list[0]

        # Creators
        creators = []
        for author in cr.get("author", []):
            if "family" in author:
                creators.append(
                    {
                        "creatorType": "author",
                        "firstName": author.get("given", ""),
                        "lastName": author["family"],
                    }
                )
            elif "name" in author:
                creators.append(
                    {
                        "creatorType": "author",
                        "name": author["name"],
                    }
                )
        for editor in cr.get("editor", []):
            if "family" in editor:
                creators.append(
                    {
                        "creatorType": "editor",
                        "firstName": editor.get("given", ""),
                        "lastName": editor["family"],
                    }
                )
            elif "name" in editor:
                creators.append(
                    {
                        "creatorType": "editor",
                        "name": editor["name"],
                    }
                )
        if creators:
            item_data["creators"] = creators

        # Date
        date_parts = cr.get("published", cr.get("created", {})).get("date-parts", [[]])
        if date_parts and date_parts[0]:
            parts = date_parts[0]
            item_data["date"] = "-".join(str(p) for p in parts)

        # Simple string fields
        field_map = {
            "DOI": normalized,
            "url": cr.get("URL", ""),
            "volume": cr.get("volume", ""),
            "issue": cr.get("issue", ""),
            "pages": cr.get("page", ""),
            "publisher": cr.get("publisher", ""),
            "ISSN": (cr.get("ISSN") or [""])[0],
        }

        container = (cr.get("container-title") or [""])[0]
        if container:
            field_map["publicationTitle"] = container

        abstract = _utils.clean_html(cr.get("abstract", ""), collapse_whitespace=True)
        if abstract:
            field_map["abstractNote"] = abstract

        for field, value in field_map.items():
            if field in item_data and value:
                item_data[field] = value

        # Tags
        tag_list = _helpers._normalize_str_list_input(tags, "tags")
        if tag_list:
            item_data["tags"] = [{"tag": t} for t in tag_list]

        # Collections (resolved to live keys above, before the CrossRef fetch)
        if coll_keys:
            item_data["collections"] = coll_keys

        # Create item
        result = await _client.run_zotero_call(
            write_zot.create_items, [item_data], operation="write_zot.create_items(doi)"
        )

        if isinstance(result, dict) and result.get("success"):
            item_key = next(iter(result["success"].values()))
            title = item_data.get("title", normalized)

            # Defensive: pyzotero's atomic ``item["collections"]`` filing is
            # intermittent (#235) — reconcile membership before reporting success
            # so the caller sees the real routing state.
            missing = _helpers.ensure_collection_membership(write_zot, item_key, coll_keys, ctx=ctx)
            collections_status = _collections_status(coll_keys, missing)

            # Attempt open-access PDF attachment (pass CrossRef metadata for arXiv fallback)
            pdf_status = await _helpers._try_attach_oa_pdf(
                write_zot, item_key, normalized, ctx, crossref_metadata=cr, attach_mode=attach_mode
            )

            return (
                f"Successfully added: **{title}**\n\n"
                f"Item key: `{item_key}`\n"
                f"Type: {zot_type}\n"
                f"DOI: {normalized}\n"
                f"Collections: {collections_status}\n"
                f"PDF: {pdf_status}\n\n"
                "_Note: To include this item in semantic search, run "
                "zotero_update_search_database._"
            )
        return f"Failed to create item: {result}"

    except requests.Timeout:
        return "Error: CrossRef API request timed out. Please try again."
    except requests.RequestException as e:
        return f"Error fetching from CrossRef: {e}"
    except Exception as e:
        await ctx.error(f"Error adding by DOI: {e}")
        return f"Error adding by DOI: {e}"


@mcp.tool(
    name="zotero_add_by_url",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True),
    description=(
        "Add an item to the active Zotero library from a URL. Routes by "
        "URL shape: doi.org/... → CrossRef metadata (same path as "
        "zotero_add_by_doi); arxiv.org/abs/... → arXiv metadata + PDF; "
        "anything else → webpage item (title + URL, minimal metadata). "
        "Prefer zotero_add_by_doi when you have a clean DOI — it skips "
        "the routing and is more robust. For a local file use "
        "zotero_add_from_file. "
        "url: the URL to import. "
        "collections: optional list of collection keys, names, or "
        "'/'-separated paths — resolved and validated before the item is "
        "created; unknown or ambiguous specs fail the call. "
        "tags: optional list of tag strings to attach. "
        "if_exists: 'duplicate' (default) always creates; 'file' reuses "
        "an existing item matching the arXiv ID / DOI / URL, filing it "
        "into missing collections and adding missing tags; 'skip' leaves "
        "a match untouched. create_missing_collections: create unknown "
        "collection specs instead of failing. "
        "attach_mode: 'auto' (default) attaches a PDF if one is "
        "available; 'none' skips; 'required' fails if no PDF can be "
        "attached. PDF uploads may fail on the Zotero cloud free-tier "
        "300MB quota — metadata still lands even when the upload fails. "
        "WARNING: for bibliography use, a general web-page URL produces "
        "a 'webpage' itemType that often isn't acceptable as a citation; "
        "resolve to a DOI and use zotero_add_by_doi instead when "
        "possible. "
        "Requires a writable library (fails in local-only mode). Run "
        "zotero_update_search_database afterwards for semantic search. "
        "Example: zotero_add_by_url(url='https://arxiv.org/abs/2602.14878', "
        "collections=['9SU943GB'])."
    ),
)
@with_zotero_api_lock
async def add_by_url(
    url: str,
    collections: list[str] | str | None = None,
    tags: list[str] | str | None = None,
    attach_mode: str = "auto",
    if_exists: Literal["duplicate", "file", "skip"] = "duplicate",
    create_missing_collections: bool = False,
    *,
    ctx: Context,
) -> str:
    try:
        read_zot, write_zot = _helpers._get_write_client(ctx)
    except ValueError as e:
        return str(e)

    try:
        if if_exists not in _IF_EXISTS_VALUES:
            return f"Error: if_exists must be one of {_IF_EXISTS_VALUES}."
        url = (url or "").strip()
        if not url:
            return "Error: No URL provided."

        # DOI URL routing
        doi = _helpers._normalize_doi(url)
        if doi:
            return await add_by_doi(
                doi=url,
                collections=collections,
                tags=tags,
                attach_mode=attach_mode,
                if_exists=if_exists,
                create_missing_collections=create_missing_collections,
                ctx=ctx,
            )

        # arXiv URL routing
        arxiv_id = _helpers._normalize_arxiv_id(url)
        if arxiv_id:
            return await _add_by_arxiv(
                arxiv_id,
                collections,
                tags,
                write_zot,
                ctx,
                attach_mode=attach_mode,
                read_zot=read_zot,
                if_exists=if_exists,
                create_missing_collections=create_missing_collections,
            )

        # Generic webpage
        try:
            coll_keys = _resolve_collections_arg(
                read_zot,
                collections,
                ctx,
                create_missing=create_missing_collections,
                write_zot=write_zot,
            )
        except ValueError as e:
            return f"Error: {e}"

        if if_exists != "duplicate":
            existing = _helpers.find_existing_items(read_zot, url=url, ctx=ctx)
            if existing:
                return _handle_existing_item(
                    write_zot,
                    existing,
                    coll_keys,
                    tags,
                    if_exists,
                    matched_by=f"URL {url}",
                    ctx=ctx,
                )

        await ctx.info(f"Creating webpage item for: {url}")
        template = await _client.run_zotero_call(
            write_zot.item_template, "webpage", operation="write_zot.item_template(webpage)"
        )
        template["url"] = url
        template["title"] = url
        template["accessDate"] = ""

        tag_list = _helpers._normalize_str_list_input(tags, "tags")
        if tag_list:
            template["tags"] = [{"tag": t} for t in tag_list]
        if coll_keys:
            template["collections"] = coll_keys

        result = await _client.run_zotero_call(
            write_zot.create_items, [template], operation="write_zot.create_items(webpage)"
        )
        if isinstance(result, dict) and result.get("success"):
            item_key = next(iter(result["success"].values()))
            missing = _helpers.ensure_collection_membership(
                write_zot, item_key, coll_keys, ctx=ctx
            )
            return (
                f"Created webpage item for: {url}\n\nItem key: `{item_key}`\n"
                f"Collections: {_collections_status(coll_keys, missing)}\n\n"
                "_Note: To include this item in semantic search, run "
                "zotero_update_search_database._"
            )
        return f"Failed to create item: {result}"

    except Exception as e:
        await ctx.error(f"Error adding by URL: {e}")
        return f"Error adding by URL: {e}"


@with_zotero_api_lock
async def _add_by_arxiv(
    arxiv_id,
    collections,
    tags,
    write_zot,
    ctx,
    attach_mode="auto",
    read_zot=None,
    if_exists="duplicate",
    create_missing_collections=False,
):
    """Add an arXiv paper by ID. Internal helper for add_by_url.

    arXiv (export.arxiv.org) periodically sheds load — rate-limiting (429),
    returning 5xx, or timing out outright. This helper degrades gracefully:
    it retries transient failures with backoff, and if arXiv stays
    unreachable it falls back to CrossRef via the arXiv DOI
    (10.48550/arXiv.{id}), which serves the same metadata from independent
    infrastructure.
    """
    try:
        coll_keys = _resolve_collections_arg(
            read_zot or write_zot,
            collections,
            ctx,
            create_missing=create_missing_collections,
            write_zot=write_zot,
        )
    except ValueError as e:
        return f"Error: {e}"

    if if_exists != "duplicate":
        existing = _helpers.find_existing_items(read_zot or write_zot, arxiv_id=arxiv_id, ctx=ctx)
        if existing:
            return _handle_existing_item(
                write_zot,
                existing,
                coll_keys,
                tags,
                if_exists,
                matched_by=f"arXiv ID {arxiv_id}",
                ctx=ctx,
            )

    await ctx.info(f"Fetching arXiv metadata for: {arxiv_id}")

    resp = None
    last_error = None
    for attempt in range(3):
        resp = rate_limited_get(
            "arxiv",
            f"https://export.arxiv.org/api/query?id_list={arxiv_id}",
            timeout=30,
        )
        if resp.status_code == 429 or resp.status_code >= 500:
            last_error = f"HTTP {resp.status_code}"
            wait = 5 * (2**attempt)  # 5s, 10s, 20s
            await ctx.info(f"arXiv API {last_error} — waiting {wait}s before retry {attempt + 1}/3...")
            await asyncio.sleep(wait)
            continue
        break

    # arXiv exhausted its retries — fall back to CrossRef (independent infra).
    if resp is None or resp.status_code == 429 or resp.status_code >= 500:
        await ctx.info(
            f"arXiv unreachable after retries ({last_error}); falling back to CrossRef via the arXiv DOI."
        )
        arxiv_doi = f"10.48550/arXiv.{arxiv_id}"
        try:
            result = await add_by_doi(
                doi=arxiv_doi,
                collections=coll_keys,
                tags=tags,
                attach_mode=attach_mode,
                if_exists=if_exists,
                ctx=ctx,
            )
        except Exception as e:  # noqa: BLE001 — fallback must not raise
            result = None
            await ctx.info(f"CrossRef fallback errored: {e}")
        if result and not result.startswith(("DOI not found", "Error")):
            return result
        return (
            f"arXiv is currently unreachable (last error: {last_error}) and the "
            f"CrossRef fallback (DOI {arxiv_doi}) did not resolve it — this is "
            f"often a transient arXiv overload. Please retry shortly. "
            f"(arXiv ID: {arxiv_id})"
        )
    try:
        resp.raise_for_status()
    except requests.RequestException as e:
        return f"arXiv API error for {arxiv_id}: {e}"

    root = ET.fromstring(resp.text)
    ns = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}

    entries = root.findall("atom:entry", ns)
    if not entries:
        return f"No arXiv paper found for ID: {arxiv_id}"

    entry = entries[0]

    # Check for error response
    id_elem = entry.find("atom:id", ns)
    if id_elem is not None and "api/errors" in (id_elem.text or ""):
        return f"arXiv API error for ID: {arxiv_id}"

    title = (entry.findtext("atom:title", "", ns) or "").strip().replace("\n", " ")
    abstract = (entry.findtext("atom:summary", "", ns) or "").strip()
    published = (entry.findtext("atom:published", "", ns) or "")[:10]

    authors = []
    for author_elem in entry.findall("atom:author", ns):
        name = (author_elem.findtext("atom:name", "", ns) or "").strip()
        if name:
            parts = name.rsplit(" ", 1)
            if len(parts) == 2:
                authors.append(
                    {
                        "creatorType": "author",
                        "firstName": parts[0],
                        "lastName": parts[1],
                    }
                )
            else:
                authors.append({"creatorType": "author", "name": name})

    template = await _client.run_zotero_call(
        write_zot.item_template, "preprint", operation="write_zot.item_template(preprint)"
    )
    template["title"] = title
    if authors:
        template["creators"] = authors
    if abstract and "abstractNote" in template:
        template["abstractNote"] = abstract
    if published and "date" in template:
        template["date"] = published
    template["url"] = f"https://arxiv.org/abs/{arxiv_id}"
    if "extra" in template:
        template["extra"] = f"arXiv:{arxiv_id}"

    tag_list = _helpers._normalize_str_list_input(tags, "tags")
    if tag_list:
        template["tags"] = [{"tag": t} for t in tag_list]
    if coll_keys:
        template["collections"] = coll_keys

    result = await _client.run_zotero_call(
        write_zot.create_items, [template], operation="write_zot.create_items(arxiv)"
    )
    if isinstance(result, dict) and result.get("success"):
        item_key = next(iter(result["success"].values()))
        missing = _helpers.ensure_collection_membership(
            write_zot, item_key, coll_keys, ctx=ctx
        )

        # arXiv always has a free PDF — try to attach it
        pdf_url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"
        pdf_status = "no PDF attached"
        try:
            pdf_resp = rate_limited_get("arxiv", pdf_url, timeout=30, stream=True)
            pdf_resp.raise_for_status()
            with tempfile.TemporaryDirectory() as tmpdir:
                filename = f"arxiv_{arxiv_id.replace('/', '_')}.pdf"
                filepath = os.path.join(tmpdir, filename)
                with open(filepath, "wb") as f:
                    for chunk in pdf_resp.iter_content(chunk_size=8192):
                        f.write(chunk)
                await _client.run_zotero_call(
                    write_zot.attachment_both,
                    [(filename, filepath)],
                    parentid=item_key,
                    operation=f"write_zot.attachment_both({item_key})",
                )
            pdf_status = "PDF attached"
        except Exception as e:
            await ctx.info(f"arXiv PDF attachment failed (non-fatal): {e}")
            pdf_status = f"no PDF attached ({e})"

        return (
            f"Successfully added arXiv paper: **{title}**\n\n"
            f"Item key: `{item_key}`\n"
            f"arXiv ID: {arxiv_id}\n"
            f"Collections: {_collections_status(coll_keys, missing)}\n"
            f"PDF: {pdf_status}\n\n"
            "_Note: To include this item in semantic search, run "
            "zotero_update_search_database._"
        )
    return f"Failed to create arXiv item: {result}"


# ---------------------------------------------------------------------------
# ISBN lookup — Open Library (primary) + Google Books (fallback) (#226)
# ---------------------------------------------------------------------------


async def _lookup_isbn_openlibrary(isbn, ctx):
    """Look up book metadata by ISBN on Open Library. Returns a dict of
    normalized fields, or None on miss / error. Network errors are logged
    and surfaced as None so the caller can fall through to Google Books.
    """
    try:
        url = f"https://openlibrary.org/api/books?bibkeys=ISBN:{isbn}&format=json&jscmd=data"
        resp = await asyncio.to_thread(
            requests.get,
            url,
            headers={"User-Agent": "zotero-mcp/1.0 (https://github.com/its-benjamin/zotero-mcp)"},
            timeout=15,
        )
        if resp.status_code != 200:
            return None
        payload = resp.json() or {}
        record = payload.get(f"ISBN:{isbn}") or {}
        if not record:
            return None

        title = record.get("title", "")
        if record.get("subtitle"):
            title = f"{title}: {record['subtitle']}"

        creators = []
        for author in record.get("authors", []) or []:
            name = (author.get("name") or "").strip()
            if not name:
                continue
            parts = name.rsplit(" ", 1)
            if len(parts) == 2:
                creators.append(
                    {
                        "creatorType": "author",
                        "firstName": parts[0],
                        "lastName": parts[1],
                    }
                )
            else:
                creators.append({"creatorType": "author", "name": name})

        publisher = ""
        publishers = record.get("publishers") or []
        if publishers:
            publisher = (publishers[0].get("name") or "").strip()

        place = ""
        places = record.get("publish_places") or []
        if places:
            place = (places[0].get("name") or "").strip()

        return {
            "source": "Open Library",
            "title": title,
            "creators": creators,
            "date": (record.get("publish_date") or "").strip(),
            "publisher": publisher,
            "place": place,
            "num_pages": str(record.get("number_of_pages", "") or "").strip(),
            "url": (record.get("url") or "").strip(),
        }
    except requests.RequestException as e:
        await ctx.info(f"Open Library lookup failed (non-fatal): {e}")
        return None
    except Exception as e:
        await ctx.info(f"Open Library parse failed (non-fatal): {e}")
        return None


async def _lookup_isbn_google_books(isbn, ctx):
    """Look up book metadata by ISBN on Google Books. Returns a dict of
    normalized fields, or None on miss / error."""
    try:
        url = f"https://www.googleapis.com/books/v1/volumes?q=isbn:{isbn}"
        resp = await asyncio.to_thread(
            requests.get,
            url,
            headers={"User-Agent": "zotero-mcp/1.0 (https://github.com/its-benjamin/zotero-mcp)"},
            timeout=15,
        )
        if resp.status_code != 200:
            return None
        payload = resp.json() or {}
        items = payload.get("items") or []
        if not items:
            return None
        info = items[0].get("volumeInfo") or {}

        title = info.get("title", "")
        if info.get("subtitle"):
            title = f"{title}: {info['subtitle']}"

        creators = []
        for name in info.get("authors", []) or []:
            name = (name or "").strip()
            if not name:
                continue
            parts = name.rsplit(" ", 1)
            if len(parts) == 2:
                creators.append(
                    {
                        "creatorType": "author",
                        "firstName": parts[0],
                        "lastName": parts[1],
                    }
                )
            else:
                creators.append({"creatorType": "author", "name": name})

        return {
            "source": "Google Books",
            "title": title,
            "creators": creators,
            "date": (info.get("publishedDate") or "").strip(),
            "publisher": (info.get("publisher") or "").strip(),
            "place": "",  # Google Books doesn't expose publication place
            "num_pages": str(info.get("pageCount", "") or "").strip(),
            "url": (info.get("infoLink") or info.get("canonicalVolumeLink") or "").strip(),
        }
    except requests.RequestException as e:
        await ctx.info(f"Google Books lookup failed (non-fatal): {e}")
        return None
    except Exception as e:
        await ctx.info(f"Google Books parse failed (non-fatal): {e}")
        return None


@mcp.tool(
    name="zotero_add_by_isbn",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True),
    description=(
        "Add a book to your Zotero library by ISBN. Resolves metadata via "
        "Open Library (primary) and Google Books (fallback). Accepts ISBN-10, "
        "ISBN-13, with or without hyphens, or a URL/isbn: prefix. Response "
        "includes the resolver source so you can audit metadata quality. "
        "collections accepts keys, names, or '/'-paths (validated before "
        "create). if_exists: 'duplicate' (default) | 'file' (reuse an "
        "existing item with this ISBN — add missing collections/tags) | "
        "'skip'. create_missing_collections: create unknown collection "
        "specs instead of failing."
    ),
)
@with_zotero_api_lock
async def add_by_isbn(
    isbn: str,
    collections: list[str] | str | None = None,
    tags: list[str] | str | None = None,
    if_exists: Literal["duplicate", "file", "skip"] = "duplicate",
    create_missing_collections: bool = False,
    *,
    ctx: Context,
) -> str:
    try:
        read_zot, write_zot = _helpers._get_write_client(ctx)
    except ValueError as e:
        return str(e)

    try:
        if if_exists not in _IF_EXISTS_VALUES:
            return f"Error: if_exists must be one of {_IF_EXISTS_VALUES}."
        normalized = _helpers._normalize_isbn(isbn)
        if not normalized:
            return f"Error: '{isbn}' does not appear to be a valid ISBN (checksum failed or wrong length)."

        try:
            coll_keys = _resolve_collections_arg(
                read_zot,
                collections,
                ctx,
                create_missing=create_missing_collections,
                write_zot=write_zot,
            )
        except ValueError as e:
            return f"Error: {e}"

        if if_exists != "duplicate":
            existing = _helpers.find_existing_items(read_zot, isbn=normalized, ctx=ctx)
            if existing:
                return _handle_existing_item(
                    write_zot,
                    existing,
                    coll_keys,
                    tags,
                    if_exists,
                    matched_by=f"ISBN {normalized}",
                    ctx=ctx,
                )

        await ctx.info(f"Resolving ISBN {normalized} via Open Library...")
        meta = await _lookup_isbn_openlibrary(normalized, ctx)
        if not meta:
            await ctx.info("Open Library miss — falling back to Google Books...")
            meta = await _lookup_isbn_google_books(normalized, ctx)
        if not meta:
            return f"ISBN not found on Open Library or Google Books: {normalized}"

        # Build Zotero book item
        template = await _client.run_zotero_call(
            write_zot.item_template, "book", operation="write_zot.item_template(book)"
        )
        item_data = dict(template)
        if meta.get("title"):
            item_data["title"] = meta["title"]
        if meta.get("creators"):
            item_data["creators"] = meta["creators"]
        if meta.get("date") and "date" in item_data:
            item_data["date"] = meta["date"]
        if meta.get("publisher") and "publisher" in item_data:
            item_data["publisher"] = meta["publisher"]
        if meta.get("place") and "place" in item_data:
            item_data["place"] = meta["place"]
        if meta.get("num_pages") and "numPages" in item_data:
            item_data["numPages"] = meta["num_pages"]
        if meta.get("url") and "url" in item_data:
            item_data["url"] = meta["url"]
        if "ISBN" in item_data:
            item_data["ISBN"] = normalized

        tag_list = _helpers._normalize_str_list_input(tags, "tags")
        if tag_list:
            item_data["tags"] = [{"tag": t} for t in tag_list]
        if coll_keys:
            item_data["collections"] = coll_keys

        result = await _client.run_zotero_call(
            write_zot.create_items, [item_data], operation="write_zot.create_items(isbn)"
        )
        if isinstance(result, dict) and result.get("success"):
            item_key = next(iter(result["success"].values()))
            missing = _helpers.ensure_collection_membership(
                write_zot, item_key, coll_keys, ctx=ctx
            )
            return (
                f"Successfully added: **{item_data.get('title', normalized)}**\n\n"
                f"Item key: `{item_key}`\n"
                f"Type: book\n"
                f"ISBN: {normalized}\n"
                f"Collections: {_collections_status(coll_keys, missing)}\n"
                f"Source: {meta['source']}\n\n"
                "_Note: Open Library and Google Books metadata can be noisy "
                "(publisher-as-author, concatenated places, off-by-one dates). "
                "Verify via `zotero_get_item_metadata` after creation. "
                "Run `zotero_update_search_database` to include this item "
                "in semantic search._"
            )
        return f"Failed to create item: {result}"

    except Exception as e:
        await ctx.error(f"Error adding by ISBN: {e}")
        return f"Error adding by ISBN: {e}"


# Maps Zotero API field names to tool parameter names for user-facing messages
_UPDATE_ITEM_API_TO_PARAM = {
    "title": "title",
    "date": "date",
    "accessDate": "access_date",
    "publicationTitle": "publication_title",
    "abstractNote": "abstract",
    "DOI": "doi",
    "url": "url",
    "extra": "extra",
    "volume": "volume",
    "issue": "issue",
    "pages": "pages",
    "publisher": "publisher",
    "place": "place",
    "ISSN": "issn",
    "language": "language",
    "shortTitle": "short_title",
    "edition": "edition",
    "ISBN": "isbn",
    "bookTitle": "book_title",
    "citationKey": "citation_key",
}


@mcp.tool(
    name="zotero_update_item",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False),
    description=(
        "Update metadata on an existing Zotero item by key. Only fields "
        "you pass are modified; unspecified fields are left alone. "
        "TAG SEMANTICS (easy to get wrong): `tags` REPLACES the entire "
        "tag list. To add tags without touching existing ones, use "
        "`add_tags`. To remove specific tags, use `remove_tags`. These "
        "three are mutually exclusive — prefer `add_tags`/`remove_tags` "
        "for incremental edits. "
        "Similarly, collections/collection_names REPLACE the item's "
        "collection memberships; for incremental moves use "
        "zotero_manage_collections instead. "
        "item_key: 8-character Zotero item key of the item to update. "
        "Editable fields include: title, creators, date, publisher, "
        "publication_title, volume, issue, pages, DOI, ISBN, ISSN, url, "
        "language, abstract, short_title, edition, book_title, extra, "
        "citation_key, item_type. "
        "To migrate an item across types (e.g., journalArticle to book), "
        "pass item_type with a valid Zotero item-type vocabulary value; "
        "overlapping fields are preserved and type-specific fields that "
        "do not map to the target type are dropped. "
        "Requires a writable library (web API key or hybrid mode); fails "
        "in local-only mode. To edit notes use zotero_update_note, not "
        "this. "
        "Example: zotero_update_item(item_key='RTKZQI8E', "
        "add_tags=['reviewed'], doi='10.1145/3708319')."
    ),
)
@with_zotero_api_lock
async def update_item(
    item_key: str,
    title: str | None = None,
    creators: list[dict] | str | None = None,
    date: str | None = None,
    access_date: str | None = None,
    publication_title: str | None = None,
    abstract: str | None = None,
    tags: list[str] | str | None = None,
    add_tags: list[str] | str | None = None,
    remove_tags: list[str] | str | None = None,
    collections: list[str] | str | None = None,
    collection_names: list[str] | str | None = None,
    doi: str | None = None,
    url: str | None = None,
    extra: str | None = None,
    volume: str | None = None,
    issue: str | None = None,
    pages: str | None = None,
    publisher: str | None = None,
    place: str | None = None,
    issn: str | None = None,
    language: str | None = None,
    short_title: str | None = None,
    edition: str | None = None,
    isbn: str | None = None,
    book_title: str | None = None,
    citation_key: Annotated[
        str | None,
        Field(description="BetterBibTeX / Zotero native citation key. Writes to data.citationKey. Useful when BBT auto-pinned the key from incomplete metadata and the programmatic refresh path is blocked (see https://github.com/retorquere/zotero-better-bibtex/issues/3522)."),
    ] = None,
    item_type: str | None = None,
    *,
    ctx: Context,
) -> str:
    """
    Update metadata fields on an existing Zotero item.

    Only fields you pass are modified; unspecified fields are left
    untouched. Fields whose API key does not exist on the item's
    itemType (e.g. ``place`` on a ``journalArticle``) are reported as
    skipped rather than written.
    """
    try:
        read_zot, write_zot = _helpers._get_write_client(ctx)
    except ValueError as e:
        return str(e)

    try:
        # Mutual exclusivity check
        if tags is not None and (add_tags is not None or remove_tags is not None):
            return (
                "Error: Cannot use 'tags' (replace all) together with "
                "'add_tags'/'remove_tags' (incremental). Use one approach or the other."
            )

        await ctx.info(f"Updating item {item_key}")

        # Fetch current item from write client for correct version
        item = await _client.run_zotero_call(write_zot.item, item_key, operation=f"write_zot.item({item_key})")
        data = item.get("data", {})
        changes = []

        # Handle item_type migration first so subsequent field updates are
        # validated against the NEW type's schema. Reshape by merging old
        # data into the new type's template: overlapping typed fields are
        # preserved; type-specific fields not present in the new template
        # are dropped; internal bookkeeping fields (key, version, tags,
        # collections, relations, creators, dateAdded, dateModified) are
        # always preserved regardless of type.
        if item_type is not None:
            old_item_type = data.get("itemType", "")
            if old_item_type != item_type:
                try:
                    new_template = await _client.run_zotero_call(
                        write_zot.item_template, item_type, operation=f"write_zot.item_template({item_type})"
                    )
                except Exception as e:
                    return f"Error: invalid item_type '{item_type}': {e}"

                preserved = {
                    "key",
                    "version",
                    "tags",
                    "collections",
                    "relations",
                    "creators",
                    "dateAdded",
                    "dateModified",
                }
                reshaped = dict(new_template)
                for k, v in data.items():
                    if k in preserved or k in new_template:
                        reshaped[k] = v
                reshaped["itemType"] = item_type
                data = reshaped
                item["data"] = data
                changes.append(f"- **item_type**: '{old_item_type}' -> '{item_type}'")

        # Apply field updates
        field_updates = {}
        if title is not None:
            field_updates["title"] = title
        if date is not None:
            field_updates["date"] = date
        if access_date is not None:
            field_updates["accessDate"] = access_date
        if publication_title is not None:
            field_updates["publicationTitle"] = publication_title
        if abstract is not None:
            field_updates["abstractNote"] = abstract
        if doi is not None:
            field_updates["DOI"] = doi
        if url is not None:
            field_updates["url"] = url
        if extra is not None:
            field_updates["extra"] = extra
        if volume is not None:
            field_updates["volume"] = volume
        if issue is not None:
            field_updates["issue"] = issue
        if pages is not None:
            field_updates["pages"] = pages
        if publisher is not None:
            field_updates["publisher"] = publisher
        if place is not None:
            field_updates["place"] = place
        if issn is not None:
            field_updates["ISSN"] = issn
        if language is not None:
            field_updates["language"] = language
        if short_title is not None:
            field_updates["shortTitle"] = short_title
        if edition is not None:
            field_updates["edition"] = edition
        if isbn is not None:
            field_updates["ISBN"] = isbn
        if book_title is not None:
            field_updates["bookTitle"] = book_title
        if citation_key is not None:
            field_updates["citationKey"] = citation_key

        skipped = []
        for field, value in field_updates.items():
            param_name = _UPDATE_ITEM_API_TO_PARAM.get(field, field)
            if field in data:
                old = data[field]
                if old != value:
                    changes.append(f"- **{param_name}**: '{old}' -> '{value}'")
                data[field] = value
            elif field == "citationKey":
                # citationKey is universally valid; absence on the fetched
                # item just means BBT has not yet auto-pinned a key, so we
                # add rather than skip-as-invalid-for-item-type.
                changes.append(f"- **{param_name}**: (none) -> '{value}'")
                data[field] = value
            else:
                skipped.append(param_name)

        # Creators
        if creators is not None:
            if isinstance(creators, str):
                creators = json.loads(creators)
            data["creators"] = creators
            changes.append("- **creators**: updated")

        # Tags
        if tags is not None:
            tag_list = _helpers._normalize_str_list_input(tags, "tags")
            data["tags"] = [{"tag": t} for t in tag_list]
            changes.append(f"- **tags**: replaced with {tag_list}")
        elif add_tags is not None or remove_tags is not None:
            existing = {t["tag"] for t in data.get("tags", [])}
            if add_tags is not None:
                to_add = _helpers._normalize_str_list_input(add_tags, "add_tags")
                existing.update(to_add)
                changes.append(f"- **tags**: added {to_add}")
            if remove_tags is not None:
                to_remove = set(_helpers._normalize_str_list_input(remove_tags, "remove_tags"))
                existing -= to_remove
                changes.append(f"- **tags**: removed {list(to_remove)}")
            data["tags"] = [{"tag": t} for t in sorted(existing)]

        # Collections — both params ADD to existing collections (never replace)
        if collections is not None:
            coll_keys = _helpers._normalize_str_list_input(collections, "collections")
            existing_colls = set(data.get("collections", []))
            existing_colls.update(coll_keys)
            data["collections"] = list(existing_colls)
            changes.append(f"- **collections**: added {coll_keys}")
        if collection_names is not None:
            names = _helpers._normalize_str_list_input(collection_names, "collection_names")
            resolved = await _helpers._resolve_collection_names(read_zot, names, ctx=ctx)
            existing_colls = set(data.get("collections", []))
            existing_colls.update(resolved)
            data["collections"] = list(existing_colls)
            changes.append(f"- **collections**: added {resolved}")

        skip_warning = ""
        if skipped:
            item_type = data.get("itemType", "unknown")
            skip_warning = f"\n\nSkipped (not valid for item type '{item_type}'): {', '.join(skipped)}"

        if not changes:
            return "No changes to apply." + skip_warning

        resp = await _client.run_zotero_call(
            write_zot.update_item, item, operation=f"write_zot.update_item({item_key})"
        )
        if await _helpers._handle_write_response(resp, ctx):
            from zotero_mcp.cache import get_item_cache

            get_item_cache().invalidate(f"item:{item_key}")
            await _helpers._notify_library_changed(ctx)
            result = f"Successfully updated item `{item_key}`:\n\n" + "\n".join(changes)
            return result + skip_warning
        return "Failed to update item: write operation returned failure"

    except ValueError as e:
        return f"Input error: {e}"
    except Exception as e:
        await ctx.error(f"Error updating item: {e}")
        return f"Error updating item: {e}"


@mcp.tool(
    name="zotero_delete_item",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False),
    description=(
        "Move a Zotero item to the Trash. Works for any item type (book, "
        "journalArticle, webpage, attachment, etc.). For notes, use "
        "zotero_delete_note — identical mechanism, constrained to notes "
        "for safety. Trashed items are recoverable from Zotero's Trash — "
        "empty the Trash in the Zotero UI for permanent deletion. "
        "By default refuses to trash notes; set allow_note=True to override."
    ),
)
async def delete_item(item_key: str, allow_note: bool = False, *, ctx: Context) -> str:
    """
    Move a Zotero item to the Trash.

    Args:
        item_key: Zotero item key/ID to trash
        allow_note: If True, permits trashing note items. Default False
            directs callers to zotero_delete_note for notes (which has the
            same mechanism but is explicit about what it affects).
        ctx: MCP context

    Returns:
        Confirmation message, or an error if the item cannot be trashed.
    """
    try:
        _, write_zot = _helpers._get_write_client(ctx)
    except ValueError as e:
        return str(e)

    try:
        await ctx.info(f"Trashing item {item_key}")

        try:
            item = await _client.run_zotero_call(write_zot.item, item_key, operation=f"write_zot.item({item_key})")
        except Exception:
            return f"Error: No item found with key: {item_key}"

        data = item.get("data", {})
        item_type = data.get("itemType", "unknown")

        if item_type == "note" and not allow_note:
            return (
                f"Error: Item {item_key} is a note. Use zotero_delete_note "
                "for notes, or pass allow_note=True to override."
            )

        # pyzotero's delete_item() permanently destroys items, and update_item()
        # strips the "deleted" field. Send a direct PATCH with {"deleted": 1}
        # to move the item to Zotero's Trash (recoverable by the user).
        from pyzotero.zotero import build_url

        url = build_url(
            write_zot.endpoint,
            f"/{write_zot.library_type}/{write_zot.library_id}/items/{item_key}",
        )
        resp = cast(Any, write_zot.client).patch(
            url=url,
            headers={"If-Unmodified-Since-Version": str(item["version"])},
            content=json.dumps({"deleted": 1}),
        )
        if resp.status_code in (200, 204):
            from zotero_mcp.cache import get_item_cache

            get_item_cache().invalidate(f"item:{item_key}")
            await _helpers._notify_library_changed(ctx)
            return f"Successfully trashed item {item_key} (type={item_type}, recoverable from Zotero's Trash)"
        return f"Failed to trash item {item_key} (HTTP {resp.status_code}): {resp.text[:200]}"

    except Exception as e:
        await ctx.error(f"Error trashing item: {str(e)}")
        return f"Error trashing item: {str(e)}"


@mcp.tool(
    name="zotero_find_duplicates",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    description=(
        "Scan the active library (or a single collection) for duplicate "
        "items and return candidate groups for review. This tool only "
        "IDENTIFIES duplicates — it doesn't merge them. Call "
        "zotero_merge_duplicates to actually merge a group. "
        "method: 'both' (default) — match on title OR DOI; 'title' — "
        "normalized-title match only (lowercase, punctuation-stripped); "
        "'doi' — exact DOI match only (safest for automation). Prefer "
        "'doi' when the user intends to run merge_duplicates "
        "unattended. "
        "collection_key: optional 8-character key to restrict scanning "
        "to one collection; otherwise scans the whole active library. "
        "LIBRARY SIZE CAP: refuses to scan a library with > 5,000 items "
        "(the whole-library scan is O(n²) on titles) — on larger "
        "libraries you MUST pass collection_key to narrow the scope. "
        "limit: max groups to return (default 50). "
        "Returns a markdown block per group with keys, titles, DOIs, "
        "and dateAdded — use this to decide which item to KEEP before "
        "calling zotero_merge_duplicates(keeper_key=..., "
        "duplicate_keys=[...]). "
        "Read-only; works in local or web mode. "
        "Example: zotero_find_duplicates(method='doi', limit=20)."
    ),
)
@with_zotero_api_lock
async def find_duplicates(
    method: Literal["title", "doi", "both"] = "both",
    collection_key: str | None = None,
    limit: int | str | None = 50,
    *,
    ctx: Context,
) -> str:
    try:
        zot = _client.get_zotero_client()
        limit = _helpers._normalize_limit(limit, default=50)
        await ctx.info(f"Searching for duplicates (method={method})")

        # Paginate manually instead of using zot.everything() which can
        # cause "cannot pickle '_thread.RLock' object" in MCP contexts.
        items = []
        start = 0
        page_size = 100
        while True:
            if collection_key:
                batch = await _client.run_zotero_call(
                    zot.collection_items,
                    collection_key,
                    start=start,
                    limit=page_size,
                    operation=f"zot.collection_items({collection_key}, start={start})",
                )
            else:
                batch = await _client.run_zotero_call(
                    zot.items,
                    start=start,
                    limit=page_size,
                    operation=f"zot.items(duplicates, start={start})",
                )
            if not batch:
                break
            items.extend(batch)
            if len(batch) < page_size:
                break
            start += page_size
            if len(items) > 5000:
                break

        if len(items) > 5000:
            return (
                f"Library has {len(items)} items — too large for duplicate scan. "
                "Please scope by collection_key to reduce the search."
            )

        # Normalize and group
        def normalize_title(t):
            t = (t or "").lower().strip()
            t = re.sub(r"[^\w\s]", "", t)
            t = re.sub(r"\s+", " ", t).strip()
            for article in ("a ", "an ", "the "):
                if t.startswith(article):
                    t = t[len(article) :]
            return t

        groups = {}
        for item in items:
            data = item.get("data", {})
            if data.get("itemType") in ("attachment", "note", "annotation"):
                continue

            keys_to_check = []
            if method in ("title", "both"):
                nt = normalize_title(data.get("title", ""))
                if nt:
                    keys_to_check.append(("title", nt))
            if method in ("doi", "both"):
                doi_val = (data.get("DOI") or "").strip().lower()
                if doi_val:
                    keys_to_check.append(("doi", doi_val))

            for group_type, group_key in keys_to_check:
                full_key = f"{group_type}:{group_key}"
                if full_key not in groups:
                    groups[full_key] = []
                groups[full_key].append(item)

        # Filter to groups with duplicates
        dups = {k: v for k, v in groups.items() if len(v) >= 2}

        if not dups:
            return "No duplicates found."

        lines = [f"# Found {len(dups)} duplicate groups", ""]
        shown = 0
        for group_key, group_items in sorted(dups.items()):
            if shown >= limit:
                lines.append(f"\n... and {len(dups) - shown} more groups")
                break
            shown += 1
            lines.append(f"## Group: {group_key}")
            for item in group_items:
                d = item.get("data", {})
                key = item.get("key", "?")
                t = d.get("title", "Untitled")
                dt = d.get("date", "")
                doi_val = d.get("DOI", "")
                lines.append(f"- `{key}` — {t} ({dt}) {f'DOI:{doi_val}' if doi_val else ''}")
            lines.append("")

        lines.append(
            "\nTo merge, call `zotero_merge_duplicates` with the key you want to keep and the keys to merge into it."
        )
        return "\n".join(lines)

    except Exception as e:
        await ctx.error(f"Error finding duplicates: {e}")
        return f"Error finding duplicates: {e}"


@mcp.tool(
    name="zotero_merge_duplicates",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False),
    description=(
        "Merge one or more duplicate items INTO a keeper: consolidates "
        "tags, collections, notes, annotations, and all child items onto "
        "the keeper, then moves the duplicates to Trash (recoverable "
        "from Zotero desktop's Trash view). "
        "SAFETY: dry-run by DEFAULT — prints what would happen without "
        "changing anything. Pass confirm=True to actually execute. Always "
        "run dry-first at least once to verify the keeper choice. "
        "Discover groups first with zotero_find_duplicates. "
        "keeper_key: 8-character key of the item to KEEP. All metadata "
        "gaps on the keeper are filled from duplicates where possible; "
        "conflicting fields keep the keeper's value. "
        "duplicate_keys: ARRAY of 8-character item keys to merge into "
        "the keeper and trash (also accepts a JSON-encoded list "
        "string) — pass as an array, not a single concatenated string. "
        "The keeper itself must NOT appear in this list. "
        "confirm: False (default) runs dry; True executes the merge. "
        "Requires a writable library (web API key or hybrid mode); fails "
        "in local-only mode. "
        "Example dry-run: zotero_merge_duplicates("
        "keeper_key='ABC12345', duplicate_keys=['XYZ98765']). "
        "Example execute: same, plus confirm=True."
    ),
)
@with_zotero_api_lock
async def merge_duplicates(
    keeper_key: str, duplicate_keys: list[str] | str, confirm: bool = False, *, ctx: Context
) -> str:
    try:
        read_zot, write_zot = _helpers._get_write_client(ctx)
    except ValueError as e:
        return str(e)

    try:
        dup_keys = _helpers._normalize_str_list_input(duplicate_keys, "duplicate_keys")

        # Safety: remove keeper from duplicates
        if keeper_key in dup_keys:
            dup_keys.remove(keeper_key)
            await ctx.warning(f"Keeper key '{keeper_key}' was in duplicate list — removed.")

        if not dup_keys:
            return "Error: No duplicate keys to merge (after removing keeper if present)."

        # Fetch all items and children
        keeper = await _client.run_zotero_call(write_zot.item, keeper_key, operation=f"write_zot.item({keeper_key})")
        keeper_children = await _client.run_zotero_call(
            write_zot.children, keeper_key, operation=f"write_zot.children({keeper_key})"
        )
        duplicates = []
        for dk in dup_keys:
            dup_item = await _client.run_zotero_call(write_zot.item, dk, operation=f"write_zot.item({dk})")
            dup_children = await _client.run_zotero_call(write_zot.children, dk, operation=f"write_zot.children({dk})")
            duplicates.append({"item": dup_item, "children": dup_children})

        # Compute what will be merged
        all_tags = set()
        for t in keeper.get("data", {}).get("tags", []):
            all_tags.add(t.get("tag", ""))
        all_collections = set(keeper.get("data", {}).get("collections", []))
        total_children_to_move = 0

        for dup in duplicates:
            for t in dup["item"].get("data", {}).get("tags", []):
                all_tags.add(t.get("tag", ""))
            all_collections.update(dup["item"].get("data", {}).get("collections", []))
            total_children_to_move += len(dup["children"])

        all_tags.discard("")
        new_tags = all_tags - {t.get("tag", "") for t in keeper.get("data", {}).get("tags", [])}
        new_collections = all_collections - set(keeper.get("data", {}).get("collections", []))

        # Build keeper's attachment signatures for deduplication
        keeper_attachment_sigs = set()
        for kc in keeper_children:
            kd = kc.get("data", {})
            if kd.get("itemType") == "attachment":
                sig = (
                    kd.get("contentType", ""),
                    kd.get("filename", ""),
                    kd.get("md5", ""),
                    kd.get("url", ""),
                )
                keeper_attachment_sigs.add(sig)

        # Count duplicate attachments that would be skipped
        skipped_attachment_count = 0
        for dup in duplicates:
            for child in dup["children"]:
                cd = child.get("data", {})
                if cd.get("itemType") == "attachment":
                    sig = (
                        cd.get("contentType", ""),
                        cd.get("filename", ""),
                        cd.get("md5", ""),
                        cd.get("url", ""),
                    )
                    if sig in keeper_attachment_sigs:
                        skipped_attachment_count += 1

        # DRY RUN
        if not confirm:
            lines = [
                "# Merge Preview (dry run)",
                "",
                f"**Keeper:** `{keeper_key}` — {keeper.get('data', {}).get('title', 'Untitled')}",
                f"**Duplicates to merge:** {', '.join(f'`{k}`' for k in dup_keys)}",
                "",
                f"**Tags to add:** {sorted(new_tags) if new_tags else 'none'}",
                f"**Collections to add:** {sorted(new_collections) if new_collections else 'none'}",
                f"**Child items to re-parent:** {total_children_to_move - skipped_attachment_count}",
                f"  ({skipped_attachment_count} duplicate attachment(s) will be skipped)"
                if skipped_attachment_count
                else "  (notes, PDFs, annotations, highlights, etc.)",
                "",
                "Duplicates will be moved to **Trash** (recoverable in Zotero).",
                "",
                "**Call again with `confirm=True` to execute.**",
            ]
            return "\n".join(lines)

        # EXECUTE MERGE
        await ctx.info(f"Merging {len(dup_keys)} duplicates into {keeper_key}")

        # Step 3: Consolidate tags
        if new_tags:
            keeper_data = keeper.get("data", {})
            existing_tags = [t.get("tag", "") for t in keeper_data.get("tags", [])]
            keeper_data["tags"] = [{"tag": t} for t in sorted(set(existing_tags) | all_tags)]
            resp = await _client.run_zotero_call(
                write_zot.update_item, keeper, operation=f"write_zot.update_item({keeper_key})"
            )
            if not await _helpers._handle_write_response(resp, ctx):
                return "Error: Failed to merge tags into keeper."
            keeper = await _client.run_zotero_call(
                write_zot.item, keeper_key, operation=f"write_zot.item({keeper_key})"
            )  # re-fetch for version

        # Step 4: Consolidate collections
        for coll_key in new_collections:
            resp = await _client.run_zotero_call(
                write_zot.addto_collection, coll_key, keeper, operation=f"write_zot.addto_collection({coll_key})"
            )
            if not await _helpers._handle_write_response(resp, ctx):
                await ctx.warning(f"Failed to add keeper to collection {coll_key}")
            keeper = await _client.run_zotero_call(
                write_zot.item, keeper_key, operation=f"write_zot.item({keeper_key})"
            )  # re-fetch for version

        # Step 5: Re-parent children (skip duplicate attachments)
        moved = []
        failed = []
        skipped_dupes = []
        for dup in duplicates:
            for child in dup["children"]:
                child_key = child.get("key", "?")
                try:
                    fresh_child = await _client.run_zotero_call(
                        write_zot.item, child_key, operation=f"write_zot.item({child_key})"
                    )
                    # Skip duplicate attachments — keeper already has this one
                    child_data = fresh_child.get("data", {})
                    if child_data.get("itemType") == "attachment":
                        child_sig = (
                            child_data.get("contentType", ""),
                            child_data.get("filename", ""),
                            child_data.get("md5", ""),
                            child_data.get("url", ""),
                        )
                        if child_sig in keeper_attachment_sigs:
                            skipped_dupes.append(child_key)
                            continue  # Skip — keeper already has this attachment
                    fresh_child.get("data", {})["parentItem"] = keeper_key
                    resp = await _client.run_zotero_call(
                        write_zot.update_item, fresh_child, operation=f"write_zot.update_item({child_key})"
                    )
                    if await _helpers._handle_write_response(resp, ctx):
                        moved.append(child_key)
                    else:
                        failed.append(child_key)
                except Exception as e:
                    failed.append(f"{child_key} ({e})")

        if failed:
            return (
                f"Merge partially completed. Moved {len(moved)} children, "
                f"but {len(failed)} failed: {failed}\n\n"
                "Duplicates were NOT trashed. Fix the failures and retry."
            )

        # Step 6: Trash duplicates (move to Zotero Trash, NOT permanent delete)
        # pyzotero's update_item() strips "deleted" and delete_item() permanently
        # destroys items. We send a direct PATCH with {"deleted": 1} which moves
        # items to Zotero's Trash — recoverable by the user.
        trashed = []
        for dup in duplicates:
            dup_key = dup["item"]["key"]
            try:
                dup_item = await _client.run_zotero_call(
                    write_zot.item, dup_key, operation=f"write_zot.item({dup_key})"
                )
                version = dup_item["version"]
                from pyzotero.zotero import build_url

                url = build_url(
                    write_zot.endpoint,
                    f"/{write_zot.library_type}/{write_zot.library_id}/items/{dup_key}",
                )
                headers = {"If-Unmodified-Since-Version": str(version)}
                rate_limit("zotero")
                resp = cast(Any, write_zot.client).patch(
                    url=url,
                    headers=headers,
                    content=json.dumps({"deleted": 1}),
                )
                if resp.status_code in (200, 204):
                    trashed.append(dup_key)
                else:
                    await ctx.warning(f"Failed to trash {dup_key}: HTTP {resp.status_code}")
            except Exception as e:
                await ctx.warning(f"Failed to trash {dup_key}: {e}")

        skip_info = f" ({len(skipped_dupes)} duplicate attachments skipped)" if skipped_dupes else ""
        await _helpers._notify_library_changed(ctx)
        return (
            f"Merge complete.\n\n"
            f"- Tags merged: {len(new_tags)} new\n"
            f"- Collections added: {len(new_collections)} new\n"
            f"- Children re-parented: {len(moved)}{skip_info}\n"
            f"- Duplicates trashed: {', '.join(f'`{k}`' for k in trashed)}\n\n"
            "Trashed items can be restored from Zotero's Trash."
        )

    except ValueError as e:
        return f"Input error: {e}"
    except Exception as e:
        await ctx.error(f"Error merging duplicates: {e}")
        return f"Error merging duplicates: {e}"


@mcp.tool(
    name="zotero_get_pdf_outline",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    description=(
        "Extract the table of contents (outline/bookmarks) from a PDF "
        "attachment, returned as a hierarchical markdown list with each "
        "entry's page number. "
        "Use this to orient in a paper before calling "
        "zotero_get_item_fulltext — the outline is typically < 200 "
        "tokens versus 10K+ for the full text. If the PDF has no "
        "embedded outline, returns a short 'no outline' message rather "
        "than failing. "
        "item_key: the PDF ATTACHMENT key OR the parent item key — both "
        "are accepted; attachment-to-parent resolution is automatic. "
        "Find the right key with zotero_get_item_children if unsure. "
        "Scope: PDFs only (EPUBs have no outline extraction here). "
        "Requires PyMuPDF. Install this fork with the pdf extra from GitHub. "
        "Read-only; works in local or web mode. "
        "Example: zotero_get_pdf_outline(item_key='RTKZQI8E')."
    ),
)
@with_zotero_api_lock
async def get_pdf_outline(item_key: str, *, ctx: Context) -> str:
    try:
        zot = await _client.run_zotero_call(_client.get_zotero_client, operation="get_zotero_client")
        await ctx.info(f"Getting PDF outline for item {item_key}")

        # Find PDF attachment
        children = await _client.run_zotero_call(zot.children, item_key, operation=f"zot.children({item_key})")
        pdf_child = None
        for child in children:
            if child.get("data", {}).get("contentType") == "application/pdf":
                pdf_child = child
                break

        if not pdf_child:
            return f"No PDF attachment found for item `{item_key}`."

        try:
            import fitz
        except ImportError:
            return "Error: PyMuPDF (fitz) is required for PDF outline extraction."

        attachment_key = pdf_child["key"]
        filename = pdf_child.get("data", {}).get("filename", "document.pdf")

        # Download PDF (works for both local/WebDAV/web storage)
        with tempfile.TemporaryDirectory() as tmpdir:
            await _client.run_zotero_call(
                zot.dump, attachment_key, filename=filename, path=tmpdir, operation=f"zot.dump({attachment_key})"
            )
            pdf_path = os.path.join(tmpdir, filename)
            if not os.path.exists(pdf_path) or os.path.getsize(pdf_path) == 0:
                return f"Could not download PDF for attachment `{attachment_key}`."
            doc = fitz.open(pdf_path)
            toc = doc.get_toc()
            doc.close()

        if not toc:
            return "This PDF does not contain a table of contents/outline."

        lines = [f"# PDF Outline for item `{item_key}`", ""]
        for level, title, page in toc:
            indent = "  " * (level - 1)
            lines.append(f"{indent}- {title} (p. {page})")

        return "\n".join(lines)

    except Exception as e:
        await ctx.error(f"Error extracting PDF outline: {e}")
        return f"Error extracting PDF outline: {e}"


@mcp.tool(
    name="zotero_add_from_file",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False),
    description=(
        "Add an item to the active Zotero library from a LOCAL .pdf or "
        ".epub file. Attempts to extract the DOI from the file content; "
        "if found, enriches metadata via CrossRef (title, creators, "
        "journal, year, abstract). If no DOI is found, falls back to "
        "best-effort title/author guesses from the filename or document "
        "text. "
        "Use this when the user has a file on disk but no DOI/URL handy. "
        "If you have a DOI use zotero_add_by_doi; for an online URL use "
        "zotero_add_by_url. "
        "file_path: ABSOLUTE path to a .pdf or .epub file (relative "
        "paths fail). Other extensions are rejected. "
        "title: optional override if metadata extraction misses. "
        "collections: optional list of collection keys, names, or "
        "'/'-separated paths to file under — resolved and validated "
        "before the item is created. "
        "tags: optional list of tag strings. "
        "if_exists: 'duplicate' (default) | 'file' (when the extracted "
        "DOI matches an existing item, reuse it: file into missing "
        "collections, attach the file to it unless an attachment with "
        "the same filename exists) | 'skip' (no item, no attachment). "
        "create_missing_collections: create unknown collection specs. "
        "Requires a writable library (fails in local-only mode). PDF "
        "uploads may hit the 300MB Zotero cloud free-tier quota — "
        "metadata still lands. Run zotero_update_search_database "
        "afterwards for semantic search. "
        "Example: zotero_add_from_file(file_path='/Users/me/paper.pdf', "
        "collections=['9SU943GB'])."
    ),
)
@with_zotero_api_lock
async def add_from_file(
    file_path: str,
    title: str | None = None,
    item_type: str = "document",
    collections: list[str] | str | None = None,
    tags: list[str] | str | None = None,
    if_exists: Literal["duplicate", "file", "skip"] = "duplicate",
    create_missing_collections: bool = False,
    *,
    ctx: Context,
) -> str:
    try:
        read_zot, write_zot = _helpers._get_write_client(ctx)
    except ValueError as e:
        return str(e)

    try:
        if if_exists not in _IF_EXISTS_VALUES:
            return f"Error: if_exists must be one of {_IF_EXISTS_VALUES}."
        # Path validation — check symlink BEFORE resolving
        if os.path.islink(file_path):
            return "Error: Symlinks are not allowed for security reasons."
        if not os.path.isabs(file_path):
            return "Error: file_path must be an absolute path."
        # Resolve ".." components after symlink check. On Windows, realpath()
        # rewrites POSIX-style absolute paths like /Users/me/file.pdf into
        # C:\Users\..., so preserve that valid Zotero/test shape.
        if os.name == "nt" and file_path.startswith("/") and not file_path.startswith("//"):
            file_path = posixpath.normpath(file_path)
        else:
            file_path = os.path.realpath(file_path)
        if not os.path.isfile(file_path):
            return f"Error: File not found: {file_path}"

        try:
            coll_keys = _resolve_collections_arg(
                read_zot, collections, ctx,
                create_missing=create_missing_collections, write_zot=write_zot,
            )
        except ValueError as e:
            return f"Error: {e}"

        ext = os.path.splitext(file_path)[1].lower()
        allowed_exts = {".pdf", ".epub", ".djvu", ".doc", ".docx", ".odt", ".rtf", ".html", ".htm"}
        if ext not in allowed_exts:
            return f"Error: Unsupported file type '{ext}'. Allowed: {', '.join(sorted(allowed_exts))}"

        await ctx.info(f"Adding file: {file_path}")

        # Try DOI extraction from PDF
        extracted_doi = None
        if ext == ".pdf":
            try:
                import fitz

                doc = fitz.open(file_path)

                # Check metadata
                meta = doc.metadata or {}
                for field in ("subject", "keywords", "title"):
                    candidate = meta.get(field, "")
                    if candidate:
                        found_doi = _helpers._normalize_doi(candidate)
                        if found_doi:
                            extracted_doi = found_doi
                            break

                # Scan first page text
                if not extracted_doi and doc.page_count > 0:
                    page_text = doc[0].get_text()
                    text = page_text[:3000] if isinstance(page_text, str) else ""
                    m = re.search(r"10\.\d{4,9}/[^\s]+", text)
                    if m:
                        found_doi = _helpers._normalize_doi(m.group(0))
                        if found_doi:
                            extracted_doi = found_doi

                doc.close()
            except Exception as e:
                await ctx.info(f"DOI extraction failed (non-fatal): {e}")

        # Create the metadata item. With if_exists='file' and a known DOI,
        # add_by_doi reuses the existing item — the attachment below then
        # lands on it instead of on a fresh duplicate.
        if extracted_doi:
            await ctx.info(f"Found DOI: {extracted_doi}")
            result_msg = await add_by_doi(
                doi=extracted_doi,
                collections=coll_keys,
                tags=tags,
                if_exists=if_exists,
                ctx=ctx,
            )
            # Extract item key from result
            key_match = re.search(r"Item key: `([^`]+)`", result_msg)
            if key_match:
                parent_key = key_match.group(1)
            else:
                return f"DOI lookup succeeded but couldn't extract item key.\n\n{result_msg}"
        else:
            # Create a basic item
            template = await _client.run_zotero_call(
                write_zot.item_template, item_type, operation=f"write_zot.item_template({item_type})"
            )
            template["title"] = title or os.path.basename(file_path)

            tag_list = _helpers._normalize_str_list_input(tags, "tags")
            if tag_list:
                template["tags"] = [{"tag": t} for t in tag_list]
            if coll_keys:
                template["collections"] = coll_keys

            result = await _client.run_zotero_call(
                write_zot.create_items, [template], operation="write_zot.create_items(from_file)"
            )
            if isinstance(result, dict) and result.get("success"):
                parent_key = next(iter(result["success"].values()))
                missing = _helpers.ensure_collection_membership(
                    write_zot, parent_key, coll_keys, ctx=ctx
                )
                if missing:
                    ctx.warning(f"Failed to file {parent_key} in {missing}")
            else:
                return f"Failed to create item: {result}"

        item_reused = bool(extracted_doi) and result_msg.startswith("Already in library")
        if item_reused and if_exists == "skip":
            return result_msg + "\n\nFile NOT attached (if_exists='skip')."

        # Attach the file. When reusing an existing item, skip the upload if
        # an attachment with the same filename is already there — re-running
        # the command must converge, not accumulate duplicate attachments.
        try:
            display_name = os.path.basename(file_path)
            if item_reused:
                try:
                    kids = await _client.run_zotero_call(
                        write_zot.children, parent_key, operation=f"write_zot.children({parent_key})"
                    )
                except Exception:
                    kids = []
                if any((k.get("data", {}) or {}).get("filename") == display_name for k in kids):
                    return (
                        f"{result_msg}\n"
                        f"Attachment already present: {display_name} (not re-uploaded)\n\n"
                        "_Note: To include this item in semantic search, run "
                        "zotero_update_search_database._"
                    )

            await _client.run_zotero_call(
                write_zot.attachment_both,
                [(display_name, file_path)],
                parentid=parent_key,
                operation=f"write_zot.attachment_both({parent_key})",
            )
            attach_info = f"File attached: {display_name}"
        except Exception as e:
            attach_info = f"Item created but file attachment failed: {e}"

        return (
            f"Item key: `{parent_key}`\n"
            f"{'DOI: ' + extracted_doi + chr(10) if extracted_doi else ''}"
            f"{attach_info}\n\n"
            "_Note: To include this item in semantic search, run "
            "zotero_update_search_database._"
        )

    except Exception as e:
        await ctx.error(f"Error adding from file: {e}")
        return f"Error adding from file: {e}"


@mcp.tool(
    name="zotero_move_item",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False),
    description=(
        "Move an item from one collection to another. "
        "Removes the item from the source collection and adds it to the target. "
        "If source_collection is omitted, the item is simply added to the target. "
        "item_key: 8-character Zotero item key. "
        "target_collection: key of the collection to move the item into. "
        "source_collection: (optional) key of the collection to remove the item from."
    ),
)
@with_zotero_api_lock
async def move_item(
    item_key: str,
    target_collection: str,
    source_collection: str | None = None,
    *,
    ctx: Context,
) -> str:
    """Move an item between collections."""
    try:
        key_err = _helpers.validate_item_key(item_key)
        if key_err:
            return f"Error: {key_err}"
        tgt_err = _helpers.validate_collection_key(target_collection)
        if tgt_err:
            return f"Error: target_collection - {tgt_err}"
        if source_collection:
            src_err = _helpers.validate_collection_key(source_collection)
            if src_err:
                return f"Error: source_collection - {src_err}"

        try:
            read_zot, write_zot = _helpers._get_write_client(ctx)
        except ValueError as e:
            return str(e)

        await ctx.info(f"Moving item {item_key} to collection {target_collection}")

        item = await _client.run_zotero_call(read_zot.item, item_key, operation=f"zot.item({item_key})")
        if not item:
            return f"Error: No item found with key: {item_key}"

        data = item.get("data", {})
        collections = set(data.get("collections", []))

        if source_collection and source_collection not in collections:
            return f"Error: Item {item_key} is not in collection {source_collection}"

        if source_collection:
            collections.discard(source_collection)
        collections.add(target_collection)

        data["collections"] = list(collections)
        item["data"] = data

        response = await _client.run_zotero_call(write_zot.update_item, item, operation=f"zot.update_item({item_key})")
        if not await _helpers._handle_write_response(response, ctx):
            return f"Error: Failed to move item {item_key}"

        from zotero_mcp.cache import get_item_cache

        get_item_cache().invalidate(f"item:{item_key}")

        src_msg = f" from {source_collection}" if source_collection else ""
        await _helpers._notify_library_changed(ctx)
        return f"Moved item {item_key}{src_msg} to collection {target_collection}"

    except Exception as e:
        await ctx.error(f"Error moving item: {e}")
        return f"Error moving item: {e}"


@mcp.tool(
    name="zotero_rename_tag",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False),
    description=(
        "Rename a tag across all items that have it. "
        "Finds all items with old_tag, removes it, and adds new_tag. "
        "old_tag: the tag name to rename. "
        "new_tag: the new tag name. "
        "limit: max items to process (default 100)."
    ),
)
@with_zotero_api_lock
async def rename_tag(
    old_tag: str,
    new_tag: str,
    limit: int | str = 100,
    *,
    ctx: Context,
) -> str:
    """Rename a tag across all items."""
    try:
        old_err = _helpers.validate_tag(old_tag)
        if old_err:
            return f"Error: old_tag - {old_err}"
        new_err = _helpers.validate_tag(new_tag)
        if new_err:
            return f"Error: new_tag - {new_err}"

        old_tag = old_tag.strip()
        new_tag = new_tag.strip()

        if old_tag == new_tag:
            return "Error: old_tag and new_tag are the same"

        try:
            read_zot, write_zot = _helpers._get_write_client(ctx)
        except ValueError as e:
            return str(e)

        await ctx.info(f"Renaming tag '{old_tag}' to '{new_tag}'")

        limit = _helpers._normalize_limit(limit, default=100, max_val=500)
        items = await _client.run_zotero_call(
            _helpers._paginate,
            read_zot.items,
            tag=old_tag,
            max_items=limit,
            operation=f"paginate(zot.items, tag={old_tag})",
        )

        if not items:
            return f"No items found with tag '{old_tag}'"

        updated = 0
        errors = []
        for item in items:
            data = item.get("data", {})
            tags = data.get("tags", [])

            new_tags = [t for t in tags if t.get("tag") != old_tag]
            if not any(t.get("tag") == new_tag for t in new_tags):
                new_tags.append({"tag": new_tag})

            if len(new_tags) == len(tags) and all(t.get("tag") != old_tag for t in tags):
                continue

            data["tags"] = new_tags
            item["data"] = data

            try:
                response = await _client.run_zotero_call(
                    write_zot.update_item, item, operation=f"zot.update_item({item.get('key', '')})"
                )
                if await _helpers._handle_write_response(response, ctx):
                    updated += 1
                    from zotero_mcp.cache import get_item_cache

                    get_item_cache().invalidate(f"item:{item['key']}")
                else:
                    errors.append(item["key"])
            except Exception as e:
                errors.append(f"{item['key']}: {e}")

        await _helpers._notify_library_changed(ctx)
        result = f"Renamed tag '{old_tag}' to '{new_tag}' on {updated} item(s)"
        if errors:
            result += f"\nFailed on {len(errors)} item(s): {errors[:5]}"
            if len(errors) > 5:
                result += f" ... and {len(errors) - 5} more"
        return result

    except Exception as e:
        await ctx.error(f"Error renaming tag: {e}")
        return f"Error renaming tag: {e}"


@mcp.tool(
    name="zotero_relate_items",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False),
    description=(
        "Create a bidirectional 'Related' link between two Zotero items. "
        "After linking, each item appears in the other's Related Items section. "
        "item_key: the 8-character key of the first item. "
        "related_key: the 8-character key of the second item to relate to. "
        "Scope: active library only (web API required for writes). "
        "Example: zotero_relate_items(item_key='ABC12345', related_key='DEF67890')."
    ),
)
@with_zotero_api_lock
async def relate_items(item_key: str, related_key: str, *, ctx: Context) -> str:
    """Create a bidirectional Related link between two items."""
    try:
        for k in [item_key, related_key]:
            err = _helpers.validate_item_key(k)
            if err:
                return f"Error: Invalid key '{k}': {err}"

        if item_key == related_key:
            return "Error: Cannot relate an item to itself."

        await ctx.info(f"Relating {item_key} ↔ {related_key}")
        zot = await _client.run_zotero_call(_client.get_web_zotero_client, operation="get_web_zotero_client")
        if not zot:
            return "Error: Web API client required for relating items. Set ZOTERO_API_KEY and ZOTERO_LIBRARY_ID."

        # Get both items
        item1 = await _client.run_zotero_call(zot.item, item_key, operation=f"zot.item({item_key})")
        item2 = await _client.run_zotero_call(zot.item, related_key, operation=f"zot.item({related_key})")
        if not item1:
            return f"Item not found: {item_key}"
        if not item2:
            return f"Item not found: {related_key}"

        # Add relations bidirectionally
        lib_type = zot.library_type if hasattr(zot, "library_type") else "users"
        lib_id = zot.library_id if hasattr(zot, "library_id") else "0"
        # pyzotero uses plural "users"/"groups" but Zotero URIs use singular
        uri_lib_type = lib_type.rstrip("s") if lib_type.endswith("s") else lib_type
        uri1 = f"http://zotero.org/{uri_lib_type}/{lib_id}/items/{item_key}"
        uri2 = f"http://zotero.org/{uri_lib_type}/{lib_id}/items/{related_key}"

        relations1 = item1.get("data", {}).get("relations", {}).get("dc:relation", [])
        relations2 = item2.get("data", {}).get("relations", {}).get("dc:relation", [])
        if isinstance(relations1, str):
            relations1 = [relations1]
        if isinstance(relations2, str):
            relations2 = [relations2]

        # Check if already related
        if uri2 in relations1:
            return f"Items {item_key} and {related_key} are already related."

        # Update item1: add relation to item2
        if "relations" not in item1["data"]:
            item1["data"]["relations"] = {}
        item1["data"]["relations"]["dc:relation"] = relations1 + [uri2]

        # Update item2: add relation to item1
        if "relations" not in item2["data"]:
            item2["data"]["relations"] = {}
        item2["data"]["relations"]["dc:relation"] = relations2 + [uri1]

        # Save both items
        errors = []
        for item, other_key in [(item1, related_key), (item2, item_key)]:
            try:
                resp = await _client.run_zotero_call(zot.update_item, item, operation=f"zot.update_item({item['key']})")
                if not await _helpers._handle_write_response(resp):
                    errors.append(f"Failed to update {item['key']}: {resp}")
            except Exception as e:
                errors.append(f"Error updating {item['key']}: {e}")

        if errors:
            return f"Partial success: {'; '.join(errors)}"

        title1 = item1["data"].get("title", item_key)[:50]
        title2 = item2["data"].get("title", related_key)[:50]
        await _helpers._notify_library_changed(ctx)
        return f"Related: '{title1}' ↔ '{title2}'"

    except Exception as e:
        await ctx.error(f"Error relating items: {str(e)}")
        return f"Error relating items: {str(e)}"


@mcp.tool(
    name="zotero_unrelate_items",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False),
    description=(
        "Remove the 'Related' link between two Zotero items. "
        "item_key: the 8-character key of the first item. "
        "related_key: the 8-character key of the item to unlink. "
        "Scope: active library only (web API required for writes)."
    ),
)
@with_zotero_api_lock
async def unrelate_items(item_key: str, related_key: str, *, ctx: Context) -> str:
    """Remove a Related link between two items."""
    try:
        for k in [item_key, related_key]:
            err = _helpers.validate_item_key(k)
            if err:
                return f"Error: Invalid key '{k}': {err}"

        await ctx.info(f"Unrelating {item_key} ↔ {related_key}")
        zot = await _client.run_zotero_call(_client.get_web_zotero_client, operation="get_web_zotero_client")
        if not zot:
            return "Error: Web API client required. Set ZOTERO_API_KEY and ZOTERO_LIBRARY_ID."

        item1 = await _client.run_zotero_call(zot.item, item_key, operation=f"zot.item({item_key})")
        item2 = await _client.run_zotero_call(zot.item, related_key, operation=f"zot.item({related_key})")
        if not item1:
            return f"Item not found: {item_key}"
        if not item2:
            return f"Item not found: {related_key}"

        lib_type = zot.library_type if hasattr(zot, "library_type") else "users"
        lib_id = zot.library_id if hasattr(zot, "library_id") else "0"
        uri_lib_type = lib_type.rstrip("s") if lib_type.endswith("s") else lib_type
        uri1 = f"http://zotero.org/{uri_lib_type}/{lib_id}/items/{item_key}"
        uri2 = f"http://zotero.org/{uri_lib_type}/{lib_id}/items/{related_key}"

        relations1 = item1.get("data", {}).get("relations", {}).get("dc:relation", [])
        relations2 = item2.get("data", {}).get("relations", {}).get("dc:relation", [])
        if isinstance(relations1, str):
            relations1 = [relations1]
        if isinstance(relations2, str):
            relations2 = [relations2]

        if uri2 not in relations1:
            return f"Items {item_key} and {related_key} are not related."

        # Remove relations bidirectionally
        item1["data"]["relations"]["dc:relation"] = [r for r in relations1 if r != uri2]
        item2["data"]["relations"]["dc:relation"] = [r for r in relations2 if r != uri1]

        errors = []
        for item in [item1, item2]:
            try:
                resp = await _client.run_zotero_call(zot.update_item, item, operation=f"zot.update_item({item['key']})")
                if not await _helpers._handle_write_response(resp):
                    errors.append(f"Failed to update {item['key']}: {resp}")
            except Exception as e:
                errors.append(f"Error updating {item['key']}: {e}")

        if errors:
            return f"Partial success: {'; '.join(errors)}"

        await _helpers._notify_library_changed(ctx)
        return f"Unrelated: {item_key} ↔ {related_key}"

    except Exception as e:
        await ctx.error(f"Error unrelating items: {str(e)}")
        return f"Error unrelating items: {str(e)}"


@mcp.tool(
    name="zotero_batch_delete_items",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False),
    description=(
        "Move multiple Zotero items to the Trash in a single batch operation. "
        "item_keys: comma-separated list of 8-character Zotero item keys (max 50). "
        "Items are recoverable from Zotero's Trash. "
        "Example: zotero_batch_delete_items(item_keys='ABC12345,DEF67890')."
    ),
)
@with_zotero_api_lock
async def batch_delete_items(item_keys: str, *, ctx: Context) -> str:
    """Batch delete (trash) multiple items."""
    try:
        keys = [k.strip() for k in item_keys.replace(";", ",").split(",") if k.strip()]
        if not keys:
            return "Error: No item keys provided."
        if len(keys) > 50:
            return f"Error: Too many keys ({len(keys)}). Max 50 per batch."

        for k in keys:
            err = _helpers.validate_item_key(k)
            if err:
                return f"Error: Invalid key '{k}': {err}"

        _, write_zot = _helpers._get_write_client(ctx)
    except ValueError as e:
        return str(e)

    try:
        await ctx.info(f"Batch trashing {len(keys)} items")
        from pyzotero.zotero import build_url

        # Get current versions for all items
        items = await _client.run_zotero_call(
            write_zot.item, ",".join(keys), operation=f"write_zot.item(batch:{len(keys)})"
        )
        if not items:
            return f"No items found for keys: {','.join(keys)}"

        # Build batch PATCH payload
        successes = []
        errors = []
        for item in items:
            key = item.get("key", "")
            try:
                url = build_url(
                    write_zot.endpoint,
                    f"/{write_zot.library_type}/{write_zot.library_id}/items/{key}",
                )
                resp = cast(Any, write_zot.client).patch(
                    url=url,
                    headers={"If-Unmodified-Since-Version": str(item["version"])},
                    content=json.dumps({"deleted": 1}),
                )
                if resp.status_code in (200, 204):
                    successes.append(key)
                else:
                    errors.append(f"{key}: HTTP {resp.status_code}")
            except Exception as e:
                errors.append(f"{key}: {e}")

        # Invalidate caches
        from zotero_mcp.cache import get_item_cache

        cache = get_item_cache()
        for key in successes:
            cache.invalidate(f"item:{key}")

        if successes:
            await _helpers._notify_library_changed(ctx)

        result = f"Trashed {len(successes)} item(s)"
        if errors:
            result += f", failed {len(errors)}: {'; '.join(errors[:5])}"
        return result

    except Exception as e:
        await ctx.error(f"Error batch trashing items: {str(e)}")
        return f"Error batch trashing items: {str(e)}"


@mcp.tool(
    name="zotero_create_saved_search",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False),
    description=(
        "Create a new saved search in Zotero. "
        "name: the name for the saved search. "
        "conditions: a list of search conditions, each with 'condition', 'operator', and 'value'. "
        "Common conditions: 'title', 'author', 'date', 'tag', 'publicationTitle', 'DOI', 'abstractNote', 'anyField'. "
        "Common operators: 'contains', 'is', 'doesNotContain', 'isNot', 'beginsWith', 'isInTheLast'. "
        "join_mode: 'all' (AND) or 'any' (OR) — default 'all'. "
        "Example: zotero_create_saved_search(name='ML Papers', conditions=[{'condition': 'title', 'operator': 'contains', 'value': 'machine learning'}])."
    ),
)
@with_zotero_api_lock
async def create_saved_search(
    name: str,
    conditions: list[dict[str, str]],
    join_mode: str = "all",
    *,
    ctx: Context,
) -> str:
    """Create a saved search in Zotero."""
    try:
        if not name.strip():
            return "Error: Search name cannot be empty."
        if not conditions:
            return "Error: At least one condition is required."

        _, write_zot = _helpers._get_write_client(ctx)
    except ValueError as e:
        return str(e)

    try:
        await ctx.info(f"Creating saved search: {name}")

        # Build the search object
        search_data = {
            "name": name.strip(),
            "conditions": [],
        }

        for cond in conditions:
            if not isinstance(cond, dict):
                return f"Error: Each condition must be a dict with 'condition', 'operator', 'value'. Got: {cond}"
            condition = cond.get("condition", "")
            operator = cond.get("operator", "contains")
            value = cond.get("value", "")
            if not condition:
                return f"Error: Condition missing 'condition' field: {cond}"
            search_data["conditions"].append(
                {
                    "condition": condition,
                    "operator": operator,
                    "value": value,
                    "required": join_mode,
                }
            )

        # Create via API
        try:
            resp = await _client.run_zotero_call(
                write_zot.create_saved_search, search_data, operation="write_zot.create_saved_search"
            )
            if resp and isinstance(resp, dict) and "success" in resp:
                key = resp["success"].get("0", "")
                if key:
                    await _helpers._notify_library_changed(ctx)
                    return f"Created saved search '{name}' (key: {key})"
            return f"Failed to create saved search: {resp}"
        except Exception as primary_err:
            await ctx.info(f"Primary create_saved_search failed, trying fallback: {primary_err}")
            # Fallback: try direct API call
            import requests as _requests

            lib_type = write_zot.library_type if hasattr(write_zot, "library_type") else "users"
            lib_id = write_zot.library_id if hasattr(write_zot, "library_id") else "0"
            if _utils.is_local_mode():
                base_url = f"http://localhost:23119/api/{lib_type}/{lib_id}"
            else:
                base_url = f"https://api.zotero.org/{lib_type}/{lib_id}"

            url = f"{base_url}/searches"
            headers = {"Content-Type": "application/json", "Zotero-API-Version": "3"}
            if not _utils.is_local_mode():
                from zotero_mcp.config import load_config

                cfg = load_config()
                api_key = cfg.get("client_env", {}).get("ZOTERO_API_KEY", "")
                if api_key:
                    headers["Zotero-API-Key"] = api_key

            resp = _requests.post(url, json=[search_data], headers=headers, timeout=30)
            if resp.status_code == 200:
                result = resp.json()
                if "success" in result and "0" in result["success"]:
                    key = result["success"]["0"]
                    await _helpers._notify_library_changed(ctx)
                    return f"Created saved search '{name}' (key: {key})"
                return f"Failed to create saved search: {result}"
            return f"Failed to create saved search (HTTP {resp.status_code}): {resp.text[:200]}"

    except Exception as e:
        await ctx.error(f"Error creating saved search: {str(e)}")
        return f"Error creating saved search: {str(e)}"


@mcp.tool(
    name="zotero_batch_add_by_doi",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False),
    description=(
        "Add multiple papers to Zotero by DOI in sequence. "
        "dois: comma-separated list of DOIs (max 10). "
        "Each DOI is processed via zotero_add_by_doi with full metadata + OA PDF download. "
        "Returns a summary of successes and failures. "
        "Example: zotero_batch_add_by_doi(dois='10.1234/abc,10.5678/def')."
    ),
)
@with_zotero_api_lock
async def batch_add_by_doi(
    dois: str,
    tags: list[str] | str | None = None,
    collection_key: str | None = None,
    *,
    ctx: Context,
) -> str:
    """Add multiple papers by DOI."""
    try:
        doi_list = [d.strip() for d in dois.replace(";", ",").split(",") if d.strip()]
        if not doi_list:
            return "Error: No DOIs provided."
        if len(doi_list) > 10:
            return f"Error: Too many DOIs ({len(doi_list)}). Max 10 per batch."

        tags = _helpers._normalize_str_list_input(tags, "tags") if tags is not None else []
    except Exception as e:
        return f"Error parsing input: {e}"

    results = []
    for doi in doi_list:
        try:
            # Call the existing add_by_doi function
            result = await add_by_doi(
                doi=doi,
                tags=tags,
                collections=collection_key,
                ctx=ctx,
            )
            results.append(f"**{doi}**: {result[:100]}")
        except Exception as e:
            results.append(f"**{doi}**: Error — {e}")

    successes = sum(1 for r in results if "Successfully" in r or "Added" in r)
    return f"# Batch Add Results ({successes}/{len(doi_list)} succeeded)\n\n" + "\n\n".join(results)


@mcp.tool(
    name="zotero_rename_collection",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False),
    description=(
        "Rename an existing Zotero collection. "
        "collection_key: the 8-character collection key. "
        "new_name: the new name for the collection. "
        "Scope: active library only (web API required for writes). "
        "Example: zotero_rename_collection(collection_key='ABC12345', new_name='Machine Learning Papers')."
    ),
)
@with_zotero_api_lock
async def rename_collection(collection_key: str, new_name: str, *, ctx: Context) -> str:
    """Rename a Zotero collection."""
    try:
        key_err = _helpers.validate_item_key(collection_key)
        if key_err:
            return f"Error: {key_err}"
        if not new_name.strip():
            return "Error: Collection name cannot be empty."

        _, write_zot = _helpers._get_write_client(ctx)
    except ValueError as e:
        return str(e)

    try:
        await ctx.info(f"Renaming collection {collection_key} to '{new_name}'")

        # Get the collection
        try:
            collection = await _client.run_zotero_call(
                write_zot.collection, collection_key, operation=f"write_zot.collection({collection_key})"
            )
        except Exception:
            return f"Error: Collection not found: {collection_key}"

        # Update the name
        collection["data"]["name"] = new_name.strip()
        resp = await _client.run_zotero_call(
            write_zot.update_collection, collection, operation=f"write_zot.update_collection({collection_key})"
        )
        if await _helpers._handle_write_response(resp, ctx):
            await _helpers._notify_library_changed(ctx)
            return f"Renamed collection to '{new_name.strip()}' (key: {collection_key})"
        return f"Failed to rename collection: {resp}"

    except Exception as e:
        await ctx.error(f"Error renaming collection: {str(e)}")
        return f"Error renaming collection: {str(e)}"


@mcp.tool(
    name="zotero_delete_collection",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False),
    description=(
        "Delete a Zotero collection. Items in the collection are NOT deleted — "
        "they remain in the library. Only the collection itself is removed. "
        "collection_key: the 8-character collection key. "
        "Scope: active library only (web API required for writes). "
        "Example: zotero_delete_collection(collection_key='ABC12345')."
    ),
)
@with_zotero_api_lock
async def delete_collection(collection_key: str, *, ctx: Context) -> str:
    """Delete a Zotero collection (items are preserved)."""
    try:
        key_err = _helpers.validate_item_key(collection_key)
        if key_err:
            return f"Error: {key_err}"

        _, write_zot = _helpers._get_write_client(ctx)
    except ValueError as e:
        return str(e)

    try:
        await ctx.info(f"Deleting collection {collection_key}")

        # Get the collection to check version
        try:
            collection = await _client.run_zotero_call(
                write_zot.collection, collection_key, operation=f"write_zot.collection({collection_key})"
            )
            name = collection["data"].get("name", "Unnamed")
        except Exception:
            return f"Error: Collection not found: {collection_key}"

        # Delete the collection
        try:
            resp = await _client.run_zotero_call(
                write_zot.delete_collection, collection, operation=f"write_zot.delete_collection({collection_key})"
            )
            if resp and hasattr(resp, "status_code") and resp.status_code in (200, 204):
                await _helpers._notify_library_changed(ctx)
                return f"Deleted collection '{name}' (key: {collection_key}). Items were preserved."
            return f"Failed to delete collection: {resp}"
        except Exception as e:
            return f"Error deleting collection: {e}"

    except Exception as e:
        await ctx.error(f"Error deleting collection: {str(e)}")
        return f"Error deleting collection: {str(e)}"


@mcp.tool(
    name="zotero_merge_tags",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False),
    description=(
        "Merge multiple tags into a single canonical tag. All items with any of "
        "the source tags will have those tags replaced with the target tag. "
        "source_tags: comma-separated list of tags to merge from. "
        "target_tag: the canonical tag to merge into. "
        "Example: zotero_merge_tags(source_tags='ML,machine-learning,machine learning', target_tag='machine learning')."
    ),
)
@with_zotero_api_lock
async def merge_tags(source_tags: str, target_tag: str, *, ctx: Context) -> str:
    """Merge multiple tags into one canonical tag."""
    try:
        tags = [t.strip() for t in source_tags.replace(";", ",").split(",") if t.strip()]
        if not tags:
            return "Error: No source tags provided."
        if not target_tag.strip():
            return "Error: Target tag cannot be empty."
        target = target_tag.strip()

        _, write_zot = _helpers._get_write_client(ctx)
    except ValueError as e:
        return str(e)

    try:
        await ctx.info(f"Merging tags {tags} into '{target}'")
        total_renamed = 0
        errors = []

        for tag in tags:
            if tag == target:
                continue
            try:
                # Use the existing rename_tag functionality
                result = await rename_tag(old_tag=tag, new_tag=target, ctx=ctx)
                if "Renamed" in result or "renamed" in result:
                    # Extract count from result
                    import re as _re

                    match = _re.search(r"(\d+)", result)
                    if match:
                        total_renamed += int(match.group(1))
                elif "No items" in result:
                    pass  # Tag doesn't exist, skip
                else:
                    errors.append(f"'{tag}': {result[:80]}")
            except Exception as e:
                errors.append(f"'{tag}': {e}")

        result = f"Merged {len(tags)} tags into '{target}' — updated {total_renamed} item(s)"
        if errors:
            result += f"\nErrors: {'; '.join(errors[:5])}"
        return result

    except Exception as e:
        await ctx.error(f"Error merging tags: {str(e)}")
        return f"Error merging tags: {str(e)}"


@mcp.tool(
    name="zotero_delete_saved_search",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False),
    description=(
        "Delete a saved search from Zotero. "
        "search_name: the exact name of the saved search to delete (case-insensitive). "
        "Scope: active library only (web API required for writes). "
        "Example: zotero_delete_saved_search(search_name='My Old Search')."
    ),
)
@with_zotero_api_lock
async def delete_saved_search(search_name: str, *, ctx: Context) -> str:
    """Delete a saved search by name."""
    from zotero_mcp.local_db import get_local_zotero_reader

    reader = get_local_zotero_reader()
    if not reader:
        return "Saved searches are only available in local mode (ZOTERO_LOCAL=true)."
    try:
        searches = reader.get_saved_searches()
        match = None
        for s in searches:
            if s["name"].lower() == search_name.lower():
                match = s
                break
        if not match:
            names = [s["name"] for s in searches]
            return f"No saved search named '{search_name}'. Available: {', '.join(names) if names else 'none'}"

        # Try to delete via API
        try:
            _, write_zot = _helpers._get_write_client(ctx)
            from pyzotero.zotero import build_url

            search_key = match["key"]
            url = build_url(
                write_zot.endpoint,
                f"/{write_zot.library_type}/{write_zot.library_id}/searches/{search_key}",
            )
            resp = write_zot.client.delete(url=url)
            if resp.status_code in (200, 204):
                await _helpers._notify_library_changed(ctx)
                return f"Deleted saved search '{match['name']}' (key: {search_key})"
            return f"Failed to delete saved search (HTTP {resp.status_code}): {resp.text[:200]}"
        except ValueError:
            return (
                "Error: Web API client required for deleting saved searches. Set ZOTERO_API_KEY and ZOTERO_LIBRARY_ID."
            )

    except Exception as e:
        await ctx.error(f"Error deleting saved search: {str(e)}")
        return f"Error deleting saved search: {str(e)}"
    finally:
        reader.close()


@mcp.tool(
    name="zotero_delete_tags",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False),
    description=(
        "Delete tags from the Zotero library. Removes the tag from all items "
        "that use it, then deletes the tag itself. "
        "tags: comma-separated list of tags to delete. "
        "Scope: active library only (web API required). "
        "Example: zotero_delete_tags(tags='old-tag,unused-tag')."
    ),
)
@with_zotero_api_lock
async def delete_tags(tags: str, *, ctx: Context) -> str:
    """Delete tags from the Zotero library."""
    try:
        tag_list = [t.strip() for t in tags.replace(";", ",").split(",") if t.strip()]
        if not tag_list:
            return "Error: No tags provided."

        _, write_zot = _helpers._get_write_client(ctx)
    except ValueError as e:
        return str(e)

    try:
        await ctx.info(f"Deleting {len(tag_list)} tag(s)")
        from pyzotero.zotero import build_url

        deleted = []
        errors = []

        for tag in tag_list:
            try:
                # URL-encode the tag name
                import urllib.parse

                encoded_tag = urllib.parse.quote(tag, safe="")
                url = build_url(
                    write_zot.endpoint,
                    f"/{write_zot.library_type}/{write_zot.library_id}/tags/{encoded_tag}",
                )
                resp = write_zot.client.delete(url=url)
                if resp.status_code in (200, 204):
                    deleted.append(tag)
                elif resp.status_code == 404:
                    errors.append(f"'{tag}': not found")
                else:
                    errors.append(f"'{tag}': HTTP {resp.status_code}")
            except Exception as e:
                errors.append(f"'{tag}': {e}")

        if deleted:
            await _helpers._notify_library_changed(ctx)

        result = f"Deleted {len(deleted)} tag(s)"
        if deleted:
            result += f": {', '.join(deleted)}"
        if errors:
            result += f"\nErrors: {'; '.join(errors[:5])}"
        return result

    except Exception as e:
        await ctx.error(f"Error deleting tags: {str(e)}")
        return f"Error deleting tags: {str(e)}"


@mcp.tool(
    name="zotero_search_papers",
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True),
    description=(
        "Search for academic papers across Semantic Scholar and OpenAlex. "
        "Returns DOIs, titles, authors, year, venue. "
        "Use add_to_zotero=true with specific DOIs to add papers to the library. "
        "Good alternative to Google Scholar (which blocks automated access). "
        "Works well for Indonesian and international papers."
    ),
)
async def search_papers(
    query: str,
    limit: int | str = 10,
    source: Literal["semantic_scholar", "openalex", "both"] = "both",
    add_to_zotero: bool = False,
    *,
    ctx: Context,
) -> str:
    """
    Search for academic papers using Semantic Scholar and/or OpenAlex.

    Args:
        query: Search query (can be in any language)
        limit: Max results per source (default 10, max 50)
        source: Which API to search: "semantic_scholar", "openalex", or "both"
        add_to_zotero: If True, add found papers to Zotero by DOI
        ctx: MCP context

    Returns:
        Formatted list of papers found
    """
    try:
        limit = _helpers._normalize_limit(limit, max_val=50)
    except ValueError as e:
        return str(e)

    results: list[dict] = []

    # Search Semantic Scholar
    if source in ("semantic_scholar", "both"):
        try:
            ss_results = await _search_semantic_scholar(query, limit)
            results.extend(ss_results)
        except Exception as e:
            await ctx.error(f"Semantic Scholar error: {e}")

    # Search OpenAlex
    if source in ("openalex", "both"):
        try:
            oa_results = await _search_openalex(query, limit)
            results.extend(oa_results)
        except Exception as e:
            await ctx.error(f"OpenAlex error: {e}")

    if not results:
        return "No papers found for your query."

    # Deduplicate by DOI
    seen_dois: set[str] = set()
    unique_results: list[dict] = []
    for paper in results:
        doi = paper.get("doi")
        if doi and doi in seen_dois:
            continue
        if doi:
            seen_dois.add(doi)
        unique_results.append(paper)

    # Add to Zotero if requested
    added_count = 0
    if add_to_zotero:
        dois_to_add = [p["doi"] for p in unique_results if p.get("doi")]
        for doi in dois_to_add:
            try:
                result = await add_by_doi(doi=doi, ctx=ctx)
                if "Added" in result or "already in your library" in result or "Successfully added" in result:
                    added_count += 1
            except Exception as e:
                await ctx.error(f"Failed to add {doi}: {e}")

    # Format output
    output_lines = [f"Found {len(unique_results)} papers:"]
    for i, paper in enumerate(unique_results, 1):
        doi = paper.get("doi", "No DOI")
        title = paper.get("title", "Untitled")
        authors = ", ".join(paper.get("authors", [])[:3])
        if len(paper.get("authors", [])) > 3:
            authors += " et al."
        year = paper.get("year", "Unknown")
        venue = paper.get("venue", "")
        source_name = paper.get("source", "")

        output_lines.append(f"\n{i}. {title}")
        output_lines.append(f"   Authors: {authors}")
        output_lines.append(f"   Year: {year}")
        if venue:
            output_lines.append(f"   Venue: {venue}")
        output_lines.append(f"   DOI: {doi}")
        output_lines.append(f"   Source: {source_name}")

    if add_to_zotero:
        output_lines.append(f"\nAdded {added_count} papers to Zotero.")

    return "\n".join(output_lines)


async def _search_semantic_scholar(query: str, limit: int) -> list[dict]:
    """Search Semantic Scholar API for papers."""
    url = "https://api.semanticscholar.org/graph/v1/paper/search"
    params = {
        "query": query,
        "limit": min(limit, 100),
        "fields": "title,authors,year,venue,externalIds",
    }

    loop = asyncio.get_event_loop()
    resp = await loop.run_in_executor(
        None,
        lambda: rate_limited_get("semantic_scholar", url, params=params, timeout=15),
    )
    resp.raise_for_status()
    data = resp.json()

    results = []
    for paper in data.get("data", []):
        doi = paper.get("externalIds", {}).get("DOI")
        authors = [a.get("name", "") for a in paper.get("authors", [])]
        results.append(
            {
                "title": paper.get("title"),
                "doi": doi,
                "authors": authors,
                "year": paper.get("year"),
                "venue": paper.get("venue"),
                "source": "Semantic Scholar",
            }
        )
    return results


async def _search_openalex(query: str, limit: int) -> list[dict]:
    """Search OpenAlex API for papers."""
    url = "https://api.openalex.org/works"
    params = {
        "search": query,
        "per_page": min(limit, 100),
        "select": "id,doi,title,publication_year,authorships,primary_location",
    }

    loop = asyncio.get_event_loop()
    resp = await loop.run_in_executor(
        None,
        lambda: rate_limited_get("openalex", url, params=params, timeout=15),
    )
    resp.raise_for_status()
    data = resp.json()

    results = []
    for work in data.get("results", []):
        doi_raw = work.get("doi", "")
        doi = doi_raw.replace("https://doi.org/", "") if doi_raw else None

        authors = []
        for authorship in work.get("authorships", [])[:5]:
            author_name = authorship.get("author", {}).get("display_name", "")
            if author_name:
                authors.append(author_name)

        venue = None
        primary_loc = work.get("primary_location", {})
        if primary_loc and primary_loc.get("source"):
            venue = primary_loc["source"].get("display_name")

        results.append(
            {
                "title": work.get("title"),
                "doi": doi,
                "authors": authors,
                "year": work.get("publication_year"),
                "venue": venue,
                "source": "OpenAlex",
            }
        )

    return results


# ---------------------------------------------------------------------------
# Import-by-citation tools (BibTeX / CSL JSON)
# ---------------------------------------------------------------------------

_CITATION_FILE_MAX_BYTES = 10 * 1024 * 1024  # 10 MB — generous for citation files


def _read_citation_file(file_path: str, allowed_exts: set[str]) -> str:
    """Read a citation file as UTF-8 text with the same safety checks as add_from_file.

    Raises ValueError on any check failure. Returns the file contents.
    """
    if os.path.islink(file_path):
        raise ValueError("Symlinks are not allowed for security reasons.")
    if not os.path.isabs(file_path):
        raise ValueError("file_path must be an absolute path.")
    resolved = os.path.realpath(file_path)
    if not os.path.isfile(resolved):
        raise ValueError(f"File not found: {file_path}")

    ext = os.path.splitext(resolved)[1].lower()
    if ext not in allowed_exts:
        raise ValueError(
            f"Unsupported file extension '{ext}'. Allowed: {', '.join(sorted(allowed_exts))}"
        )

    size = os.path.getsize(resolved)
    if size > _CITATION_FILE_MAX_BYTES:
        raise ValueError(f"File is too large ({size} bytes). Maximum {_CITATION_FILE_MAX_BYTES} bytes.")

    try:
        with open(resolved, encoding="utf-8") as f:
            return f.read()
    except UnicodeDecodeError as e:
        raise ValueError(f"File is not valid UTF-8: {e}") from e


def _apply_caller_tags_and_collections(
    item_data: dict,
    caller_tags: list[str] | str | None,
    caller_collections: list[str] | str | None,
) -> None:
    """Merge caller tags with any source-tags already in ``item_data`` and set collections."""
    extra_tags = _helpers._normalize_str_list_input(caller_tags, "tags")
    source_tags = [t.get("tag", "") for t in item_data.get("tags", []) if t.get("tag")]
    merged = _citation_import.merge_tags(source_tags, extra_tags)
    if merged:
        item_data["tags"] = [{"tag": t} for t in merged]

    coll_keys = _helpers._normalize_str_list_input(caller_collections, "collections")
    if coll_keys:
        existing = list(item_data.get("collections") or [])
        seen = set(existing)
        for k in coll_keys:
            if k not in seen:
                existing.append(k)
                seen.add(k)
        item_data["collections"] = existing


def _create_and_attach(
    write_zot,
    item_data: dict,
    attach_mode: str,
    ctx: Context,
) -> dict:
    """Create one Zotero item and, if it has a DOI, try to attach an OA PDF."""
    title = item_data.get("title") or "(untitled)"
    try:
        result = write_zot.create_items([item_data])
    except Exception as e:
        return {
            "ok": False,
            "key": None,
            "doi": None,
            "pdf_status": None,
            "error": str(e),
            "title": title,
            "collections_failed": [],
        }

    if not (isinstance(result, dict) and result.get("success")):
        return {
            "ok": False,
            "key": None,
            "doi": None,
            "pdf_status": None,
            "error": f"create_items failed: {result}",
            "title": title,
            "collections_failed": [],
        }

    item_key = next(iter(result["success"].values()))

    # #235 backstop: atomic filing via item["collections"] is intermittent.
    collections_failed = _helpers.ensure_collection_membership(
        write_zot, item_key, item_data.get("collections") or [], ctx=ctx
    )

    doi_raw = item_data.get("DOI") or ""
    doi = _helpers._normalize_doi(doi_raw) if doi_raw else None

    pdf_status = None
    if doi:
        try:
            # Fork made _try_attach_oa_pdf async; bridge for sync callers.
            coro = _helpers._try_attach_oa_pdf(
                write_zot, item_key, doi, ctx, attach_mode=attach_mode
            )
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                pdf_status = asyncio.run(coro)
            else:
                # Already inside an event loop — run in a worker thread.
                import concurrent.futures

                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    pdf_status = pool.submit(asyncio.run, coro).result()
        except Exception as e:
            pdf_status = f"OA PDF attach failed: {e}"

    return {
        "ok": True,
        "key": item_key,
        "doi": doi,
        "pdf_status": pdf_status,
        "error": None,
        "title": title,
        "collections_failed": collections_failed,
    }


def _maybe_reuse_existing(read_zot, write_zot, item_data, coll_keys, tags, if_exists, ctx) -> dict | None:
    """Batch-import dedup: reuse an existing item matching the entry's DOI."""
    if if_exists == "duplicate":
        return None
    doi_raw = item_data.get("DOI") or ""
    doi = _helpers._normalize_doi(doi_raw) if doi_raw else None
    if not doi:
        return None
    existing = _helpers.find_existing_items(read_zot, doi=doi, ctx=ctx)
    if not existing:
        return None

    item = existing[0]
    if if_exists == "skip":
        return {
            "ok": True,
            "key": item.get("key"),
            "doi": doi,
            "pdf_status": None,
            "error": None,
            "title": item.get("data", {}).get("title") or "(untitled)",
            "collections_failed": [],
            "existed": "skipped — already in library",
        }

    summary = _converge_existing_item(write_zot, item, coll_keys, tags, ctx)
    bits = []
    if summary["colls_added"]:
        bits.append(f"added to {summary['colls_added']}")
    if summary["colls_already"]:
        bits.append(f"already in {summary['colls_already']}")
    if summary["tags_added"]:
        bits.append(f"tags added {summary['tags_added']}")
    detail = "; ".join(bits) if bits else "already in requested state"
    return {
        "ok": True,
        "key": summary["key"],
        "doi": doi,
        "pdf_status": None,
        "error": None,
        "title": summary["title"],
        "collections_failed": summary["colls_failed"],
        "existed": f"reused existing — {detail}",
    }


def _format_batch_result(header: str, results: list[dict]) -> str:
    """Render a per-entry markdown summary for add_by_bibtex / add_by_csl_json."""
    ok_count = sum(1 for r in results if r["ok"])
    reused_count = sum(1 for r in results if r["ok"] and r.get("existed"))
    lines = [header, ""]
    if len(results) == 1:
        r = results[0]
        if r["ok"]:
            verb = "Already in library" if r.get("existed") else "Successfully added"
            lines.append(f"{verb}: **{r['title']}**")
            lines.append("")
            lines.append(f"Item key: `{r['key']}`")
            if r["doi"]:
                lines.append(f"DOI: {r['doi']}")
            if r.get("existed"):
                lines.append(f"Status: {r['existed']}")
            if r["pdf_status"]:
                lines.append(f"PDF: {r['pdf_status']}")
            if r.get("collections_failed"):
                lines.append(f"WARNING: failed to file in {r['collections_failed']}")
        else:
            lines.append(f"Failed to add **{r['title']}**: {r['error']}")
    else:
        summary_line = f"Added {ok_count - reused_count}/{len(results)} items."
        if reused_count:
            summary_line += f" {reused_count} already existed (reused, not duplicated)."
        lines.append(summary_line)
        lines.append("")
        for i, r in enumerate(results, 1):
            if r["ok"]:
                line = f"{i}. `{r['key']}` — {r['title']}"
                if r["doi"]:
                    line += f" (DOI: {r['doi']})"
                if r.get("existed"):
                    line += f" [{r['existed']}]"
                if r["pdf_status"]:
                    line += f" [{r['pdf_status']}]"
                if r.get("collections_failed"):
                    line += f" [failed to file in {r['collections_failed']}]"
                lines.append(line)
            else:
                lines.append(f"{i}. ❌ {r['title']}: {r['error']}")
    lines.append("")
    lines.append(
        "_Note: To include new items in semantic search, run zotero_update_search_database._"
    )
    return "\n".join(lines)


@mcp.tool(
    name="zotero_add_by_bibtex",
    description=(
        "Add one or more items to Zotero from BibTeX. "
        "Provide EITHER `bibtex` (inline string) OR `file_path` "
        "(absolute path to a .bib / .bibtex file) — not both. "
        "Supports multiple @entries per call. "
        "The citation key from each entry is preserved in the Extra field. "
        "If an entry has a DOI, an open-access PDF attachment is attempted. "
        "collections accepts keys, names, or '/'-paths (validated before "
        "create). if_exists: 'duplicate' (default) | 'file' (entries whose "
        "DOI already exists reuse that item — add missing collections/tags "
        "instead of duplicating) | 'skip' (leave existing matches "
        "untouched); entries without a DOI always create. "
        "create_missing_collections: create unknown collection specs."
    ),
)
@with_zotero_api_lock
def add_by_bibtex(
    bibtex: str | None = None,
    file_path: str | None = None,
    collections: list[str] | str | None = None,
    tags: list[str] | str | None = None,
    attach_mode: str = "auto",
    if_exists: Literal["duplicate", "file", "skip"] = "duplicate",
    create_missing_collections: bool = False,
    *,
    ctx: Context,
) -> str:
    try:
        _read_zot, write_zot = _helpers._get_write_client(ctx)
    except ValueError as e:
        return str(e)

    try:
        if if_exists not in _IF_EXISTS_VALUES:
            return f"Error: if_exists must be one of {_IF_EXISTS_VALUES}."
        bibtex_provided = bool((bibtex or "").strip())
        if bibtex_provided and file_path:
            return "Error: Provide either `bibtex` or `file_path`, not both."
        if not bibtex_provided and not file_path:
            return "Error: Must provide `bibtex` (inline string) or `file_path`."

        if file_path:
            try:
                bibtex = _read_citation_file(file_path, allowed_exts={".bib", ".bibtex"})
            except ValueError as e:
                return f"Error: {e}"
            ctx.info(f"Loaded BibTeX from {file_path} ({len(bibtex)} bytes)")

        try:
            entries = _citation_import.parse_bibtex(bibtex)
        except Exception as e:
            return f"Error parsing BibTeX: {e}"

        if not entries:
            return "Error: No valid @entries found in the BibTeX input."

        try:
            coll_keys = _resolve_collections_arg(
                _read_zot,
                collections,
                ctx,
                create_missing=create_missing_collections,
                write_zot=write_zot,
            )
        except ValueError as e:
            return f"Error: {e}"

        ctx.info(f"Parsed {len(entries)} BibTeX entries")

        results = []
        for entry in entries:
            try:
                item_data = _citation_import.bibtex_entry_to_zotero(entry, write_zot.item_template)
            except Exception as e:
                results.append(
                    {
                        "ok": False,
                        "key": None,
                        "doi": None,
                        "pdf_status": None,
                        "error": f"conversion failed: {e}",
                        "title": entry.get("citekey") or "(unknown)",
                    }
                )
                continue

            reused = _maybe_reuse_existing(_read_zot, write_zot, item_data, coll_keys, tags, if_exists, ctx)
            if reused is not None:
                results.append(reused)
                continue

            _apply_caller_tags_and_collections(item_data, tags, coll_keys)
            results.append(_create_and_attach(write_zot, item_data, attach_mode, ctx))

        return _format_batch_result("# zotero_add_by_bibtex", results)

    except Exception as e:
        ctx.error(f"Error adding by BibTeX: {e}")
        return f"Error adding by BibTeX: {e}"


@mcp.tool(
    name="zotero_add_by_csl_json",
    description=(
        "Add one or more items to Zotero from CSL JSON. "
        "Provide EITHER `csl_json` (inline — a JSON string, object, or array) "
        "OR `file_path` (absolute path to a .json / .csljson file) — not both. "
        "The `id` field is preserved in the Extra field as the Citation Key. "
        "If an entry has a DOI, an open-access PDF attachment is attempted. "
        "collections accepts keys, names, or '/'-paths (validated before "
        "create). if_exists: 'duplicate' (default) | 'file' (entries whose "
        "DOI already exists reuse that item — add missing collections/tags) "
        "| 'skip'; entries without a DOI always create. "
        "create_missing_collections: create unknown collection specs."
    ),
)
@with_zotero_api_lock
def add_by_csl_json(
    csl_json: str | list | dict | None = None,
    file_path: str | None = None,
    collections: list[str] | str | None = None,
    tags: list[str] | str | None = None,
    attach_mode: str = "auto",
    if_exists: Literal["duplicate", "file", "skip"] = "duplicate",
    create_missing_collections: bool = False,
    *,
    ctx: Context,
) -> str:
    try:
        _read_zot, write_zot = _helpers._get_write_client(ctx)
    except ValueError as e:
        return str(e)

    try:
        if if_exists not in _IF_EXISTS_VALUES:
            return f"Error: if_exists must be one of {_IF_EXISTS_VALUES}."
        csl_provided = csl_json not in (None, "", [], {})
        if csl_provided and file_path:
            return "Error: Provide either `csl_json` or `file_path`, not both."
        if not csl_provided and not file_path:
            return "Error: Must provide `csl_json` (inline) or `file_path`."

        if file_path:
            try:
                csl_json = _read_citation_file(file_path, allowed_exts={".json", ".csljson"})
            except ValueError as e:
                return f"Error: {e}"
            ctx.info(f"Loaded CSL JSON from {file_path} ({len(csl_json)} bytes)")

        try:
            entries = _citation_import.coerce_csl_json_input(csl_json)
        except ValueError as e:
            return f"Error: {e}"

        if not entries:
            return "Error: No valid CSL JSON objects provided."

        try:
            coll_keys = _resolve_collections_arg(
                _read_zot,
                collections,
                ctx,
                create_missing=create_missing_collections,
                write_zot=write_zot,
            )
        except ValueError as e:
            return f"Error: {e}"

        ctx.info(f"Processing {len(entries)} CSL JSON entries")

        results = []
        for entry in entries:
            try:
                item_data = _citation_import.csl_json_to_zotero(entry, write_zot.item_template)
            except Exception as e:
                results.append(
                    {
                        "ok": False,
                        "key": None,
                        "doi": None,
                        "pdf_status": None,
                        "error": f"conversion failed: {e}",
                        "title": str(entry.get("id") or entry.get("title") or "(unknown)"),
                    }
                )
                continue

            reused = _maybe_reuse_existing(_read_zot, write_zot, item_data, coll_keys, tags, if_exists, ctx)
            if reused is not None:
                results.append(reused)
                continue

            _apply_caller_tags_and_collections(item_data, tags, coll_keys)
            results.append(_create_and_attach(write_zot, item_data, attach_mode, ctx))

        return _format_batch_result("# zotero_add_by_csl_json", results)

    except Exception as e:
        ctx.error(f"Error adding by CSL JSON: {e}")
        return f"Error adding by CSL JSON: {e}"
