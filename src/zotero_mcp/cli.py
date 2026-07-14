"""
Command-line interface for Zotero MCP server.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# NOTE: Do NOT import zotero_mcp.server at module level.
# That triggers heavy imports (FastMCP, ChromaDB, sentence-transformers, torch)
# which take several seconds. Import lazily only when needed (serve command).
# This allows CLI commands like update-db to print "Starting up..." instantly.


def obfuscate_sensitive_value(value, keep_chars=4):
    """Obfuscate sensitive values by showing only the first few characters."""
    if not value or not isinstance(value, str):
        return value
    if len(value) <= keep_chars:
        return "*" * len(value)
    return value[:keep_chars] + "*" * (len(value) - keep_chars)


def obfuscate_config_for_display(config):
    """Create a copy of config with sensitive values obfuscated."""
    if not isinstance(config, dict):
        return config

    obfuscated = config.copy()
    sensitive_keys = [
        "ZOTERO_API_KEY",
        "ZOTERO_LIBRARY_ID",
        "ZOTERO_WEBDAV_URL",
        "ZOTERO_WEBDAV_USERNAME",
        "ZOTERO_WEBDAV_PASSWORD",
        "API_KEY",
        "LIBRARY_ID",
        "WEBDAV_URL",
        "WEBDAV_USERNAME",
        "WEBDAV_PASSWORD",
    ]

    for key in sensitive_keys:
        if key in obfuscated:
            obfuscated[key] = obfuscate_sensitive_value(obfuscated[key])

    return obfuscated


def load_claude_desktop_env_vars():
    """Load Zotero environment variables from Claude Desktop config unless globally disabled."""
    # Global guard to skip Claude detection entirely
    if str(os.environ.get("ZOTERO_NO_CLAUDE", "")).lower() in ("1", "true", "yes"):
        return {}
    from zotero_mcp.setup_helper import find_claude_config

    try:
        config_path = find_claude_config()
        if not config_path or not config_path.exists():
            return {}

        with open(config_path) as f:
            config = json.load(f)

        # Extract Zotero MCP server environment variables
        mcp_servers = config.get("mcpServers", {})
        zotero_config = mcp_servers.get("zotero", {})
        env_vars = zotero_config.get("env", {})

        return env_vars

    except Exception:
        return {}


def load_standalone_env_vars():
    """Load environment variables from standalone config (~/.config/zotero-mcp/config.json)."""
    from zotero_mcp.config import ConfigError, load_config

    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"Warning: standalone config could not be loaded: {exc}", file=sys.stderr)
        return {}
    return cfg.get("client_env", {}) or {}


def apply_environment_variables(env_vars):
    """Apply environment variables to current process."""
    for key, value in env_vars.items():
        if key not in os.environ:  # Don't override existing env vars
            os.environ[key] = str(value)


def _save_zotero_db_path_to_config(config_path: Path, db_path: str) -> None:
    """
    Save the Zotero database path to the configuration file.

    This allows users to specify --db-path once and have it remembered
    for subsequent runs without needing to specify it again.

    Args:
        config_path: Path to the configuration file
        db_path: Path to the Zotero database file
    """
    try:
        # Ensure config directory exists
        config_path.parent.mkdir(parents=True, exist_ok=True)

        # Load existing config or create new one
        full_config = {}
        if config_path.exists():
            try:
                with open(config_path) as f:
                    full_config = json.load(f)
            except Exception:
                pass

        # Ensure semantic_search section exists
        if "semantic_search" not in full_config:
            full_config["semantic_search"] = {}

        # Save the db_path
        full_config["semantic_search"]["zotero_db_path"] = db_path

        # Write back to file
        with open(config_path, "w") as f:
            json.dump(full_config, f, indent=2)
        # The config can hold credentials (API/embedding keys) — keep it
        # owner-only. Best-effort; no-op on platforms without POSIX perms.
        try:
            os.chmod(config_path, 0o600)
        except OSError:
            pass

        print(f"Saved Zotero database path to config: {config_path}")

    except Exception as e:
        print(f"Warning: Could not save db_path to config: {e}")


def _semantic_config_path(path_arg: str | None) -> Path:
    return Path(path_arg) if path_arg else Path.home() / ".config" / "zotero-mcp" / "config.json"


def _warmup_reranker_in_background() -> None:
    """Preload the reranker (if enabled) off the request path — see issue #283.

    Runs in a daemon thread so server startup is never delayed and a failed or
    slow model load can never crash the server. No-op when the optional
    ``[semantic]`` extra isn't installed or the reranker is disabled.
    """
    import threading

    def _run() -> None:
        try:
            from zotero_mcp.semantic_search import warmup_reranker
        except Exception:
            return  # semantic extra not installed
        try:
            config_path = str(_semantic_config_path(None))
            if warmup_reranker(config_path):
                print("Reranker warmed up.", file=sys.stderr)
        except Exception:
            pass  # best-effort: never let warmup break serving

    threading.Thread(target=_run, daemon=True, name="zmcp-reranker-warmup").start()


def _print_update_stats(stats: dict) -> None:
    is_batch = stats.get("batch_mode") or stats.get("batch_submitted")
    label = "OpenAI batch submission" if is_batch else "Database update"
    outcome = "failed" if stats.get("error") else "completed"
    print(f"\n{label} {outcome}:")
    print(f"- Total items: {stats.get('total_items', 0)}")
    print(f"- Processed: {stats.get('processed_items', 0)}")
    if stats.get("batch_submitted"):
        print(f"- Submitted: {stats.get('submitted_items', 0)}")
        print(f"- Estimated new items: {stats.get('estimated_added_items', 0)}")
        print(f"- Estimated existing items: {stats.get('estimated_updated_items', 0)}")
    else:
        print(f"- Added: {stats.get('added_items', 0)}")
        print(f"- Updated: {stats.get('updated_items', 0)}")
    print(f"- Skipped: {stats.get('skipped_items', 0)}")
    print(f"- Errors: {stats.get('errors', 0)}")
    print(f"- Duration: {stats.get('duration', 'Unknown')}")
    if stats.get("batch_submitted"):
        print(f"- Batch run: {stats.get('batch_run_id')}")
        print(f"- Manifest: {stats.get('batch_manifest')}")
        for batch_id in stats.get("batch_ids", []):
            print(f"- Batch ID: {batch_id}")
        print("\nNext steps:")
        print("  zotero-mcp openai-batch-status")
        print("  zotero-mcp openai-batch-import")


def _print_batch_status(status: dict) -> None:
    print("=== OpenAI Batch Status ===")
    print(f"Run: {status.get('run_id')}")
    print(f"Model: {status.get('model')}")
    print(f"Manifest: {status.get('manifest_path')}")
    print(f"Force rebuild: {status.get('force_full_rebuild', False)}")
    for batch in status.get("batches", []):
        counts = batch.get("request_counts") or {}
        if not isinstance(counts, dict):
            counts = {}
        print()
        print(f"Batch: {batch.get('batch_id')}")
        print(f"- Status: {batch.get('status')}")
        print(f"- Requests: {batch.get('request_count', counts.get('total', 'Unknown'))}")
        if counts:
            print(f"- Completed: {counts.get('completed', 0)}")
            print(f"- Failed: {counts.get('failed', 0)}")
        print(f"- Imported: {batch.get('imported_at') or 'No'}")


def _print_batch_import(stats: dict) -> None:
    print("=== OpenAI Batch Import ===")
    print(f"Run: {stats.get('run_id')}")
    print(f"Manifest: {stats.get('manifest_path')}")
    print(f"- Batches seen: {stats.get('batches_seen', 0)}")
    print(f"- Batches imported: {stats.get('batches_imported', 0)}")
    print(f"- Batches skipped: {stats.get('batches_skipped', 0)}")
    print(f"- Imported items: {stats.get('imported_items', 0)}")
    print(f"- Added: {stats.get('added_items', 0)}")
    print(f"- Updated: {stats.get('updated_items', 0)}")
    print(f"- Failed rows: {stats.get('failed_items', 0)}")
    print(f"- Missing rows: {stats.get('missing_items', 0)}")
    if stats.get("errors"):
        print("\nWarnings/errors:")
        for error in stats["errors"][:20]:
            print(f"- {error}")
        if len(stats["errors"]) > 20:
            print(f"- ... {len(stats['errors']) - 20} more")


def setup_zotero_environment():
    """Setup Zotero environment for CLI commands."""
    # Load standalone env first so global flags (e.g., ZOTERO_NO_CLAUDE) take effect
    standalone_env_vars = load_standalone_env_vars()
    apply_environment_variables(standalone_env_vars)

    # Respect global switch to disable Claude detection
    no_claude = str(os.environ.get("ZOTERO_NO_CLAUDE", "")).lower() in ("1", "true", "yes")

    # Load and apply Claude Desktop env unless disabled
    if not no_claude:
        claude_env_vars = load_claude_desktop_env_vars()
        apply_environment_variables(claude_env_vars)

    # Local-first default for reads. If neither standalone config, Claude
    # config, nor process env chooses a mode, try Zotero Desktop first and let
    # client.py fall back to web credentials when auto-local is unreachable.
    prefer_local = str(os.environ.get("ZOTERO_PREFER_LOCAL", "true")).lower() not in ("0", "false", "no")
    if prefer_local and "ZOTERO_LOCAL" not in os.environ:
        fallback_env_vars = {
            "ZOTERO_LOCAL": "true",
            "ZOTERO_LOCAL_AUTO": "true",
        }
        if not os.environ.get("ZOTERO_LIBRARY_ID"):
            fallback_env_vars["ZOTERO_LIBRARY_ID"] = "0"
        apply_environment_variables(fallback_env_vars)


def _run_doctor():
    """Print setup diagnostics without starting the MCP server."""
    from zotero_mcp.config import ConfigError, get_config_path, load_config

    print("=== Zotero MCP Doctor ===")

    config_path = get_config_path()
    try:
        config = load_config(config_path, missing_ok=False)
        print(f"[OK] Config: {config_path}")
    except ConfigError as exc:
        config = {}
        print(f"[WARN] Config: {exc}")

    setup_zotero_environment()

    local_mode = str(os.environ.get("ZOTERO_LOCAL", "")).lower() in ("1", "true", "yes")
    print(f"[OK] Mode: {'local' if local_mode else 'web'}")

    api_key = os.environ.get("ZOTERO_API_KEY")
    if api_key:
        try:
            from zotero_mcp import client as _client

            zot = _client.get_web_zotero_client() or _client.get_zotero_client()
            if hasattr(zot, "key_info"):
                zot.key_info()
            print("[OK] Zotero API key: accepted")
        except Exception as exc:
            print(f"[WARN] Zotero API key: could not verify ({exc})")
    else:
        print("[INFO] Zotero API key: not configured")

    semantic_cfg = config.get("semantic_search", {}) if isinstance(config, dict) else {}
    db_path = semantic_cfg.get("zotero_db_path") or os.environ.get("ZOTERO_DB_PATH")
    if db_path:
        path = Path(db_path)
        if path.exists():
            print(f"[OK] Local Zotero DB: {path}")
        else:
            print(f"[WARN] Local Zotero DB not found: {path}")
    else:
        print("[INFO] Local Zotero DB path: not configured")

    try:
        from zotero_mcp.fts_index import get_fts_index

        fts = get_fts_index()
        if fts.exists:
            print("[OK] FTS sidecar: exists")
        else:
            print("[WARN] FTS sidecar: not built (run 'zotero-mcp build-fts' to build)")
    except Exception as exc:
        print(f"[WARN] FTS sidecar: could not inspect ({exc})")

    try:
        from zotero_mcp.semantic_search import ZoteroSemanticSearch

        ss = ZoteroSemanticSearch()
        if hasattr(ss, "chroma_client") and ss.chroma_client:
            info = ss.chroma_client.get_collection_info()
            count = info.get("count", "unknown") if isinstance(info, dict) else "unknown"
            print(f"[OK] Semantic index: {count} document(s)")
        else:
            print("[INFO] Semantic index: not initialized")
    except Exception as exc:
        print(f"[INFO] Semantic index: not available ({type(exc).__name__}: {exc})")


def _normalize_help_args(argv: list[str]) -> list[str]:
    """Support `zotero-mcp help [command]` in addition to argparse's `--help`."""
    if not argv or argv[0] != "help":
        return argv
    if len(argv) == 1:
        return ["--help"]
    return [*argv[1:], "--help"]


