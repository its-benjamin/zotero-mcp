# Zotero MCP: Chat with your Research Library—Local or Web—in Claude, ChatGPT, and more.

<p align="center">
  <a href="https://www.zotero.org/">
    <img src="https://img.shields.io/badge/Zotero-CC2936?style=for-the-badge&logo=zotero&logoColor=white" alt="Zotero">
  </a>
  <a href="https://www.anthropic.com/claude">
    <img src="https://img.shields.io/badge/Claude-6849C3?style=for-the-badge&logo=anthropic&logoColor=white" alt="Claude">
  </a>
  <a href="https://chatgpt.com/">
    <img src="https://img.shields.io/badge/ChatGPT-74AA9C?style=for-the-badge&logo=openai&logoColor=white" alt="ChatGPT">
  </a>
  <a href="https://modelcontextprotocol.io/introduction">
    <img src="https://img.shields.io/badge/MCP-0175C2?style=for-the-badge&logoColor=white" alt="MCP">
  </a>
  <a href="https://github.com/its-benjamin/zotero-mcp/releases">
    <img src="https://img.shields.io/github/v/release/its-benjamin/zotero-mcp?style=for-the-badge&logo=github" alt="GitHub release">
  </a>
  <a href="https://discord.gg/BvgjbcBUqg">
    <img src="https://img.shields.io/badge/Discord-5865F2?style=for-the-badge&logo=discord&logoColor=white" alt="Discord">
  </a>
</p>

