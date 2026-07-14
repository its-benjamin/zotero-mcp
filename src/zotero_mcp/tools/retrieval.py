"""Retrieval tool functions — read-only access to Zotero items, collections, tags, libraries, and feeds."""

import base64
import json
import logging as _logging
import os
import platform
import shutil
import tempfile
import time as _time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from mcp.types import ImageContent, TextContent, ToolAnnotations

from zotero_mcp import client as _client
from zotero_mcp import utils as _utils
from zotero_mcp._app import mcp
from zotero_mcp._context import Context
from zotero_mcp.cache import (
    get_children_cache,
    get_collections_cache,
    get_item_cache,
    get_tags_cache,
)
from zotero_mcp.client import with_zotero_api_lock
from zotero_mcp.tools import _helpers


@mcp.tool(
    name="zotero_get_item_metadata",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    description=(
        "Fetch detailed metadata (title, creators, date, DOI, publisher, "
        "tags, abstract, URL, etc.) for ONE Zotero item by key, as "
        "markdown or BibTeX. "
        "If the metadata and abstract don't contain what you need, call "
        "zotero_get_item_fulltext to read the paper — but that is "
        "resource-intensive (10K+ tokens) and should NEVER be used for "
        "searching; use zotero_search_items or zotero_semantic_search "
        "instead. "
        "item_key: the 8-character Zotero item key (NOT a DOI or title). "
        "include_abstract=True (default) includes the abstractNote in "
        "markdown output; pass False to trim tokens when you don't need "
        "it. (Ignored in bibtex format.) "
        "format='markdown' (default) returns a human-readable block; "
        "format='bibtex' returns a BibTeX citation string suitable for "
        ".bib files. "
        "Scope: active library only (switch with zotero_switch_library). "
        "Unlike list endpoints, this returns items EVEN IF THEY ARE IN "
        "THE TRASH — a Status: In Trash line is surfaced when the item "
        "is trashed (recoverable via the Zotero UI). Collection "
        "membership is shown as keys rather than a bare count so the "
        "caller can verify entries against zotero_search_collections "
        "(the Zotero API does not cascade collection-delete to items, "
        "so dangling references can linger). "
        "Example: zotero_get_item_metadata(item_key='RTKZQI8E', "
        "format='bibtex')."
    ),
)
@with_zotero_api_lock
async def get_item_metadata(
    item_key: str, include_abstract: bool = True, format: Literal["markdown", "bibtex"] = "markdown", *, ctx: Context
) -> str:
    """
    Get detailed metadata for a Zotero item.

    Args:
        item_key: Zotero item key/ID
        include_abstract: Whether to include the abstract in the output (markdown format only)
        format: Output format - 'markdown' for detailed metadata or 'bibtex' for BibTeX citation
        ctx: MCP context

    Returns:
        Formatted item metadata (markdown or BibTeX)
    """
    _ret_logger = _logging.getLogger("zotero_mcp.retrieval")
    try:
        key_err = _helpers.validate_item_key(item_key)
        if key_err:
            return f"Error: {key_err}"
        await ctx.info(f"Fetching metadata for item {item_key} in {format} format")
        zot = await _client.run_zotero_call(_client.get_zotero_client, operation="get_zotero_client")

        cache = get_item_cache()
        cache_key = f"item:{item_key}"
        item = cache.get(cache_key)
        if item is None:
            t0 = _time.monotonic()
            item = await _client.run_zotero_call(zot.item, item_key, operation=f"zot.item({item_key})")
            _ret_logger.debug(f"[METADATA] zot.item({item_key}): {_time.monotonic() - t0:.2f}s")
            if item:
                cache.set(cache_key, item)
        else:
            _ret_logger.debug(f"[METADATA] cache hit for {item_key}")
        if not item:
            return f"No item found with key: {item_key}"

        if format == "bibtex":
            return _client.generate_bibtex(item)
        else:
            return _client.format_item_metadata(item, include_abstract)

    except Exception as e:
        error_msg = str(e)
        suggestion = ""
        if "timeout" in error_msg.lower() or "timed out" in error_msg.lower():
            suggestion = " Zotero may be slow to respond. Try again in a moment."
        elif "connection" in error_msg.lower() or "refused" in error_msg.lower():
            suggestion = " Check if Zotero is running and the local API is enabled (Settings → Advanced → 'Allow other applications to communicate with Zotero')."
        elif "not found" in error_msg.lower() or "404" in error_msg:
            suggestion = " Verify the item key is correct (8 alphanumeric characters). Use zotero_search_items to find the right key."
        await ctx.error(f"Error fetching item metadata: {error_msg}")
        return f"Error fetching item metadata: {error_msg}{suggestion}"


@mcp.tool(
    name="zotero_get_item_fulltext",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    description=(
        "Return the full extracted text of a Zotero item's primary "
        "attachment (PDF or EPUB). "
        "WARNING: returns the entire paper (often 10K+ tokens). Use ONLY "
        "when the user explicitly wants to READ the paper — not for "
        "searching or browsing. For topic search use "
        "zotero_semantic_search; for metadata only use "
        "zotero_get_item_metadata. "
        "Avoid calling this on multiple papers in one conversation unless "
        "the user specifically asked to read several. "
        "item_key: 8-character Zotero item key (parent item, not the "
        "attachment). The tool locates the attached PDF/EPUB itself. "
        "Scope: active library only. "
        "Extraction path (in order): local Zotero storage via SQLite when "
        "running in local mode (fastest, respects pdf_max_pages config); "
        "Zotero's server-side fulltext index; direct download + PyMuPDF "
        "parsing as a last resort. Image-only scanned PDFs without OCR "
        "may return little or no text. "
        "Example: zotero_get_item_fulltext(item_key='RTKZQI8E')."
    ),
)
@with_zotero_api_lock
async def get_item_fulltext(item_key: str, *, ctx: Context) -> str:
    """
    Get the full text content of a Zotero item.

    Args:
        item_key: Zotero item key/ID
        ctx: MCP context

    Returns:
        Markdown-formatted item full text
    """
    try:
        key_err = _helpers.validate_item_key(item_key)
        if key_err:
            return f"Error: {key_err}"
        await ctx.info(f"Fetching full text for item {item_key}")
        zot = await _client.run_zotero_call(_client.get_zotero_client, operation="get_zotero_client")

        # First get the item metadata
        item = await _client.run_zotero_call(zot.item, item_key, operation=f"zot.item({item_key})")
        if not item:
            return f"No item found with key: {item_key}"

        # Get item metadata in markdown format
        metadata = _client.format_item_metadata(item, include_abstract=True)

        # In local mode, prefer direct local DB/storage extraction first.
        # This avoids pyzotero dump() failures on linked file:// attachments
        # when using remote clients over SSE/HTTP.
        local_extract_error_msg = None
        try:
            from zotero_mcp.local_db import LocalZoteroReader

            if _utils.is_local_mode():
                semantic_cfg = _helpers._load_zotero_mcp_config().get("semantic_search", {})
                zotero_db_path = semantic_cfg.get("zotero_db_path")
                extraction_cfg = semantic_cfg.get("extraction", {})
                pdf_max_pages = extraction_cfg.get("pdf_max_pages")
                # Separate display limit for when Claude reads papers
                # (reduces token usage vs. indexing which can be higher)
                fulltext_display_max = extraction_cfg.get("fulltext_display_max_pages")
                pdf_backend = extraction_cfg.get("pdf_backend")
                pdf_use_ocr = bool(extraction_cfg.get("pdf_use_ocr", False))
                paddleocr_lang = extraction_cfg.get("paddleocr_lang")

                # Use display limit if configured, otherwise fall back to
                # pdf_max_pages, with a default cap of 10 pages.
                DEFAULT_FULLTEXT_DISPLAY_MAX = 10
                if fulltext_display_max is not None:
                    pdf_max_pages = fulltext_display_max
                elif pdf_max_pages is None:
                    pdf_max_pages = DEFAULT_FULLTEXT_DISPLAY_MAX

                with LocalZoteroReader(
                    db_path=zotero_db_path,
                    pdf_max_pages=pdf_max_pages,
                    pdf_backend=pdf_backend,
                    pdf_use_ocr=pdf_use_ocr,
                    paddleocr_lang=paddleocr_lang,
                ) as reader:
                    local_item = reader.get_item_by_key(item_key)
                    if local_item:
                        extracted = reader.extract_fulltext_for_item(local_item.item_id)
                        if extracted and extracted[0]:
                            # Skip timeout sentinel — don't show "__EXTRACTION_TIMEOUT__" as content
                            if isinstance(extracted, tuple) and len(extracted) >= 2 and extracted[1] == "timeout":
                                await ctx.info("PDF extraction timed out — skipping local fulltext")
                            else:
                                source = extracted[1] if len(extracted) > 1 else "file"
                                attachments = reader.get_attachment_paths(item_key)
                                attachment_key = next(
                                    (
                                        att["key"]
                                        for att in attachments
                                        if att.get("exists") and att.get("content_type") == "application/pdf"
                                    ),
                                    None,
                                ) or next(
                                    (
                                        att["key"]
                                        for att in attachments
                                        if att.get("exists") and (att.get("content_type") or "").startswith("text/html")
                                    ),
                                    None,
                                )
                                source_block = _fulltext_source_block(
                                    item_key,
                                    f"local storage ({source})",
                                    attachment_key,
                                    pdf_backend=pdf_backend or "pdfminer",
                                    pdf_use_ocr=pdf_use_ocr,
                                    pages_extracted=pdf_max_pages if source == "pdf" else None,
                                )
                                await ctx.info(f"Retrieved full text from local storage ({source})")
                                return _helpers._prepend_size_warning(
                                    f"{metadata}\n\n---\n\n{source_block}\n\n## Full Text\n\n{extracted[0]}",
                                    "Consider using zotero_semantic_search to find specific content instead of reading full papers.",
                                )
        except Exception as local_extract_error:
            local_extract_error_msg = str(local_extract_error)
            await ctx.info(f"Local extraction fallback not available: {str(local_extract_error)}")

        # Try to get attachment details
        attachment = await _client.run_zotero_call(
            _client.get_attachment_details, zot, item, operation=f"get_attachment_details({item_key})"
        )
        if not attachment:
            return f"{metadata}\n\n---\n\nNo suitable attachment found for this item."

        await ctx.info(f"Found attachment: {attachment.key} ({attachment.content_type})")

        # Try fetching full text from Zotero's full text index first
        try:
            full_text_data = await _client.run_zotero_call(
                zot.fulltext_item, attachment.key, operation=f"zot.fulltext_item({attachment.key})"
            )
            if full_text_data and "content" in full_text_data and full_text_data["content"]:
                await ctx.info("Successfully retrieved full text from Zotero's index")
                source_block = _fulltext_source_block(item_key, "Zotero full-text index", attachment.key)
                return _helpers._prepend_size_warning(
                    f"{metadata}\n\n---\n\n{source_block}\n\n## Full Text\n\n{full_text_data['content']}",
                    "Consider using zotero_semantic_search to find specific content instead of reading full papers.",
                )
        except Exception as fulltext_error:
            await ctx.info(f"Couldn't retrieve indexed full text: {str(fulltext_error)}")

        # If we couldn't get indexed full text, try to download and convert the file
        try:
            await ctx.info(f"Attempting to download and convert attachment {attachment.key}")

            # Download the file to a temporary location

            with tempfile.TemporaryDirectory() as tmpdir:
                file_path = os.path.join(tmpdir, attachment.filename or f"{attachment.key}.pdf")
                await _client.run_zotero_call(
                    zot.dump,
                    attachment.key,
                    filename=os.path.basename(file_path),
                    path=tmpdir,
                    operation=f"zot.dump({attachment.key})",
                )

                if os.path.exists(file_path):
                    await ctx.info(f"Downloaded file to {file_path}, converting to markdown")
                    converted_text = _client.convert_to_markdown(file_path)
                    source_block = _fulltext_source_block(item_key, "downloaded attachment conversion", attachment.key)
                    return _helpers._prepend_size_warning(
                        f"{metadata}\n\n---\n\n{source_block}\n\n## Full Text\n\n{converted_text}",
                        "Consider using zotero_semantic_search to find specific content instead of reading full papers.",
                    )
                else:
                    return f"{metadata}\n\n---\n\nFile download failed."
        except Exception as download_error:
            await ctx.error(f"Error downloading/converting file: {str(download_error)}")
            if local_extract_error_msg:
                return (
                    f"{metadata}\n\n---\n\nError accessing attachment: {str(download_error)}\n\n"
                    f"Local extraction fallback error: {local_extract_error_msg}"
                )
            return f"{metadata}\n\n---\n\nError accessing attachment: {str(download_error)}"

    except Exception as e:
        error_msg = str(e)
        suggestion = ""
        if "timeout" in error_msg.lower() or "timed out" in error_msg.lower():
            suggestion = " Full text extraction can be slow for large PDFs. Try again or use zotero_extract_pdf_pages for specific pages."
        elif "connection" in error_msg.lower():
            suggestion = " Check if Zotero is running."
        elif "not found" in error_msg.lower():
            suggestion = " The item may not have an attached PDF. Use zotero_get_item_children to check attachments."
        await ctx.error(f"Error fetching item full text: {error_msg}")
        return f"Error fetching item full text: {error_msg}{suggestion}"


