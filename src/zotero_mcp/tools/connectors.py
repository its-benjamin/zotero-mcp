"""ChatGPT connector tool functions (search & fetch)."""

import json
import os
import uuid

from mcp.types import ToolAnnotations

from zotero_mcp import client as _client
from zotero_mcp import utils as _utils
from zotero_mcp._app import mcp
from zotero_mcp._context import Context
from zotero_mcp.client import with_zotero_api_lock
from zotero_mcp.tools.retrieval import get_item_fulltext

# These are required for ChatGPT custom MCP servers via web "connectors"
# specific tools required are "search" and "fetch"
# See: https://platform.openai.com/docs/mcp


@mcp.tool(
    name="search",
    description="ChatGPT-compatible search wrapper. Performs semantic search with keyword fallback, returns JSON results.",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
)
@with_zotero_api_lock
async def chatgpt_connector_search(query: str, *, ctx: Context) -> str:
    """
    Returns a JSON-encoded string with shape {"results": [{"id","title","url"}, ...]}.
    Uses semantic search with fallback to keyword search.
    """
    try:
        default_limit = 10
        result_list: list[dict[str, str]] = []

        # Try semantic search first
        try:
            from zotero_mcp.config import get_config_path
            from zotero_mcp.semantic_search import create_semantic_search

            config_path = str(get_config_path())
            search = create_semantic_search(config_path)
            results = search.search(query=query, limit=default_limit, filters=None) or {}
            for r in results.get("results", []):
                item_key = r.get("item_key") or ""
                title = ""
                if r.get("zotero_item"):
                    data = (r.get("zotero_item") or {}).get("data", {})
                    title = data.get("title", "")
                if not title:
                    title = f"Zotero Item {item_key}" if item_key else "Zotero Item"
                url = f"zotero://select/items/{item_key}" if item_key else ""
                result_list.append({"id": item_key or uuid.uuid4().hex[:8], "title": title, "url": url})
        except Exception:
            pass

        # Fallback to keyword search if semantic search returned nothing
        if not result_list:
            try:
                zot = await _client.run_zotero_call(_client.get_zotero_client, operation="get_zotero_client")
                items = await _client.run_zotero_call(
                    zot.items, q=query, qmode="titleCreatorYear", limit=default_limit,
                    itemType="-attachment -note -annotation", sort="dateAdded", direction="desc",
                    operation="zot.items(connector_search)",
                )
                for item in (items or []):
                    data = item.get("data", {})
                    key = item.get("key", "")
                    title = data.get("title", "Untitled")
                    url = f"zotero://select/items/{key}" if key else ""
                    result_list.append({"id": key or uuid.uuid4().hex[:8], "title": title, "url": url})
            except Exception:
                pass

        return json.dumps({"results": result_list}, separators=(",", ":"))
    except Exception as e:
        await ctx.error(f"Error in search wrapper: {str(e)}")
        return json.dumps({"results": []}, separators=(",", ":"))


