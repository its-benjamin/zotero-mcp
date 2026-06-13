"""
Zotero client wrapper for MCP server.
"""

import asyncio
import contextvars
import functools
import inspect
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from markitdown import MarkItDown
from pyzotero import zotero

from zotero_mcp.rate_limiter import RateLimitedZotero
from zotero_mcp.utils import format_creators

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Client singleton cache — avoid rebuilding pyzotero instances on every call
# ---------------------------------------------------------------------------
_cached_client: RateLimitedZotero | None = None
_client_cache_ts: float = 0.0
_client_cache_ttl: float = 300.0  # 5 minutes
_client_first_success: bool = False  # Track first successful local connectivity check

# Separate caches for local and web clients
_cached_local_client: zotero.Zotero | None = None
_local_client_cache_ts: float = 0.0
_cached_web_client: RateLimitedZotero | None = None
_web_client_cache_ts: float = 0.0


def clear_client_cache() -> None:
    """Invalidate the cached Zotero client.

    Called by set_active_library() / clear_active_library() so the next
    get_zotero_client() builds a fresh client with the updated credentials.
    """
    global _cached_client, _client_cache_ts, _client_first_success
    global _cached_local_client, _local_client_cache_ts
    global _cached_web_client, _web_client_cache_ts
    _cached_client = None
    _client_cache_ts = 0.0
    _client_first_success = False
    _cached_local_client = None
    _local_client_cache_ts = 0.0
    _cached_web_client = None
    _web_client_cache_ts = 0.0

# Load environment variables
load_dotenv()

# Serialize all Zotero API access. The local API (port 23119) is single-threaded;
# concurrent requests from parallel MCP tools queue at the network layer and risk
# hitting pyzotero's 30s timeout. A process-local async lock ensures only one
# request is in-flight at a time. A ContextVar keeps nested decorated calls
# re-entrant within the same task.
_zotero_api_lock = asyncio.Lock()
_zotero_api_lock_depth: contextvars.ContextVar[int] = contextvars.ContextVar("zotero_api_lock_depth", default=0)


def with_zotero_api_lock(func):
    """Serialize Zotero API access across concurrent MCP tool tasks."""

    if inspect.iscoroutinefunction(func):

        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            depth = _zotero_api_lock_depth.get()
            if depth > 0:
                token = _zotero_api_lock_depth.set(depth + 1)
                try:
                    return await func(*args, **kwargs)
                finally:
                    _zotero_api_lock_depth.reset(token)

            async with _zotero_api_lock:
                token = _zotero_api_lock_depth.set(1)
                try:
                    return await func(*args, **kwargs)
                finally:
                    _zotero_api_lock_depth.reset(token)

        return async_wrapper

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        return func(*args, **kwargs)

    return wrapper


async def run_blocking(func, /, *args, **kwargs) -> Any:
    """Run blocking pyzotero, requests, or filesystem work off the event loop."""
    return await asyncio.to_thread(func, *args, **kwargs)


DEFAULT_ZOTERO_CALL_TIMEOUT_SECONDS = 30.0


class ZoteroAPITimeout(TimeoutError):
    """Raised when a blocking Zotero API call exceeds the configured budget."""


def get_zotero_call_timeout_seconds() -> float:
    """Return per-call Zotero API timeout in seconds."""
    raw = os.getenv("ZOTERO_MCP_CALL_TIMEOUT_SECONDS", "")
    if not raw:
        return DEFAULT_ZOTERO_CALL_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_ZOTERO_CALL_TIMEOUT_SECONDS
    if value <= 0:
        return DEFAULT_ZOTERO_CALL_TIMEOUT_SECONDS
    return value


async def run_zotero_call(func, /, *args, timeout: float | None = None, operation: str | None = None, **kwargs) -> Any:
    """Run a blocking Zotero API call with a bounded async wait."""
    effective_timeout = timeout if timeout is not None else get_zotero_call_timeout_seconds()
    call_name = operation or getattr(func, "__name__", repr(func))
    try:
        return await asyncio.wait_for(asyncio.to_thread(func, *args, **kwargs), timeout=effective_timeout)
    except TimeoutError as exc:
        raise ZoteroAPITimeout(
            f"Zotero API call timed out after {effective_timeout:g}s: {call_name}. "
            "Zotero Desktop may be busy or its local API may be stalled."
        ) from exc