@dataclass
class _ResolvedPdfAttachment:
    parent_key: str | None
    attachment_key: str
    filename: str
    content_type: str
    pdf_path: Path
    temp_dir: str | None = None

    def cleanup(self) -> None:
        if self.temp_dir:
            shutil.rmtree(self.temp_dir, ignore_errors=True)


def _parse_pdf_page_spec(pages: str, page_count: int, *, max_pages: int) -> list[int]:
    """Parse a 1-indexed page spec into sorted unique 0-indexed page numbers."""
    if page_count < 1:
        raise ValueError("PDF has no pages.")
    if not isinstance(pages, str) or not pages.strip():
        raise ValueError("pages must be a non-empty string like '1', '1-5', or '1,3,7-9'.")

    selected: set[int] = set()
    for raw_part in pages.split(","):
        part = raw_part.strip()
        if not part:
            raise ValueError(f"Invalid page spec: {pages!r}.")
        if "-" in part:
            bounds = [p.strip() for p in part.split("-")]
            if len(bounds) != 2 or not bounds[0].isdigit() or not bounds[1].isdigit():
                raise ValueError(f"Invalid page range: {part!r}.")
            start, end = int(bounds[0]), int(bounds[1])
            if start < 1 or end < 1 or start > end:
                raise ValueError(f"Invalid page range: {part!r}.")
            selected.update(range(start, end + 1))
        else:
            if not part.isdigit():
                raise ValueError(f"Invalid page number: {part!r}.")
            page = int(part)
            if page < 1:
                raise ValueError("Page numbers are 1-indexed; page 0 is invalid.")
            selected.add(page)

    if not selected:
        raise ValueError("No pages selected.")
    if len(selected) > max_pages:
        raise ValueError(f"Too many pages requested: {len(selected)}. Maximum is {max_pages} pages per call.")
    too_high = [page for page in selected if page > page_count]
    if too_high:
        raise ValueError(f"Page {too_high[0]} is outside PDF page count ({page_count}).")
    return [page - 1 for page in sorted(selected)]


def _local_pdf_attachment_for_key(item_key: str) -> _ResolvedPdfAttachment | None:
    if not _utils.is_local_mode():
        return None
    from zotero_mcp.local_db import LocalZoteroReader

    zotero_db_path = _helpers._load_zotero_mcp_config().get("semantic_search", {}).get("zotero_db_path")
    with LocalZoteroReader(db_path=zotero_db_path) as reader:
        direct = None
        if hasattr(reader, "get_attachment_path_by_key"):
            direct = reader.get_attachment_path_by_key(item_key)
        candidates = [direct] if direct else reader.get_attachment_paths(item_key)

    for att in candidates:
        if not att or att.get("content_type") != "application/pdf" or not att.get("exists"):
            continue
        resolved_path = att.get("resolved_path")
        if not resolved_path:
            continue
        path = Path(resolved_path)
        return _ResolvedPdfAttachment(
            parent_key=att.get("parent_key") or (None if att.get("key") == item_key else item_key),
            attachment_key=att["key"],
            filename=path.name,
            content_type=att.get("content_type") or "application/pdf",
            pdf_path=path,
        )
    return None


async def _resolve_pdf_attachment(item_key: str, ctx: Context) -> _ResolvedPdfAttachment:
    """Resolve a parent item key or PDF attachment key to a local PDF path."""
    try:
        local = _local_pdf_attachment_for_key(item_key)
    except Exception as e:
        await ctx.info(f"Local PDF path lookup unavailable: {e}")
        local = None
    if local:
        return local

    zot = await _client.run_zotero_call(_client.get_zotero_client, operation="get_zotero_client")
    item = await _client.run_zotero_call(zot.item, item_key, operation=f"zot.item({item_key})")
    data = item.get("data", {}) if item else {}
    parent_key: str | None = data.get("parentItem")

    if data.get("itemType") == "attachment":
        if data.get("contentType") != "application/pdf":
            raise ValueError(f"Item `{item_key}` is not a PDF attachment.")
        attachment = item
    else:
        parent_key = item_key
        children = await _client.run_zotero_call(zot.children, item_key, operation=f"zot.children({item_key})")
        attachment = next(
            (child for child in children if child.get("data", {}).get("contentType") == "application/pdf"), None
        )
        if not attachment:
            raise ValueError(f"No PDF attachment found for item `{item_key}`.")

    attachment_key = attachment["key"]
    attachment_data = attachment.get("data", {})
    filename = attachment_data.get("filename") or f"{attachment_key}.pdf"
    temp_dir = tempfile.mkdtemp(prefix="zotero-mcp-pdf-")
    await ctx.info(f"Downloading PDF attachment {attachment_key}")
    await _client.run_zotero_call(
        zot.dump,
        attachment_key,
        filename=filename,
        path=temp_dir,
        operation=f"zot.dump({attachment_key})",
    )
    pdf_path = Path(temp_dir) / filename
    if not pdf_path.exists() or pdf_path.stat().st_size == 0:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise ValueError(f"Could not download PDF for attachment `{attachment_key}`.")

    return _ResolvedPdfAttachment(
        parent_key=parent_key,
        attachment_key=attachment_key,
        filename=filename,
        content_type=attachment_data.get("contentType") or "application/pdf",
        pdf_path=pdf_path,
        temp_dir=temp_dir,
    )


def _pdf_page_count(pdf_path: Path) -> int:
    try:
        import fitz
    except ImportError as exc:
        raise RuntimeError("PyMuPDF (fitz) is required for PDF page tools.") from exc

    doc = fitz.open(str(pdf_path))
    try:
        return len(doc)
    finally:
        doc.close()


def _format_page_list(page_indexes: list[int]) -> str:
    return ", ".join(str(page + 1) for page in page_indexes)


def _render_cache_root() -> Path:
    """Return OS-native cache root for rendered PDF page images."""
    if os.name == "nt":
        base = os.getenv("LOCALAPPDATA")
        if base:
            return Path(base) / "zotero-mcp" / "Cache" / "rendered_pages"
    if platform.system() == "Darwin":
        return Path.home() / "Library" / "Caches" / "zotero-mcp" / "rendered_pages"
    base = os.getenv("XDG_CACHE_HOME")
    if base:
        return Path(base) / "zotero-mcp" / "rendered_pages"
    return Path.home() / ".cache" / "zotero-mcp" / "rendered_pages"


def _cleanup_render_cache(root: Path, *, max_age_days: int = 30) -> None:
    """Best-effort removal of stale rendered page cache files."""
    if max_age_days <= 0 or not root.exists():
        return
    cutoff = _time.time() - (max_age_days * 24 * 60 * 60)
    try:
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
            except OSError:
                continue
        for directory in sorted((p for p in root.rglob("*") if p.is_dir()), reverse=True):
            try:
                directory.rmdir()
            except OSError:
                continue
    except OSError:
        return


@mcp.tool(
    name="zotero_extract_pdf_pages",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    description=(
        "Extract text from targeted PDF pages. item_key accepts a parent item key "
        "or PDF attachment key. pages is 1-indexed and supports '1', '1-5', "
        "and '1,3,7-9'. use_ocr is per-call and defaults false. "
        "output_format supports 'markdown' and 'text'. Hard cap: 25 pages without OCR, "
        "5 pages with OCR."
    ),
)
@with_zotero_api_lock
async def extract_pdf_pages(
    item_key: str,
    pages: str = "1-5",
    use_ocr: bool = False,
    output_format: Literal["markdown", "text"] = "markdown",
    *,
    ctx: Context,
) -> str:
    if output_format not in {"markdown", "text"}:
        return "Error: output_format must be 'markdown' or 'text'."
    resolved: _ResolvedPdfAttachment | None = None
    try:
        resolved = await _resolve_pdf_attachment(item_key, ctx)
        page_count = _pdf_page_count(resolved.pdf_path)
        max_pages = 5 if use_ocr else 25
        page_indexes = _parse_pdf_page_spec(pages, page_count, max_pages=max_pages)

        # Load PaddleOCR config for potential fallback
        _paddleocr_cfg: dict = {}
        try:
            _semantic_cfg = _helpers._load_zotero_mcp_config().get("semantic_search", {})
            _paddleocr_cfg = _semantic_cfg.get("extraction", {})
        except Exception:
            pass

        if output_format == "markdown":
            # Check if PaddleOCR should be used as primary
            paddleocr_backend = False
            if use_ocr:
                try:
                    backend_cfg = (_paddleocr_cfg.get("pdf_backend") or "").strip().lower()
                    if backend_cfg in {"paddleocr", "paddle"}:
                        from zotero_mcp.paddleocr_backend import extract_pages_paddleocr

                        paddleocr_lang = _paddleocr_cfg.get("paddleocr_lang")
                        body = extract_pages_paddleocr(resolved.pdf_path, page_indexes, lang=paddleocr_lang) or ""
                        backend = "PaddleOCR PP-OCRv6"
                        paddleocr_backend = True
                except ImportError:
                    pass  # PaddleOCR not installed, fall through to pymupdf4llm
                except Exception:
                    pass  # Fall through to pymupdf4llm

            if not paddleocr_backend:
                try:
                    import pymupdf4llm
                except ImportError:
                    return "Error: pymupdf4llm is required for markdown PDF page extraction."
                body = pymupdf4llm.to_markdown(str(resolved.pdf_path), pages=page_indexes, use_ocr=use_ocr) or ""
                backend = "pymupdf4llm"

                # Auto-fallback: if pymupdf4llm returned little text, try PaddleOCR
                if not use_ocr and len(body.strip()) < len(page_indexes) * 20:
                    try:
                        from zotero_mcp.paddleocr_backend import extract_pages_paddleocr

                        paddleocr_lang = _paddleocr_cfg.get("paddleocr_lang")
                        ocr_body = extract_pages_paddleocr(resolved.pdf_path, page_indexes, lang=paddleocr_lang) or ""
                        if len(ocr_body.strip()) > len(body.strip()):
                            body = ocr_body
                            backend = "PaddleOCR PP-OCRv6 (auto-fallback)"
                    except ImportError:
                        pass  # PaddleOCR not installed
                    except Exception:
                        pass
        else:
            try:
                import fitz
            except ImportError:
                return "Error: PyMuPDF (fitz) is required for text PDF page extraction."
            doc = fitz.open(str(resolved.pdf_path))
            try:
                body = "\n\n".join(doc[index].get_text() or "" for index in page_indexes)
            finally:
                doc.close()
            backend = "PyMuPDF get_text"

            # Auto-fallback: if fitz returned little text, try PaddleOCR
            if len(body.strip()) < len(page_indexes) * 20:
                try:
                    from zotero_mcp.paddleocr_backend import extract_pages_paddleocr

                    paddleocr_lang = _paddleocr_cfg.get("paddleocr_lang")
                    ocr_body = extract_pages_paddleocr(resolved.pdf_path, page_indexes, lang=paddleocr_lang) or ""
                    if len(ocr_body.strip()) > len(body.strip()):
                        body = ocr_body
                        backend = "PaddleOCR PP-OCRv6 (auto-fallback)"
                except ImportError:
                    pass
                except Exception:
                    pass

        page_list = _format_page_list(page_indexes)
        if use_ocr:
            next_hint = "OCR already enabled for this call."
        elif "(auto-fallback)" in backend:
            next_hint = "PaddleOCR auto-fallback was used because the primary backend returned little text."
        else:
            next_hint = (
                f'retry scanned pages with OCR: `zotero_extract_pdf_pages(item_key="{resolved.attachment_key}", '
                f'pages="{pages}", use_ocr=true)`.'
            )
        lines = [
            "# PDF Page Extraction",
            "",
            "## Source",
            f"- **Item Key:** {item_key}",
            f"- **Parent Key:** {resolved.parent_key or 'unknown'}",
            f"- **Attachment Key:** {resolved.attachment_key}",
            f"- **Filename:** {resolved.filename}",
            f"- **Backend:** {backend}",
            f"- **OCR:** {'enabled' if use_ocr else 'disabled'}",
            f"- **Requested Pages:** {pages}",
            f"- **Extracted Pages:** {page_list}",
            f"- **Page Count:** {page_count}",
            "- **Next:** "
            f'render same pages for vision: `zotero_render_pdf_pages(item_key="{resolved.attachment_key}", pages="{pages}")`; '
            f"{next_hint}",
            "",
            "## Extracted Text",
            "",
            body,
        ]
        return "\n".join(lines).rstrip()
    except ValueError as e:
        return f"Error: {e}"
    except Exception as e:
        await ctx.error(f"Error extracting PDF pages: {e}")
        return f"Error extracting PDF pages: {e}"
    finally:
        if resolved:
            resolved.cleanup()


