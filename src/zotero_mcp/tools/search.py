"""Search-related tool functions for the Zotero MCP server."""

import asyncio
import json
import logging as _logging
import re
import threading as _threading
import time as _time
from typing import Literal

from mcp.types import ToolAnnotations

from zotero_mcp import client as _client
from zotero_mcp import utils as _utils
from zotero_mcp._app import mcp
from zotero_mcp._context import Context
from zotero_mcp.cache import TTLCache
from zotero_mcp.client import with_zotero_api_lock
from zotero_mcp.tools import _helpers

# Search result cache: key = (query, qmode, item_type, limit, frozenset(tag))
# value = list of items. TTL = 60s to avoid stale results.
# Using TTLCache for O(1) eviction and thread safety.
_search_cache = TTLCache(ttl_seconds=60.0, max_size=256)

_search_logger = _logging.getLogger("zotero_mcp.search")

CASCADE_TIMEOUT = 60  # seconds — total budget for the entire fallback cascade

# Pre-search background sync debounce: at most one fire-and-forget sync per
# this many seconds, shared across all semantic_search tool invocations.
_PRESEARCH_SYNC_MIN_INTERVAL = 60.0
_last_presearch_sync_ts: float = 0.0
_presearch_sync_lock = _threading.Lock()


def _maybe_fire_presearch_sync(search) -> None:
    """Schedule a background semantic-search DB update if auto-update is due.

    Runs in a daemon thread so the current tool call returns immediately.
    Intentionally swallows exceptions — a failed background sync must never
    surface as a search-tool error to the user.
    """
    global _last_presearch_sync_ts
    try:
        if not search.should_update_database():
            return
    except Exception:
        return
    now = _time.monotonic()
    with _presearch_sync_lock:
        if now - _last_presearch_sync_ts < _PRESEARCH_SYNC_MIN_INTERVAL:
            return
        _last_presearch_sync_ts = now

    def _run():
        try:
            search.update_database(extract_fulltext=_utils.is_local_mode())
        except Exception as e:
            _search_logger.debug(f"Background pre-search sync failed: {e}")

    _threading.Thread(target=_run, daemon=True, name="zmcp-presearch-sync").start()


@with_zotero_api_lock
def _search_with_variants(
    zot,
    query: str,
    qmode: str,
    limit: int,
    item_type: str = "-attachment",
    tag: list[str] | None = None,
    cascade_start: float | None = None,
    cascade_timeout: float | None = None,
    sort_by: str = "dateAdded",
    sort_direction: str = "desc",
) -> list:
    """Search using multiple query variants, deduplicate by key.

    Generates ASCII, dash-to-space, and umlaut-expanded variants of the query
    and searches for each one.  Results are deduplicated by item key.

    All params (including item_type and tag) are explicitly set on every
    add_parameters call to avoid stale accumulated params in pyzotero.

    If cascade_start and cascade_timeout are provided, checks the budget
    before each API call and bails out if exceeded.

    Early termination: stops trying variants once we have >= limit results,
    saving API calls for queries that already found enough matches.
    """
    variants = _utils._generate_search_variants(query)
    _search_logger.debug(f"[SEARCH] query='{query}' variants={variants}")

    all_items: list[dict] = []
    seen_keys: set[str] = set()
    for variant in variants:
        # Early termination: stop if we already have enough results
        if len(all_items) >= limit:
            _search_logger.debug(f"[SEARCH] Early termination: {len(all_items)} results >= limit {limit}")
            break

        # Check cascade timeout before each API call
        if cascade_start is not None and cascade_timeout is not None:
            if _time.monotonic() - cascade_start > cascade_timeout:
                _search_logger.debug("[SEARCH] Cascade timeout reached, skipping remaining variants")
                break

        params: dict = {
            "q": variant,
            "qmode": qmode,
            "limit": limit,
            "itemType": item_type,
            "sort": sort_by,
            "direction": sort_direction,
        }
        if tag:
            params["tag"] = tag
        zot.add_parameters(**params)
        try:
            t0 = _time.monotonic()
            batch = zot.items()
            elapsed = _time.monotonic() - t0
            _search_logger.debug(f"[SEARCH] variant='{variant}' qmode={qmode}: {len(batch)} results in {elapsed:.2f}s")
            for item in batch:
                key = item.get("key", "")
                if key and key not in seen_keys:
                    seen_keys.add(key)
                    all_items.append(item)
        except Exception as e:
            _search_logger.debug(f"[SEARCH] variant='{variant}' failed: {e}")
            continue  # Skip failed variant, try next

    return all_items