# Runtime library override state — set by zotero_switch_library tool.
# When non-empty, these values override the corresponding environment variables
# in get_zotero_client(). Keys: "library_id", "library_type".
_active_library_override: dict[str, str] = {}

_TRUTHY = {"true", "yes", "1"}


def _env_flag(name: str) -> str:
    return os.getenv(name, "").strip().lower()


def _is_truthy_env(name: str) -> bool:
    return _env_flag(name) in _TRUTHY


def set_active_library(library_id: str, library_type: str) -> None:
    """Set runtime library override for all subsequent get_zotero_client() calls."""
    _active_library_override["library_id"] = library_id
    _active_library_override["library_type"] = library_type
    clear_client_cache()  # Force fresh client with new library


def clear_active_library() -> None:
    """Clear runtime library override, reverting to environment variable defaults."""
    _active_library_override.clear()
    clear_client_cache()  # Force fresh client with default library


def get_active_library() -> dict[str, str]:
    """Return the current active library override (empty dict if using defaults)."""
    return dict(_active_library_override)


@dataclass
class AttachmentDetails:
    """Details about a Zotero attachment."""

    key: str
    title: str
    filename: str
    content_type: str


def get_zotero_client() -> Any:
    """
    Get authenticated Zotero client using environment variables.

    If a runtime library override is active (via set_active_library()),
    those values take precedence over environment variables.

    Returns a cached singleton client when the cache is fresh, avoiding
    repeated pyzotero construction and the costly eager local-mode
    connectivity check. The cache is invalidated by set_active_library()
    and clear_active_library().

    Returns:
        A configured Zotero client instance.

    Raises:
        ValueError: If required environment variables are missing.
    """
    global _cached_client, _client_cache_ts, _client_first_success

    # Return cached client if still fresh
    now = time.monotonic()
    if _cached_client is not None and (now - _client_cache_ts) < _client_cache_ttl:
        return _cached_client

    # Runtime overrides take precedence over environment variables
    override = _active_library_override
    library_id = override.get("library_id") or os.getenv("ZOTERO_LIBRARY_ID")
    library_type = override.get("library_type") or os.getenv("ZOTERO_LIBRARY_TYPE", "user")
    api_key = os.getenv("ZOTERO_API_KEY")
    local = _is_truthy_env("ZOTERO_LOCAL")
    auto_local = _is_truthy_env("ZOTERO_LOCAL_AUTO")

    # For auto-local mode, prefer Zotero Desktop's local user library even
    # when web credentials also define a remote user ID. Explicit runtime
    # library switches still win, and group IDs are shared across local/web.
    client_library_id = library_id
    if local:
        if library_type == "user" and not override:
            client_library_id = "0"
        elif not client_library_id:
            client_library_id = "0"

    # For remote API, we need both library_id and api_key
    if not local and not (library_id and api_key):
        raise ValueError(
            "Missing required environment variables. Please set ZOTERO_LIBRARY_ID and ZOTERO_API_KEY, "
            "or use ZOTERO_LOCAL=true for local Zotero instance."
        )

    client = zotero.Zotero(
        library_id=client_library_id,
        library_type=library_type,
        api_key=api_key,
        local=local,
    )
    wrapped = RateLimitedZotero(client, enabled=not local)

    # Eagerly verify local Zotero is reachable so users get a clear message.
    # Skip the check after the first successful connection — saves 200-500ms
    # per tool call by avoiding the items(limit=1) round-trip.
    if local and not _client_first_success:
        try:
            client.items(limit=1)
            _client_first_success = True
        except Exception as exc:
            _logger.debug("Local Zotero connection check failed: %s", exc)
            if auto_local and library_id and api_key:
                web_client = zotero.Zotero(
                    library_id=library_id,
                    library_type=library_type,
                    api_key=api_key,
                    local=False,
                )
                wrapped = RateLimitedZotero(web_client)
                _cached_client = wrapped
                _client_cache_ts = now
                _client_first_success = True  # Web fallback succeeded
                return wrapped
            raise ConnectionError(
                "Could not connect to Zotero on localhost:23119. "
                "Make sure the Zotero desktop app is running and that "
                "'Allow other applications on this computer to communicate with Zotero' "
                "is enabled in Settings > Advanced."
            ) from exc

    _cached_client = wrapped
    _client_cache_ts = now
    return wrapped


