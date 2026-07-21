"""Progressive tool discovery meta-tools (search → schema → call).

These are the only tools listed in ``ZOTERO_MCP_TOOL_MODE=meta`` (default).
They implement the standard MCP progressive-disclosure pattern so hosts do not
inject 80+ full tool schemas into every model turn.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from zotero_mcp._app import mcp
from zotero_mcp.tool_mode import (
    call_internal_tool,
    catalog_tools,
    get_tool_mode,
    get_tool_schema,
    list_packs,
)


def _as_json(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False, default=str)


@mcp.tool(
    name="zotero_search_tools",
    description=(
        "Progressive tool discovery for this Zotero MCP server. "
        "MOST TOOLS ARE HIDDEN from the host tool list to save context; "
        "use this first to find the right tool name, then "
        "zotero_get_tool_schema for parameters, then zotero_call_tool to run it. "
        "query: natural-language keywords (e.g. 'add paper doi', 'semantic search', "
        "'annotations', 'delete tag'). Empty query lists packs + top tools. "
        "pack: optional filter — search, retrieval, write, annotations, pdf, "
        "discovery, synthesis, scite, connectors, other. "
        "detail: 'brief' (default) returns name+short description; "
        "'schema' includes full inputSchema. "
        "limit: max results (default 25, max 100)."
    ),
    tags={"meta"},
)
async def search_tools(
    query: str = "",
    pack: str | None = None,
    detail: Literal["brief", "schema"] = "brief",
    limit: int = 25,
) -> str:
    """Search the full internal catalog of Zotero tools."""
    result = await catalog_tools(
        mcp,
        query=query or "",
        pack=pack,
        detail=detail,
        limit=limit,
    )
    # Always include pack index when browsing
    if result.get("packs") is None and not (query or "").strip():
        result["packs"] = list_packs()
    result["tool_mode"] = get_tool_mode()
    return _as_json(result)


@mcp.tool(
    name="zotero_get_tool_schema",
    description=(
        "Get the full JSON Schema (parameters) for one Zotero tool by exact name. "
        "Use after zotero_search_tools to learn required/optional arguments before "
        "calling zotero_call_tool. name: tool name "
        "(e.g. 'zotero_add_by_doi', 'zotero_semantic_search')."
    ),
    tags={"meta"},
)
async def get_tool_schema_tool(name: str) -> str:
    """Return full input/output schema for a tool."""
    result = await get_tool_schema(mcp, name)
    return _as_json(result)


@mcp.tool(
    name="zotero_call_tool",
    description=(
        "Execute any Zotero MCP tool by name with a JSON arguments object. "
        "Works for tools that are hidden in progressive (meta/core) mode. "
        "Workflow: zotero_search_tools → zotero_get_tool_schema → zotero_call_tool. "
        "name: exact tool name (e.g. 'zotero_search_items'). "
        "arguments: object of parameter names to values matching the tool schema "
        "(default {}). Example: name='zotero_add_by_doi', "
        'arguments={"doi": "10.1038/nature12373"}.'
    ),
    tags={"meta"},
)
async def call_tool(name: str, arguments: dict[str, Any] | None = None) -> Any:
    """Run a (possibly hidden) tool via the local provider."""
    return await call_internal_tool(mcp, name, arguments)