@mcp.tool(
    name="zotero_render_pdf_pages",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    description=(
        "Render targeted PDF pages as vision-ready images. item_key accepts a parent "
        "item key or PDF attachment key. pages is 1-indexed. dpi defaults to 150. "
        "image_format supports png and jpeg. return_mode supports paths, images, or both. "
        "Hard cap: 10 pages per call."
    ),
)
@with_zotero_api_lock
async def render_pdf_pages(
    item_key: str,
    pages: str = "1",
    dpi: int | str = 150,
    image_format: Literal["png", "jpeg", "jpg"] = "png",
    return_mode: Literal["paths", "images", "both"] = "both",
    *,
    ctx: Context,
) -> list[TextContent | ImageContent] | str:
    try:
        dpi_int = int(dpi)
    except (TypeError, ValueError):
        return "Error: dpi must be an integer."
    if dpi_int < 36 or dpi_int > 600:
        return "Error: dpi must be between 36 and 600."
    image_ext = "jpeg" if image_format == "jpg" else image_format
    if image_ext not in {"png", "jpeg"}:
        return "Error: image_format must be 'png', 'jpeg', or 'jpg'."
    if return_mode not in {"paths", "images", "both"}:
        return "Error: return_mode must be 'paths', 'images', or 'both'."

    resolved: _ResolvedPdfAttachment | None = None
    try:
        try:
            import fitz
        except ImportError:
            return "Error: PyMuPDF (fitz) is required for PDF rendering."

        resolved = await _resolve_pdf_attachment(item_key, ctx)
        doc = fitz.open(str(resolved.pdf_path))
        try:
            page_indexes = _parse_pdf_page_spec(pages, len(doc), max_pages=10)
            cache_root = _render_cache_root()
            _cleanup_render_cache(cache_root)
            out_dir = cache_root / resolved.attachment_key
            out_dir.mkdir(parents=True, exist_ok=True)
            matrix = fitz.Matrix(dpi_int / 72, dpi_int / 72)

            def _render_page(page_index: int) -> tuple[int, Path]:
                pixmap = doc[page_index].get_pixmap(matrix=matrix)
                out_path = out_dir / f"page-{page_index + 1}-{dpi_int}.{image_ext}"
                pixmap.save(str(out_path))
                return (page_index, out_path)

            # Render pages in parallel using thread pool
            import asyncio as _asyncio

            rendered = list(await _asyncio.gather(*[_asyncio.to_thread(_render_page, pi) for pi in page_indexes]))
        finally:
            doc.close()

        page_list = _format_page_list(page_indexes)
        mime = "image/png" if image_ext == "png" else "image/jpeg"
        lines = [
            "# PDF Page Render",
            "",
            "## Source",
            f"- **Item Key:** {item_key}",
            f"- **Parent Key:** {resolved.parent_key or 'unknown'}",
            f"- **Attachment Key:** {resolved.attachment_key}",
            f"- **Source PDF:** `{resolved.pdf_path}`",
            f"- **Rendered Pages:** {page_list}",
            f"- **DPI:** {dpi_int}",
            f"- **Image Format:** {image_ext}",
            "",
            "## Local Image Paths",
        ]
        for page_index, out_path in rendered:
            lines.append(f"- **Page {page_index + 1}:** `{out_path}`")
        lines.extend(
            [
                "",
                "## Next",
                "- **OCR Text:** "
                f'`zotero_extract_pdf_pages(item_key="{resolved.attachment_key}", pages="{pages}", use_ocr=true)`',
            ]
        )

        blocks: list[TextContent | ImageContent] = [TextContent(type="text", text="\n".join(lines))]
        if return_mode in {"images", "both"}:
            for _page_index, out_path in rendered:
                data = base64.b64encode(out_path.read_bytes()).decode()
                blocks.append(ImageContent(type="image", mimeType=mime, data=data))
        return blocks
    except ValueError as e:
        return f"Error: {e}"
    except Exception as e:
        await ctx.error(f"Error rendering PDF pages: {e}")
        return f"Error rendering PDF pages: {e}"
    finally:
        if resolved:
            resolved.cleanup()


@mcp.tool(
    name="zotero_get_attachment_path",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    description=(
        "Return the local filesystem path(s) of a Zotero item's attachments. "
        "Local mode only. Useful when you want to read a large PDF directly "
        "(e.g., a book) instead of going through zotero_get_item_fulltext, "
        "which is page-limited."
    ),
)
async def get_attachment_path(item_key: str, *, ctx: Context) -> str:
    """List resolved local paths for an item's attachments."""
    if not _utils.is_local_mode():
        return (
            "Error: zotero_get_attachment_path requires local mode "
            "(set ZOTERO_LOCAL=true). Cloud-only attachments have no local path."
        )
    try:
        from zotero_mcp.local_db import LocalZoteroReader

        zotero_db_path = _helpers._load_zotero_mcp_config().get("semantic_search", {}).get("zotero_db_path")

        with LocalZoteroReader(db_path=zotero_db_path) as reader:
            attachments = reader.get_attachment_paths(item_key)

        if not attachments:
            return f"No attachments found for item `{item_key}`."

        lines = ["# Attachment Paths", "", f"**Item Key:** {item_key}", ""]
        for att in attachments:
            resolved_path = att.get("resolved_path")
            filename = resolved_path.name if resolved_path is not None else "unknown"
            exists = "yes" if att.get("exists") else "no"
            lines.append(f"## Attachment {att['key']}")
            lines.append(f"- **Attachment Key:** {att['key']}")
            lines.append(f"- **Content Type:** {att.get('content_type') or 'unknown'}")
            lines.append(f"- **Filename:** {filename}")
            lines.append(f"- **Zotero Path:** `{att.get('zotero_path') or ''}`")
            if resolved_path is not None:
                lines.append(f"- **Local Path:** `{resolved_path}`")
            else:
                lines.append("- **Local Path:** *unresolved*")
            lines.append(f"- **Exists:** {exists}")
            next_parts = []
            if resolved_path is not None:
                next_parts.append("inspect this file directly with local filesystem tools")
            if att.get("content_type") == "application/pdf":
                next_parts.append(f'outline: `zotero_get_pdf_outline(item_key="{att["key"]}")`')
                next_parts.append(f"annotations: use attachment key `{att['key']}`")
            lines.append(f"- **Next:** {'; '.join(next_parts) if next_parts else 'resolve or sync attachment first'}")
            lines.append("")
        return "\n".join(lines).rstrip()
    except Exception as e:
        await ctx.error(f"Error resolving attachment path: {e}")
        return f"Error resolving attachment path: {e}"


@mcp.tool(
    name="zotero_get_collections",
    description="List all collections in your Zotero library.",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
)
@with_zotero_api_lock
async def get_collections(limit: int | str | None = None, *, ctx: Context) -> str:
    """
    List all collections in your Zotero library.

    Args:
        limit: Maximum number of collections to return
        ctx: MCP context

    Returns:
        Markdown-formatted list of collections
    """
    try:
        await ctx.info("Fetching collections")
        zot = await _client.run_zotero_call(_client.get_zotero_client, operation="get_zotero_client")

        limit = _helpers._normalize_limit(limit, default=100, max_val=5000)

        # Check collections cache - use single cache key for all collections
        coll_cache = get_collections_cache()
        cache_key = "collections:all"
        all_collections = coll_cache.get(cache_key)
        if all_collections is None:
            all_collections = await _client.run_zotero_call(
                _helpers._paginate, zot.collections, max_items=5000, operation="paginate(zot.collections)"
            )
            coll_cache.set(cache_key, all_collections or [])

        # Apply limit after cache retrieval
        collections = all_collections[:limit] if all_collections else []

        # Always return the header, even if empty
        output = ["# Zotero Collections", ""]

        if not collections:
            output.append("No collections found in your Zotero library.")
            return "\n".join(output)

        # Create a mapping of collection IDs to their data
        collection_map = {c["key"]: c for c in collections}

        # Create a mapping of parent to child collections
        # Only add entries for collections that actually exist
        hierarchy = {}
        for coll in collections:
            parent_key = coll["data"].get("parentCollection")
            # Handle various representations of "no parent"
            if parent_key in ["", None] or not parent_key:
                parent_key = None  # Normalize to None

            if parent_key not in hierarchy:
                hierarchy[parent_key] = []
            hierarchy[parent_key].append(coll["key"])

        # Function to recursively format collections
        def format_collection(key, level=0):
            if key not in collection_map:
                return []

            coll = collection_map[key]
            name = coll["data"].get("name", "Unnamed Collection")

            # Create indentation for hierarchy
            indent = "  " * level
            lines = [f"{indent}- **{name}** (Key: {key})"]

            # Add children if they exist
            child_keys = hierarchy.get(key, [])
            for child_key in sorted(child_keys):  # Sort for consistent output
                lines.extend(format_collection(child_key, level + 1))

            return lines

        # Start with top-level collections (those with None as parent)
        top_level_keys = hierarchy.get(None, [])

        if not top_level_keys:
            # If no clear hierarchy, just list all collections
            output.append("Collections (flat list):")
            for coll in sorted(collections, key=lambda x: x["data"].get("name", "")):
                name = coll["data"].get("name", "Unnamed Collection")
                key = coll["key"]
                output.append(f"- **{name}** (Key: {key})")
        else:
            # Display hierarchical structure
            for key in sorted(top_level_keys):
                output.extend(format_collection(key))

        return "\n".join(output)

    except Exception as e:
        error_msg = str(e)
        suggestion = ""
        if "timeout" in error_msg.lower() or "timed out" in error_msg.lower():
            suggestion = " Zotero may be slow to respond. Try again in a moment."
        elif "connection" in error_msg.lower():
            suggestion = " Check if Zotero is running and the local API is enabled."
        await ctx.error(f"Error fetching collections: {error_msg}")
        return f"# Zotero Collections\n\nError fetching collections: {error_msg}{suggestion}"


def _build_attachment_extra(info):
    """Build extra_fields dict from attachment_info for format_item_result."""
    if not info:
        return None
    parts = []
    if info.get("has_pdf"):
        parts.append("PDF")
    att_count = info.get("attachment_count", 0)
    if att_count:
        parts.append(f"{att_count} attachment{'s' if att_count != 1 else ''}")
    if info.get("has_notes"):
        parts.append("has notes")
    return {"Attachments": ", ".join(parts)} if parts else None


def _clean_note_text(note_html: str, max_chars: int = 500) -> str:
    note_text = _utils.clean_html(note_html).strip()
    if len(note_text) > max_chars:
        return note_text[:max_chars].rstrip() + "...\n\n(Note truncated)"
    return note_text


def _child_title(data: dict, fallback: str = "Untitled") -> str:
    return data.get("title") or data.get("filename") or fallback


def _fulltext_source_block(
    item_key: str,
    source: str,
    attachment_key: str | None = None,
    *,
    pdf_backend: str | None = None,
    pdf_use_ocr: bool | None = None,
    pages_extracted: int | None = None,
) -> str:
    lines = ["## Full Text Source", f"- **Item Key:** {item_key}", f"- **Source:** {source}"]
    if attachment_key:
        lines.append(f"- **Attachment Key:** {attachment_key}")
    if pdf_backend:
        lines.append(f"- **PDF Backend:** {pdf_backend}")
    if pdf_use_ocr is not None:
        lines.append(f"- **OCR:** {'enabled' if pdf_use_ocr else 'disabled'}")
    if pages_extracted is not None:
        lines.append(f"- **Pages Extracted:** {pages_extracted}")
    if pdf_backend and not pdf_use_ocr:
        lines.append(
            "- **Next:** If text is empty or garbled, retry specific pages with "
            f'`zotero_extract_pdf_pages(item_key="{attachment_key or item_key}", pages="1-5", use_ocr=true)` '
            "or inspect page images with "
            f'`zotero_render_pdf_pages(item_key="{attachment_key or item_key}", pages="1")` for a vision model. '
            "Config fallback: `semantic_search.extraction.pdf_use_ocr=true`."
        )
    return "\n".join(lines)


def _append_child_block(
    output: list[str], child: dict, index: int | None = None, *, parent_key: str | None = None, compact: bool = False
) -> None:
    data = child.get("data", {})
    child_type = data.get("itemType", "unknown")
    child_key = child.get("key", "")
    title = _child_title(data)
    prefix = f"{index}. " if index is not None else ""

    if compact:
        output.append(f"### {prefix}{title}")
    else:
        output.append(f"{prefix}**{title}**")
    output.append(f"- **Child Key:** {child_key}")
    output.append(f"- **Child Type:** {child_type}")

    if child_type == "attachment":
        if filename := data.get("filename", ""):
            output.append(f"- **Filename:** {filename}")
        if content_type := data.get("contentType", ""):
            output.append(f"- **Content Type:** {content_type}")
        if link_mode := data.get("linkMode", ""):
            output.append(f"- **Link Mode:** {link_mode}")
        if content_type == "application/pdf" and child_key:
            output.append(f'- **PDF Outline:** call `zotero_get_pdf_outline(item_key="{child_key}")`')
        if parent_key:
            output.append(f'- **Local Path:** call `zotero_get_attachment_path(item_key="{parent_key}")`')
    elif child_type == "note":
        note_text = _clean_note_text(data.get("note", ""))
        output.append("- **Note Text:**")
        output.append("```text")
        output.append(note_text)
        output.append("```")
    elif child_type == "annotation":
        if ann_type := data.get("annotationType", ""):
            output.append(f"- **Annotation Type:** {ann_type}")
        if ann_text := data.get("annotationText", ""):
            output.append(f"- **Annotation Text:** {ann_text[:500]}")
        if page_label := data.get("annotationPageLabel", ""):
            output.append(f"- **Page Label:** {page_label}")

    output.append("")