def main():
    """Main entry point for the CLI."""
    from zotero_mcp._version import __version__

    parser = argparse.ArgumentParser(
        description="Zotero Model Context Protocol server",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "OpenAI Batch API indexing:\n"
            "  zotero-mcp update-db --openai-batch     Submit embeddings asynchronously\n"
            "  zotero-mcp openai-batch-status          Check submitted batch status\n"
            "  zotero-mcp openai-batch-import          Import completed embeddings\n"
            "  zotero-mcp help update-db               Show update-db options\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"zotero-mcp {__version__}")

    # Create subparsers for different commands
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # Server command (default behavior)
    server_parser = subparsers.add_parser("serve", help="Run the MCP server")
    server_parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http", "sse"],
        default="stdio",
        help="Transport to use (default: stdio)",
    )
    server_parser.add_argument(
        "--host",
        default="localhost",
        help="Host to bind to for SSE transport (default: localhost)",
    )
    server_parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port to bind to for SSE transport (default: 8000)",
    )

    # Setup command
    setup_parser = subparsers.add_parser("setup", help="Configure zotero-mcp (Claude Desktop or standalone)")
    setup_parser.add_argument(
        "--no-local", action="store_true", help="Configure for Zotero Web API instead of local API"
    )
    setup_parser.add_argument("--api-key", help="Zotero API key (only needed with --no-local)")
    setup_parser.add_argument("--library-id", help="Zotero library ID (only needed with --no-local)")
    setup_parser.add_argument(
        "--library-type",
        choices=["user", "group"],
        default="user",
        help="Zotero library type (only needed with --no-local)",
    )
    setup_parser.add_argument(
        "--no-claude",
        action="store_true",
        help="Skip Claude Desktop config; write standalone config for web-based clients",
    )
    setup_parser.add_argument("--config-path", help="Path to Claude Desktop config file")
    setup_parser.add_argument("--skip-semantic-search", action="store_true", help="Skip semantic search configuration")
    setup_parser.add_argument(
        "--semantic-config-only", action="store_true", help="Only configure semantic search, skip Zotero setup"
    )

    # Update database command
    update_db_parser = subparsers.add_parser("update-db", help="Update semantic search database")
    update_db_parser.add_argument("--force-rebuild", action="store_true", help="Force complete rebuild of the database")
    update_db_parser.add_argument("--limit", type=int, help="Limit number of items to process (for testing)")
    update_db_parser.add_argument(
        "--fulltext",
        action="store_true",
        help="Extract fulltext content from local Zotero database (slower but more comprehensive)",
    )
    update_db_parser.add_argument("--config-path", help="Path to semantic search configuration file")
    update_db_parser.add_argument("--db-path", help="Path to Zotero database file (zotero.sqlite), overrides config")
    openai_batch_group = update_db_parser.add_mutually_exclusive_group()
    openai_batch_group.add_argument(
        "--openai-batch",
        dest="openai_batch",
        action="store_true",
        help="Submit OpenAI embeddings through the asynchronous Batch API",
    )
    openai_batch_group.add_argument(
        "--no-openai-batch",
        dest="openai_batch",
        action="store_false",
        help="Use realtime embeddings even if OpenAI Batch API is enabled in config",
    )
    update_db_parser.set_defaults(openai_batch=None)

    # OpenAI batch lifecycle commands
    batch_status_parser = subparsers.add_parser("openai-batch-status", help="Show OpenAI Batch API status")
    batch_status_parser.add_argument(
        "--batch-id", action="append", help="Specific OpenAI batch ID to inspect; can be repeated"
    )
    batch_status_parser.add_argument("--config-path", help="Path to semantic search configuration file")

    batch_import_parser = subparsers.add_parser(
        "openai-batch-import", help="Import completed OpenAI batch embeddings"
    )
    batch_import_parser.add_argument(
        "--batch-id", action="append", help="Specific OpenAI batch ID to import; can be repeated"
    )
    batch_import_parser.add_argument("--config-path", help="Path to semantic search configuration file")

    # Database status command
    db_status_parser = subparsers.add_parser("db-status", help="Show semantic search database status")
    db_status_parser.add_argument("--config-path", help="Path to semantic search configuration file")

    # DB inspect command (sample and filter indexed docs; also supports stats)
    inspect_parser = subparsers.add_parser(
        "db-inspect", help="Inspect indexed documents or show aggregate stats for the semantic DB"
    )
    inspect_parser.add_argument("--limit", type=int, default=20, help="How many records to show (default: 20)")
    inspect_parser.add_argument("--filter", dest="filter_text", help="Substring to match in title or creators")
    inspect_parser.add_argument("--show-documents", action="store_true", help="Show beginning of stored document text")
    inspect_parser.add_argument("--stats", action="store_true", help="Show aggregate stats (formerly db-stats)")
    inspect_parser.add_argument("--config-path", help="Path to semantic search configuration file")

    # Update command
    update_parser = subparsers.add_parser("update", help="Update zotero-mcp to the latest version")
    update_parser.add_argument("--check-only", action="store_true", help="Only check for updates without installing")
    update_parser.add_argument("--force", action="store_true", help="Force update even if already up to date")
    update_parser.add_argument(
        "--method", choices=["pip", "uv", "conda", "pipx"], help="Override auto-detected installation method"
    )

    # Version command
    subparsers.add_parser("version", help="Print version information")

    # Setup info command
    subparsers.add_parser("setup-info", help="Show installation path and configuration info for MCP clients")

    # Doctor command
    subparsers.add_parser("doctor", help="Diagnose zotero-mcp setup (config, API key, local DB, FTS, semantic index)")

    # Build FTS command
    build_fts_parser = subparsers.add_parser(
        "build-fts", help="Build/rebuild the FTS sidecar index for notes and annotations"
    )
    build_fts_parser.add_argument("--db-path", help="Path to Zotero database file (zotero.sqlite)")

    # Search command
    search_parser = subparsers.add_parser("search", help="Search Zotero library from the command line")
    search_parser.add_argument("query", help="Search query")
    search_parser.add_argument("--limit", type=int, default=10, help="Max results (default: 10)")
    search_parser.add_argument("--type", dest="item_type", help="Filter by item type (e.g., journalArticle, book)")

    # Export command
    export_parser = subparsers.add_parser("export", help="Export Zotero items in various formats")
    export_parser.add_argument("keys", help="Comma-separated item keys to export")
    export_parser.add_argument(
        "--format", dest="export_format", default="bibtex", help="Export format (bibtex, ris, csljson, etc.)"
    )

    args = parser.parse_args(_normalize_help_args(sys.argv[1:]))

    # If no command is provided, default to 'serve'
    if not args.command:
        args.command = "serve"
        # Also set default transport since we're defaulting to serve
        args.transport = "stdio"

    if args.command == "version":
        from zotero_mcp._version import __version__

        print(f"Zotero MCP v{__version__}")
        sys.exit(0)

    elif args.command == "setup-info":
        # Setup Zotero environment variables
        setup_zotero_environment()

        # Get the installation path
        executable_path = shutil.which("zotero-mcp")
        if not executable_path:
            executable_path = sys.executable + " -m zotero_mcp"

        # Determine whether Claude is disabled globally
        no_claude = str(os.environ.get("ZOTERO_NO_CLAUDE", "")).lower() in ("1", "true", "yes")

        # Load current environment configurations
        standalone_env_vars = load_standalone_env_vars()
        claude_env_vars = {} if no_claude else load_claude_desktop_env_vars()

        # Choose which env to display: prefer standalone if present or if Claude disabled
        display_env = (
            standalone_env_vars if (no_claude or standalone_env_vars) else (claude_env_vars or {"ZOTERO_LOCAL": "true"})
        )

        print("=== Zotero MCP Setup Information ===")
        print()
        print("🔧 Installation Details:")
        print(f"  Command path: {executable_path}")
        print(f"  Python path: {sys.executable}")

        # Detect installation method
        try:
            # Check if installed via uv
            result = subprocess.run(["uv", "tool", "list"], capture_output=True, text=True, timeout=5)
            if "zotero-mcp-server" in result.stdout or "zotero-mcp" in result.stdout:
                print("  Installation method: uv tool")
            else:
                # Check pip
                result = subprocess.run(
                    [sys.executable, "-m", "pip", "show", "zotero-mcp-server"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if result.returncode == 0:
                    print("  Installation method: pip")
                else:
                    print("  Installation method: unknown")
        except (subprocess.TimeoutExpired, subprocess.SubprocessError, FileNotFoundError):
            print("  Installation method: unknown")

        print()
        print("⚙️  MCP Client Configuration:")
        print(f"  Command: {executable_path}")
        print("  Arguments: [] (empty)")

        # Show environment variables with obfuscated sensitive values
        obfuscated_env_vars = obfuscate_config_for_display(display_env)
        print(f"  Environment (single-line): {json.dumps(obfuscated_env_vars, separators=(',', ':'))}")
        print("  💡 Note: This shows client config. Shell variables may override for CLI use.")
        print(f"  Claude integration: {'disabled' if no_claude else 'enabled'}")

        # Only show Claude Desktop config if not globally disabled
        if not no_claude:
            print()
            print("For Claude Desktop (claude_desktop_config.json):")
            config_snippet = {"mcpServers": {"zotero": {"command": executable_path, "env": obfuscated_env_vars}}}
            print(json.dumps(config_snippet, indent=2))

        # Show semantic search database info with detailed statistics
        print()
        print("🧠 Semantic Search Database:")

        # Check for semantic search config
        config_path = Path.home() / ".config" / "zotero-mcp" / "config.json"
        if config_path.exists():
            try:
                from zotero_mcp.semantic_search import create_semantic_search

                # Get database status (similar to db-status command)
                search = create_semantic_search(str(config_path))
                status = search.get_database_status()

                collection_info = status.get("collection_info", {})

                print("  Status: ✅ Configuration file found")
                print(f"  Config path: {config_path}")
                print(f"  Collection: {collection_info.get('name', 'Unknown')}")
                print(f"  Document count: {collection_info.get('count', 0)}")
                print(f"  Embedding model: {collection_info.get('embedding_model', 'Unknown')}")
                print(f"  Database path: {collection_info.get('persist_directory', 'Unknown')}")

                update_config = status.get("update_config", {})
                batch_config = status.get("openai_batch", {})
                print(f"  Auto update: {update_config.get('auto_update', False)}")
                print(f"  Update frequency: {update_config.get('update_frequency', 'manual')}")
                print(f"  Last update: {update_config.get('last_update', 'Never')}")
                print(f"  Should update: {status.get('should_update', False)}")
                print(f"  OpenAI Batch API: {'active' if batch_config.get('active') else 'inactive'}")

                if collection_info.get("error"):
                    print(f"  Error: {collection_info['error']}")

            except Exception as e:
                print("  Status: ⚠️ Configuration found but database error")
                print(f"  Error: {e}")
        else:
            print("  Status: ⚠️ Not configured")
            print("  💡 Run 'zotero-mcp setup' to configure semantic search")

        sys.exit(0)

    elif args.command == "doctor":
        _run_doctor()
        sys.exit(0)

    elif args.command == "build-fts":
        setup_zotero_environment()
        from zotero_mcp.fts_index import get_fts_index

        # Find the Zotero DB path
        db_path = args.db_path
        if not db_path:
            from zotero_mcp.config import load_config

            config = load_config()
            db_path = config.get("semantic_search", {}).get("zotero_db_path")
        if not db_path:
            db_path = os.environ.get("ZOTERO_DB_PATH")
        if not db_path:
            # Try default locations
            import platform

            if platform.system() == "Windows":
                default_path = Path.home() / "AppData" / "Roaming" / "Zotero" / "Zotero" / "Profiles"
            elif platform.system() == "Darwin":
                default_path = Path.home() / "Library" / "Application Support" / "Zotero" / "Profiles"
            else:
                default_path = Path.home() / ".zotero" / "profiles"

            if default_path.exists():
                for profile_dir in default_path.iterdir():
                    candidate = profile_dir / "zotero.sqlite"
                    if candidate.exists():
                        db_path = str(candidate)
                        break

        if not db_path or not Path(db_path).exists():
            print("[ERROR] Could not find Zotero database. Pass --db-path or set ZOTERO_DB_PATH.")
            sys.exit(1)

        print(f"Building FTS index from: {db_path}")
        import sqlite3

        try:
            zotero_conn = sqlite3.connect(f"file:{db_path}?mode=immutable", uri=True)
            fts = get_fts_index()
            count = fts.populate_from_zotero_db(zotero_conn)
            zotero_conn.close()
            print(f"[OK] FTS index built: {count} entries indexed")
        except Exception as e:
            print(f"[ERROR] Failed to build FTS index: {e}")
            sys.exit(1)
        sys.exit(0)

    elif args.command == "search":
        setup_zotero_environment()
        from zotero_mcp import client as _client
        from zotero_mcp import utils as _utils

        try:
            zot = _client.get_zotero_client()
            params = {"q": args.query, "qmode": "titleCreatorYear", "limit": args.limit}
            if args.item_type:
                params["itemType"] = args.item_type
            else:
                params["itemType"] = "-attachment -note -annotation"
            zot.add_parameters(**params)
            items = zot.items()

            if not items:
                print(f"No items found matching '{args.query}'")
                sys.exit(0)

            print(f"Found {len(items)} items matching '{args.query}':\n")
            for i, item in enumerate(items, 1):
                data = item.get("data", {})
                title = data.get("title", "Untitled")
                creators = _utils.format_creators(data.get("creators", []), max_authors=3)
                date = data.get("date", "")[:4] if data.get("date") else ""
                key = item.get("key", "")
                item_type = data.get("itemType", "")
                print(f"{i}. [{item_type}] {title} ({date})")
                print(f"   Key: {key} | Authors: {creators}")
        except Exception as e:
            print(f"[ERROR] Search failed: {e}")
            sys.exit(1)
        sys.exit(0)

    elif args.command == "export":
        setup_zotero_environment()
        from zotero_mcp import client as _client

        keys = [k.strip() for k in args.keys.split(",") if k.strip()]
        if not keys:
            print("[ERROR] No item keys provided.")
            sys.exit(1)

        try:
            zot = _client.get_zotero_client()
            joined = ",".join(keys)

            # Try API-based export
            import requests as _requests

            base_url = "http://localhost:23119/api/users/0"
            resp = _requests.get(
                f"{base_url}/items",
                params={"itemKey": joined, "format": args.export_format},
                headers={"Zotero-API-Version": "3"},
                timeout=30,
            )
            if resp.status_code == 200 and resp.text.strip():
                print(resp.text.strip())
            else:
                # Fallback: BibTeX generation
                if args.export_format == "bibtex":
                    items = zot.items(itemKey=joined)
                    for item in items:
                        print(_client.generate_bibtex(item))
                        print()
                else:
                    print(f"[ERROR] Export failed (HTTP {resp.status_code})")
                    sys.exit(1)
        except Exception as e:
            print(f"[ERROR] Export failed: {e}")
            sys.exit(1)
        sys.exit(0)

    elif args.command == "setup":
        from zotero_mcp.setup_helper import main as setup_main

        sys.exit(setup_main(args))

    elif args.command == "update-db":
        # Setup Zotero environment variables
        setup_zotero_environment()

        from zotero_mcp.semantic_search import create_semantic_search

        # Determine config path
        config_path = _semantic_config_path(args.config_path)

        print(f"Using configuration: {config_path}")

        # Get optional db_path override from CLI
        db_path = getattr(args, "db_path", None)
        if db_path:
            print(f"Using custom Zotero database: {db_path}")
            # Save the db_path to config file for future use
            _save_zotero_db_path_to_config(config_path, db_path)

        try:
            # Create semantic search instance with optional db_path override
            search = create_semantic_search(str(config_path), db_path=db_path)
            if args.openai_batch is True and search.chroma_client.embedding_model != "openai":
                print("Error: --openai-batch requires ZOTERO_EMBEDDING_MODEL=openai", file=sys.stderr)
                sys.exit(1)

            print("Starting database update...")
            if args.fulltext:
                from zotero_mcp.utils import is_local_mode

                if not is_local_mode():
                    print(
                        "Error: --fulltext requires local mode but ZOTERO_LOCAL is not enabled.\n"
                        "Full-text indexing needs access to Zotero's local database.\n"
                        "Set ZOTERO_LOCAL=true or run 'zotero-mcp setup' to enable local mode.",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                print("Extracting full-text content from local Zotero database...")
            stats = search.update_database(
                force_full_rebuild=args.force_rebuild,
                limit=args.limit,
                extract_fulltext=args.fulltext,
                use_openai_batch=args.openai_batch,
            )

            _print_update_stats(stats)

            if stats.get("error"):
                print(f"Error: {stats['error']}")
                sys.exit(1)

        except Exception as e:
            print(f"Error updating database: {e}")
            sys.exit(1)

    elif args.command == "openai-batch-status":
        setup_zotero_environment()

        from zotero_mcp.semantic_search import create_semantic_search

        config_path = _semantic_config_path(args.config_path)
        try:
            search = create_semantic_search(str(config_path))
            status = search.get_openai_batch_status(batch_ids=args.batch_id)
            _print_batch_status(status)
        except Exception as e:
            print(f"Error getting OpenAI batch status: {e}")
            sys.exit(1)

    elif args.command == "openai-batch-import":
        setup_zotero_environment()

        from zotero_mcp.semantic_search import create_semantic_search

        config_path = _semantic_config_path(args.config_path)
        try:
            search = create_semantic_search(str(config_path))
            stats = search.import_openai_batch(batch_ids=args.batch_id)
            _print_batch_import(stats)
        except Exception as e:
            print(f"Error importing OpenAI batch: {e}")
            sys.exit(1)

    elif args.command == "db-status":
        # Setup Zotero environment variables
        setup_zotero_environment()

        from zotero_mcp.semantic_search import create_semantic_search

        # Determine config path
        config_path = args.config_path
        if not config_path:
            config_path = Path.home() / ".config" / "zotero-mcp" / "config.json"
        else:
            config_path = Path(config_path)

        try:
            # Create semantic search instance
            search = create_semantic_search(str(config_path))

            # Get database status
            status = search.get_database_status()

            print("=== Semantic Search Database Status ===")

            collection_info = status.get("collection_info", {})
            print(f"Collection: {collection_info.get('name', 'Unknown')}")
            print(f"Document count: {collection_info.get('count', 0)}")
            print(f"Embedding model: {collection_info.get('embedding_model', 'Unknown')}")
            print(f"Database path: {collection_info.get('persist_directory', 'Unknown')}")

            update_config = status.get("update_config", {})
            batch_config = status.get("openai_batch", {})
            print("\nUpdate configuration:")
            print(f"- Auto update: {update_config.get('auto_update', False)}")
            print(f"- Frequency: {update_config.get('update_frequency', 'manual')}")
            print(f"- Last update: {update_config.get('last_update', 'Never')}")
            print(f"- Should update: {status.get('should_update', False)}")
            print(f"- OpenAI Batch API: {'active' if batch_config.get('active') else 'inactive'}")

            if collection_info.get("error"):
                print(f"\nError: {collection_info['error']}")

        except Exception as e:
            print(f"Error getting database status: {e}")
            sys.exit(1)

    elif args.command == "db-inspect":
        # Setup Zotero environment variables
        setup_zotero_environment()

        from collections import Counter

        from zotero_mcp.semantic_search import create_semantic_search

        # Determine config path
        config_path = args.config_path
        if not config_path:
            config_path = Path.home() / ".config" / "zotero-mcp" / "config.json"
        else:
            config_path = Path(config_path)

        try:
            search = create_semantic_search(str(config_path))
            client = search.chroma_client
            col = client.collection

            if args.stats:
                # Show aggregate stats (merged from former db-stats)
                meta = col.get(include=["metadatas"])  # type: ignore
                metas = meta.get("metadatas", [])
                print("=== Semantic DB Inspection (Stats) ===")
                info = client.get_collection_info()
                print(f"Collection: {info.get('name')} @ {info.get('persist_directory')}")
                print(f"Count: {info.get('count')}")

                # Item type distribution
                item_types = [(m or {}).get("item_type", "") for m in metas]
                ct_types = Counter(item_types)
                print("Item types:")
                for t, c in ct_types.most_common(20):
                    print(f"  {t or '(missing)'}: {c}")

                # Fulltext coverage by type (pdf/html)
                coverage = {}
                for m in metas:
                    m = m or {}
                    t = m.get("item_type", "") or "(missing)"
                    cov = coverage.setdefault(t, {"total": 0, "with_fulltext": 0, "pdf": 0, "html": 0})
                    cov["total"] += 1
                    if m.get("has_fulltext"):
                        cov["with_fulltext"] += 1
                        src = (m.get("fulltext_source") or "").lower()
                        if src == "pdf":
                            cov["pdf"] += 1
                        elif src == "html":
                            cov["html"] += 1
                print("Fulltext coverage (by type):")
                for t, cov in coverage.items():
                    print(f"  {t}: {cov['with_fulltext']}/{cov['total']} (pdf:{cov['pdf']}, html:{cov['html']})")

                # Common titles (may indicate duplicates)
                titles = [(m or {}).get("title", "") for m in metas]
                from collections import Counter as _Counter

                ct_titles = _Counter([t for t in titles if t])
                common = [(t, c) for t, c in ct_titles.most_common(10)]
                if common:
                    print("Common titles:")
                    for t, c in common:
                        ts = str(t)
                        print(f"  {ts[:80]}{'...' if len(ts) > 80 else ''}: {c}")
                return

            include = ["metadatas"]
            if args.show_documents:
                include.append("documents")

            # Fetch up to limit; filter client-side if requested
            data = col.get(limit=args.limit, include=include)  # type: ignore[arg-type]

            print("=== Semantic DB Inspection ===")
            total = client.get_collection_info().get("count", 0)
            print(f"Total documents: {total}")
            print(f"Showing up to: {args.limit}")

            shown = 0
            for i, meta in enumerate(data.get("metadatas") or []):
                meta = meta or {}
                title = meta.get("title", "")
                creators = meta.get("creators", "")
                if args.filter_text:
                    needle = args.filter_text.lower()
                    if needle not in (title or "").lower() and needle not in (creators or "").lower():
                        continue
                print(f"- {title} | {creators}")
                if args.show_documents:
                    doc = (data.get("documents", [""])[i] or "").strip()
                    snippet = doc[:200].replace("\n", " ") + ("..." if len(doc) > 200 else "")
                    if snippet:
                        print(f"  doc: {snippet}")
                shown += 1
                if shown >= args.limit:
                    break

            if shown == 0:
                print("No records matched your filter.")

        except Exception as e:
            print(f"Error inspecting database: {e}")
            sys.exit(1)

    elif args.command == "update":
        from zotero_mcp.updater import update_zotero_mcp

        try:
            print("Checking for updates...")

            result = update_zotero_mcp(check_only=args.check_only, force=args.force, method=args.method)

            print("\n" + "=" * 50)
            print("UPDATE RESULTS")
            print("=" * 50)

            if args.check_only:
                print(f"Current version: {result.get('current_version', 'Unknown')}")
                print(f"Latest version: {result.get('latest_version', 'Unknown')}")
                print(f"Update needed: {result.get('needs_update', False)}")
                print(f"Status: {result.get('message', 'Unknown')}")
            else:
                if result.get("success"):
                    print("✅ Update completed successfully!")
                    print(
                        f"Version: {result.get('current_version', 'Unknown')} → {result.get('latest_version', 'Unknown')}"
                    )
                    print(f"Method: {result.get('method', 'Unknown')}")
                    print(f"Message: {result.get('message', '')}")

                    print("\n📋 Next steps:")
                    print("• All configurations have been preserved")
                    print("• Restart Claude Desktop if it's running")
                    print("• Your semantic search database is intact")
                    print("• Run 'zotero-mcp version' to verify the update")
                else:
                    print("❌ Update failed!")
                    print(f"Error: {result.get('message', 'Unknown error')}")

                    if backup_dir := result.get("backup_dir"):
                        print(f"\n🔄 Backup created at: {backup_dir}")
                        print("You can manually restore configurations if needed")

                    sys.exit(1)

        except Exception as e:
            print(f"❌ Update error: {e}")
            sys.exit(1)

    elif args.command == "serve":
        # Lazy import — triggers heavy dependencies (FastMCP, ChromaDB, etc.)
        from zotero_mcp.server import mcp

        # Get transport with a default value if not specified
        transport = getattr(args, "transport", "stdio")
        # Ensure environment is initialized (Claude config or standalone config)
        setup_zotero_environment()
        # If the reranker is enabled, warm it up in the background so the first
        # semantic search doesn't pay the ~tens-of-seconds model load inside the
        # request path and time out (issue #283). Daemon thread: never blocks
        # startup, never crashes the server if loading fails.
        _warmup_reranker_in_background()
        if transport == "stdio":
            mcp.run(transport="stdio")
        elif transport == "streamable-http":
            host = getattr(args, "host", "localhost")
            port = getattr(args, "port", 8000)
            mcp.run(transport="streamable-http", host=host, port=port)
        elif transport == "sse":
            host = getattr(args, "host", "localhost")
            port = getattr(args, "port", 8000)
            import warnings

            warnings.warn(
                "The SSE transport is deprecated and may be removed in a future version. New applications should use Streamable HTTP transport instead.",
                UserWarning,
            )
            mcp.run(transport="sse", host=host, port=port)


if __name__ == "__main__":
    main()