@mcp.tool(
    name="zotero_search_items",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    description="Search for items in your Zotero library, given a query string. Returns metadata and abstracts. IMPORTANT: Use short, simple queries — 'Author Year' (e.g., 'Brewer 2011') or just the author name (e.g., 'Cladder-Micus'). Do NOT add extra keywords like topic words — this is substring matching, not web search. More words make the search STRICTER, not broader. If no results are found, the tool will automatically retry with simplified queries and semantic search. Optionally scope to a specific collection with collection_key. sort_by: 'dateAdded' (default), 'dateModified', 'title', 'creator', 'date', 'publicationTitle'. sort_direction: 'desc' (default) or 'asc'.",
)
@with_zotero_api_lock
async def search_items(
    query: str,
    qmode: Literal["titleCreatorYear", "everything"] = "titleCreatorYear",
    item_type: str = "-attachment",  # Exclude attachments by default
    limit: int | str | None = 10,
    tag: list[str] | list[dict] | str | None = None,
    collection_key: str | None = None,
    sort_by: str = "dateAdded",
    sort_direction: Literal["desc", "asc"] = "desc",
    *,
    ctx: Context,
) -> str:
    """
    Search for items in your Zotero library.

    Args:
        query: Search query string
        qmode: Query mode (titleCreatorYear or everything)
        item_type: Type of items to search for. Use "-attachment" to exclude attachments.
        limit: Maximum number of results to return
        tag: Tag filter. Accepts ["tagA", "tagB"] (preferred), a bare string
            "tagA", a JSON-string list '["tagA", "tagB"]', or the dict-shape
            [{"tag": "tagA"}] sometimes emitted by clients that confuse the
            filter form with Zotero's stored-tag form. All are normalized
            internally to the list[str] form pyzotero expects.
        collection_key: Optional collection key to scope the search to a specific collection.
        sort_by: Sort field (dateAdded, dateModified, title, creator, date, publicationTitle)
        sort_direction: Sort direction (desc or asc)
            When provided, bypasses the fallback cascade and searches the collection directly.
        ctx: MCP context

    Returns:
        Markdown-formatted search results
    """
    try:
        if not query.strip():
            return "Error: Search query cannot be empty"

        # Normalize tag across every wire shape clients produce (#237).
        tag = _helpers._normalize_tag_filter(tag)

        tag_condition_str = ""
        if tag:
            tag_condition_str = f" with tags: '{', '.join(tag)}'"

        await ctx.info(f"Searching Zotero for '{query}'{tag_condition_str}")
        zot = await _client.run_zotero_call(_client.get_zotero_client, operation="get_zotero_client")

        limit = _helpers._normalize_limit(limit, default=10)

        if collection_key:
            # Collection-scoped search — query the collection directly, no cascade needed
            try:
                _col = await _client.run_zotero_call(
                    zot.collection, collection_key, operation=f"zot.collection({collection_key})"
                )
            except Exception:
                _col = None
            if not _col or _col.get("key") != collection_key:
                return f"Collection not found: '{collection_key}'. Use zotero_get_collections or zotero_search_collections to find valid collection keys."
            items = _helpers._paginate(
                zot.collection_items,
                collection_key,
                q=query,
                qmode=qmode,
                itemType=item_type,
                sort=sort_by,
                direction=sort_direction,
                max_items=limit,
                **({"tag": tag} if tag else {}),
            )
            fallback_strategy = None
        else:
            # --- Initial search with variant generation ---
            _cascade_start = _time.monotonic()

            # Check search cache first
            _tag_key = frozenset(tag) if tag else frozenset()
            _cache_key = f"{query}|{qmode}|{item_type}|{limit}|{_tag_key}"
            _cached = _search_cache.get(_cache_key)
            if _cached is not None:
                items = _cached
                _search_logger.debug(f"[SEARCH] Cache hit for '{query}': {len(items)} results")
            else:
                items = _search_with_variants(
                    zot,
                    query,
                    qmode,
                    limit,
                    item_type=item_type,
                    tag=tag,
                    cascade_start=_cascade_start,
                    cascade_timeout=CASCADE_TIMEOUT,
                    sort_by=sort_by,
                    sort_direction=sort_direction,
                )
                # Cache the result (TTLCache handles expiry and eviction)
                _search_cache.set(_cache_key, items)
            _search_logger.debug(
                f"[CASCADE] initial: {len(items)} results in {_time.monotonic() - _cascade_start:.2f}s"
            )

            # --- Fallback cascade (only if initial search returned nothing) ---
            fallback_strategy = None
            _timed_out = False

            async def _check_cascade_timeout():
                nonlocal _timed_out
                if _time.monotonic() - _cascade_start > CASCADE_TIMEOUT:
                    _timed_out = True
                    _search_logger.debug("[CASCADE] Timeout — stopping cascade")
                    await ctx.info("Search took too long — returning best results found so far")
                return _timed_out

            if not items and query.strip():
                await ctx.info("No results with original query, trying fallback strategies...")
                words = query.strip().split()

                # Strategy 1: Simplify to author + year (P2 fix)
                if not await _check_cascade_timeout() and not items and len(words) > 2:
                    # Extract year-like token (4 digits between 1800-2099)
                    year_token = next((w for w in words if re.match(r"^(1[89]\d{2}|20\d{2})$", w)), None)
                    # Extract author (first non-numeric word)
                    author_token = next((w for w in words if not re.match(r"^\d+$", w)), None)

                    if author_token and year_token:
                        simple_query = f"{author_token} {year_token}"
                    elif author_token:
                        simple_query = author_token
                    else:
                        simple_query = words[0]

                    t0 = _time.monotonic()
                    await ctx.info(f"Retry with simplified query: '{simple_query}'")
                    items = _search_with_variants(
                        zot,
                        simple_query,
                        qmode,
                        limit,
                        item_type=item_type,
                        tag=tag,
                        cascade_start=_cascade_start,
                        cascade_timeout=CASCADE_TIMEOUT,
                    )
                    _search_logger.debug(
                        f"[CASCADE] strategy 1 (author+year): {len(items)} results in {_time.monotonic() - t0:.2f}s"
                    )
                    if items:
                        fallback_strategy = f"simplified to '{simple_query}'"

                # Strategy 2: Author surname only (first non-numeric word)
                if not await _check_cascade_timeout() and not items and len(words) >= 2:
                    author_only = next((w for w in words if not re.match(r"^\d+$", w)), words[0])
                    t0 = _time.monotonic()
                    await ctx.info(f"Retry with author only: '{author_only}'")
                    items = _search_with_variants(
                        zot,
                        author_only,
                        qmode,
                        limit,
                        item_type=item_type,
                        tag=tag,
                        cascade_start=_cascade_start,
                        cascade_timeout=CASCADE_TIMEOUT,
                    )
                    _search_logger.debug(
                        f"[CASCADE] strategy 2 (author only): {len(items)} results in {_time.monotonic() - t0:.2f}s"
                    )
                    if items:
                        fallback_strategy = f"author only '{author_only}'"

                # Strategy 3: qmode="everything" (searches full text on Zotero's side)
                # Safe — no tokens consumed, only metadata returned
                if not await _check_cascade_timeout() and not items and qmode != "everything":
                    t0 = _time.monotonic()
                    await ctx.info(f"Retry with qmode='everything': '{query}'")
                    items = _search_with_variants(
                        zot,
                        query,
                        "everything",
                        limit,
                        item_type=item_type,
                        tag=tag,
                        cascade_start=_cascade_start,
                        cascade_timeout=CASCADE_TIMEOUT,
                    )
                    _search_logger.debug(
                        f"[CASCADE] strategy 3 (everything): {len(items)} results in {_time.monotonic() - t0:.2f}s"
                    )
                    if items:
                        fallback_strategy = "full-text search"

                # Strategy 4: Semantic search (if database exists)
                if not await _check_cascade_timeout() and not items:
                    try:
                        from zotero_mcp.config import get_config_path
                        from zotero_mcp.semantic_search import create_semantic_search

                        config_path = get_config_path()
                        if config_path.exists():
                            await ctx.info(f"Retry with semantic search: '{query}'")
                            t0 = _time.monotonic()
                            sem_search = create_semantic_search(str(config_path))
                            _search_logger.debug(f"[CASCADE] semantic init: {_time.monotonic() - t0:.2f}s")
                            t0 = _time.monotonic()
                            sem_results = sem_search.search(query=query, limit=limit or 10)
                            _search_logger.debug(f"[CASCADE] semantic query: {_time.monotonic() - t0:.2f}s")
                            if sem_results and sem_results.get("results"):
                                seen_keys: set[str] = set()
                                for sr in sem_results["results"]:
                                    zot_item = sr.get("zotero_item", {})
                                    key = sr.get("item_key", zot_item.get("key", ""))
                                    if key and key not in seen_keys:
                                        seen_keys.add(key)
                                        if "key" not in zot_item:
                                            zot_item["key"] = key
                                        items.append(zot_item)
                                if items:
                                    fallback_strategy = "semantic search"
                    except Exception as e:
                        _search_logger.debug(f"[CASCADE] semantic failed: {e}")
                        await ctx.info(f"Semantic search fallback failed: {e}")

            _search_logger.debug(
                f"[CASCADE] total: {_time.monotonic() - _cascade_start:.2f}s, fallback={fallback_strategy}"
            )

        # --- No results after all strategies ---
        if not items:
            return f"No items found matching query: '{query}'{tag_condition_str}"

        # --- Format results as markdown ---
        output = [f"# Search Results for '{query}'", f"{tag_condition_str}", ""]

        for i, item in enumerate(items, 1):
            output.extend(_utils.format_item_result(item, index=i))

        # Prepend fallback verification note (AFTER output is built)
        if fallback_strategy:
            if fallback_strategy == "semantic search":
                note_text = (
                    f"*Note: Original search for '{query}' returned no results. "
                    f"The following {len(items)} item(s) are semantically related papers found "
                    f"via AI-powered search — they may be ABOUT the same topic but may NOT be "
                    f"the exact paper you're looking for. The target paper may not be in your "
                    f"library. Verify carefully by checking title, authors, and journal.*"
                )
            else:
                note_text = (
                    f"*Note: Original search for '{query}' returned no results. "
                    f"Found {len(items)} item(s) via {fallback_strategy} — verify the correct one "
                    f"by checking title, authors, journal, and year match your original query.*"
                )
            output.insert(1, "")
            output.insert(2, note_text)
            output.insert(3, "")

        return _helpers._prepend_size_warning("\n".join(output))

    except Exception as e:
        error_msg = str(e)
        suggestion = ""
        if "timeout" in error_msg.lower() or "timed out" in error_msg.lower():
            suggestion = " Try simplifying your search query or checking if Zotero is running."
        elif "connection" in error_msg.lower():
            suggestion = " Check if Zotero is running and the local API is enabled (Settings → Advanced)."
        await ctx.error(f"Error searching Zotero: {error_msg}")
        return f"Error searching Zotero: {error_msg}{suggestion}"


