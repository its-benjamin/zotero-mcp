"""Progressive tool disclosure (meta / core / full modes).

MCP hosts inject every listed tool schema into the model context. With 80+
tools that cost is large (the so-called \"tools tax\"). This module implements
the standard progressive-discovery pattern:

* **meta** (default) — expose only discovery + call meta-tools; the rest stay
  registered but hidden and are invoked via ``zotero_call_tool``.
* **core** — keep a small always-on set of high-use tools visible, plus meta
  tools for everything else.
* **full** — classic behaviour: every tool is listed (no progressive loading).

Configure with ``ZOTERO_MCP_TOOL_MODE=meta|core|full``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from typing import Any, Literal

logger = logging.getLogger(__name__)

ToolMode = Literal["meta", "core", "full"]

# Meta-tools that implement progressive disclosure. Always visible.
META_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "zotero_search_tools",
        "zotero_get_tool_schema",
        "zotero_call_tool",
    }
)

# High-frequency tools kept listed in ``core`` mode.
CORE_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "zotero_search_items",
        "zotero_semantic_search",
        "zotero_advanced_search",
        "zotero_get_item_metadata",
        "zotero_get_item_fulltext",
        "zotero_get_collections",
        "zotero_get_collection_items",
        "zotero_get_annotations",
        "zotero_get_notes",
        "zotero_search_notes",
        "zotero_add_by_doi",
        "zotero_update_item",
        "zotero_create_note",
    }
)

# Domain packs for catalog grouping / filtered search.
# Unknown tools fall into ``other``.
TOOL_PACKS: dict[str, frozenset[str]] = {
    "search": frozenset(
        {
            "zotero_search_items",
            "zotero_search_by_tag",
            "zotero_search_by_citation_key",
            "zotero_advanced_search",
            "zotero_semantic_search",
            "zotero_update_search_database",
            "zotero_get_search_database_status",
            "zotero_list_saved_searches",
            "zotero_execute_saved_search",
            "zotero_create_saved_search",
            "zotero_delete_saved_search",
            "zotero_search_papers",
        }
    ),
    "retrieval": frozenset(
        {
            "zotero_get_item_metadata",
            "zotero_get_items_metadata",
            "zotero_get_item_fulltext",
            "zotero_get_item_children",
            "zotero_get_items_children",
            "zotero_get_collections",
            "zotero_get_collection_items",
            "zotero_get_collection_tags",
            "zotero_get_tags",
            "zotero_get_item_tags",
            "zotero_get_recent",
            "zotero_get_recent_changes",
            "zotero_get_library_changes",
            "zotero_get_trash_items",
            "zotero_restore_from_trash",
            "zotero_list_libraries",
            "zotero_switch_library",
            "zotero_list_feeds",
            "zotero_get_feed_items",
            "zotero_get_publications",
            "zotero_get_citation_graph",
            "zotero_summarize_collection",
            "zotero_generate_bibliography",
            "zotero_export_items",
            "zotero_get_item_types",
            "zotero_get_item_fields",
            "zotero_get_item_template",
            "zotero_get_attachment_path",
        }
    ),
    "write": frozenset(
        {
            "zotero_add_by_doi",
            "zotero_add_by_url",
            "zotero_add_by_isbn",
            "zotero_add_by_bibtex",
            "zotero_add_by_csl_json",
            "zotero_add_from_file",
            "zotero_batch_add_by_doi",
            "zotero_update_item",
            "zotero_delete_item",
            "zotero_batch_delete_items",
            "zotero_batch_update_tags",
            "zotero_batch_update_extra",
            "zotero_create_collection",
            "zotero_search_collections",
            "zotero_manage_collections",
            "zotero_rename_collection",
            "zotero_delete_collection",
            "zotero_move_item",
            "zotero_rename_tag",
            "zotero_merge_tags",
            "zotero_delete_tags",
            "zotero_relate_items",
            "zotero_unrelate_items",
            "zotero_find_duplicates",
            "zotero_merge_duplicates",
            "zotero_get_pdf_outline",
        }
    ),
    "annotations": frozenset(
        {
            "zotero_get_annotations",
            "zotero_get_notes",
            "zotero_search_notes",
            "zotero_create_note",
            "zotero_update_note",
            "zotero_delete_note",
            "zotero_create_annotation",
            "zotero_create_area_annotation",
        }
    ),
    "pdf": frozenset(
        {
            "zotero_read_pdf_pages",
            "zotero_extract_pdf_pages",
            "zotero_render_pdf_pages",
            "zotero_get_pdf_outline",
        }
    ),
    "discovery": frozenset(
        {
            "zotero_find_related_papers",
            "zotero_library_coverage",
            "zotero_search_papers",
        }
    ),
    "synthesis": frozenset(
        {
            "zotero_synthesize_annotations",
            "zotero_export_bibliography",
        }
    ),
    "scite": frozenset(
        {
            "scite_enrich_item",
            "scite_enrich_search",
            "scite_check_retractions",
        }
    ),
    "connectors": frozenset(
        {
            "search",
            "fetch",
        }
    ),
    "meta": META_TOOL_NAMES,
}

PACK_DESCRIPTIONS: dict[str, str] = {
    "search": "Keyword, tag, citation-key, advanced, and semantic search",
    "retrieval": "Read items, collections, tags, fulltext, libraries, export",
    "write": "Add/update/delete items, collections, tags, duplicates, relations",
    "annotations": "Notes and PDF annotations CRUD",
    "pdf": "Read/render/extract PDF pages and outlines",
    "discovery": "Related papers (OpenAlex) and library coverage",
    "synthesis": "Annotation synthesis and bibliography export",
    "scite": "Scite citation tallies and retraction checks",
    "connectors": "ChatGPT connector search/fetch adapters",
    "meta": "Progressive tool discovery and invocation",
    "other": "Tools not assigned to a named pack",
}

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def get_tool_mode() -> ToolMode:
    """Resolve tool mode from ``ZOTERO_MCP_TOOL_MODE`` (default: ``meta``)."""
    raw = (os.environ.get("ZOTERO_MCP_TOOL_MODE") or "meta").strip().lower()
    if raw in ("meta", "progressive", "lazy"):
        return "meta"
    if raw in ("core", "lite", "essential"):
        return "core"
    if raw in ("full", "all", "classic"):
        return "full"
    logger.warning("Unknown ZOTERO_MCP_TOOL_MODE=%r; using 'meta'", raw)
    return "meta"


def pack_for_tool(name: str) -> str:
    """Return the pack name for a tool (``other`` if unmapped)."""
    for pack, names in TOOL_PACKS.items():
        if name in names:
            return pack
    return "other"


def list_packs() -> list[dict[str, Any]]:
    """Return pack metadata for catalog responses."""
    packs = []
    for name, desc in PACK_DESCRIPTIONS.items():
        tool_count = len(TOOL_PACKS.get(name, ()))
        packs.append({"name": name, "description": desc, "tool_count": tool_count})
    return packs


def _tokenize(text: str) -> set[str]:
    return set(_TOKEN_RE.findall((text or "").lower()))


def _score_match(query: str, name: str, description: str, pack: str) -> float:
    """Simple keyword score for progressive tool search (BM25-ish light)."""
    q = (query or "").strip().lower()
    if not q:
        return 1.0  # empty query = list all (equal score)

    name_l = name.lower()
    desc_l = (description or "").lower()
    pack_l = pack.lower()

    if q == name_l or q == name_l.removeprefix("zotero_"):
        return 100.0
    if q in name_l:
        return 50.0 + name_l.count(q)

    q_tokens = _tokenize(q)
    if not q_tokens:
        return 0.0

    name_tokens = _tokenize(name_l.replace("_", " "))
    desc_tokens = _tokenize(desc_l)
    pack_tokens = _tokenize(pack_l)

    score = 0.0
    for tok in q_tokens:
        if tok in name_tokens:
            score += 8.0
        elif any(tok in nt for nt in name_tokens):
            score += 4.0
        if tok in pack_tokens or tok == pack_l:
            score += 5.0
        if tok in desc_tokens:
            score += 2.0
        elif any(tok in dt for dt in desc_tokens):
            score += 1.0
    return score


def _registered_tool_names(mcp: Any) -> set[str]:
    """Collect tool names from the local provider (includes disabled tools)."""
    names: set[str] = set()
    components = getattr(getattr(mcp, "local_provider", None), "_components", None) or {}
    for key, comp in components.items():
        if not str(key).startswith("tool:"):
            continue
        name = getattr(comp, "name", None)
        if name:
            names.add(name)
    return names


def apply_tool_mode(mcp: Any) -> ToolMode:
    """Enable/disable tools on the FastMCP instance according to mode.

    Safe to call after all ``@mcp.tool`` registrations. Does not send
    ``list_changed`` notifications during startup (no active request context).

    Only toggles **tools** by name. Resources/prompts are left alone — using
    ``enable(..., only=True)`` would incorrectly hide them.
    """
    mode = get_tool_mode()
    all_names = _registered_tool_names(mcp)

    if mode == "core":
        visible = (META_TOOL_NAMES | CORE_TOOL_NAMES) & all_names
    elif mode == "meta":
        visible = META_TOOL_NAMES & all_names
    else:
        visible = set(all_names)

    hidden = all_names - visible
    if hidden:
        mcp.disable(names=hidden)
    if visible:
        mcp.enable(names=visible)

    if mode == "full":
        msg = f"Tool mode=full — {len(visible)} tools listed (highest context cost)"
    else:
        msg = (
            f"Tool mode={mode} — {len(visible)} tools listed; "
            "discover the rest via zotero_search_tools / zotero_call_tool "
            "(set ZOTERO_MCP_TOOL_MODE=full to list everything)"
        )
    logger.info(msg)
    sys.stderr.write(f"{msg}\n")
    return mode


async def catalog_tools(
    mcp: Any,
    *,
    query: str = "",
    pack: str | None = None,
    detail: Literal["brief", "schema"] = "brief",
    limit: int = 25,
) -> dict[str, Any]:
    """Search the full internal tool catalog (including disabled tools)."""
    limit = max(1, min(int(limit or 25), 100))
    pack_filter = (pack or "").strip().lower() or None
    if pack_filter == "all":
        pack_filter = None

    tools = await mcp.local_provider.list_tools()
    scored: list[tuple[float, Any]] = []
    for tool in tools:
        name = getattr(tool, "name", "") or ""
        if name in META_TOOL_NAMES:
            continue  # meta tools are always visible; no need to rediscover
        tool_pack = pack_for_tool(name)
        if pack_filter and tool_pack != pack_filter and pack_filter not in name.lower():
            continue
        desc = getattr(tool, "description", None) or ""
        score = _score_match(query, name, desc, tool_pack)
        if score <= 0 and query.strip():
            continue
        scored.append((score, tool))

    scored.sort(key=lambda pair: (-pair[0], pair[1].name))
    selected = scored[:limit]

    results: list[dict[str, Any]] = []
    for score, tool in selected:
        entry: dict[str, Any] = {
            "name": tool.name,
            "pack": pack_for_tool(tool.name),
            "description": _short_description(tool.description),
            "score": round(score, 2),
        }
        if detail == "schema":
            entry["inputSchema"] = getattr(tool, "parameters", None) or {}
            if getattr(tool, "output_schema", None):
                entry["outputSchema"] = tool.output_schema
        results.append(entry)

    mode = get_tool_mode()
    return {
        "mode": mode,
        "query": query,
        "pack": pack_filter,
        "detail": detail,
        "total_matches": len(scored),
        "returned": len(results),
        "packs": list_packs() if not query.strip() and not pack_filter else None,
        "tools": results,
        "hint": (
            "Use zotero_get_tool_schema(name) for full parameters, then "
            "zotero_call_tool(name, arguments={...}) to run a tool."
            if mode != "full"
            else "Tool mode is full; tools are also listed directly by the host."
        ),
    }


async def get_tool_schema(mcp: Any, name: str) -> dict[str, Any]:
    """Return full schema for one tool (works for disabled tools)."""
    name = (name or "").strip()
    if not name:
        return {"error": "Tool name is required."}

    tool = await mcp.local_provider.get_tool(name)
    if tool is None:
        # Suggest close matches
        catalog = await catalog_tools(mcp, query=name, limit=5)
        return {
            "error": f"Unknown tool: {name!r}",
            "suggestions": [t["name"] for t in catalog.get("tools", [])],
        }

    return {
        "name": tool.name,
        "pack": pack_for_tool(tool.name),
        "description": tool.description or "",
        "inputSchema": getattr(tool, "parameters", None) or {},
        "outputSchema": getattr(tool, "output_schema", None),
        "annotations": _annotations_dict(tool),
    }


async def call_internal_tool(mcp: Any, name: str, arguments: dict[str, Any] | None = None) -> Any:
    """Invoke a tool by name, including tools hidden by progressive mode.

    Uses the local provider so disabled tools remain callable from meta-tools.
    """
    name = (name or "").strip()
    if not name:
        return "Error: tool name is required."
    if name in META_TOOL_NAMES:
        return (
            f"Error: refusing to recursively call meta-tool {name!r}. "
            "Search/call tools are already available to the host."
        )

    tool = await mcp.local_provider.get_tool(name)
    if tool is None:
        catalog = await catalog_tools(mcp, query=name, limit=5)
        suggestions = ", ".join(t["name"] for t in catalog.get("tools", [])) or "(none)"
        return f"Error: unknown tool {name!r}. Suggestions: {suggestions}"

    args = arguments if isinstance(arguments, dict) else {}
    try:
        result = await tool.run(args)
    except Exception as e:
        return f"Error calling {name}: {e}"

    return _format_tool_result(result)


def _short_description(description: str | None, max_len: int = 220) -> str:
    text = " ".join((description or "").split())
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


def _annotations_dict(tool: Any) -> dict[str, Any] | None:
    ann = getattr(tool, "annotations", None)
    if ann is None:
        return None
    if hasattr(ann, "model_dump"):
        return ann.model_dump(exclude_none=True)
    if isinstance(ann, dict):
        return ann
    return None


def _format_tool_result(result: Any) -> Any:
    """Normalize a FastMCP ToolResult into something MCP can return cleanly."""
    # Prefer structured content when present
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        if isinstance(structured, dict) and set(structured.keys()) == {"result"}:
            return structured["result"]
        return structured

    content = getattr(result, "content", None)
    if content is None:
        return result

    texts: list[str] = []
    blocks = content if isinstance(content, list) else [content]
    for block in blocks:
        if isinstance(block, str):
            texts.append(block)
            continue
        text = getattr(block, "text", None)
        if text is not None:
            texts.append(text)
            continue
        if isinstance(block, dict) and "text" in block:
            texts.append(str(block["text"]))
        else:
            texts.append(str(block))

    combined = "\n".join(texts)
    # If the underlying tool returned JSON text, parse for structured use
    stripped = combined.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass
    return combined