def get_local_zotero_client() -> zotero.Zotero | None:
    """
    Get a local Zotero client for file access (WebDAV/local storage).

    This client connects to the local Zotero instance running on port 23119.
    It's useful for accessing PDF files stored via WebDAV when the main
    client is configured for web API.

    Results are cached for 5 minutes to avoid repeated client creation.

    Returns:
        A local Zotero client instance, or None if local Zotero is not available.
    """
    global _cached_local_client, _local_client_cache_ts

    now = time.monotonic()
    if _cached_local_client is not None and (now - _local_client_cache_ts) < _client_cache_ttl:
        return _cached_local_client

    try:
        # Create a local client - library_id 0 is the default for local
        client = zotero.Zotero(
            library_id="0",
            library_type="user",
            api_key=None,
            local=True,
        )
        # Test connection by making a simple request
        client.items(limit=1)
        _cached_local_client = client
        _local_client_cache_ts = now
        return client
    except Exception as e:
        _logger.debug("Failed to create local Zotero client: %s", e)
        return None


def get_web_zotero_client() -> Any | None:
    """
    Get a web API Zotero client for write operations.

    This client connects to the Zotero web API and can create/modify items.
    Requires ZOTERO_API_KEY and ZOTERO_LIBRARY_ID environment variables.

    Results are cached for 5 minutes to avoid repeated client creation.

    Returns:
        A web API Zotero client instance, or None if credentials are not available.
    """
    global _cached_web_client, _web_client_cache_ts

    now = time.monotonic()
    if _cached_web_client is not None and (now - _web_client_cache_ts) < _client_cache_ttl:
        return _cached_web_client

    library_id = os.getenv("ZOTERO_LIBRARY_ID")
    library_type = os.getenv("ZOTERO_LIBRARY_TYPE", "user")
    api_key = os.getenv("ZOTERO_API_KEY")

    if not library_id or not api_key:
        return None

    client = zotero.Zotero(
        library_id=library_id,
        library_type=library_type,
        api_key=api_key,
        local=False,
    )
    wrapped = RateLimitedZotero(client)
    _cached_web_client = wrapped
    _web_client_cache_ts = now
    return wrapped


def is_local_zotero_available() -> bool:
    """Check if local Zotero instance is running and accessible."""
    client = get_local_zotero_client()
    return client is not None