@mcp.tool(
    name="zotero_search_by_tag",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    description="Search for items in your Zotero library by tag, optionally scoped to a collection. "
    "Conditions are ANDed, each term supports disjunction (`OR`) and exclusion (`-`).",
)
@with_zotero_api_lock
async def search_by_tag(
    tag: list[str] | list[dict] | str,
    item_type: str = "-attachment",
    limit: int | str | None = 10,
    collection_key: str | None = None,
    *,
    ctx: Context,
) -> str:
    """
    Search for items in your Zotero library by tag.
    Conditions are ANDed, each term supports disjunction (`OR`) and exclusion (`-`).

    Args:
        tag: List of tag conditions. Items are returned only if they satisfy
            ALL conditions in the list. Each tag condition can be expressed
            in two ways:
                As alternatives: tag1 OR tag2 (matches items with either tag1 OR tag2)
                As exclusions: -tag (matches items that do NOT have this tag)
            For example, a tag field with ["research OR important", "-draft"] would
            return items that:
                Have either "research" OR "important" tags, AND
                Do NOT have the "draft" tag
        item_type: Type of items to search for. Use "-attachment" to exclude attachments.
        limit: Maximum number of results to return
        collection_key: Optional collection key to scope the search to a specific collection
        ctx: MCP context

    Returns:
        Markdown-formatted search results
    """
    try:
        # Normalize tag across every wire shape clients produce (#237).
        tag = _helpers._normalize_tag_filter(tag)
        if not tag:
            return "Error: Tag cannot be empty"

        await ctx.info(f"Searching Zotero for tag '{tag}'")
        zot = await _client.run_zotero_call(_client.get_zotero_client, operation="get_zotero_client")

        limit = _helpers._normalize_limit(limit, default=10)

        # Search library-wide or scoped to a collection
        if collection_key:
            try:
                _col = await _client.run_zotero_call(
                    zot.collection, collection_key, operation=f"zot.collection({collection_key})"
                )
            except Exception:
                _col = None
            if not _col or _col.get("key") != collection_key:
                return f"Collection not found: '{collection_key}'. Use zotero_get_collections or zotero_search_collections to find valid collection keys."
            results = _helpers._paginate(
                zot.collection_items,
                collection_key,
                tag=tag,
                itemType=item_type,
                max_items=limit,
            )
        else:

            def _fetch_tag_results():
                zot.add_parameters(q="", tag=tag, itemType=item_type, limit=limit)
                return zot.items()

            results = await _client.run_zotero_call(_fetch_tag_results, operation="zot.items(tag search)")

        if not results:
            return f"No items found with tag: '{tag}'"

        # Format results as markdown
        scope = f" in Collection {collection_key}" if collection_key else ""
        output = [f"# Search Results for Tag: '{tag}'{scope}", ""]

        for i, item in enumerate(results, 1):
            output.extend(_utils.format_item_result(item, index=i))

        text = "\n".join(output)
        return _helpers._prepend_size_warning(text)

    except Exception as e:
        error_msg = str(e)
        suggestion = ""
        if "timeout" in error_msg.lower() or "timed out" in error_msg.lower():
            suggestion = " Try using a more specific tag or adding a collection filter."
        elif "connection" in error_msg.lower():
            suggestion = " Check if Zotero is running and the local API is enabled."
        await ctx.error(f"Error searching by tag: {error_msg}")
        return f"Error searching by tag: {error_msg}{suggestion}"


