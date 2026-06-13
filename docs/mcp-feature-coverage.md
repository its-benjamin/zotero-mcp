# MCP Feature Coverage

This fork now exposes more of FastMCP/MCP than plain Zotero CRUD tools, so CLI and AI clients can inspect, browse, and orchestrate the library more effectively.

## Implemented

| MCP area | Zotero MCP support |
| --- | --- |
| Tools | Search, retrieval, annotation, write, Scite, connector, and capability tools. Most tools include read-only/destructive/idempotent/open-world annotations. |
| Resources | Static browse resources: `zotero://library/info`, `zotero://collections`, `zotero://tags`, `zotero://recent`. |
| Resource Templates | Parameterized resources: `zotero://item/{item_key}`, `zotero://item/{item_key}/children`, `zotero://collection/{collection_key}/items`, `zotero://tag/{tag_name}/items`. |
| Prompts | Reusable workflows for summaries, paper comparison, literature review, annotated bibliography, paper discovery, and citation context. |
| Context / Logging | Tools use `ctx.info`, `ctx.warning`, and `ctx.error` for client-visible status. |
| Progress | `zotero_update_search_database` reports indexing progress through `ctx.report_progress` when the client supports it. |
| Elicitation | `zotero_merge_duplicates` can ask for client confirmation before destructive execution. |
| Sampling | `zotero_suggest_tags` asks the connected client model for read-only tag suggestions. |
| Roots | `zotero_list_client_roots` lists client workspace roots for local-file workflows. |
| Notifications | Successful library mutations send best-effort `resources/list_changed` notifications. |
| Tasks | `zotero_update_search_database` is task-enabled when installed with `fastmcp[tasks]`. |
| Integrations | ChatGPT-compatible `search`/`fetch`, `mcp.json`-style server setup, and standalone CLI commands. |

## Not Implemented Yet

| MCP/FastMCP area | Status |
| --- | --- |
| Apps / FastMCPApp / Interactive Tools / Generative UI / Custom HTML | Not implemented. These are UI-host features and are less useful for stdio CLI-first Zotero workflows. |
| Client-only package | Not implemented. This project is an MCP server plus standalone Zotero CLI, not a general MCP client SDK. |
| Custom transport management | Not implemented. Transport handling remains delegated to FastMCP and client config. |
| Server-owned sampling models | Not implemented. Sampling delegates to the connected MCP client model by design. |

Use `zotero_mcp_capabilities` from an MCP client to get the live matrix exposed by the server.