@mcp.tool(
    name="zotero_get_collection_items",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    description="Get all items in a specific Zotero collection. Supports detail='keys_only' (minimal), 'summary' (default, no abstracts), or 'full' (with abstracts). Includes PDF/notes indicators. TIP: To find papers on a specific topic, use zotero_semantic_search instead — it's faster and returns only relevant results.",
)
@with_zotero_api_lock
async def get_collection_items(
    collection_key: str,
    detail: Literal["keys_only", "summary", "full"] = "summary",
    limit: int | str | None = 50,
    *,
    ctx: Context,
) -> str:
    """
    Get all items in a specific Zotero collection.

    Args:
        collection_key: The collection key/ID
        limit: Maximum number of items to return
        ctx: MCP context

    Returns:
        Markdown-formatted list of items in the collection
    """
    try:
        await ctx.info(f"Fetching items for collection {collection_key}")
        zot = await _client.run_zotero_call(_client.get_zotero_client, operation="get_zotero_client")

        # First get the collection details. Fail fast on lookup error: the
        # Zotero web API returns library-wide items for invalid or not-yet-
        # propagated collection keys rather than 404ing, so we must not fall
        # through to collection_items() when we can't confirm the collection
        # exists.
        try:
            collection = await _client.run_zotero_call(
                zot.collection, collection_key, operation=f"zot.collection({collection_key})"
            )
            collection_name = collection["data"].get("name", "Unnamed Collection")
        except Exception as e:
            await ctx.error(f"Collection lookup failed for {collection_key}: {e}")
            return (
                f"Collection not found or not yet accessible: `{collection_key}`. "
                f"If you just created this collection, wait a moment and try again."
            )

        limit = _helpers._normalize_limit(limit, default=50)

        # Fetch all items (includes children mixed in with parents)
        all_items = await _client.run_zotero_call(
            _helpers._paginate,
            zot.collection_items,
            collection_key,
            operation=f"paginate(zot.collection_items, {collection_key})",
        )
        if not all_items:
            return f"No items found in collection: {collection_name} (Key: {collection_key})"

        # Build attachment/note summary from already-fetched children (zero extra API calls)
        attachment_info = {}
        for item in all_items:
            data = item.get("data", {})
            item_type = data.get("itemType", "")
            parent_key = data.get("parentItem", "")
            if not parent_key:
                continue
            if parent_key not in attachment_info:
                attachment_info[parent_key] = {"has_pdf": False, "attachment_count": 0, "has_notes": False}
            if item_type == "attachment":
                attachment_info[parent_key]["attachment_count"] += 1
                if data.get("contentType", "") == "application/pdf":
                    attachment_info[parent_key]["has_pdf"] = True
            elif item_type == "note":
                attachment_info[parent_key]["has_notes"] = True

        # Filter to parent items only (exclude attachments, notes, annotations)
        child_types = {"attachment", "note", "annotation"}
        parent_items = [item for item in all_items if item.get("data", {}).get("itemType", "") not in child_types]

        if not parent_items:
            return f"No items found in collection: {collection_name} (Key: {collection_key})"

        # Apply display limit after filtering
        if limit and len(parent_items) > limit:
            display_items = parent_items[:limit]
            truncated = True
        else:
            display_items = parent_items
            truncated = False

        # Format items as markdown based on detail level
        output = [f"# Items in Collection: {collection_name} ({len(parent_items)} items)", ""]

        for i, item in enumerate(display_items, 1):
            key = item.get("key", "")
            info = attachment_info.get(key, {})

            if detail == "keys_only":
                data = item.get("data", {})
                title = data.get("title", "Untitled")
                date = data.get("date", "")
                flags = []
                if info.get("has_pdf"):
                    flags.append("PDF")
                if info.get("has_notes"):
                    flags.append("Notes")
                attachments = ", ".join(flags) if flags else "none"
                output.append(
                    f"- **Item Key:** {key} | **Title:** {title} | "
                    f"**Date:** {date or 'No date'} | **Attachments:** {attachments}"
                )

            elif detail == "full":
                extra = _build_attachment_extra(info)
                output.extend(
                    _utils.format_item_result(item, index=i, abstract_len=None, include_tags=True, extra_fields=extra)
                )

            else:  # "summary" (default)
                extra = _build_attachment_extra(info)
                output.extend(
                    _utils.format_item_result(item, index=i, abstract_len=0, include_tags=True, extra_fields=extra)
                )

        if truncated:
            output.append(
                f"\n*Showing {limit} of {len(parent_items)} items. Increase the limit parameter to see more.*"
            )

        result = "\n".join(output)
        if detail == "full":
            result = _helpers._prepend_size_warning(result, 'Use detail="summary" for a lighter response.')
        return result

    except Exception as e:
        await ctx.error(f"Error fetching collection items: {str(e)}")
        return f"Error fetching collection items: {str(e)}"


@mcp.tool(
    name="zotero_get_item_children",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    description=(
        "List the child items (attachments, notes, and annotations that are "
        "direct children of the attachment) of ONE parent Zotero item. "
        "Use this to find an item's PDF/EPUB attachment key before calling "
        "zotero_create_annotation, zotero_create_area_annotation, or "
        "zotero_get_pdf_outline — all of which take an attachment key, NOT "
        "the parent item key. "
        "If you need children for several items at once, use "
        "zotero_get_items_children (one batched API call instead of N). "
        "item_key: the parent item's 8-character key. "
        "Returns parent-child structure as markdown: each attachment with "
        "its content type and filename, each note with its title. "
        "Scope: active library only. "
        "Example: zotero_get_item_children(item_key='RTKZQI8E') → its "
        "PDF attachment key + any notes."
    ),
)
@with_zotero_api_lock
async def get_item_children(item_key: str, *, ctx: Context) -> str:
    """
    Get all child items (attachments, notes) for a specific Zotero item.

    Args:
        item_key: Zotero item key/ID
        ctx: MCP context

    Returns:
        Markdown-formatted list of child items
    """
    try:
        await ctx.info(f"Fetching children for item {item_key}")
        zot = await _client.run_zotero_call(_client.get_zotero_client, operation="get_zotero_client")

        # First get the parent item details
        try:
            parent = await _client.run_zotero_call(zot.item, item_key, operation=f"zot.item({item_key})")
            parent_title = parent["data"].get("title", "Untitled Item")
        except Exception:
            parent_title = f"Item {item_key}"

        # Check children cache
        children_cache = get_children_cache()
        cache_key = f"children:{item_key}"
        children = children_cache.get(cache_key)
        if children is None:
            # Then get the children
            children = await _client.run_zotero_call(zot.children, item_key, operation=f"zot.children({item_key})")
            children_cache.set(cache_key, children or [])
        if not children:
            return f"No child items found for: {parent_title} (Key: {item_key})"

        # Format children as markdown
        output = ["# Child Items", "", f"**Parent Key:** {item_key}", f"**Parent Title:** {parent_title}", ""]

        # Group children by type
        attachments = []
        notes = []
        others = []

        for child in children:
            data = child.get("data", {})
            item_type = data.get("itemType", "unknown")

            if item_type == "attachment":
                attachments.append(child)
            elif item_type == "note":
                notes.append(child)
            else:
                others.append(child)

        # Format attachments
        if attachments:
            output.append("## Attachments")
            for i, att in enumerate(attachments, 1):
                _append_child_block(output, att, i, parent_key=item_key)

        # Format notes
        if notes:
            output.append("## Notes")
            for i, note in enumerate(notes, 1):
                _append_child_block(output, note, i, parent_key=item_key)

        # Format other item types
        if others:
            output.append("## Other Items")
            for i, other in enumerate(others, 1):
                _append_child_block(output, other, i, parent_key=item_key)

        return "\n".join(output)

    except Exception as e:
        error_msg = str(e)
        suggestion = ""
        if "timeout" in error_msg.lower() or "timed out" in error_msg.lower():
            suggestion = " Zotero may be slow to respond. Try again in a moment."
        elif "connection" in error_msg.lower():
            suggestion = " Check if Zotero is running."
        elif "not found" in error_msg.lower():
            suggestion = " Verify the item key is correct. Use zotero_search_items to find items."
        await ctx.error(f"Error fetching item children: {error_msg}")
        return f"Error fetching item children: {error_msg}{suggestion}"


@mcp.tool(
    name="zotero_get_items_children",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    description=(
        "Batch variant of zotero_get_item_children: fetch child items "
        "(attachments, notes, annotations) for MULTIPLE parent items in a "
        "single API round trip. "
        "Much cheaper than calling zotero_get_item_children N times — use "
        "this whenever you have 2+ item keys in hand. "
        "item_keys: list of 8-character parent item keys (also accepts a "
        "JSON-encoded list string). Pass as an ARRAY, not a single "
        "concatenated string. "
        "Returns a markdown section per parent with its children grouped "
        "underneath. Missing keys are reported per-item rather than "
        "aborting the whole call. "
        "Scope: active library only. "
        "Example: zotero_get_items_children("
        "item_keys=['RTKZQI8E', '9UZR8GXT'])."
    ),
)
@with_zotero_api_lock
async def get_items_children(item_keys: list[str] | str, *, ctx: Context) -> str:
    """
    Get child items for multiple Zotero items in a single call.

    Args:
        item_keys: List of item keys (or JSON string, or comma-separated string)
        ctx: MCP context
    """
    try:
        zot = await _client.run_zotero_call(_client.get_zotero_client, operation="get_zotero_client")
        keys = _helpers._normalize_str_list_input(item_keys, "item_keys")

        if not keys:
            return "Error: No item keys provided."

        # Batch-resolve parent titles (50 per API call)
        parent_titles = {}
        for batch_start in range(0, len(keys), 50):
            batch = keys[batch_start : batch_start + 50]
            try:
                items = await _client.run_zotero_call(
                    zot.items, itemKey=",".join(batch), operation=f"zot.items(batch {batch_start // 50 + 1})"
                )
                for item in items:
                    k = item.get("key", "")
                    parent_titles[k] = item.get("data", {}).get("title", "Untitled")
            except Exception as e:
                await ctx.warning(f"Batch parent lookup failed: {e}")
                for k in batch:
                    parent_titles.setdefault(k, f"(key: {k})")

        output = [f"# Children for {len(keys)} Items", ""]

        for key in keys:
            title = parent_titles.get(key, f"(key: {key})")
            output.append(f"## {title}")
            output.append(f"**Parent Key:** {key}")

            try:
                children = await _client.run_zotero_call(zot.children, key, operation=f"zot.children({key})")
            except Exception as e:
                output.append(f"**Error fetching children:** {e}")
                output.append("")
                continue

            if not children:
                output.append("No child items.")
                output.append("")
                continue

            for child in children:
                _append_child_block(output, child, parent_key=key, compact=True)

            output.append("")

        return "\n".join(output)

    except ValueError as e:
        return f"Input error: {e}"
    except Exception as e:
        await ctx.error(f"Error fetching items children: {str(e)}")
        return f"Error fetching items children: {str(e)}"