@mcp.tool(
    name="zotero_search_by_citation_key",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    description="Look up a Zotero item by its BetterBibTeX citation key (e.g., 'Smith2024'). "
    "Works in local mode via the BetterBibTeX API, or in web mode by searching the Extra field.",
)
@with_zotero_api_lock
async def search_by_citation_key(citekey: str, *, ctx: Context) -> str:
    """
    Look up a Zotero item by its BetterBibTeX citation key.

    Args:
        citekey: The BetterBibTeX citation key to search for (e.g., 'Smith2024')
        ctx: MCP context

    Returns:
        Formatted item details or error message
    """
    try:
        if not citekey.strip():
            return "Error: Citation key cannot be empty"

        citekey = citekey.strip()
        await ctx.info(f"Looking up citation key: {citekey}")

        # Strategy A: Try BetterBibTeX JSON-RPC API (local mode only)
        if _utils.is_local_mode():
            try:
                from zotero_mcp.better_bibtex_client import ZoteroBetterBibTexAPI

                bibtex = ZoteroBetterBibTexAPI()
                if bibtex.is_zotero_running():
                    search_results = bibtex._make_request("item.search", [citekey])
                    if isinstance(search_results, list):
                        matched = next(
                            (
                                item
                                for item in search_results
                                if isinstance(item, dict) and item.get("citekey") == citekey
                            ),
                            None,
                        )
                        if matched:
                            item_key = matched.get("itemKey") or matched.get("key")
                            if item_key:
                                zot = await _client.run_zotero_call(
                                    _client.get_zotero_client, operation="get_zotero_client"
                                )
                                item = await _client.run_zotero_call(
                                    zot.item, item_key, operation=f"zot.item({item_key})"
                                )
                                if item:
                                    return _helpers._format_citekey_result(item, citekey)
                            return _helpers._format_bbt_result(matched, citekey)
            except Exception as e:
                await ctx.warning(f"BetterBibTeX lookup failed, falling back to Extra field search: {e}")

        # Strategy B: Search via pyzotero Extra field
        zot = await _client.run_zotero_call(_client.get_zotero_client, operation="get_zotero_client")
        zot.add_parameters(q=citekey, qmode="everything", itemType="-attachment", limit=25)
        results = await _client.run_zotero_call(zot.items, operation="zot.items(citation key search)")

        for item in results:
            extra = item.get("data", {}).get("extra", "")
            if _helpers._extra_has_citekey(extra, citekey):
                return _helpers._format_citekey_result(item, citekey)

        return f"No item found with citation key: '{citekey}'"

    except Exception as e:
        await ctx.error(f"Error looking up citation key: {str(e)}")
        return f"Error looking up citation key: {str(e)}"


