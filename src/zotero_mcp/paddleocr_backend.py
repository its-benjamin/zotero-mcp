"""PaddleOCR PP-OCRv6 backend for PDF text extraction.

Renders PDF pages to images via PyMuPDF (fitz) and runs PaddleOCR on each page.
This provides higher-quality OCR than Tesseract-based backends, especially for
multilingual documents, scanned PDFs, and complex layouts.
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

_ocr_engine = None
_ocr_lock = threading.Lock()
_ocr_lang: str | None = None


def _get_ocr_engine(lang: str | None = None):
    """Return a lazily-initialized PaddleOCR engine singleton."""
    global _ocr_engine, _ocr_lang
    if _ocr_engine is not None and _ocr_lang == lang:
        return _ocr_engine
    with _ocr_lock:
        if _ocr_engine is not None and _ocr_lang == lang:
            return _ocr_engine
        from paddleocr import PaddleOCR

        kwargs: dict = {
            "use_doc_orientation_classify": False,
            "use_doc_unwarping": False,
            "use_textline_orientation": False,
            "text_detection_model_name": "PP-OCRv6_small_det",
            "text_recognition_model_name": "PP-OCRv6_small_rec",
        }
        if lang:
            kwargs["lang"] = lang
        _ocr_engine = PaddleOCR(**kwargs)
        _ocr_lang = lang
        return _ocr_engine


def _extract_text_from_result(result) -> str:
    """Extract text from PaddleOCR predict() result.

    Handles both v3 pipeline output and legacy v2 output formats.
    """
    if not result:
        return ""

    lines: list[str] = []

    # v3 pipeline output: list of result objects with rec_texts
    for item in result:
        if hasattr(item, "rec_texts"):
            texts = item.rec_texts
            if isinstance(texts, (list, tuple)):
                lines.extend(str(t) for t in texts if t)
            continue
        if hasattr(item, "text"):
            lines.append(str(item.text))
            continue
        # Legacy v2 format: list of [bbox, (text, confidence)]
        if isinstance(item, (list, tuple)):
            for line_info in item:
                if isinstance(line_info, (list, tuple)) and len(line_info) >= 2:
                    text_part = line_info[1]
                    if isinstance(text_part, (list, tuple)):
                        lines.append(str(text_part[0]))
                    else:
                        lines.append(str(text_part))

    return "\n".join(lines)


def _ocr_image(engine, image_path: str) -> str:
    """Run PaddleOCR on a single image and return extracted text."""
    try:
        result = engine.predict(image_path)
        return _extract_text_from_result(result)
    except AttributeError:
        # Fallback to v2 API if predict() not available
        result = engine.ocr(image_path, cls=True)
        return _extract_text_from_result(result)


def extract_text_from_pdf_paddleocr(
    file_path: Path,
    maxpages: int,
    timeout: int = 30,
    lang: str | None = None,
) -> str:
    """Extract text from a PDF by rendering pages to images and running PaddleOCR.

    Args:
        file_path: Path to the PDF file.
        maxpages: Maximum number of pages to process.
        timeout: Timeout in seconds (unused; PaddleOCR runs in-process).
        lang: PaddleOCR language code (e.g. 'en', 'ch', 'japan'). None for auto-detect.

    Returns:
        Extracted text from the PDF, or empty string on failure.
    """
    try:
        import fitz

        engine = _get_ocr_engine(lang=lang)
        doc = fitz.open(str(file_path))
        try:
            total = len(doc)
            pages_to_process = min(maxpages, total) if maxpages > 0 else total
            page_texts: list[str] = []
            for i in range(pages_to_process):
                page = doc[i]
                pix = page.get_pixmap(dpi=300)
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                    tmp.write(pix.tobytes("png"))
                    tmp_path = tmp.name
                try:
                    text = _ocr_image(engine, tmp_path)
                    if text:
                        page_texts.append(text)
                finally:
                    os.unlink(tmp_path)
            return "\n\n".join(page_texts)
        finally:
            doc.close()
    except ImportError:
        logger.warning(
            "PaddleOCR not installed. Install with: pip install paddlepaddle paddleocr"
        )
        return ""
    except Exception as e:
        logger.warning(f"PaddleOCR PDF extraction failed: {file_path.name}: {e}")
        return ""


def extract_pages_paddleocr(
    pdf_path: Path,
    page_indexes: list[int],
    lang: str | None = None,
) -> str:
    """Extract text from specific PDF pages using PaddleOCR.

    Args:
        pdf_path: Path to the PDF file.
        page_indexes: 0-indexed page numbers to extract.
        lang: PaddleOCR language code. None for auto-detect.

    Returns:
        Extracted text joined by double newlines.
    """
    try:
        import fitz

        engine = _get_ocr_engine(lang=lang)
        doc = fitz.open(str(pdf_path))
        try:
            page_texts: list[str] = []
            for idx in page_indexes:
                if idx >= len(doc):
                    continue
                page = doc[idx]
                pix = page.get_pixmap(dpi=300)
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                    tmp.write(pix.tobytes("png"))
                    tmp_path = tmp.name
                try:
                    text = _ocr_image(engine, tmp_path)
                    if text:
                        page_texts.append(text)
                finally:
                    os.unlink(tmp_path)
            return "\n\n".join(page_texts)
        finally:
            doc.close()
    except ImportError:
        logger.warning(
            "PaddleOCR not installed. Install with: pip install paddlepaddle paddleocr"
        )
        return ""
    except Exception as e:
        logger.warning(f"PaddleOCR page extraction failed: {pdf_path.name}: {e}")
        return ""