@mcp.tool(
    name="zotero_get_tags",
    description="Get all tags used in your Zotero library.",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
)
@with_zotero_api_lock
async def get_tags(limit: int | str | None = None, *, ctx: Context) -> str:
    """
    Get all tags used in your Zotero library.

    Args:
        limit: Maximum number of tags to return
        ctx: MCP context

    Returns:
        Markdown-formatted list of tags
    """
    try:
        await ctx.info("Fetching tags")
        zot = await _client.run_zotero_call(_client.get_zotero_client, operation="get_zotero_client")

        limit = _helpers._normalize_limit(limit, default=500, max_val=5000)

        # Check tags cache — cache the full list, slice per-request
        tags_cache = get_tags_cache()
        cache_key = "tags:all"
        tags = tags_cache.get(cache_key)
        if tags is None:
            # Use _paginate instead of zot.everything() to avoid RLock pickling
            tags = await _client.run_zotero_call(_helpers._paginate, zot.tags, operation="paginate(zot.tags)")
            tags_cache.set(cache_key, tags or [])
        if not tags:
            return "No tags found in your Zotero library."

        # Format tags as markdown
        total_count = len(tags)
        output = [f"# Zotero Tags ({total_count} total)", ""]

        # Sort tags alphabetically
        sorted_tags = sorted(tags)

        # Apply display limit
        truncated = False
        if limit and len(sorted_tags) > limit:
            sorted_tags = sorted_tags[:limit]
            truncated = True

        # Group tags alphabetically
        current_letter = None
        for tag in sorted_tags:
            first_letter = tag[0].upper() if tag else "#"

            if first_letter != current_letter:
                current_letter = first_letter
                output.append(f"## {current_letter}")

            output.append(f"- `{tag}`")

        if truncated:
            output.append(f"\n*Showing {limit} of {total_count} tags. Increase the limit parameter to see more.*")

        return "\n".join(output)

    except Exception as e:
        error_msg = str(e)
        suggestion = ""
        if "timeout" in error_msg.lower() or "timed out" in error_msg.lower():
            suggestion = " Zotero may be slow to respond. Try again in a moment."
        elif "connection" in error_msg.lower():
            suggestion = " Check if Zotero is running."
        await ctx.error(f"Error fetching tags: {error_msg}")
        return f"Error fetching tags: {error_msg}{suggestion}"


@mcp.tool(
    name="zotero_list_libraries",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    description=(
        "List every Zotero library this MCP can address: the user's "
        "personal library (libraryID=1 conventionally), all group "
        "libraries the user is a member of (with groupID), and (in "
        "local mode) RSS feed libraries. Each entry shows the "
        "library/group ID, display name, and item count. "
        "Use this to discover a library ID before calling "
        "zotero_switch_library — the two form a read-then-switch "
        "workflow. If the user only wants to see Zotero collections "
        "inside the CURRENT library, use zotero_get_collections "
        "instead. "
        "No parameters. "
        "In local mode: reads the local Zotero SQLite DB (fast, includes "
        "RSS feeds). In web mode: queries /groups via the Zotero web "
        "API (no feeds). "
        "Read-only; no side effects. The active library isn't flagged "
        "in the output — track it yourself from the last successful "
        "zotero_switch_library call (or the ZOTERO_LIBRARY_ID env var "
        "if none). "
        "Example: zotero_list_libraries()."
    ),
)
@with_zotero_api_lock
async def list_libraries(*, ctx: Context) -> str:
    """
    List all accessible Zotero libraries.

    In local mode, reads directly from the SQLite database.
    In web mode, queries groups via the Zotero API.

    Returns:
        Markdown-formatted list of libraries with item counts.
    """
    try:
        await ctx.info("Listing accessible libraries")
        local = _utils.is_local_mode()
        override = _client.get_active_library()

        output = ["# Zotero Libraries", ""]

        # Show active library context
        if override:
            output.append(f"> **Active library:** ID={override['library_id']}, type={override['library_type']}")
            output.append("")

        if local:
            from zotero_mcp.local_db import LocalZoteroReader

            reader = LocalZoteroReader()
            try:
                libraries = reader.get_libraries()

                # User library
                user_libs = [lib for lib in libraries if lib["type"] == "user"]
                if user_libs:
                    output.append("## User Library")
                    for lib in user_libs:
                        output.append(f"- **My Library** — {lib['itemCount']} items (libraryID={lib['libraryID']})")
                    output.append("")

                # Group libraries
                group_libs = [lib for lib in libraries if lib["type"] == "group"]
                if group_libs:
                    output.append("## Group Libraries")
                    for lib in group_libs:
                        desc = f" — {lib['groupDescription']}" if lib.get("groupDescription") else ""
                        output.append(
                            f"- **{lib['groupName']}** — {lib['itemCount']} items (groupID={lib['groupID']}){desc}"
                        )
                    output.append("")

                # Feeds
                feed_libs = [lib for lib in libraries if lib["type"] == "feed"]
                if feed_libs:
                    output.append("## RSS Feeds")
                    for lib in feed_libs:
                        output.append(
                            f"- **{lib['feedName']}** — {lib['itemCount']} items (libraryID={lib['libraryID']})"
                        )
                    output.append("")
            finally:
                reader.close()
        else:
            # Web mode: query groups via pyzotero
            zot = await _client.run_zotero_call(_client.get_zotero_client, operation="get_zotero_client")
            output.append("## User Library")
            output.append(f"- **My Library** (libraryID={os.getenv('ZOTERO_LIBRARY_ID', '?')})")
            output.append("")

            try:
                groups = await _client.run_zotero_call(zot.groups, operation="zot.groups()")
                if groups:
                    output.append("## Group Libraries")
                    for group in groups:
                        gdata = group.get("data", {})
                        output.append(f"- **{gdata.get('name', 'Unknown')}** (groupID={group.get('id', '?')})")
                    output.append("")
            except Exception:
                output.append("*Could not retrieve group libraries.*\n")

            output.append("*Note: RSS feeds are only accessible in local mode.*")

        output.append("")
        output.append("Use `zotero_switch_library` to switch to a different library.")

        return "\n".join(output)

    except Exception as e:
        await ctx.error(f"Error listing libraries: {str(e)}")
        return f"Error listing libraries: {str(e)}"


@mcp.tool(
    name="zotero_switch_library",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False),
    description=(
        "Switch the active library context. EVERY subsequent read/write "
        "tool call (collections, items, annotations, search — all of "
        "them) operates on the library set here. Changes persist for the "
        "rest of the session or until the next switch. "
        "Discover valid library IDs/types via zotero_list_libraries "
        "first; don't guess. "
        "library_id: library ID string as returned by "
        "zotero_list_libraries (numeric for user/group, numeric for "
        "feeds). "
        "library_type: 'user' — the personal library; 'group' (default) "
        "— a group library; 'feeds' — a local RSS feed library; "
        "'default' — RESET to whatever the ZOTERO_LIBRARY_ID / "
        "ZOTERO_LIBRARY_TYPE env vars configure (library_id is ignored "
        "in this mode). "
        "Fails fast if the library_id isn't accessible under the "
        "current credentials. "
        "Example: zotero_switch_library(library_id='5294983', "
        "library_type='group') or zotero_switch_library("
        "library_id='', library_type='default')."
    ),
)
@with_zotero_api_lock
async def switch_library(
    library_id: str,
    library_type: str = "group",
    *,
    ctx: Context,
) -> str:
    """
    Switch the active library for all subsequent MCP tool calls.

    Args:
        library_id: The library/group ID to switch to.
            For user library: "0" (local mode) or your user ID (web mode).
            For group libraries: the groupID (e.g. "6069773").
        library_type: "user", "group", or "default" to reset to env var defaults.
        ctx: MCP context

    Returns:
        Confirmation message with active library details.
    """
    try:
        if library_type == "default":
            _client.clear_active_library()
            await ctx.info("Reset to default library configuration")
            return (
                "Switched back to default library configuration "
                f"(ZOTERO_LIBRARY_ID={os.getenv('ZOTERO_LIBRARY_ID', '0')}, "
                f"ZOTERO_LIBRARY_TYPE={os.getenv('ZOTERO_LIBRARY_TYPE', 'user')})"
            )

        error = validate_library_switch(library_id, library_type)
        if error:
            return error

        _client.set_active_library(library_id, library_type)
        await ctx.info(f"Switched to library {library_id} (type={library_type})")

        # Verify the switch works by making a test call
        try:
            zot = await _client.run_zotero_call(_client.get_zotero_client, operation="get_zotero_client")

            def _validate_lib():
                zot.add_parameters(limit=1)
                zot.items()

            await _client.run_zotero_call(_validate_lib, operation=f"validate_library({library_id})")
            return (
                f"Successfully switched to library **{library_id}** "
                f"(type={library_type}). All tools now operate on this library."
            )
        except Exception as e:
            # Roll back on failure
            _client.clear_active_library()
            return (
                f"Error: Could not access library {library_id} (type={library_type}): {e}. Reverted to default library."
            )

    except Exception as e:
        await ctx.error(f"Error switching library: {str(e)}")
        return f"Error switching library: {str(e)}"


@with_zotero_api_lock
def validate_library_switch(library_id: str, library_type: str) -> str | None:
    """Validate a library switch request before applying it.

    Returns an error message string if the switch should be rejected,
    or None if the switch is valid and should proceed.
    """
    if library_type not in ("user", "group", "feed"):
        return f"Invalid library_type '{library_type}'. Must be 'user', 'group', or 'feed'."

    # In local mode, verify the library actually exists in the database
    local = _utils.is_local_mode()
    if local:
        try:
            from zotero_mcp.local_db import LocalZoteroReader

            reader = LocalZoteroReader()
            try:
                libraries = reader.get_libraries()
                if library_type == "group":
                    valid_ids = {str(lib["groupID"]) for lib in libraries if lib["type"] == "group"}
                    if library_id not in valid_ids:
                        return f"Group '{library_id}' not found. Available groups: {', '.join(sorted(valid_ids))}"
                elif library_type == "feed":
                    valid_ids = {str(lib["libraryID"]) for lib in libraries if lib["type"] == "feed"}
                    if library_id not in valid_ids:
                        return (
                            f"Feed with libraryID '{library_id}' not found. "
                            f"Available feeds: {', '.join(sorted(valid_ids))}"
                        )
            finally:
                reader.close()
        except Exception:
            pass  # If DB unavailable, skip validation — the test call will catch it

    return None


@mcp.tool(
    name="zotero_list_feeds",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    description=(
        "List all RSS feed subscriptions configured in the local Zotero "
        "desktop install. Each entry includes the feed's library ID, "
        "display name, source URL, item count, and last-checked "
        "timestamp. "
        "Use this to discover a feed's library_id before calling "
        "zotero_get_feed_items; the two form a list-then-fetch workflow "
        "analogous to list_libraries + switch_library. "
        "No parameters. "
        "LOCAL MODE ONLY — RSS feeds live in the local SQLite database "
        "and are not exposed by the Zotero web API. Running this in web "
        "mode returns a clear error. Read-only; no side effects. "
        "Example: zotero_list_feeds() → all subscribed feeds."
    ),
)
@with_zotero_api_lock
async def list_feeds(*, ctx: Context) -> str:
    """
    List all RSS feed subscriptions from the local Zotero database.

    Returns:
        Markdown-formatted list of RSS feeds.
    """
    try:
        local = _utils.is_local_mode()
        if not local:
            return "RSS feeds are only accessible in local mode (ZOTERO_LOCAL=true)."

        await ctx.info("Listing RSS feeds")
        from zotero_mcp.local_db import LocalZoteroReader

        reader = LocalZoteroReader()
        try:
            feeds = reader.get_feeds()
            if not feeds:
                return "No RSS feeds found in your Zotero installation."

            output = ["# RSS Feeds", ""]
            for feed in feeds:
                last_check = feed["lastCheck"] or "never"
                error = f" (error: {feed['lastCheckError']})" if feed.get("lastCheckError") else ""
                output.append(f"### {feed['name']}")
                output.append(f"- **URL:** {feed['url']}")
                output.append(f"- **Items:** {feed['itemCount']}")
                output.append(f"- **Last checked:** {last_check}{error}")
                output.append(f"- **Library ID:** {feed['libraryID']}")
                output.append("")

            output.append("Use `zotero_get_feed_items` with a feed's library ID to view its items.")
            return "\n".join(output)
        finally:
            reader.close()

    except Exception as e:
        await ctx.error(f"Error listing feeds: {str(e)}")
        return f"Error listing feeds: {str(e)}"


@mcp.tool(
    name="zotero_get_feed_items",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    description=(
        "Fetch recent items from a SPECIFIC Zotero RSS feed by its local "
        "library ID. Returns titles, authors, dates, and URLs as a "
        "markdown list. "
        "Find the right library_id first with zotero_list_feeds — "
        "guessing feed IDs never works. "
        "library_id: INTEGER library ID of the feed (as shown by "
        "zotero_list_feeds, NOT the feed's name or URL). "
        "limit: max feed items to return (default 20). "
        "LOCAL MODE ONLY — feeds aren't exposed by the Zotero web API. "
        "Calls in web mode return a clear error. Read-only; does not "
        "trigger a new RSS fetch (Zotero desktop refreshes on its own "
        "schedule). "
        "Example: zotero_get_feed_items(library_id=12, limit=30)."
    ),
)
@with_zotero_api_lock
async def get_feed_items(
    library_id: int,
    limit: int = 20,
    *,
    ctx: Context,
) -> str:
    """
    Retrieve items from a specific RSS feed.

    Args:
        library_id: The libraryID of the feed (from zotero_list_feeds).
        limit: Maximum number of items to return.
        ctx: MCP context

    Returns:
        Markdown-formatted list of feed items.
    """
    try:
        local = _utils.is_local_mode()
        if not local:
            return "RSS feed items are only accessible in local mode (ZOTERO_LOCAL=true)."

        await ctx.info(f"Fetching items from feed (libraryID={library_id})")
        from zotero_mcp.local_db import LocalZoteroReader

        reader = LocalZoteroReader()
        try:
            # Verify this is actually a feed
            feeds = reader.get_feeds()
            feed_info = next((f for f in feeds if f["libraryID"] == library_id), None)
            if not feed_info:
                valid_ids = [str(f["libraryID"]) for f in feeds]
                return f"No feed found with libraryID={library_id}. Valid feed IDs: {', '.join(valid_ids)}"

            items = reader.get_feed_items(library_id, limit=limit)
            if not items:
                return f"No items found in feed '{feed_info['name']}'."

            output = [f"# Feed: {feed_info['name']}", f"**URL:** {feed_info['url']}", ""]

            for item in items:
                read_status = "Read" if item.get("readTime") else "Unread"
                title = item.get("title") or "Untitled"
                output.append(f"### {title}")
                output.append(f"- **Status:** {read_status}")
                if item.get("creators"):
                    output.append(f"- **Authors:** {item['creators']}")
                if item.get("url"):
                    output.append(f"- **URL:** {item['url']}")
                output.append(f"- **Added:** {item.get('dateAdded', 'unknown')}")
                if item.get("abstract"):
                    abstract = _utils.clean_html(item["abstract"])
                    if len(abstract) > 200:
                        abstract = abstract[:200] + "..."
                    output.append(f"- **Abstract:** {abstract}")
                output.append("")

            return "\n".join(output)
        finally:
            reader.close()

    except Exception as e:
        await ctx.error(f"Error fetching feed items: {str(e)}")
        return f"Error fetching feed items: {str(e)}"