@mcp.tool(
    name="zotero_advanced_search",
    description="Perform an advanced search with multiple criteria.",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
)
@with_zotero_api_lock
async def advanced_search(
    conditions: list[dict[str, str]],
    join_mode: Literal["all", "any"] = "all",
    sort_by: str | None = None,
    sort_direction: Literal["asc", "desc"] = "asc",
    limit: int | str = 50,
    *,
    ctx: Context,
) -> str:
    """
    Perform an advanced search with multiple criteria.

    Args:
        conditions: List of search condition dictionaries, each containing:
                   - field: The field to search (title, creator, date, tag, etc.)
                   - operation: The operation to perform (is, isNot, contains, etc.)
                   - value: The value to search for
        join_mode: Whether all conditions must match ("all") or any condition can match ("any")
        sort_by: Field to sort by (dateAdded, dateModified, title, creator, etc.)
        sort_direction: Direction to sort (asc or desc)
        limit: Maximum number of results to return
        ctx: MCP context

    Returns:
        Markdown-formatted search results
    """
    try:
        if isinstance(conditions, str):
            try:
                conditions = json.loads(conditions)
            except json.JSONDecodeError as parse_error:
                return f"Error: conditions must be valid JSON when provided as a string ({parse_error})"

        if not isinstance(conditions, list) or not conditions:
            return "Error: No search conditions provided"

        if join_mode not in {"all", "any"}:
            return "Error: join_mode must be either 'all' or 'any'"

        limit = _helpers._normalize_limit(limit, default=50, max_val=500)

        await ctx.info(f"Performing advanced search with {len(conditions)} conditions")
        zot = await _client.run_zotero_call(_client.get_zotero_client, operation="get_zotero_client")

        valid_operations = {
            "is",
            "isNot",
            "contains",
            "doesNotContain",
            "beginsWith",
            "endsWith",
            "isGreaterThan",
            "isLessThan",
            "isBefore",
            "isAfter",
            "isTrue",
            "isFalse",
            "isInTheLast",
        }

        parsed_conditions: list[dict[str, str]] = []
        for i, condition in enumerate(conditions, 1):
            if not isinstance(condition, dict):
                return f"Error: Condition {i} must be an object"
            if "field" not in condition or "operation" not in condition or "value" not in condition:
                return f"Error: Condition {i} is missing required fields (field, operation, value)"

            field = str(condition["field"]).strip()
            operation = str(condition["operation"]).strip()
            value = str(condition["value"]).strip()

            if operation not in valid_operations:
                return (
                    f"Error: Unsupported operation '{operation}' in condition {i}. "
                    f"Supported: {', '.join(sorted(valid_operations))}"
                )
            if not field:
                return f"Error: Condition {i} has an empty field"

            parsed_conditions.append({"field": field, "operation": operation, "value": value})

        # Optimization: identify conditions that can be pushed server-side
        # to reduce the number of items fetched from the API.
        server_params: dict = {}
        client_conditions: list[dict[str, str]] = []

        for cond in parsed_conditions:
            field_lower = cond["field"].lower()
            operation = cond["operation"]
            value = cond["value"]

            # Title/creator contains → use q parameter (only one can be used)
            if field_lower in {"title", "creator", "author"} and operation == "contains" and "q" not in server_params:
                server_params["q"] = value
                server_params["qmode"] = "titleCreatorYear"
            # Tag exact match → use tag parameter
            elif field_lower in {"tag", "tags"} and operation == "is" and "tag" not in server_params:
                server_params["tag"] = value
            # Year range → use since/until parameters
            elif field_lower == "year" and operation in {"is", "isAfter", "isBefore"}:
                try:
                    year = int(value)
                    if operation == "is":
                        server_params["since"] = year
                        server_params["until"] = year
                    elif operation == "isAfter":
                        server_params["since"] = year + 1
                    elif operation == "isBefore":
                        server_params["until"] = year - 1
                except ValueError:
                    client_conditions.append(cond)
            else:
                client_conditions.append(cond)

        _search_logger.debug(f"[ADVANCED] server_params={server_params}, client_conditions={len(client_conditions)}")

        def _extract_values(data: dict[str, object], field: str) -> list[str]:
            field_lower = field.lower()

            if field_lower in {"author", "authors", "creator", "creators"}:
                creators = data.get("creators", []) or []
                values: list[str] = []
                for creator in creators:
                    if not isinstance(creator, dict):
                        continue
                    if creator.get("firstName") or creator.get("lastName"):
                        full_name = " ".join(
                            [
                                str(creator.get("firstName", "")).strip(),
                                str(creator.get("lastName", "")).strip(),
                            ]
                        ).strip()
                        if full_name:
                            values.append(full_name)
                    if creator.get("name"):
                        values.append(str(creator.get("name", "")).strip())
                return values

            if field_lower in {"tag", "tags"}:
                tags = data.get("tags", []) or []
                values = []
                for tag in tags:
                    if isinstance(tag, dict) and tag.get("tag"):
                        values.append(str(tag.get("tag", "")).strip())
                return values

            if field_lower == "year":
                date_value = str(data.get("date", "")).strip()
                return [date_value[:4]] if len(date_value) >= 4 else []

            field_aliases = {
                "itemtype": "itemType",
                "dateadded": "dateAdded",
                "datemodified": "dateModified",
                "doi": "DOI",
            }
            source_field = field_aliases.get(field_lower, field)
            raw_value = data.get(source_field, "")
            if raw_value is None:
                return []
            return [str(raw_value).strip()]

        def _as_float(text: str) -> float | None:
            try:
                return float(text)
            except ValueError:
                return None

        def _compare(candidate: str, expected: str, operation: str) -> bool:
            # Normalize both sides for diacritics/dashes before comparison
            left = _utils._normalize_for_search(candidate).lower()
            right = _utils._normalize_for_search(expected).lower()

            if operation == "is":
                return left == right
            if operation == "isNot":
                return left != right
            if operation == "contains":
                return right in left
            if operation == "doesNotContain":
                return right not in left
            if operation == "beginsWith":
                return left.startswith(right)
            if operation == "endsWith":
                return left.endswith(right)

            left_num = _as_float(left)
            right_num = _as_float(right)
            if (
                operation in {"isGreaterThan", "isLessThan", "isBefore", "isAfter"}
                and left_num is not None
                and right_num is not None
            ):
                if operation in {"isGreaterThan", "isAfter"}:
                    return left_num > right_num
                return left_num < right_num

            if operation in {"isGreaterThan", "isAfter"}:
                return left > right
            return left < right

        def _matches_condition(data: dict[str, object], condition: dict[str, str]) -> bool:
            values = _extract_values(data, condition["field"])
            if not values:
                return False

            operation = condition["operation"]
            target = condition["value"]
            comparisons = [_compare(value, target, operation) for value in values]

            if operation in {"isNot", "doesNotContain"}:
                return all(comparisons)
            return any(comparisons)

        # Execute advanced search.
        # Optimization: push itemType filter and simple conditions server-side
        # to reduce the number of items fetched from the API.
        results = []
        batch_size = 100
        start = 0
        while True:
            batch = await _client.run_zotero_call(
                zot.items,
                start=start,
                limit=batch_size,
                itemType="-attachment -note -annotation",
                **server_params,
                operation=f"zot.items(advanced batch start={start})",
            )
            if not batch:
                break

            # Only apply client-side conditions if any remain
            if client_conditions:
                for item in batch:
                    data = item.get("data", {})
                    checks = [_matches_condition(data, c) for c in client_conditions]
                    matched = all(checks) if join_mode == "all" else any(checks)
                    if matched:
                        results.append(item)
                # Early termination: stop fetching once we have enough results
                if len(results) >= limit:
                    break
            else:
                # All conditions were pushed server-side
                results.extend(batch)

            if len(batch) < batch_size:
                break
            start += batch_size

        if sort_by:
            sort_field = sort_by.strip()
            reverse = sort_direction == "desc"

            def _sort_key(item: dict[str, object]) -> str:
                data = item.get("data", {}) if isinstance(item, dict) else {}
                if sort_field in {"creator", "author"}:
                    return _utils.format_creators(data.get("creators", []))
                return str(data.get(sort_field, "")).lower()

            results.sort(key=_sort_key, reverse=reverse)

        if not results:
            return "No items found matching the search criteria."

        results = results[:limit]

        output = ["# Advanced Search Results", ""]
        output.append(f"Found {len(results)} items matching the search criteria:")
        output.append("")
        output.append("## Search Criteria")
        output.append(f"Join mode: {join_mode.upper()}")
        for i, condition in enumerate(parsed_conditions, 1):
            output.append(f'{i}. {condition["field"]} {condition["operation"]} "{condition["value"]}"')
        output.append("")
        output.append("## Results")

        for i, item in enumerate(results, 1):
            output.extend(_utils.format_item_result(item, index=i))

        return "\n".join(output)

    except Exception as e:
        error_msg = str(e)
        suggestion = ""
        if "timeout" in error_msg.lower() or "timed out" in error_msg.lower():
            suggestion = " Try simplifying your search or reducing the number of conditions."
        elif "connection" in error_msg.lower():
            suggestion = " Check if Zotero is running."
        elif "invalid" in error_msg.lower() or "condition" in error_msg.lower():
            suggestion = (
                " Check that condition fields and operators are valid. Use zotero_get_item_fields to see valid fields."
            )
        await ctx.error(f"Error in advanced search: {error_msg}")
        return f"Error in advanced search: {error_msg}{suggestion}"