def format_item_metadata(item: dict[str, Any], include_abstract: bool = True) -> str:
    """
    Format a Zotero item's metadata as markdown.

    Args:
        item: A Zotero item dictionary.
        include_abstract: Whether to include the abstract in the output.

    Returns:
        Markdown-formatted metadata.
    """
    data = item.get("data", {})
    item_type = data.get("itemType", "unknown")
    item_key = data.get("key") or item.get("key")

    # Basic information
    lines = [
        f"# {data.get('title', 'Untitled')}",
        f"**Type:** {item_type}",
        f"**Item Key:** {item_key}",
    ]

    # Trash status. The Zotero web API returns data.deleted=1 for items in
    # the Trash; prior versions silently rendered trashed items as if live,
    # so agents reasoning about "current" state could cite papers the user
    # had explicitly removed. Surface it near the top where it's hard to miss.
    if data.get("deleted"):
        lines.append("**Status:** 🗑️ In Trash (recoverable from Zotero Trash view)")

    # Date
    if date := data.get("date"):
        lines.append(f"**Date:** {date}")

    # Authors/Creators
    if creators := data.get("creators", []):
        lines.append(f"**Creators:** {format_creators(creators)}")

    # Publication details based on item type
    if item_type == "journalArticle":
        if journal := data.get("publicationTitle"):
            journal_info = f"**Journal:** {journal}"
            if volume := data.get("volume"):
                journal_info += f", Volume {volume}"
            if issue := data.get("issue"):
                journal_info += f", Issue {issue}"
            if pages := data.get("pages"):
                journal_info += f", Pages {pages}"
            lines.append(journal_info)
    elif item_type == "bookSection":
        if book_title := data.get("bookTitle"):
            lines.append(f"**Book:** {book_title}")
        if pages := data.get("pages"):
            lines.append(f"**Pages:** {pages}")
    elif item_type == "conferencePaper":
        if proceedings := data.get("proceedingsTitle"):
            lines.append(f"**Proceedings:** {proceedings}")
        if conference := data.get("conferenceName"):
            lines.append(f"**Conference:** {conference}")
        if pages := data.get("pages"):
            lines.append(f"**Pages:** {pages}")
    elif item_type == "thesis":
        if thesis_type := data.get("thesisType"):
            lines.append(f"**Thesis Type:** {thesis_type}")
        if university := data.get("university"):
            lines.append(f"**University:** {university}")
    elif item_type == "report":
        if report_type := data.get("reportType"):
            lines.append(f"**Report Type:** {report_type}")
        if report_number := data.get("reportNumber"):
            lines.append(f"**Report Number:** {report_number}")
        if institution := data.get("institution"):
            lines.append(f"**Institution:** {institution}")
    elif item_type == "patent":
        if patent_number := data.get("patentNumber"):
            lines.append(f"**Patent Number:** {patent_number}")
        if filing_date := data.get("filingDate"):
            lines.append(f"**Filing Date:** {filing_date}")
        if assignee := data.get("assignee"):
            lines.append(f"**Assignee:** {assignee}")
    elif item_type == "preprint":
        if repository := data.get("repository"):
            lines.append(f"**Repository:** {repository}")
        if archive_id := data.get("archiveID"):
            lines.append(f"**Archive ID:** {archive_id}")
    elif item_type == "dataset":
        if version := data.get("versionNumber"):
            lines.append(f"**Version:** {version}")
        if genre := data.get("genre"):
            lines.append(f"**Genre:** {genre}")
    elif item_type == "standard":
        if number := data.get("number"):
            lines.append(f"**Standard Number:** {number}")
    elif item_type == "presentation":
        if pres_type := data.get("presentationType"):
            lines.append(f"**Presentation Type:** {pres_type}")
        if meeting := data.get("meetingName"):
            lines.append(f"**Meeting:** {meeting}")
    elif item_type in ("magazineArticle", "newspaperArticle"):
        if pub := data.get("publicationTitle"):
            lines.append(f"**Publication:** {pub}")
        if section := data.get("section"):
            lines.append(f"**Section:** {section}")
        if pages := data.get("pages"):
            lines.append(f"**Pages:** {pages}")
    elif item_type == "podcast":
        if series := data.get("seriesTitle"):
            lines.append(f"**Series:** {series}")
        if episode := data.get("episodeNumber"):
            lines.append(f"**Episode:** {episode}")
    elif item_type == "webpage":
        if website := data.get("websiteTitle"):
            lines.append(f"**Website:** {website}")
        if wtype := data.get("websiteType"):
            lines.append(f"**Website Type:** {wtype}")
    elif item_type == "blogPost":
        if blog := data.get("blogTitle"):
            lines.append(f"**Blog:** {blog}")
        if wtype := data.get("websiteType"):
            lines.append(f"**Website Type:** {wtype}")
    elif item_type == "forumPost":
        if forum := data.get("forumTitle"):
            lines.append(f"**Forum:** {forum}")
        if ptype := data.get("postType"):
            lines.append(f"**Post Type:** {ptype}")
    elif item_type == "artwork":
        if medium := data.get("artworkMedium"):
            lines.append(f"**Medium:** {medium}")
        if size := data.get("artworkSize"):
            lines.append(f"**Size:** {size}")
    elif item_type in ("audioRecording", "videoRecording"):
        if fmt := data.get("audioRecordingFormat") or data.get("videoRecordingFormat"):
            lines.append(f"**Format:** {fmt}")
        if runtime := data.get("runningTime"):
            lines.append(f"**Running Time:** {runtime}")
        if series := data.get("seriesTitle"):
            lines.append(f"**Series:** {series}")
    elif item_type in ("film", "radioBroadcast", "tvBroadcast"):
        if director := data.get("director"):
            lines.append(f"**Director:** {director}")
        if runtime := data.get("runningTime"):
            lines.append(f"**Running Time:** {runtime}")
        if network := data.get("network"):
            lines.append(f"**Network:** {network}")
    elif item_type == "case":
        if case_name := data.get("caseName"):
            lines.append(f"**Case Name:** {case_name}")
        if court := data.get("court"):
            lines.append(f"**Court:** {court}")
        if date_decided := data.get("dateDecided"):
            lines.append(f"**Date Decided:** {date_decided}")
        if docket := data.get("docketNumber"):
            lines.append(f"**Docket Number:** {docket}")
    elif item_type == "bill":
        if bill_num := data.get("billNumber"):
            lines.append(f"**Bill Number:** {bill_num}")
        if body := data.get("legislativeBody"):
            lines.append(f"**Legislative Body:** {body}")
        if session := data.get("session"):
            lines.append(f"**Session:** {session}")
    elif item_type == "statute":
        if act := data.get("nameOfAct"):
            lines.append(f"**Name of Act:** {act}")
        if code_num := data.get("codeNumber"):
            lines.append(f"**Code Number:** {code_num}")
        if session := data.get("session"):
            lines.append(f"**Session:** {session}")
    elif item_type == "hearing":
        if committee := data.get("committee"):
            lines.append(f"**Committee:** {committee}")
        if body := data.get("legislativeBody"):
            lines.append(f"**Legislative Body:** {body}")
        if session := data.get("session"):
            lines.append(f"**Session:** {session}")
    elif item_type == "interview":
        if medium := data.get("interviewMedium"):
            lines.append(f"**Medium:** {medium}")
    elif item_type == "letter":
        if ltype := data.get("letterType"):
            lines.append(f"**Letter Type:** {ltype}")
    elif item_type == "manuscript":
        if mtype := data.get("manuscriptType"):
            lines.append(f"**Manuscript Type:** {mtype}")
        if num_pages := data.get("numPages"):
            lines.append(f"**Pages:** {num_pages}")
    elif item_type == "map":
        if mtype := data.get("mapType"):
            lines.append(f"**Map Type:** {mtype}")
        if scale := data.get("scale"):
            lines.append(f"**Scale:** {scale}")
    elif item_type == "computerProgram":
        if lang := data.get("programmingLanguage"):
            lines.append(f"**Language:** {lang}")
        if company := data.get("company"):
            lines.append(f"**Company:** {company}")
        if version := data.get("versionNumber"):
            lines.append(f"**Version:** {version}")
    elif item_type in ("encyclopediaArticle", "dictionaryEntry"):
        if title_field := data.get("encyclopediaTitle") or data.get("dictionaryTitle"):
            lines.append(f"**Source:** {title_field}")
        if pages := data.get("pages"):
            lines.append(f"**Pages:** {pages}")

    # Publisher and place — emitted as independent labeled lines for any
    # item type that has them (book, bookSection, thesis, report, etc.).
    # Round-trip parity: agents that read these need a stable, labeled form.
    if publisher := data.get("publisher"):
        lines.append(f"**Publisher:** {publisher}")
    if place := data.get("place"):
        lines.append(f"**Place:** {place}")

    # Identifiers and URL
    if doi := data.get("DOI"):
        lines.append(f"**DOI:** {doi}")
    if isbn := data.get("ISBN"):
        lines.append(f"**ISBN:** {isbn}")
    if issn := data.get("ISSN"):
        lines.append(f"**ISSN:** {issn}")
    if pmid := data.get("PMID"):
        lines.append(f"**PMID:** {pmid}")
    if pmcid := data.get("PMCID"):
        lines.append(f"**PMCID:** {pmcid}")
    if url := data.get("url"):
        lines.append(f"**URL:** {url}")

    # Original publication fields (Zotero 7.0.31+)
    if orig_date := data.get("originalDate"):
        lines.append(f"**Original Date:** {orig_date}")
    if orig_place := data.get("originalPlace"):
        lines.append(f"**Original Place:** {orig_place}")
    if orig_publisher := data.get("originalPublisher"):
        lines.append(f"**Original Publisher:** {orig_publisher}")
    if orig_title := data.get("originalTitle"):
        lines.append(f"**Original Title:** {orig_title}")

    # Extra field often holds citation key / misc metadata
    if extra := data.get("extra"):
        lines.extend(["", "## Extra", extra])

        # Try to surface a citation key if present in Extra
        for line in extra.splitlines():
            if "citation key" in line.lower():
                key_part = line.split(":", 1)[1].strip() if ":" in line else line.strip()
                lines.append(f"**Citation Key (from Extra):** {key_part}")
                break

    # Tags
    if tags := data.get("tags"):
        tag_list = [f"`{tag['tag']}`" for tag in tags]
        if tag_list:
            lines.append(f"**Tags:** {' '.join(tag_list)}")

    # Abstract
    if include_abstract and (abstract := data.get("abstractNote")):
        lines.extend(["", "## Abstract", abstract])

    # Related Items (dc:relation URIs → item keys)
    dc_relations = data.get("relations", {}).get("dc:relation", [])
    if isinstance(dc_relations, str):
        dc_relations = [dc_relations]
    if dc_relations:
        related_keys = [uri.rstrip("/").split("/")[-1] for uri in dc_relations]
        lines.extend(["", "## Related Items", *[f"- {k}" for k in related_keys]])

    # Collections — list actual keys rather than a bare count. The Zotero
    # web API does NOT cascade collection-delete to items, so the array
    # can contain dangling references to collections that no longer exist.
    # Showing the keys lets agents verify against zotero_search_collections
    # instead of trusting a potentially stale count.
    if collections := data.get("collections", []):
        lines.append(f"**Collections:** {', '.join(collections)}")

    # Notes - this requires additional API calls, so we just indicate if there are notes
    if "meta" in item and item["meta"].get("numChildren", 0) > 0:
        lines.append(f"**Notes/Attachments:** {item['meta']['numChildren']}")

    if item_key:
        lines.extend(
            [
                "",
                "## Next Steps",
                f'- Full text: call `zotero_get_item_fulltext(item_key="{item_key}")`',
                f'- Children: call `zotero_get_item_children(item_key="{item_key}")`',
                f'- Attachment paths: call `zotero_get_attachment_path(item_key="{item_key}")`',
            ]
        )

    return "\n\n".join(lines)