@mcp.tool(
    name="zotero_get_recent",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    description=(
        "List the most recently ADDED items (by dateAdded) in the active "
        "library, optionally scoped to a single collection. "
        "Use this for 'what did I add recently?' questions — NOT for "
        "general topic search (use zotero_semantic_search) or for a "
        "collection's full contents (use zotero_get_collection_items). "
        "limit: how many recent items to return (default 10). "
        "collection_key: optional 8-character collection key to restrict "
        "results to that collection; when omitted, returns the N most "
        "recent items across the whole library. "
        "Ordering is dateAdded DESC. All item types are returned, "
        "INCLUDING standalone notes and attachments — so results can mix "
        "papers, notes, and loose PDFs. If you only want parent items, "
        "filter client-side by itemType in the output. "
        "Scope: active library only (switch with zotero_switch_library). "
        "Example: zotero_get_recent(limit=20) or "
        "zotero_get_recent(collection_key='MT53KB66', limit=5)."
    ),
)
@with_zotero_api_lock
async def get_recent(limit: int | str = 10, collection_key: str | None = None, *, ctx: Context) -> str:
    """
    Get recently added items to your Zotero library.

    Args:
        limit: Number of items to return
        collection_key: Optional collection key to scope results to a specific collection
        ctx: MCP context

    Returns:
        Markdown-formatted list of recent items
    """
    try:
        await ctx.info(f"Fetching {limit} recent items")
        zot = await _client.run_zotero_call(_client.get_zotero_client, operation="get_zotero_client")

        limit = _helpers._normalize_limit(limit, default=10)

        # Get recent items, optionally scoped to a collection
        if collection_key:
            try:
                _col = await _client.run_zotero_call(
                    zot.collection, collection_key, operation=f"zot.collection({collection_key})"
                )
            except Exception:
                _col = None
            if not _col or _col.get("key") != collection_key:
                return f"Collection not found: '{collection_key}'. Use zotero_get_collections or zotero_search_collections to find valid collection keys."
            items = await _client.run_zotero_call(
                zot.collection_items,
                collection_key,
                sort="dateAdded",
                direction="desc",
                limit=limit,
                operation=f"zot.collection_items({collection_key})",
            )
        else:
            items = await _client.run_zotero_call(
                zot.items, limit=limit, sort="dateAdded", direction="desc", operation="zot.items(recent)"
            )

        if not items:
            return (
                "No items found in your Zotero library."
                if not collection_key
                else f"No items found in collection: {collection_key}"
            )

        # Format items as markdown
        scope = f" in Collection {collection_key}" if collection_key else ""
        output = [f"# {limit} Most Recently Added Items{scope}", ""]

        for i, item in enumerate(items, 1):
            added = item.get("data", {}).get("dateAdded", "Unknown")
            output.extend(
                _utils.format_item_result(
                    item,
                    index=i,
                    abstract_len=0,
                    include_tags=False,
                    extra_fields={"Added": added},
                )
            )

        return "\n".join(output)

    except Exception as e:
        await ctx.error(f"Error fetching recent items: {str(e)}")
        return f"Error fetching recent items: {str(e)}"


@mcp.tool(
    name="zotero_get_items_metadata",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    description=(
        "Fetch metadata for MULTIPLE Zotero items in a single call. "
        "Much more efficient than calling zotero_get_item_metadata "
        "repeatedly. "
        "item_keys: comma-separated list of 8-character Zotero item keys "
        "(e.g. 'ABC12345,DEF67890'). Max 50 keys per call. "
        "include_abstract=False (default) omits abstracts to save tokens; "
        "set True when you need abstracts. "
        "format='markdown' (default) or 'bibtex'. "
        "Scope: active library only. "
        "Example: zotero_get_items_metadata(item_keys='RTKZQI8E,ABCD1234')."
    ),
)
@with_zotero_api_lock
async def get_items_metadata(
    item_keys: str,
    include_abstract: bool = False,
    format: Literal["markdown", "bibtex"] = "markdown",
    *,
    ctx: Context,
) -> str:
    """Batch metadata retrieval for multiple items."""
    try:
        keys = [k.strip() for k in item_keys.replace(";", ",").split(",") if k.strip()]
        if not keys:
            return "Error: No item keys provided."
        if len(keys) > 50:
            return f"Error: Too many keys ({len(keys)}). Max 50 per call."

        for k in keys:
            err = _helpers.validate_item_key(k)
            if err:
                return f"Error: Invalid key '{k}': {err}"

        await ctx.info(f"Fetching metadata for {len(keys)} items")
        zot = await _client.run_zotero_call(_client.get_zotero_client, operation="get_zotero_client")

        # Use comma-separated keys for a single API call
        joined = ",".join(keys)
        items = await _client.run_zotero_call(zot.items, itemKey=joined, operation=f"zot.items({joined})")
        if not items:
            return f"No items found for keys: {joined}"

        # Build a lookup for ordering
        items_by_key = {}
        for item in items:
            k = item.get("key", "")
            items_by_key[k] = item

        output = []
        missing = []
        for k in keys:
            item = items_by_key.get(k)
            if not item:
                missing.append(k)
                continue
            if format == "bibtex":
                output.append(_client.generate_bibtex(item))
            else:
                output.append(_client.format_item_metadata(item, include_abstract))
                output.append("\n---\n")

        result = "\n".join(output)
        if missing:
            result += f"\n\n*Keys not found: {', '.join(missing)}*"
        return result

    except Exception as e:
        await ctx.error(f"Error fetching batch metadata: {str(e)}")
        return f"Error fetching batch metadata: {str(e)}"


@mcp.tool(
    name="zotero_get_recent_changes",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    description=(
        "List items most recently MODIFIED (by dateModified) in the active "
        "library. Use this for 'what did I edit/update recently?' questions. "
        "Unlike zotero_get_recent (which sorts by dateAdded), this shows "
        "items whose metadata, tags, or notes were recently changed. "
        "limit: how many items to return (default 10). "
        "since: optional ISO date string (e.g. '2026-06-01') to only show "
        "items modified after that date. "
        "Scope: active library only. "
        "Example: zotero_get_recent_changes(limit=20, since='2026-06-01')."
    ),
)
@with_zotero_api_lock
async def get_recent_changes(
    limit: int | str = 10,
    since: str | None = None,
    *,
    ctx: Context,
) -> str:
    """Get recently modified items."""
    try:
        await ctx.info("Fetching recently modified items")
        zot = await _client.run_zotero_call(_client.get_zotero_client, operation="get_zotero_client")
        limit = _helpers._normalize_limit(limit, default=10)

        params = dict(limit=limit, sort="dateModified", direction="desc")
        if since:
            params["since"] = since

        items = await _client.run_zotero_call(zot.items, operation="zot.items(recent_changes)", **params)
        if not items:
            return "No recently modified items found."

        output = [f"# {limit} Most Recently Modified Items", ""]
        for i, item in enumerate(items, 1):
            modified = item.get("data", {}).get("dateModified", "Unknown")
            added = item.get("data", {}).get("dateAdded", "Unknown")
            output.extend(
                _utils.format_item_result(
                    item,
                    index=i,
                    abstract_len=0,
                    include_tags=False,
                    extra_fields={"Modified": modified, "Added": added},
                )
            )

        return "\n".join(output)

    except Exception as e:
        await ctx.error(f"Error fetching recent changes: {str(e)}")
        return f"Error fetching recent changes: {str(e)}"


@mcp.tool(
    name="zotero_get_citation_graph",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    description=(
        "Find items RELATED to a given item via Zotero's 'Related' links "
        "(dc:relation). Returns items that the target cites AND items that "
        "cite the target (if both are in your library). "
        "item_key: the 8-character Zotero item key. "
        "depth: how many hops to traverse (default 1, max 2). "
        "Scope: active library only. "
        "Example: zotero_get_citation_graph(item_key='RTKZQI8E')."
    ),
)
@with_zotero_api_lock
async def get_citation_graph(
    item_key: str,
    depth: int | str = 1,
    *,
    ctx: Context,
) -> str:
    """Traverse citation/related-item graph."""
    try:
        key_err = _helpers.validate_item_key(item_key)
        if key_err:
            return f"Error: {key_err}"

        depth = int(depth)
        if depth < 1 or depth > 2:
            depth = 1

        await ctx.info(f"Building citation graph for {item_key} (depth={depth})")
        zot = await _client.run_zotero_call(_client.get_zotero_client, operation="get_zotero_client")

        # Get the seed item
        seed = await _client.run_zotero_call(zot.item, item_key, operation=f"zot.item({item_key})")
        if not seed:
            return f"No item found with key: {item_key}"

        seed_data = seed.get("data", {})
        seed_title = seed_data.get("title", "Untitled")

        # Extract related URIs from the seed
        related_uris = seed_data.get("relations", {}).get("dc:relation", [])
        if isinstance(related_uris, str):
            related_uris = [related_uris]

        # Parse item keys from URIs (format: "http://zotero.org/users/.../items/XXXXX")
        def extract_key(uri: str) -> str | None:
            if "/items/" in uri:
                return uri.split("/items/")[-1]
            return None

        related_keys = [k for uri in related_uris if (k := extract_key(uri))]

        if not related_keys:
            return f"No related items found for '{seed_title}' ({item_key})."

        # Fetch related items
        seen = {item_key}
        graph: dict[str, dict] = {}

        async def _fetch_related(key: str, current_depth: int):
            if key in seen or current_depth > depth:
                return
            seen.add(key)
            try:
                item = await _client.run_zotero_call(zot.item, key, operation=f"zot.item({key})")
            except Exception:
                return
            if not item:
                return
            data = item.get("data", {})
            graph[key] = {
                "title": data.get("title", "Untitled"),
                "creators": _utils.format_creators(data.get("creators", []), max_authors=3),
                "year": data.get("date", "")[:4] if data.get("date") else "",
                "itemType": data.get("itemType", ""),
                "related_keys": [
                    k
                    for uri in data.get("relations", {}).get("dc:relation", [])
                    if (k := extract_key(uri)) and k not in seen
                ],
            }
            if current_depth < depth:
                for rk in graph[key]["related_keys"]:
                    await _fetch_related(rk, current_depth + 1)

        for rk in related_keys:
            await _fetch_related(rk, 1)

        if not graph:
            return f"Found {len(related_keys)} related keys for '{seed_title}', but none are in your library."

        output = [f"# Citation Graph for: {seed_title}", f"**Key:** {item_key}", ""]
        output.append(f"**Related items found:** {len(graph)}")
        output.append("")

        for key, info in graph.items():
            year_str = f" ({info['year']})" if info["year"] else ""
            output.append(f"- **{info['title']}**{year_str}")
            output.append(f"  - Key: `{key}` | Type: {info['itemType']} | Authors: {info['creators']}")
            if info["related_keys"]:
                output.append(f"  - Also relates to: {', '.join(f'`{k}`' for k in info['related_keys'])}")

        return "\n".join(output)

    except Exception as e:
        await ctx.error(f"Error building citation graph: {str(e)}")
        return f"Error building citation graph: {str(e)}"