@mcp.tool(
    name="zotero_semantic_search",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    description="Prioritized search tool. Perform semantic search over your Zotero library using AI-powered embeddings. BEST TOOL for finding papers on a specific topic — much more efficient than scanning collection items or reading abstracts. Works across your entire library.",
)
@with_zotero_api_lock
async def semantic_search(
    query: str, limit: int = 10, filters: dict[str, str] | str | None = None, *, ctx: Context
) -> str:
    """
    Perform semantic search over your Zotero library.

    Args:
        query: Search query text - can be concepts, topics, or natural language descriptions
        limit: Maximum number of results to return (default: 10)
        filters: Optional metadata filters as dict or JSON string. Example: {"item_type": "note"}
        ctx: MCP context

    Returns:
        Markdown-formatted search results with similarity scores
    """
    try:
        if not query.strip():
            return "Error: Search query cannot be empty"

        # Parse and validate filters parameter
        if filters is not None:
            # Handle JSON string input
            if isinstance(filters, str):
                try:
                    filters = json.loads(filters)
                    await ctx.info(f"Parsed JSON string filters: {filters}")
                except json.JSONDecodeError as e:
                    return f"Error: Invalid JSON in filters parameter: {str(e)}"

            # Validate it's a dictionary
            if not isinstance(filters, dict):
                return 'Error: filters parameter must be a dictionary or JSON string. Example: {"item_type": "note"}'

            # Automatically translate common field names
            if "itemType" in filters:
                filters["item_type"] = filters.pop("itemType")
                await ctx.info(f"Automatically translated 'itemType' to 'item_type': {filters}")

            # Additional field name translations can be added here
            # Example: if "creatorType" in filters:
            #     filters["creator_type"] = filters.pop("creatorType")

        await ctx.info(f"Performing semantic search for: '{query}'")

        # Import semantic search module
        try:
            from zotero_mcp.semantic_search import create_semantic_search
        except ImportError:
            return (
                "Semantic search is not available. Install the required packages with:\n"
                '  pip install "zotero-mcp-server[semantic] @ git+https://github.com/its-benjamin/zotero-mcp.git@v0.3.1"\n\n'
                "This installs chromadb, sentence-transformers, and related dependencies."
            )

        # Determine config path
        from zotero_mcp.config import get_config_path

        config_path = get_config_path()

        # Create semantic search instance
        search = create_semantic_search(str(config_path))

        # Fire-and-forget: if auto-update is due, kick off a background sync
        # so subsequent searches see fresh library state. Never blocks here.
        _maybe_fire_presearch_sync(search)

        # Perform search
        results = search.search(query=query, limit=limit, filters=filters)

        if results.get("error"):
            return f"Semantic search error: {results['error']}"

        search_results = results.get("results", [])

        if not search_results:
            return f"No semantically similar items found for query: '{query}'"

        # Format results as markdown
        output = [f"# Semantic Search Results for '{query}'", ""]
        output.append(f"Found {len(search_results)} similar items:")
        output.append("")

        for i, result in enumerate(search_results, 1):
            similarity_score = result.get("similarity_score", 0)
            zotero_item = result.get("zotero_item", {})

            if zotero_item:
                extra = {"Similarity Score": f"{similarity_score:.3f}"}
                matched_text = result.get("matched_text", "")
                if matched_text:
                    snippet = matched_text[:300] + "..." if len(matched_text) > 300 else matched_text
                    extra["Matched Content"] = snippet
                # Override key from result since it may differ from item["key"]
                zotero_item.setdefault("key", result.get("item_key", ""))
                output.extend(_utils.format_item_result(zotero_item, index=i, extra_fields=extra))
            else:
                # Fallback if full Zotero item not available
                output.append(f"## {i}. Item {result.get('item_key', 'Unknown')}")
                output.append(f"**Similarity Score:** {similarity_score:.3f}")
                if error := result.get("error"):
                    output.append(f"**Error:** {error}")
                output.append("")

        return "\n".join(output)

    except Exception as e:
        error_msg = str(e)
        suggestion = ""
        if "not installed" in error_msg.lower() or "no module" in error_msg.lower():
            suggestion = " Install semantic search with: pip install 'zotero-mcp-server[semantic]'"
        elif "not configured" in error_msg.lower() or "config" in error_msg.lower():
            suggestion = " Run 'zotero-mcp setup' to configure semantic search."
        elif "timeout" in error_msg.lower():
            suggestion = " The search may be processing. Try a simpler query or check if ChromaDB is running."
        elif "connection" in error_msg.lower():
            suggestion = " Check if Zotero is running."
        await ctx.error(f"Error in semantic search: {error_msg}")
        return f"Error in semantic search: {error_msg}{suggestion}"