def generate_bibtex(item: dict[str, Any]) -> str:
    """
    Generate BibTeX format for a Zotero item.

    Args:
        item: Zotero item data

    Returns:
        BibTeX formatted string
    """
    data = item.get("data", {})
    item_key = data.get("key")

    # Try Better BibTeX first
    try:
        from zotero_mcp.better_bibtex_client import ZoteroBetterBibTexAPI

        bibtex = ZoteroBetterBibTexAPI()

        if bibtex.is_zotero_running():
            exported = bibtex.export_bibtex(item_key)
            if exported:
                return exported

    except Exception as e:
        _logger.debug("Better BibTeX export failed for %s, using fallback: %s", item_key, e)

    # Fallback to basic BibTeX generation
    item_type = data.get("itemType", "misc")

    if item_type in ["attachment", "note"]:
        raise ValueError(f"Cannot export BibTeX for item type '{item_type}'")

    # Map Zotero item types to BibTeX types
    type_map = {
        "journalArticle": "article",
        "book": "book",
        "bookSection": "incollection",
        "conferencePaper": "inproceedings",
        "thesis": "phdthesis",
        "report": "techreport",
        "webpage": "misc",
        "manuscript": "unpublished",
    }

    # Create citation key
    creators = data.get("creators", [])
    author = ""
    if creators:
        first = creators[0]
        author = first.get("lastName", first.get("name", "").split()[-1] if first.get("name") else "").replace(" ", "")

    year = data.get("date", "")[:4] if data.get("date") else "nodate"
    cite_key = f"{author}{year}_{item_key}"

    # Build BibTeX entry
    bib_type = type_map.get(item_type, "misc")
    lines = [f"@{bib_type}{{{cite_key},"]

    # Add fields
    field_mappings = [
        ("title", "title"),
        ("publicationTitle", "journal"),
        ("bookTitle", "booktitle"),
        ("volume", "volume"),
        ("issue", "number"),
        ("pages", "pages"),
        ("publisher", "publisher"),
        ("place", "address"),
        ("DOI", "doi"),
        ("url", "url"),
        ("abstractNote", "abstract"),
        ("ISBN", "isbn"),
        ("ISSN", "issn"),
        ("PMID", "pmid"),
        ("PMCID", "pmcid"),
        ("originalDate", "origdate"),
        ("originalPlace", "origlocation"),
        ("originalPublisher", "origpublisher"),
        ("originalTitle", "origtitle"),
        ("language", "language"),
        ("rights", "rights"),
    ]

    for zotero_field, bibtex_field in field_mappings:
        if value := data.get(zotero_field):
            # Escape BibTeX-special characters (preserves LaTeX commands like \'{e})
            for ch in ("&", "%", "#", "_", "~", "^", "$"):
                value = value.replace(ch, f"\\{ch}")
            value = re.sub(r"(?<!\\)\{", "\\{", value)
            value = re.sub(r"(?<!\\)\}", "\\}", value)
            lines.append(f"  {bibtex_field} = {{{value}}},")

    # Add authors
    if creators:
        authors = []
        editors = []
        translators = []
        for creator in creators:
            ctype = creator.get("creatorType", "")
            name = ""
            if "lastName" in creator and "firstName" in creator:
                name = f"{creator['lastName']}, {creator['firstName']}"
            elif "name" in creator:
                name = creator["name"]
            if not name:
                continue
            if ctype == "author":
                authors.append(name)
            elif ctype == "editor":
                editors.append(name)
            elif ctype == "translator":
                translators.append(name)
        if authors:
            lines.append(f"  author = {{{' and '.join(authors)}}},")
        if editors:
            lines.append(f"  editor = {{{' and '.join(editors)}}},")
        if translators:
            lines.append(f"  translator = {{{' and '.join(translators)}}},")

    # Add year
    if year != "nodate":
        lines.append(f"  year = {{{year}}},")

    # Remove trailing comma from last field and close entry
    if lines[-1].endswith(","):
        lines[-1] = lines[-1][:-1]
    lines.append("}")

    return "\n".join(lines)