@mcp.tool(
    name="zotero_summarize_collection",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    description=(
        "Get a bird's-eye statistical overview of a Zotero collection: "
        "item count, top authors, year distribution, item types, and "
        "common tags. Useful for quickly understanding what a collection "
        "contains without reading every item. "
        "collection_key: the 8-character collection key. "
        "Scope: active library only. "
        "Example: zotero_summarize_collection(collection_key='MT53KB66')."
    ),
)
@with_zotero_api_lock
async def summarize_collection(
    collection_key: str,
    *,
    ctx: Context,
) -> str:
    """Generate a statistical summary of a collection."""
    try:
        key_err = _helpers.validate_collection_key(collection_key)
        if key_err:
            return f"Error: {key_err}"

        await ctx.info(f"Summarizing collection {collection_key}")
        zot = await _client.run_zotero_call(_client.get_zotero_client, operation="get_zotero_client")

        # Get collection name
        try:
            col = await _client.run_zotero_call(
                zot.collection, collection_key, operation=f"zot.collection({collection_key})"
            )
            col_name = col["data"].get("name", "Unnamed Collection")
        except Exception:
            return f"Collection not found: {collection_key}"

        # Fetch all items
        all_items = await _client.run_zotero_call(
            _helpers._paginate,
            zot.collection_items,
            collection_key,
            operation=f"paginate(zot.collection_items, {collection_key})",
        )
        if not all_items:
            return f"Collection '{col_name}' is empty."

        # Filter to parent items
        child_types = {"attachment", "note", "annotation"}
        parent_items = [i for i in all_items if i.get("data", {}).get("itemType", "") not in child_types]

        if not parent_items:
            return f"Collection '{col_name}' has no parent items (only attachments/notes)."

        # Compute stats
        from collections import Counter

        type_counter: Counter[str] = Counter()
        year_counter: Counter[str] = Counter()
        author_counter: Counter[str] = Counter()
        tag_counter: Counter[str] = Counter()
        has_pdf = 0
        has_abstract = 0

        for item in parent_items:
            data = item.get("data", {})
            type_counter[data.get("itemType", "unknown")] += 1

            date = data.get("date", "")
            year = date[:4] if len(date) >= 4 and date[:4].isdigit() else "Unknown"
            year_counter[year] += 1

            for creator in data.get("creators", []):
                name = creator.get("lastName") or creator.get("name") or ""
                if name:
                    author_counter[name] += 1

            for tag in data.get("tags", []):
                tag_counter[tag.get("tag", "")] += 1

            if data.get("abstractNote"):
                has_abstract += 1

        # Count PDFs from children
        pdf_keys = set()
        for item in all_items:
            d = item.get("data", {})
            if d.get("itemType") == "attachment" and d.get("contentType") == "application/pdf":
                parent = d.get("parentItem", "")
                if parent:
                    pdf_keys.add(parent)
        has_pdf = len(pdf_keys)

        # Build output
        total = len(parent_items)
        output = [
            f"# Collection Summary: {col_name}",
            f"**Total items:** {total}",
            f"**With PDF:** {has_pdf} ({has_pdf * 100 // total}%)",
            f"**With abstract:** {has_abstract} ({has_abstract * 100 // total}%)",
            "",
        ]

        # Item types
        output.append("## Item Types")
        for itype, count in type_counter.most_common(10):
            output.append(f"- {itype}: {count}")
        output.append("")

        # Year distribution (top 10)
        output.append("## Year Distribution")
        for year, count in sorted(year_counter.items(), reverse=True)[:10]:
            bar = "█" * min(count, 30)
            output.append(f"- {year}: {bar} ({count})")
        output.append("")

        # Top authors
        output.append("## Top Authors")
        for author, count in author_counter.most_common(15):
            output.append(f"- {author}: {count} papers")
        output.append("")

        # Top tags
        if tag_counter:
            output.append("## Top Tags")
            for tag, count in tag_counter.most_common(15):
                output.append(f"- `{tag}`: {count}")

        return "\n".join(output)

    except Exception as e:
        await ctx.error(f"Error summarizing collection: {str(e)}")
        return f"Error summarizing collection: {str(e)}"


@mcp.tool(
    name="zotero_generate_bibliography",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    description=(
        "Generate a formatted bibliography from Zotero item keys using any "
        "CSL citation style (APA, Chicago, IEEE, Harvard, etc.). "
        "item_keys: comma-separated list of 8-character Zotero item keys "
        "(max 150). "
        "style: CSL style name from zotero.org/styles (default 'apa'). "
        "Examples: 'chicago-note-bibliography', 'ieee', 'harvard-cite-them-right', "
        "'vancouver', 'nature'. "
        "locale: bibliography locale (default 'en-US'). "
        "Returns formatted bibliography as text. "
        "Example: zotero_generate_bibliography(item_keys='ABC12345,DEF67890', style='apa')."
    ),
)
@with_zotero_api_lock
async def generate_bibliography(
    item_keys: str,
    style: str = "apa",
    locale: str = "en-US",
    *,
    ctx: Context,
) -> str:
    """Generate a formatted bibliography from item keys."""
    try:
        keys = [k.strip() for k in item_keys.replace(";", ",").split(",") if k.strip()]
        if not keys:
            return "Error: No item keys provided."
        if len(keys) > 150:
            return f"Error: Too many keys ({len(keys)}). Max 150 per call."

        for k in keys:
            err = _helpers.validate_item_key(k)
            if err:
                return f"Error: Invalid key '{k}': {err}"

        await ctx.info(f"Generating bibliography for {len(keys)} items (style={style})")
        zot = await _client.run_zotero_call(_client.get_zotero_client, operation="get_zotero_client")

        # Use Zotero's built-in bibliography generation
        joined = ",".join(keys)
        try:
            # Try using the API's format=bib feature
            import requests as _requests

            # Build the request URL
            lib_type = zot.library_type if hasattr(zot, "library_type") else "users"
            lib_id = zot.library_id if hasattr(zot, "library_id") else "0"
            if _utils.is_local_mode():
                base_url = f"http://localhost:23119/api/{lib_type}/{lib_id}"
            else:
                base_url = f"https://api.zotero.org/{lib_type}/{lib_id}"

            url = f"{base_url}/items"
            params = {
                "itemKey": joined,
                "format": "bib",
                "style": style,
                "locale": locale,
            }
            headers = {"Zotero-API-Version": "3"}
            if not _utils.is_local_mode():
                from zotero_mcp.config import load_config

                cfg = load_config()
                api_key = cfg.get("client_env", {}).get("ZOTERO_API_KEY", "")
                if api_key:
                    headers["Zotero-API-Key"] = api_key

            resp = _requests.get(url, params=params, headers=headers, timeout=30)
            if resp.status_code == 200:
                bib_text = resp.text.strip()
                if bib_text:
                    return f"# Bibliography ({style})\n\n{bib_text}"

        except Exception as e:
            await ctx.info(f"API bibliography failed ({e}), falling back to local generation")

        # Fallback: generate bibliography locally from item data
        items = await _client.run_zotero_call(zot.items, itemKey=joined, operation=f"zot.items(bib:{joined})")
        if not items:
            return f"No items found for keys: {joined}"

        output = [f"# Bibliography ({style})", ""]
        for item in items:
            # Use the existing BibTeX generator as fallback
            output.append(_client.generate_bibtex(item))
            output.append("")

        output.append(
            "*Note: Generated using local BibTeX fallback. For formatted bibliography, ensure Zotero is running with the local API enabled.*"
        )
        return "\n".join(output)

    except Exception as e:
        await ctx.error(f"Error generating bibliography: {str(e)}")
        return f"Error generating bibliography: {str(e)}"


@mcp.tool(
    name="zotero_export_items",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    description=(
        "Export Zotero items in various bibliographic formats. "
        "item_keys: comma-separated list of 8-character Zotero item keys (max 50). "
        "format: export format. Options: 'bibtex', 'biblatex', 'ris', 'csljson', "
        "'csv', 'tei', 'wikipedia', 'mods', 'rdf_dc', 'rdf_zotero', 'refer'. "
        "Default is 'bibtex'. "
        "Example: zotero_export_items(item_keys='ABC12345', format='ris')."
    ),
)
@with_zotero_api_lock
async def export_items(
    item_keys: str,
    format: str = "bibtex",
    *,
    ctx: Context,
) -> str:
    """Export items in various bibliographic formats."""
    try:
        keys = [k.strip() for k in item_keys.replace(";", ",").split(",") if k.strip()]
        if not keys:
            return "Error: No item keys provided."
        if len(keys) > 50:
            return f"Error: Too many keys ({len(keys)}). Max 50 per call."

        for k in keys:
            err = _helpers.validate_item_key(k)
            if err:
                return f"Error: Invalid key '{k}': {err}"

        valid_formats = {
            "bibtex",
            "biblatex",
            "ris",
            "csljson",
            "csv",
            "tei",
            "wikipedia",
            "mods",
            "rdf_dc",
            "rdf_zotero",
            "refer",
            "bookmarks",
            "coins",
        }
        if format not in valid_formats:
            return f"Error: Unknown format '{format}'. Valid: {', '.join(sorted(valid_formats))}"

        await ctx.info(f"Exporting {len(keys)} items as {format}")
        zot = await _client.run_zotero_call(_client.get_zotero_client, operation="get_zotero_client")

        joined = ",".join(keys)

        # Try API-based export first
        try:
            import requests as _requests

            lib_type = zot.library_type if hasattr(zot, "library_type") else "users"
            lib_id = zot.library_id if hasattr(zot, "library_id") else "0"
            if _utils.is_local_mode():
                base_url = f"http://localhost:23119/api/{lib_type}/{lib_id}"
            else:
                base_url = f"https://api.zotero.org/{lib_type}/{lib_id}"

            url = f"{base_url}/items"
            params = {"itemKey": joined, "format": format}
            headers = {"Zotero-API-Version": "3"}
            if not _utils.is_local_mode():
                from zotero_mcp.config import load_config

                cfg = load_config()
                api_key = cfg.get("client_env", {}).get("ZOTERO_API_KEY", "")
                if api_key:
                    headers["Zotero-API-Key"] = api_key

            resp = _requests.get(url, params=params, headers=headers, timeout=30)
            if resp.status_code == 200 and resp.text.strip():
                return f"# Export ({format})\n\n{resp.text.strip()}"

        except Exception as e:
            await ctx.info(f"API export failed ({e}), falling back to local")

        # Fallback: local BibTeX generation
        if format == "bibtex":
            items = await _client.run_zotero_call(zot.items, itemKey=joined, operation=f"zot.items(export:{joined})")
            if not items:
                return f"No items found for keys: {joined}"
            output = []
            for item in items:
                output.append(_client.generate_bibtex(item))
            return "\n\n".join(output)

        return f"Error: {format} export requires the Zotero API. Ensure Zotero is running."

    except Exception as e:
        await ctx.error(f"Error exporting items: {str(e)}")
        return f"Error exporting items: {str(e)}"


@mcp.tool(
    name="zotero_get_trash_items",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    description=(
        "List items in the Zotero trash. Trashed items can be restored. "
        "limit: max items to return (default 25). "
        "Scope: active library only."
    ),
)
@with_zotero_api_lock
async def get_trash_items(limit: int | str = 25, *, ctx: Context) -> str:
    """List items in the Zotero trash."""
    try:
        await ctx.info("Fetching trashed items")
        zot = await _client.run_zotero_call(_client.get_zotero_client, operation="get_zotero_client")
        limit = _helpers._normalize_limit(limit, default=25)

        items = await _client.run_zotero_call(
            zot.trash, limit=limit, sort="dateModified", direction="desc", operation="zot.trash"
        )
        if not items:
            return "Trash is empty."

        output = [f"# Zotero Trash ({len(items)} items)", ""]
        for i, item in enumerate(items, 1):
            data = item.get("data", {})
            key = item.get("key", "")
            title = data.get("title", "Untitled")
            item_type = data.get("itemType", "")
            date = data.get("date", "")
            output.append(f"{i}. **{title}** [{item_type}]")
            output.append(f"   Key: `{key}` | Date: {date}")

        return "\n".join(output)

    except Exception as e:
        await ctx.error(f"Error fetching trash: {str(e)}")
        return f"Error fetching trash: {str(e)}"


@mcp.tool(
    name="zotero_restore_from_trash",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False),
    description=(
        "Restore an item from the Zotero trash back to the library. "
        "item_key: the 8-character key of the trashed item. "
        "Scope: active library only."
    ),
)
@with_zotero_api_lock
async def restore_from_trash(item_key: str, *, ctx: Context) -> str:
    """Restore an item from trash."""
    try:
        key_err = _helpers.validate_item_key(item_key)
        if key_err:
            return f"Error: {key_err}"

        await ctx.info(f"Restoring item {item_key} from trash")
        zot = await _client.run_zotero_call(_client.get_web_zotero_client, operation="get_web_zotero_client")
        if not zot:
            return "Error: Web API client required for restoring items. Set ZOTERO_API_KEY and ZOTERO_LIBRARY_ID."

        # Get the item to check its current state
        item = await _client.run_zotero_call(zot.item, item_key, operation=f"zot.item({item_key})")
        if not item:
            return f"Item not found: {item_key}"

        data = item.get("data", {})
        if not data.get("deleted"):
            return f"Item '{data.get('title', item_key)}' is not in trash."

        # Restore by setting deleted to 0
        item["data"]["deleted"] = 0
        try:
            resp = zot.update_item(item)
            if await _helpers._handle_write_response(resp, ctx):
                return f"Restored '{data.get('title', item_key)}' ({item_key}) from trash."
            return f"Failed to restore item: {resp}"
        except Exception as e:
            return f"Error restoring item: {e}"

    except Exception as e:
        await ctx.error(f"Error restoring from trash: {str(e)}")
        return f"Error restoring from trash: {str(e)}"