@mcp.tool(
    name="zotero_update_search_database",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False),
    description=(
        "Update the semantic search database with latest Zotero items. "
        "Run this after adding items (via add_by_doi, add_by_url, or add_from_file) "
        "to make them immediately available for semantic search. Also useful if the "
        "user has added items directly in Zotero since the last update."
    ),
)
@with_zotero_api_lock
async def update_search_database(force_rebuild: bool = False, limit: int | None = None, *, ctx: Context) -> str:
    """
    Update the semantic search database.

    Args:
        force_rebuild: Whether to rebuild the entire database from scratch
        limit: Limit number of items to process (useful for testing)
        ctx: MCP context

    Returns:
        Update status and statistics
    """
    try:
        await ctx.info("Starting semantic search database update...")

        # Import semantic search module
        try:
            from zotero_mcp.semantic_search import create_semantic_search
        except ImportError:
            return (
                "Semantic search is not available. Install the required packages with:\n"
                '  pip install "zotero-mcp-server[semantic] @ git+https://github.com/its-benjamin/zotero-mcp.git@v0.3.1"\n\n'
                "This installs chromadb, sentence-transformers, and related dependencies."
            )

        # Determine config path
        from zotero_mcp.config import get_config_path

        config_path = get_config_path()

        # Create semantic search instance
        search = create_semantic_search(str(config_path))

        # Use fulltext extraction when in local mode (has access to PDFs)
        loop = asyncio.get_running_loop()

        def _progress(done: int, total: int, message: str) -> None:
            asyncio.run_coroutine_threadsafe(ctx.report_progress(done, total, message), loop)

        stats = await asyncio.to_thread(
            search.update_database,
            force_full_rebuild=force_rebuild,
            limit=limit,
            extract_fulltext=_utils.is_local_mode(),
            progress_callback=_progress,
        )

        # Format results
        output = ["# Database Update Results", ""]

        if stats.get("error"):
            output.append(f"**Error:** {stats['error']}")
        else:
            output.append(f"**Total items:** {stats.get('total_items', 0)}")
            output.append(f"**Processed:** {stats.get('processed_items', 0)}")
            output.append(f"**Added:** {stats.get('added_items', 0)}")
            output.append(f"**Updated:** {stats.get('updated_items', 0)}")
            output.append(f"**Skipped:** {stats.get('skipped_items', 0)}")
            output.append(f"**Errors:** {stats.get('errors', 0)}")
            output.append(f"**Duration:** {stats.get('duration', 'Unknown')}")

            if stats.get("start_time"):
                output.append(f"**Started:** {stats['start_time']}")
            if stats.get("end_time"):
                output.append(f"**Completed:** {stats['end_time']}")

        return "\n".join(output)

    except Exception as e:
        await ctx.error(f"Error updating search database: {str(e)}")
        return f"Error updating search database: {str(e)}"


@mcp.tool(
    name="zotero_get_search_database_status",
    description="Get status information about the semantic search database.",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
)
@with_zotero_api_lock
async def get_search_database_status(*, ctx: Context) -> str:
    """
    Get semantic search database status.

    Args:
        ctx: MCP context

    Returns:
        Database status information
    """
    try:
        await ctx.info("Getting semantic search database status...")

        # Import semantic search module
        try:
            from zotero_mcp.semantic_search import create_semantic_search
        except ImportError:
            return (
                "Semantic search is not available. Install the required packages with:\n"
                '  pip install "zotero-mcp-server[semantic] @ git+https://github.com/its-benjamin/zotero-mcp.git@v0.3.1"\n\n'
                "This installs chromadb, sentence-transformers, and related dependencies."
            )

        # Determine config path
        from zotero_mcp.config import get_config_path

        config_path = get_config_path()

        # Create semantic search instance
        search = create_semantic_search(str(config_path))

        # Get status
        status = search.get_database_status()

        # Format results
        output = ["# Semantic Search Database Status", ""]

        collection_info = status.get("collection_info", {})
        output.append("## Collection Information")
        output.append(f"**Name:** {collection_info.get('name', 'Unknown')}")
        output.append(f"**Document Count:** {collection_info.get('count', 0)}")
        output.append(f"**Embedding Model:** {collection_info.get('embedding_model', 'Unknown')}")
        output.append(f"**Database Path:** {collection_info.get('persist_directory', 'Unknown')}")

        if collection_info.get("error"):
            output.append(f"**Error:** {collection_info['error']}")

        output.append("")

        update_config = status.get("update_config", {})
        output.append("## Update Configuration")
        output.append(f"**Auto Update:** {update_config.get('auto_update', False)}")
        output.append(f"**Frequency:** {update_config.get('update_frequency', 'manual')}")
        output.append(f"**Last Update:** {update_config.get('last_update', 'Never')}")
        output.append(f"**Should Update Now:** {status.get('should_update', False)}")

        frequency = update_config.get("update_frequency", "manual")
        if frequency.startswith("every_") and update_config.get("update_days"):
            output.append(f"**Update Interval:** Every {update_config['update_days']} days")

        return "\n".join(output)

    except Exception as e:
        await ctx.error(f"Error getting database status: {str(e)}")
        return f"Error getting database status: {str(e)}"