def get_attachment_details(zot: zotero.Zotero, item: dict[str, Any]) -> AttachmentDetails | None:
    """
    Get attachment details for a Zotero item, finding the most relevant attachment.

    Args:
        zot: A Zotero client instance.
        item: A Zotero item dictionary.

    Returns:
        AttachmentDetails if found, None otherwise.
    """
    data = item.get("data", {})
    item_type = data.get("itemType")
    item_key = data.get("key")

    # Direct attachment
    if item_type == "attachment":
        return AttachmentDetails(
            key=item_key,
            title=data.get("title", "Untitled"),
            filename=data.get("filename", ""),
            content_type=data.get("contentType", ""),
        )

    # For regular items, look for child attachments
    try:
        children = zot.children(item_key)

        # Group attachments by content type
        pdfs = []
        htmls = []
        others = []

        for child in children:
            child_data = child.get("data", {})
            if child_data.get("itemType") == "attachment":
                content_type = child_data.get("contentType", "")
                filename = child_data.get("filename", "")
                title = child_data.get("title", "Untitled")
                key = child.get("key", "")

                # Use MD5 as proxy for size (longer MD5 usually means larger file)
                size_proxy = len(child_data.get("md5", ""))

                attachment = (key, title, filename, content_type, size_proxy)

                if content_type == "application/pdf":
                    pdfs.append(attachment)
                elif content_type.startswith("text/html"):
                    htmls.append(attachment)
                else:
                    others.append(attachment)

        # Return first match in priority order (PDF > HTML > other)
        # Sort each category by size (descending) to get largest/most complete file
        for category in [pdfs, htmls, others]:
            if category:
                category.sort(key=lambda x: x[4], reverse=True)
                key, title, filename, content_type, _ = category[0]
                return AttachmentDetails(
                    key=key,
                    title=title,
                    filename=filename,
                    content_type=content_type,
                )
    except Exception as e:
        _logger.debug("Failed to get attachment details for %s: %s", item_key, e)

    return None


def convert_to_markdown(file_path: str | Path) -> str:
    """
    Convert a file to markdown using markitdown library.

    Args:
        file_path: Path to the file to convert.

    Returns:
        Markdown text.
    """
    try:
        md = MarkItDown()
        result = md.convert(str(file_path))
        return result.text_content
    except Exception as e:
        return f"Error converting file to markdown: {str(e)}"