@mcp.tool(
    name="zotero_get_item_types",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    description=(
        "List all available Zotero item types (book, journalArticle, webpage, etc.) "
        "with their localized names. Useful for understanding what types of items "
        "can be created or searched for. "
        "Returns a list of item type identifiers and their human-readable names."
    ),
)
@with_zotero_api_lock
async def get_item_types(*, ctx: Context) -> str:
    """List all available Zotero item types."""
    try:
        await ctx.info("Fetching item types")

        # Try to get item types from the API
        try:
            import requests as _requests

            if _utils.is_local_mode():
                base_url = "http://localhost:23119/api"
            else:
                base_url = "https://api.zotero.org"

            resp = _requests.get(f"{base_url}/itemTypes", headers={"Zotero-API-Version": "3"}, timeout=10)
            if resp.status_code == 200:
                types = resp.json()
                output = [f"# Zotero Item Types ({len(types)})", ""]
                for t in types:
                    output.append(f"- `{t['itemType']}` — {t.get('localized', t['itemType'])}")
                return "\n".join(output)
        except Exception as e:
            await ctx.info(f"API item types fetch failed ({e}), using local fallback")

        # Fallback: use known item types
        known_types = [
            ("book", "Book"),
            ("bookSection", "Book Section"),
            ("journalArticle", "Journal Article"),
            ("magazineArticle", "Magazine Article"),
            ("newspaperArticle", "Newspaper Article"),
            ("thesis", "Thesis"),
            ("conferencePaper", "Conference Paper"),
            ("patent", "Patent"),
            ("report", "Report"),
            ("webpage", "Web Page"),
            ("attachment", "Attachment"),
            ("note", "Note"),
            ("preprint", "Preprint"),
            ("dataset", "Dataset"),
            ("standard", "Standard"),
            ("podcast", "Podcast"),
            ("presentation", "Presentation"),
            ("videoRecording", "Video Recording"),
            ("audioRecording", "Audio Recording"),
            ("bill", "Bill"),
            ("case", "Case"),
            ("hearing", "Hearing"),
            ("statute", "Statute"),
            ("letter", "Letter"),
            ("manuscript", "Manuscript"),
            ("interview", "Interview"),
            ("film", "Film"),
            ("artwork", "Artwork"),
            ("map", "Map"),
            ("blogPost", "Blog Post"),
            ("forumPost", "Forum Post"),
            ("instantMessage", "Instant Message"),
            ("email", "Email"),
            ("encyclopediaArticle", "Encyclopedia Article"),
            ("dictionaryEntry", "Dictionary Entry"),
        ]
        output = [f"# Zotero Item Types ({len(known_types)})", ""]
        for itype, name in known_types:
            output.append(f"- `{itype}` — {name}")
        output.append("\n*Using cached list. Some types may be missing.*")
        return "\n".join(output)

    except Exception as e:
        await ctx.error(f"Error fetching item types: {str(e)}")
        return f"Error fetching item types: {str(e)}"


@mcp.tool(
    name="zotero_get_item_fields",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    description=(
        "Get the valid fields for a specific Zotero item type. "
        "item_type: the item type identifier (e.g., 'book', 'journalArticle'). "
        "Returns field names and their localized labels. "
        "Useful for understanding what metadata fields are available when "
        "creating or updating items. "
        "Example: zotero_get_item_fields(item_type='book')."
    ),
)
@with_zotero_api_lock
async def get_item_fields(item_type: str = "book", *, ctx: Context) -> str:
    """Get valid fields for a Zotero item type."""
    try:
        await ctx.info(f"Fetching fields for item type: {item_type}")

        import requests as _requests

        if _utils.is_local_mode():
            base_url = "http://localhost:23119/api"
        else:
            base_url = "https://api.zotero.org"

        resp = _requests.get(
            f"{base_url}/itemTypeFields",
            params={"itemType": item_type},
            headers={"Zotero-API-Version": "3"},
            timeout=10,
        )
        if resp.status_code == 200:
            fields = resp.json()
            output = [f"# Fields for Item Type: {item_type}", ""]
            for f in fields:
                output.append(f"- `{f['field']}` — {f.get('localized', f['field'])}")
            return "\n".join(output)
        return f"Error: Could not fetch fields for type '{item_type}' (HTTP {resp.status_code})"

    except Exception as e:
        await ctx.error(f"Error fetching item fields: {str(e)}")
        return f"Error fetching item fields: {str(e)}"


@mcp.tool(
    name="zotero_get_item_template",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    description=(
        "Get an empty JSON template for creating a new Zotero item of a "
        "specific type. Shows all available fields with empty values. "
        "item_type: the item type identifier (e.g., 'book', 'journalArticle'). "
        "Useful for understanding the structure needed when creating items. "
        "Example: zotero_get_item_template(item_type='journalArticle')."
    ),
)
@with_zotero_api_lock
async def get_item_template(item_type: str = "book", *, ctx: Context) -> str:
    """Get an empty item template for a Zotero item type."""
    try:
        await ctx.info(f"Fetching template for item type: {item_type}")

        import requests as _requests

        if _utils.is_local_mode():
            base_url = "http://localhost:23119/api"
        else:
            base_url = "https://api.zotero.org"

        resp = _requests.get(
            f"{base_url}/items/new",
            params={"itemType": item_type},
            headers={"Zotero-API-Version": "3"},
            timeout=10,
        )
        if resp.status_code == 200:
            template = resp.json()
            return f"# Item Template: {item_type}\n\n```json\n{json.dumps(template, indent=2)}\n```"
        return f"Error: Could not fetch template for type '{item_type}' (HTTP {resp.status_code})"

    except Exception as e:
        await ctx.error(f"Error fetching item template: {str(e)}")
        return f"Error fetching item template: {str(e)}"


@mcp.tool(
    name="zotero_get_library_changes",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    description=(
        "Get items, collections, and tags that have changed since a specific "
        "library version. Useful for efficient sync-like operations. "
        "since_version: the library version to check from (use 0 for all items). "
        "limit: max items to return (default 50). "
        "Returns lists of changed item keys with their versions. "
        "Scope: active library only."
    ),
)
@with_zotero_api_lock
async def get_library_changes(
    since_version: int | str = 0,
    limit: int | str = 50,
    *,
    ctx: Context,
) -> str:
    """Get library changes since a specific version."""
    try:
        since_version = int(since_version)
        limit = _helpers._normalize_limit(limit, default=50)
        await ctx.info(f"Fetching changes since version {since_version}")
        zot = await _client.run_zotero_call(_client.get_zotero_client, operation="get_zotero_client")

        # Get changed items
        items = await _client.run_zotero_call(
            zot.items,
            since=since_version,
            limit=limit,
            sort="dateModified",
            direction="desc",
            operation=f"zot.items(changes since={since_version})",
        )

        if not items:
            return f"No changes since version {since_version}."

        output = [f"# Library Changes Since Version {since_version}", f"**Changed items:** {len(items)}", ""]

        for item in items:
            data = item.get("data", {})
            key = item.get("key", "")
            title = data.get("title", "Untitled")
            item_type = data.get("itemType", "")
            version = item.get("version", 0)
            date_modified = data.get("dateModified", "")
            output.append(f"- `{key}` v{version} — **{title}** [{item_type}] ({date_modified})")

        return "\n".join(output)

    except Exception as e:
        await ctx.error(f"Error fetching library changes: {str(e)}")
        return f"Error fetching library changes: {str(e)}"


@mcp.tool(
    name="zotero_get_publications",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    description=(
        "Get items in your Zotero 'My Publications' collection. "
        "These are items you've chosen to showcase on your Zotero profile. "
        "limit: max items to return (default 25). "
        "Scope: active library only."
    ),
)
@with_zotero_api_lock
async def get_publications(limit: int | str = 25, *, ctx: Context) -> str:
    """Get items from My Publications."""
    try:
        await ctx.info("Fetching My Publications items")
        zot = await _client.run_zotero_call(_client.get_zotero_client, operation="get_zotero_client")
        limit = _helpers._normalize_limit(limit, default=25)

        items = await _client.run_zotero_call(
            zot.publications,
            limit=limit,
            sort="dateAdded",
            direction="desc",
            operation="zot.publications",
        )

        if not items:
            return "No items found in My Publications."

        output = [f"# My Publications ({len(items)} items)", ""]
        for i, item in enumerate(items, 1):
            data = item.get("data", {})
            title = data.get("title", "Untitled")
            creators = _utils.format_creators(data.get("creators", []), max_authors=3)
            date = data.get("date", "")[:4] if data.get("date") else ""
            key = item.get("key", "")
            item_type = data.get("itemType", "")
            output.append(f"{i}. **{title}** ({date})")
            output.append(f"   Key: `{key}` | Type: {item_type} | Authors: {creators}")

        return "\n".join(output)

    except Exception as e:
        error_msg = str(e)
        suggestion = ""
        if "403" in error_msg or "forbidden" in error_msg.lower():
            suggestion = " My Publications may not be accessible with the current API key."
        elif "connection" in error_msg.lower():
            suggestion = " Check if Zotero is running."
        await ctx.error(f"Error fetching publications: {error_msg}")
        return f"Error fetching publications: {error_msg}{suggestion}"


@mcp.tool(
    name="zotero_get_collection_tags",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    description=(
        "Get tags used within a specific collection. "
        "collection_key: the 8-character collection key. "
        "Returns tags with their usage frequency within the collection. "
        "Useful for understanding what topics a collection covers. "
        "Scope: active library only."
    ),
)
@with_zotero_api_lock
async def get_collection_tags(collection_key: str, *, ctx: Context) -> str:
    """Get tags used within a specific collection."""
    try:
        key_err = _helpers.validate_collection_key(collection_key)
        if key_err:
            return f"Error: {key_err}"

        await ctx.info(f"Fetching tags for collection {collection_key}")
        zot = await _client.run_zotero_call(_client.get_zotero_client, operation="get_zotero_client")

        # Get collection name
        try:
            col = await _client.run_zotero_call(
                zot.collection, collection_key, operation=f"zot.collection({collection_key})"
            )
            col_name = col["data"].get("name", "Unnamed Collection")
        except Exception:
            return f"Collection not found: {collection_key}"

        # Get tags for this collection
        tags = await _client.run_zotero_call(
            zot.collection_tags, collection_key, operation=f"zot.collection_tags({collection_key})"
        )

        if not tags:
            return f"No tags found in collection '{col_name}'."

        # Format tags
        from collections import Counter

        tag_counter: Counter[str] = Counter()
        for tag in tags:
            tag_name = tag.get("tag", "") if isinstance(tag, dict) else str(tag)
            if tag_name:
                tag_counter[tag_name] += 1

        output = [f"# Tags in Collection: {col_name}", f"**Total unique tags:** {len(tag_counter)}", ""]
        for tag, count in tag_counter.most_common(50):
            output.append(f"- `{tag}`: {count}")

        return "\n".join(output)

    except Exception as e:
        await ctx.error(f"Error fetching collection tags: {str(e)}")
        return f"Error fetching collection tags: {str(e)}"


@mcp.tool(
    name="zotero_get_item_tags",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    description=(
        "Get tags for a specific item. "
        "item_key: the 8-character item key. "
        "Returns the list of tags assigned to the item. "
        "Scope: active library only."
    ),
)
@with_zotero_api_lock
async def get_item_tags(item_key: str, *, ctx: Context) -> str:
    """Get tags for a specific item."""
    try:
        key_err = _helpers.validate_item_key(item_key)
        if key_err:
            return f"Error: {key_err}"

        await ctx.info(f"Fetching tags for item {item_key}")
        zot = await _client.run_zotero_call(_client.get_zotero_client, operation="get_zotero_client")

        item = await _client.run_zotero_call(zot.item, item_key, operation=f"zot.item({item_key})")
        if not item:
            return f"No item found with key: {item_key}"

        data = item.get("data", {})
        title = data.get("title", "Untitled")
        tags = data.get("tags", [])

        if not tags:
            return f"No tags for '{title}' ({item_key})."

        output = [f"# Tags for: {title}", f"**Key:** `{item_key}`", ""]
        for tag in tags:
            tag_name = tag.get("tag", "")
            tag_type = tag.get("type", "")
            type_str = " (automatic)" if tag_type == 1 else ""
            output.append(f"- `{tag_name}`{type_str}")

        return "\n".join(output)

    except Exception as e:
        await ctx.error(f"Error fetching item tags: {str(e)}")
        return f"Error fetching item tags: {str(e)}"
