"""Tool modules — importing this package registers all tools with the MCP app."""

# Resources and prompts
from zotero_mcp import prompts as prompts  # noqa: F401
from zotero_mcp import resources as resources  # noqa: F401
from zotero_mcp.tools import (  # noqa: F401
    annotations,
    connectors,
    discovery,
    meta_tools,
    read_pdf,
    retrieval,
    search,
    synthesis,
    write,
)

# Optional: Scite enrichment (requires installing this fork with the scite extra).
try:
    from zotero_mcp.tools import scite as scite  # noqa: F401
except ImportError:
    pass

# Progressive disclosure: hide non-essential tools according to
# ZOTERO_MCP_TOOL_MODE (meta|core|full). Must run after all @mcp.tool
# registrations above so the allowlist can include every registered name.
from zotero_mcp._app import mcp as _mcp  # noqa: E402
from zotero_mcp.tool_mode import apply_tool_mode  # noqa: E402

apply_tool_mode(_mcp)
