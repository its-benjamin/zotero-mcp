"""Tool modules — importing this package registers all tools with the MCP app."""

# Resources and prompts
from zotero_mcp import prompts as prompts  # noqa: F401
from zotero_mcp import resources as resources  # noqa: F401
from zotero_mcp.tools import (  # noqa: F401
    annotations,
    connectors,
    read_pdf,
    retrieval,
    search,
    write,
)

# Optional: Scite enrichment (requires installing this fork with the scite extra).
try:
    from zotero_mcp.tools import scite as scite  # noqa: F401
except ImportError:
    pass