**Zotero MCP** seamlessly connects your [Zotero](https://www.zotero.org/) research library with [ChatGPT](https://openai.com), [Claude](https://www.anthropic.com/claude), and other AI assistants (e.g., [Cherry Studio](https://cherry-ai.com/), [Chorus](https://chorus.sh), [Cursor](https://www.cursor.com/)) via the [Model Context Protocol](https://modelcontextprotocol.io/introduction). Review papers, get summaries, analyze citations, extract PDF annotations, and more!

This fork is released from GitHub only. It is not published to PyPI; install it from this repository or a release tag.

---

## 🔀 What's different in this fork

This is a fork of [54yyyu/zotero-mcp](https://github.com/54yyyu/zotero-mcp) with a focus on reliability, performance, and extra tooling.

**More reliable Zotero access**
- Automatic retry with backoff on transient errors (429 rate limits, 503 service unavailable, timeouts, connection drops). Long-running indexing jobs no longer fail silently when the Zotero API briefly hiccups.
- Local Zotero database access serialized with a re-entrant async lock — no more nested-call deadlocks when tools call each other.

**Faster search and indexing**
- Full-text search for notes and annotations powered by an SQLite FTS5 sidecar index, instead of slow `LIKE '%query%'` scans. Queries on large libraries return in milliseconds.
- In-memory cache for item metadata so repeated lookups during a session don't re-hit the API.
- Semantic indexing extracts PDFs in parallel (ThreadPoolExecutor), cutting full-library index time noticeably on multi-core machines.

**More embedding providers for semantic search**
- **OpenRouter** — OpenAI-compatible router, gives you access to many embedding models behind one API key.
- **Local HuggingFace Hub models** — point at any sentence-transformers model name (e.g. `BAAI/bge-small-en-v1.5`, `intfloat/e5-small-v2`, `Qwen/Qwen3-Embedding-0.6B`) for fully local, private embeddings. No API key, no data leaves your machine.

**New library management tools**
- `zotero_move_item` — move an item between collections in a single call (instead of remove + add).
- `zotero_rename_tag` — rename a tag across every item that uses it.

**Better setup experience**
- `zotero-mcp doctor` — one command that checks your config, API key, local Zotero database, FTS sidecar, and semantic index, then tells you what's missing.
- `zotero-mcp --version` at the root level.
- Clearer error messages when item keys, collection keys, or tags are malformed — you get "expected 8 alphanumeric characters" instead of an opaque API failure.

**Distribution**
- Released from GitHub only — install directly from a release tag with uv, pip, or pipx (no PyPI publishing required).

---

## ✨ Features

### 🧠 AI-Powered Semantic Search
- **Vector-based similarity search** over your entire research library (requires `[semantic]` extra)
- **Multiple embedding models**: Default (free, local), OpenAI, Gemini, Voyage AI, OpenRouter, and any HuggingFace Hub model
- **Intelligent results** with similarity scores and contextual matching
- **Auto-updating database** with configurable sync schedules

### 🔍 Search Your Library
- Find papers, articles, and books by title, author, or content
- Perform complex searches with multiple criteria
- Browse collections, tags, and recent additions
- Semantic search for conceptual and topic-based discovery

### 📚 Access Your Content
- Retrieve detailed metadata for any item (markdown or BibTeX export)
- Get full text content (when available)
- Look up items by BetterBibTeX citation key

### 📝 Work with Annotations
- Extract and search PDF annotations with page numbers
- Access Zotero's native annotations
- Create and update notes and annotations
- Extract PDF table of contents / outlines (requires `[pdf]` extra)

### ✏️ Write Operations
- **Add papers by DOI** with auto-fetched metadata and open-access PDF cascade (Unpaywall, arXiv, Semantic Scholar, PMC)
- **Add papers by URL** (arXiv, DOI links, generic webpages) or from local files
- Create and manage collections, update item metadata, batch-update tags
- Find and merge duplicate items with dry-run preview
- **Hybrid mode**: local reads + web API writes for local-mode users

### 📊 Scite Citation Intelligence (optional `[scite]` extra)
- **Citation tallies**: See how many papers support, contrast, or mention each item — the MCP version of the [Scite Zotero Plugin](https://github.com/scitedotai/scite-zotero-plugin)
- **Retraction alerts**: Scan your library for retracted or corrected papers
- No Scite account required — uses public API endpoints

### 🌐 Flexible Access Methods
- Local mode for offline access (no API key needed)
- Web API for cloud library access
- Hybrid mode: read from local Zotero, write via web API


### ⚡ Async MCP Runtime (v0.3.3+)
- FastMCP `Context` logging is awaited correctly
- Blocking Zotero/API calls run off the event loop with `asyncio.to_thread`
- Zotero local API access is serialized with an async re-entrant lock for reliability
### ⌨️ Standalone CLI (`zotero-cli`)
- Search, browse, and edit your library directly from the terminal — no AI assistant required
- Ideal for scripting, automation, and quick lookups
- Short aliases (`s`, `g`, `ann`, `coll`) for interactive use

## 🚀 Quick Install

> **New to the command line?** Try the community-built [Zotero MCP Setup](https://github.com/ehawkin/zotero-mcp-setup) — includes a macOS GUI installer (DMG), one-click install scripts for Mac/Windows, and a step-by-step guide. No Terminal experience needed.

### Default Installation (core tools only)

The base install is lightweight — it includes search, metadata retrieval, annotations, and write operations. No ML/AI dependencies are pulled in.

#### Installing from GitHub via uv (recommended)

```bash
uv tool install git+https://github.com/its-benjamin/zotero-mcp.git@v0.3.5
zotero-mcp setup  # Auto-configure (Claude Desktop supported)
```

#### Installing from GitHub via pip

```bash
pip install "zotero-mcp-server @ git+https://github.com/its-benjamin/zotero-mcp.git@v0.3.5"
zotero-mcp setup  # Auto-configure (Claude Desktop supported)
```

#### Installing from GitHub via pipx

```bash
pipx install git+https://github.com/its-benjamin/zotero-mcp.git@v0.3.5
zotero-mcp setup  # Auto-configure (Claude Desktop supported)
```

> **Want the newest development build instead of the pinned release?** Replace `@v0.3.5` with `@main`. Use `@v0.3.5` for normal installs because it is the tested release tag.

### Optional Extras

Heavy ML/PDF dependencies are separated into optional extras so the base install stays fast and small:

| Extra | What it adds | Install command |
|-------|-------------|-----------------|
| `semantic` | Semantic search via ChromaDB, sentence-transformers, OpenAI/Gemini/Voyage embeddings | `pip install "zotero-mcp-server[semantic] @ git+https://github.com/its-benjamin/zotero-mcp.git@v0.3.5"` |
| `pdf` | PDF outline extraction (PyMuPDF) and EPUB annotation support | `pip install "zotero-mcp-server[pdf] @ git+https://github.com/its-benjamin/zotero-mcp.git@v0.3.5"` |
| `scite` | [Scite](https://scite.ai) citation intelligence — tallies and retraction alerts (no account needed) | `pip install "zotero-mcp-server[scite] @ git+https://github.com/its-benjamin/zotero-mcp.git@v0.3.5"` |
| `all` | Everything above | `pip install "zotero-mcp-server[all] @ git+https://github.com/its-benjamin/zotero-mcp.git@v0.3.5"` |

For example, with uv:
```bash
uv tool install "zotero-mcp-server[all] @ git+https://github.com/its-benjamin/zotero-mcp.git@v0.3.5"
```

If you only need basic library access (search, read, annotate, write), the default install with no extras is all you need.

#### Updating Your Installation

Keep zotero-mcp up to date with the smart update command:

```bash
# Check for updates
zotero-mcp update --check-only

# Update to latest version (preserves all configurations)
zotero-mcp update
```

## 🧠 Semantic Search

Zotero MCP now includes powerful AI-powered semantic search capabilities that let you find research based on concepts and meaning, not just keywords.

### Setup Semantic Search

During setup or separately, configure semantic search:

```bash
# Configure during initial setup (recommended)
zotero-mcp setup

# Or configure semantic search separately
zotero-mcp setup --semantic-config-only
```

**Available Embedding Models:**
- **Default (all-MiniLM-L6-v2)**: Free, runs locally, good for most use cases
- **OpenAI**: Better quality, requires API key (`text-embedding-3-small` or `text-embedding-3-large`)
- **Gemini**: Better quality, requires API key (`gemini-embedding-001`)
- **Voyage AI**: Better retrieval quality, requires API key (`voyage-4-lite`)
- **OpenRouter**: OpenAI-compatible router supporting many models, requires API key (default: `openai/text-embedding-3-small`)
- **Local HuggingFace**: Run any sentence-transformers model locally (e.g., `intfloat/e5-small-v2`, `BAAI/bge-small-en-v1.5`)

OpenRouter uses the OpenAI-compatible `/embeddings` endpoint at `https://openrouter.ai/api/v1` and is a remote provider; it is not part of local-only mode. Example config:

```json
{
  "semantic_search": {
    "embedding_model": "openrouter",
    "embedding_config": {
      "model_name": "openai/text-embedding-3-small",
      "api_key": "${OPENROUTER_API_KEY}"
    }
  }
}
```

For local/private embeddings, set `embedding_model` directly to a HuggingFace Hub model name such as `intfloat/e5-small-v2`, `BAAI/bge-small-en-v1.5`, or `Qwen/Qwen3-Embedding-0.6B`. The first run downloads model weights locally.

Changing `embedding_model` resets the semantic index model metadata. Rebuild with `zotero-mcp update-db --force-rebuild` after switching providers or model names.

**Update Frequency Options:**
- **Manual**: Update only when you run `zotero-mcp update-db`
- **Auto on startup**: Update database every time the server starts
- **Daily**: Update once per day automatically
- **Every N days**: Set custom interval

### Using Semantic Search

After setup, initialize your search database:

```bash
# Build the semantic search database (fast, metadata-only)
zotero-mcp update-db

# Build with full-text extraction (slower, more comprehensive)
zotero-mcp update-db --fulltext

# Use your custom zotero.sqlite path
zotero-mcp update-db --fulltext --db-path "/Your_custom_path/zotero.sqlite"

# If you have embedding conflicts or changed models, force a rebuild
zotero-mcp update-db --force-rebuild

# Check database status
zotero-mcp db-status
```

**Example Semantic Queries in your AI assistant:**
- *"Find research similar to machine learning concepts in neuroscience"*
- *"Papers that discuss climate change impacts on agriculture"*
- *"Research related to quantum computing applications"*
- *"Studies about social media influence on mental health"*
- *"Find papers conceptually similar to this abstract: [paste abstract]"*

The semantic search provides similarity scores and finds papers based on conceptual understanding, not just keyword matching.

## 🖥️ Setup & Usage

Full documentation for this fork is available in this README and [Getting Started guide](./docs/getting-started.md).

**Requirements**
- Python 3.10+
- Zotero 7+ (for local API with full-text access)
- An MCP-compatible client (e.g., Claude Desktop, ChatGPT Developer Mode, Cherry Studio, Chorus)

**For ChatGPT setup: see the [Getting Started guide](./docs/getting-started.md).**

### For Claude Desktop (example MCP client)

#### Configuration
After installation, either:

1. **Auto-configure** (recommended):
   ```bash
   zotero-mcp setup
   ```

2. **Manual configuration**:
   Add to your `claude_desktop_config.json`:
   ```json
   {
     "mcpServers": {
       "zotero": {
         "command": "zotero-mcp",
         "env": {
           "ZOTERO_LOCAL": "true"
         }
       }
     }
   }
   ```

#### Usage

1. Start Zotero desktop (make sure local API is enabled in preferences)
2. Launch Claude Desktop
3. Access the Zotero-MCP tool through Claude Desktop's tools interface

Example prompts:
- "Search my library for papers on machine learning"
- "Find recent articles I've added about climate change"
- "Summarize the key findings from my paper on quantum computing"
- "Extract all PDF annotations from my paper on neural networks"
- "Search my notes and annotations for mentions of 'reinforcement learning'"
- "Show me papers tagged '#Arm' excluding those with '#Crypt' in my library"
- "Search for papers on operating system with tag '#Arm'"
- "Export the BibTeX citation for papers on machine learning"
- **"Find papers conceptually similar to deep learning in computer vision"** *(semantic search)*
- **"Research that relates to the intersection of AI and healthcare"** *(semantic search)*
- **"Papers that discuss topics similar to this abstract: [paste text]"** *(semantic search)*

### For Cherry Studio

#### Configuration
Go to Settings -> MCP Servers -> Edit MCP Configuration, and add the following:

```json
{
  "mcpServers": {
    "zotero": {
      "name": "zotero",
      "type": "stdio",
      "isActive": true,
      "command": "zotero-mcp",
      "args": [],
      "env": {
        "ZOTERO_LOCAL": "true"
      }
    }
  }
}
```
Then click "Save".

Cherry Studio also provides a visual configuration method for general settings and tools selection.

## 🔧 Advanced Configuration

### Using Web API Instead of Local API

For accessing your Zotero library via the web API (useful for remote setups):

```bash
zotero-mcp setup --no-local --api-key YOUR_API_KEY --library-id YOUR_LIBRARY_ID
```

### Environment Variables

**Zotero Connection:**
- `ZOTERO_LOCAL=true`: Use the local Zotero API (default: false)
- `ZOTERO_API_KEY`: Your Zotero API key (for web API)
- `ZOTERO_LIBRARY_ID`: Your Zotero library ID (for web API)
- `ZOTERO_LIBRARY_TYPE`: The type of library (user or group, default: user)

**Semantic Search:**
- `ZOTERO_EMBEDDING_MODEL`: Embedding model to use (`default`, `openai`, `gemini`, `voyage`, `openrouter`, or any HuggingFace Hub model name like `intfloat/e5-small-v2`)
- `OPENAI_API_KEY`: Your OpenAI API key (for OpenAI embeddings)
- `OPENAI_EMBEDDING_MODEL`: OpenAI model name (text-embedding-3-small, text-embedding-3-large)
- `OPENAI_BASE_URL`: Custom OpenAI endpoint URL (optional, for use with compatible APIs)
- `GEMINI_API_KEY`: Your Gemini API key (for Gemini embeddings)
- `GEMINI_EMBEDDING_MODEL`: Gemini model name (gemini-embedding-001)
- `GEMINI_BASE_URL`: Custom Gemini endpoint URL (optional, for use with compatible APIs)
- `GEMINI_OUTPUT_DIMENSIONALITY`: Gemini embedding output dimensionality (default: 768)
- `VOYAGE_API_KEY`: Your Voyage AI API key (for Voyage embeddings)
- `VOYAGE_EMBEDDING_MODEL`: Voyage model name (default: voyage-4-lite)
- `VOYAGE_BASE_URL`: Custom Voyage endpoint URL (optional)
- `VOYAGE_TOKENS_PER_MINUTE`: Voyage embedding token budget used by the local throttler (default: 10000)
- `VOYAGE_REQUEST_BATCH_SIZE`: Voyage embedding request batch size (default: 16)
- `VOYAGE_OUTPUT_DIMENSION`: Voyage embedding output dimension for Voyage 4 models (default: 512)
- `OPENROUTER_API_KEY`: Your OpenRouter API key (for OpenRouter embeddings)
- `OPENROUTER_EMBEDDING_MODEL`: OpenRouter model name (default: `openai/text-embedding-3-small`)
- `OPENROUTER_BASE_URL`: Custom OpenRouter endpoint URL (default: `https://openrouter.ai/api/v1`)
- `ZOTERO_DB_PATH`: Custom `zotero.sqlite` path (optional)

**API Rate Limits:**
External API calls are throttled by provider instead of being fired in a burst. Defaults are conservative and can be overridden with `ZOTERO_MCP_RATE_<PROVIDER>_REQUESTS` and `ZOTERO_MCP_RATE_<PROVIDER>_WINDOW_SECONDS`, where provider is one of `ZOTERO`, `CROSSREF`, `ARXIV`, `UNPAYWALL`, `SEMANTIC_SCHOLAR`, `PMC`, `SCITE`, `OPENAI`, `GEMINI`, or `VOYAGE`.
Embedding-provider rate limits are retried until the current batch succeeds; non-rate-limit errors still fail normally.

### Command-Line Options

```bash
# Run the server directly
zotero-mcp serve

# Specify transport method
zotero-mcp serve --transport stdio|streamable-http|sse

# Setup and configuration
zotero-mcp setup --help                    # Get help on setup options
zotero-mcp setup --semantic-config-only    # Configure only semantic search
zotero-mcp setup-info                      # Show installation path and config info for MCP clients

# Updates and maintenance
zotero-mcp update                          # Update to latest version
zotero-mcp update --check-only             # Check for updates without installing
zotero-mcp update --force                  # Force update even if up to date

# Semantic search database management
zotero-mcp update-db                       # Update semantic search database (fast, metadata-only)
zotero-mcp update-db --fulltext             # Update with full-text extraction (comprehensive but slower)
zotero-mcp update-db --force-rebuild       # Force complete database rebuild
zotero-mcp update-db --fulltext --force-rebuild  # Rebuild with full-text extraction
zotero-mcp update-db --fulltext --db-path "your_path_to/zotero.sqlite" # Customize your zotero database path
zotero-mcp db-status                       # Show database status and info

# General
zotero-mcp version                         # Show current version
```

## ⌨️ CLI Mode (`zotero-cli`)

`zotero-cli` is a standalone terminal interface to your Zotero library. It uses the same tools as the MCP server but without needing an AI assistant — useful for quick lookups, shell scripts, and automation.

Use `zotero-mcp` when your AI client supports MCP (Claude Desktop, ChatGPT). Use `zotero-cli` for shell scripts, cron jobs, or agentic pipelines with shell access (e.g. Claude Code) — CLI commands cost far fewer tokens than MCP tool schemas and compose naturally with Unix pipes.

Both share the same configuration set up by `zotero-mcp setup`.

### Quick reference

```bash
# Search
zotero-cli search "machine learning"           # keyword search
zotero-cli s "neural networks" --limit 5       # short alias, limit results
zotero-cli search --mode semantic "attention mechanisms"
zotero-cli search --mode tag "important,reviewed"

# Get item details
zotero-cli get metadata ABC123                 # markdown metadata
zotero-cli g metadata ABC123 --format bibtex  # BibTeX export
zotero-cli get fulltext ABC123                 # full text
zotero-cli get children ABC123                 # attachments and notes

# Edit item metadata
zotero-cli edit ABC123 --title "New Title"
zotero-cli edit ABC123 --add-tags "reviewed,important" --date "2024"

# Notes and annotations
zotero-cli notes list ABC123
zotero-cli notes create --item-key ABC123 --text "My note" --tags "idea"
zotero-cli notes create --item-key ABC123 --text -   # read from stdin
zotero-cli ann list ABC123                    # annotations (short alias)
zotero-cli ann search "highlight text"

# Add items
zotero-cli add doi 10.1038/s41586-021-03819-2
zotero-cli add url https://arxiv.org/abs/2301.00001
zotero-cli add file /path/to/paper.pdf

# Collections and tags
zotero-cli coll list                          # list collections (short alias)
zotero-cli coll search "PhD Research"
zotero-cli tags list

# Semantic search database
zotero-cli db update
zotero-cli db update --fulltext --force-rebuild
zotero-cli db status

# Library and duplicates
zotero-cli library info
zotero-cli duplicates find
```

### Verbose mode

Add `-v` anywhere to see progress messages (e.g., which API calls are made):

```bash
zotero-cli -v search "CRISPR"
```

## 📑 PDF Annotation Extraction

Zotero MCP includes advanced PDF annotation extraction capabilities:

- **Direct PDF Processing**: Extract annotations directly from PDF files, even if they're not yet indexed by Zotero
- **Enhanced Search**: Search through PDF annotations and comments
- **Image Annotation Support**: Extract image annotations from PDFs
- **Seamless Integration**: Works alongside Zotero's native annotation system

For optimal annotation extraction, it is **highly recommended** to install the [Better BibTeX plugin](https://retorque.re/zotero-better-bibtex/installation/) for Zotero. The annotation-related functions have been primarily tested with this plugin and provide enhanced functionality when it's available.


The first time you use PDF annotation features, the necessary tools will be automatically downloaded.

## 📚 Available Tools

### 🧠 Semantic Search Tools
- `zotero_semantic_search`: AI-powered similarity search with embedding models
- `zotero_update_search_database`: Manually update the semantic search database
- `zotero_get_search_database_status`: Check database status and configuration

### 🔍 Search Tools
- `zotero_search_items`: Search your library by keywords
- `zotero_advanced_search`: Perform complex searches with multiple criteria
- `zotero_get_collections`: List collections
- `zotero_get_collection_items`: Get items in a collection
- `zotero_get_tags`: List all tags
- `zotero_get_recent`: Get recently added items
- `zotero_search_by_tag`: Search your library using custom tag filters

### 📚 Content Tools
- `zotero_get_item_metadata`: Get detailed metadata (supports BibTeX export via `format="bibtex"`)
- `zotero_get_item_fulltext`: Get full text content
- `zotero_get_item_children`: Get attachments and notes

### 📝 Annotation & Notes Tools
- `zotero_get_annotations`: Get annotations (including direct PDF extraction)
- `zotero_get_notes`: Retrieve notes from your Zotero library
- `zotero_search_notes`: Search in notes and annotations (including PDF-extracted)
- `zotero_create_note`: Create a new note for an item (beta feature)

### 📊 Scite Citation Intelligence Tools
- `scite_enrich_item`: Get Scite citation tallies and retraction alerts for a paper
- `scite_enrich_search`: Search your Zotero library with Scite-enriched results (tallies + alerts inline)
- `scite_check_retractions`: Scan items for retractions and editorial notices

### 📦 Item & Collection Management Tools
- `zotero_add_by_doi`: Add a paper by DOI with automatic metadata and open-access PDF attachment
- `zotero_add_by_url`: Add a paper by URL (arXiv, DOI URLs, and general webpages)
- `zotero_add_from_file`: Import a local PDF or EPUB file with automatic DOI extraction
- `zotero_create_collection`: Create a new collection (folder/project) in your library
- `zotero_search_collections`: Search for collections by name to find their keys
- `zotero_manage_collections`: Add or remove items from collections
- `zotero_update_item`: Update metadata for an existing item (title, tags, abstract, date, etc.)
- `zotero_find_duplicates`: Find duplicate items by title and/or DOI
- `zotero_merge_duplicates`: Merge duplicate items with dry-run preview; consolidates all child items
- `zotero_get_pdf_outline`: Extract the table of contents / outline from a PDF attachment
- `zotero_search_by_citation_key`: Look up items by BetterBibTeX citation key (with Extra field fallback)

## 🧪 Testing

### Unit Tests
```bash
uv run pytest tests/     # 294 tests, ~2 seconds
```

### Integration Test Plan
A 45-point live integration test plan is included at `docs/integration-test-plan.md`. It's designed to be given to Claude in Claude Desktop, which will execute each test against your real Zotero library. Tests cover all tools, PDF attachment cascade, attach_mode, BetterBibTeX lookups, and multi-step showcase prompts. See the file for full instructions.

## 🔍 Troubleshooting

### General Issues
- **No results found**: Ensure Zotero is running and the local API is enabled. You need to toggle on `Allow other applications on this computer to communicate with Zotero` in Zotero preferences.
- **Can't connect to library**: Check your API key and library ID if using web API
- **Full text not available**: Make sure you're using Zotero 7+ for local full-text access
- **Local library limitations**: Some functionality (tagging, library modifications) may not work with local JS API. Consider using web library setup for full functionality. (See the [docs](docs/getting-started.md#local-library-limitations) for more info.)
- **Installation/search option switching issues**: Database problems from changing install methods or search options can often be resolved with `zotero-mcp update-db --force-rebuild`

### Semantic Search Issues
- **"Missing required environment variables" when running update-db**: Run `zotero-mcp setup` to configure your environment, or the CLI will automatically load settings from your MCP client config (e.g., Claude Desktop)
- **ChromaDB / stale embedding model errors**: If you changed embedding models and see 404 errors (e.g., `text-embedding-004 is not found`), run `zotero-mcp update-db --force-rebuild` to recreate the collection with your current model. If that doesn't work, delete `~/.config/zotero-mcp/chroma_db/` and rebuild.
- **Database update takes long**: By default, `update-db` is fast (metadata-only). For comprehensive indexing with full-text, use `--fulltext` flag. Use `--limit` parameter for testing: `zotero-mcp update-db --limit 100`
- **Semantic search returns no results**: Ensure the database is initialized with `zotero-mcp update-db` and check status with `zotero-mcp db-status`
- **Limited search quality**: For better semantic search results, use `zotero-mcp update-db --fulltext` to index full-text content (requires local Zotero setup)
- **OpenAI/Gemini/Voyage API errors**: Verify your API keys are correctly set and have sufficient credits/quota

### Update Issues
- **Update command fails**: Check your internet connection and try `zotero-mcp update --force`
- **Configuration lost after update**: The update process preserves configs automatically, but check `~/.config/zotero-mcp/` for backup files

## 📄 License

MIT