@mcp.tool(
    name="fetch",
    description="ChatGPT-compatible fetch wrapper. Retrieves fulltext/metadata for a Zotero item by ID.",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
)
@with_zotero_api_lock
async def connector_fetch(id: str, *, ctx: Context) -> str:
    """
    Returns a JSON-encoded string with shape {"id","title","text","url","metadata":{...}}.
    The MCP runtime wraps this string as a single text content item.
    """
    try:
        item_key = (id or "").strip()
        if not item_key:
            return json.dumps(
                {"id": id, "title": "", "text": "", "url": "", "metadata": {"error": "missing item key"}},
                separators=(",", ":"),
            )

        # Fetch item metadata for title and context
        zot = await _client.run_zotero_call(_client.get_zotero_client, operation="get_zotero_client")
        try:
            item = await _client.run_zotero_call(zot.item, item_key, operation=f"zot.item({item_key})")
            data = item.get("data", {}) if item else {}
        except Exception:
            item = None
            data = {}

        title = data.get("title", f"Zotero Item {item_key}")
        zotero_url = f"zotero://select/items/{item_key}"
        # Prefer web URL for connectors; fall back to zotero:// if unknown
        lib_type = (os.getenv("ZOTERO_LIBRARY_TYPE", "user") or "user").lower()
        lib_id = os.getenv("ZOTERO_LIBRARY_ID", "")
        if lib_type not in ["user", "group"]:
            lib_type = "user"
        web_url = (
            f"https://www.zotero.org/{'users' if lib_type == 'user' else 'groups'}/{lib_id}/items/{item_key}"
            if lib_id
            else ""
        )
        url = web_url or zotero_url

        # Use existing tool to get best-effort fulltext/markdown
        text_md = await get_item_fulltext(item_key=item_key, ctx=ctx)
        # Extract the actual full text section if present, else keep as-is
        text_clean = text_md
        try:
            marker = "\n## Full Text\n"
            pos = text_md.find(marker)
            if pos >= 0:
                text_clean = text_md[pos + len(marker) :].lstrip("\n #")
        except Exception:
            pass
        if (not text_clean or len(text_clean.strip()) < 40) and data:
            abstract = data.get("abstractNote", "")
            creators = data.get("creators", [])
            byline = _utils.format_creators(creators)
            text_clean = (
                f"{title}\n\n"
                + (f"Authors: {byline}\n" if byline else "")
                + (f"Abstract:\n{abstract}" if abstract else "")
            ) or text_md

        metadata = {
            "itemType": data.get("itemType", ""),
            "date": data.get("date", ""),
            "key": item_key,
            "doi": data.get("DOI", ""),
            "isbn": data.get("ISBN", ""),
            "issn": data.get("ISSN", ""),
            "publisher": data.get("publisher", ""),
            "place": data.get("place", ""),
            "authors": _utils.format_creators(data.get("creators", [])),
            "tags": [t.get("tag", "") for t in (data.get("tags", []) or [])],
            "zotero_url": zotero_url,
            "web_url": web_url,
            "source": "zotero-mcp",
        }

        return json.dumps(
            {"id": item_key, "title": title, "text": text_clean, "url": url, "metadata": metadata},
            separators=(",", ":"),
        )
    except Exception as e:
        await ctx.error(f"Error in fetch wrapper: {str(e)}")
        return json.dumps(
            {"id": id, "title": "", "text": "", "url": "", "metadata": {"error": str(e)}}, separators=(",", ":")
        )

@mcp.tool(
    name="zotero_mcp_capabilities",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    description=(
        "Report which MCP protocol surfaces this Zotero server implements, "
        "and which FastMCP features remain intentionally unsupported."
    ),
)
async def mcp_capabilities(*, ctx: Context) -> str:
    """Return a concise MCP feature matrix for AI clients and CLI users."""
    await ctx.info("Reporting Zotero MCP capability matrix")
    return "\n".join(
        [
            "# Zotero MCP Capability Matrix",
            "",
            "## Implemented",
            "- Tools: Zotero search, retrieval, annotations, write operations, Scite, connectors.",
            "- Tool annotations: read-only/destructive/idempotent/open-world hints on tools.",
            "- Resources: library info, collections, tags, recent items.",
            "- Resource Templates: item, item children, collection items, tag items.",
            "- Prompts: paper summaries, comparison, literature review, annotated bibliography, discovery, citation context.",
            "- Context Logging: info/warning/error messages from tools.",
            "- Progress: semantic database update reports indexing progress when supported by the client.",
            "- Elicitation: duplicate merge can ask the client for confirmation before destructive execution.",
            "- Sampling: zotero_suggest_tags asks the client model for read-only tag suggestions.",
            "- Roots: zotero_list_client_roots lists client workspace roots when available.",
            "- Notifications: write tools send resources/list_changed after successful library mutations.",
            "- Tasks: zotero_update_search_database is task-enabled when installed with fastmcp[tasks].",
            "- Integrations: ChatGPT-compatible search/fetch tools and standalone CLI entry points.",
            "",
            "## Not implemented",
            "- Apps / Generative UI / FastMCPApp / Interactive Tools / Custom HTML: not useful for stdio CLI clients yet.",
            "- Client-only package: this project is a server plus standalone CLI, not an MCP client SDK.",
            "- Transport management: delegated to FastMCP and client launch config rather than custom transport code.",
            "- Server-side sampling models: sampling uses the connected MCP client model, not a server-owned LLM.",
        ]
    )