@mcp.tool(
    name="zotero_list_saved_searches",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    description=(
        "List all saved searches in your Zotero library (local mode only). "
        "Shows each search's name and the conditions that define it. "
        "Useful for discovering existing search workflows. "
        "Scope: active library only."
    ),
)
@with_zotero_api_lock
async def list_saved_searches(*, ctx: Context) -> str:
    """List all saved searches from the local Zotero database."""
    from zotero_mcp.local_db import get_local_zotero_reader

    reader = get_local_zotero_reader()
    if not reader:
        return "Saved searches are only available in local mode (ZOTERO_LOCAL=true)."
    try:
        searches = reader.get_saved_searches()
        if not searches:
            return "No saved searches found in your Zotero library."

        output = [f"# Saved Searches ({len(searches)})", ""]
        for s in searches:
            output.append(f"## {s['name']}")
            output.append(f"- Key: `{s['key']}`")
            for cond in s["conditions"]:
                req = " (required)" if cond["required"] == "all" else ""
                output.append(f"  - {cond['condition']} {cond['operator']} `{cond['value']}`{req}")
            output.append("")

        return "\n".join(output)
    except Exception as e:
        await ctx.error(f"Error listing saved searches: {str(e)}")
        return f"Error listing saved searches: {str(e)}"
    finally:
        reader.close()


@mcp.tool(
    name="zotero_execute_saved_search",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    description=(
        "Execute a saved search by name and return matching items (local mode only). "
        "Uses the Zotero Local API's native search execution for accurate results. "
        "search_name: the exact name of the saved search (case-insensitive). "
        "limit: max items to return (default 25). "
        "Scope: active library only."
    ),
)
@with_zotero_api_lock
async def execute_saved_search(
    search_name: str,
    limit: int | str = 25,
    *,
    ctx: Context,
) -> str:
    """Execute a saved search by name using the Local API."""
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
            return f"No saved search named '{search_name}'. Available searches: {', '.join(names) if names else 'none'}"

        limit = _helpers._normalize_limit(limit, default=25)
        search_key = match["key"]

        # Try Local API native search execution first (most accurate)
        try:
            import requests as _requests

            lib_type = "users"
            lib_id = "0"
            active = _client.get_active_library()
            if active:
                lib_type = active.get("library_type", "users")
                lib_id = active.get("library_id", "0")
            resp = _requests.get(
                f"http://localhost:23119/api/{lib_type}/{lib_id}/searches/{search_key}/items",
                params={"limit": limit, "sort": "dateAdded", "direction": "desc"},
                headers={"Zotero-API-Version": "3"},
                timeout=15,
            )
            if resp.status_code == 200:
                items = resp.json()
                if items:
                    output = [
                        f"# Results for Saved Search: {match['name']}",
                        f"**Results:** {len(items)} items",
                        "",
                    ]
                    for i, item in enumerate(items, 1):
                        data = item.get("data", {})
                        title = data.get("title", "Untitled")
                        creators = _utils.format_creators(data.get("creators", []), max_authors=3)
                        date = data.get("date", "")[:4] if data.get("date") else ""
                        key = item.get("key", "")
                        output.append(f"{i}. **{title}** ({date})")
                        output.append(f"   Key: `{key}` | Authors: {creators}")
                    return "\n".join(output)
        except Exception as e:
            await ctx.info(f"Local API search execution failed ({e}), falling back to keyword search")

        # Fallback: translate conditions to keyword search
        query_parts = []
        for cond in match["conditions"]:
            op = cond["operator"]
            val = cond["value"]
            if not val:
                continue
            if op == "contains":
                query_parts.append(val)
            elif op == "is":
                query_parts.append(f'"{val}"')
            elif op == "doesNotContain":
                pass
            elif op == "isNot":
                pass
            else:
                query_parts.append(val)

        if not query_parts:
            return f"Saved search '{match['name']}' has no processable conditions."

        query = " ".join(query_parts)
        await ctx.info(f"Executing saved search '{match['name']}' via keyword: {query}")
        zot = await _client.run_zotero_call(_client.get_zotero_client, operation="get_zotero_client")
        items = await _client.run_zotero_call(
            zot.items,
            q=query,
            qmode="everything",
            limit=limit,
            sort="dateAdded",
            direction="desc",
            operation=f"zot.items(saved_search:{match['name']})",
        )

        if not items:
            return f"Saved search '{match['name']}' returned no results."

        output = [
            f"# Results for Saved Search: {match['name']}",
            f"**Query:** {query}",
            f"**Results:** {len(items)} items",
            "",
        ]
        for i, item in enumerate(items, 1):
            output.extend(_utils.format_item_result(item, index=i, abstract_len=0, include_tags=False))

        return "\n".join(output)

    except Exception as e:
        await ctx.error(f"Error executing saved search: {str(e)}")
        return f"Error executing saved search: {str(e)}"
    finally:
        reader.close()
