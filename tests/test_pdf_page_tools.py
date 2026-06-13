import os
import sys
import types
from pathlib import Path

import pytest
from conftest import DummyContext
from mcp.types import ImageContent, TextContent

from zotero_mcp.tools import retrieval


def test_parse_pdf_page_spec_single_range_mixed_and_duplicates():
    assert retrieval._parse_pdf_page_spec("1", 10, max_pages=25) == [0]
    assert retrieval._parse_pdf_page_spec("1-3", 10, max_pages=25) == [0, 1, 2]
    assert retrieval._parse_pdf_page_spec("1,3,7-9,3", 10, max_pages=25) == [0, 2, 6, 7, 8]


@pytest.mark.parametrize("spec", ["", "0", "-1", "3-1", "1--3", "abc", "1,"])
def test_parse_pdf_page_spec_rejects_bad_syntax(spec):
    with pytest.raises(ValueError):
        retrieval._parse_pdf_page_spec(spec, 10, max_pages=25)


def test_parse_pdf_page_spec_rejects_out_of_bounds_and_caps():
    with pytest.raises(ValueError, match="outside PDF page count"):
        retrieval._parse_pdf_page_spec("11", 10, max_pages=25)
    with pytest.raises(ValueError, match="Too many pages"):
        retrieval._parse_pdf_page_spec("1-6", 10, max_pages=5)


def test_render_cache_cleanup_removes_stale_files(tmp_path):
    root = tmp_path / "rendered_pages"
    old_dir = root / "ATTACH01"
    old_dir.mkdir(parents=True)
    old_file = old_dir / "page-1-100.png"
    old_file.write_bytes(b"old")
    new_file = old_dir / "page-2-100.png"
    new_file.write_bytes(b"new")
    old_mtime = 1
    os.utime(old_file, (old_mtime, old_mtime))

    retrieval._cleanup_render_cache(root, max_age_days=1)

    assert not old_file.exists()
    assert new_file.exists()


def test_render_cache_root_uses_platform_cache_env(monkeypatch, tmp_path):
    if os.name == "nt":
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        assert retrieval._render_cache_root() == tmp_path / "zotero-mcp" / "Cache" / "rendered_pages"
    else:
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        assert retrieval._render_cache_root() == tmp_path / "zotero-mcp" / "rendered_pages"


class FakePage:
    def __init__(self, text="page text"):
        self.text = text

    def get_text(self):
        return self.text

    def get_pixmap(self, matrix=None):  # noqa: ARG002
        return FakePixmap()


class FakePixmap:
    def save(self, path):
        Path(path).write_bytes(b"fake-image")


class FakeDoc:
    def __init__(self, page_count=3):
        self.page_count = page_count

    def __len__(self):
        return self.page_count

    def __getitem__(self, index):
        return FakePage(f"text page {index + 1}")

    def close(self):
        pass


def _patch_fitz(monkeypatch, page_count=3):
    fake_fitz = types.ModuleType("fitz")
    fake_fitz.open = lambda *args, **kwargs: FakeDoc(page_count)  # noqa: ARG005
    fake_fitz.Matrix = lambda *args, **kwargs: (args, kwargs)
    monkeypatch.setitem(sys.modules, "fitz", fake_fitz)


def _resolved_pdf(tmp_path, key="ATTACH01"):
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"%PDF fake")
    return retrieval._ResolvedPdfAttachment(
        parent_key="PARENT01",
        attachment_key=key,
        filename="paper.pdf",
        content_type="application/pdf",
        pdf_path=pdf_path,
    )


async def _async_resolved_pdf(tmp_path, key="ATTACH01"):
    return _resolved_pdf(tmp_path, key=key)


@pytest.mark.asyncio
async def test_extract_pdf_pages_markdown_passes_ocr_false(monkeypatch, tmp_path):
    calls = []

    def fake_to_markdown(path, **kwargs):
        calls.append((path, kwargs))
        return "markdown body"

    monkeypatch.setitem(sys.modules, "pymupdf4llm", types.SimpleNamespace(to_markdown=fake_to_markdown))
    _patch_fitz(monkeypatch, page_count=10)
    monkeypatch.setattr(retrieval, "_resolve_pdf_attachment", lambda item_key, ctx: _async_resolved_pdf(tmp_path))

    result = await retrieval.extract_pdf_pages("PARENT01", pages="1,3", ctx=DummyContext())

    assert calls[0][1] == {"pages": [0, 2], "use_ocr": False}
    assert "**Backend:** pymupdf4llm" in result
    assert "**OCR:** disabled" in result
    assert "**Extracted Pages:** 1, 3" in result
    assert "**Attachment Key:** ATTACH01" in result
    assert "zotero_render_pdf_pages" in result
    assert "use_ocr=true" in result
    assert "markdown body" in result


@pytest.mark.asyncio
async def test_extract_pdf_pages_ocr_true_is_capped(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "pymupdf4llm", types.SimpleNamespace(to_markdown=lambda *a, **k: "body"))
    _patch_fitz(monkeypatch, page_count=10)
    monkeypatch.setattr(retrieval, "_resolve_pdf_attachment", lambda item_key, ctx: _async_resolved_pdf(tmp_path))

    result = await retrieval.extract_pdf_pages("ATTACH01", pages="1-6", use_ocr=True, ctx=DummyContext())

    assert "Too many pages" in result
    assert "Maximum is 5" in result


@pytest.mark.asyncio
async def test_extract_pdf_pages_text_mode(monkeypatch, tmp_path):
    _patch_fitz(monkeypatch, page_count=3)
    monkeypatch.setattr(retrieval, "_resolve_pdf_attachment", lambda item_key, ctx: _async_resolved_pdf(tmp_path))

    result = await retrieval.extract_pdf_pages("ATTACH01", pages="1-2", output_format="text", ctx=DummyContext())

    assert "**Backend:** PyMuPDF get_text" in result
    assert "text page 1" in result
    assert "text page 2" in result


@pytest.mark.asyncio
async def test_render_pdf_pages_returns_paths_and_image_blocks(monkeypatch, tmp_path):
    _patch_fitz(monkeypatch, page_count=3)
    monkeypatch.setattr(retrieval, "_resolve_pdf_attachment", lambda item_key, ctx: _async_resolved_pdf(tmp_path))

    result = await retrieval.render_pdf_pages("PARENT01", pages="1,2", dpi=100, ctx=DummyContext())

    assert isinstance(result, list)
    assert isinstance(result[0], TextContent)
    assert "# PDF Page Render" in result[0].text
    assert "**Attachment Key:** ATTACH01" in result[0].text
    assert "page-1-100.png" in result[0].text
    assert "zotero_extract_pdf_pages" in result[0].text
    assert len(result) == 3
    assert all(isinstance(block, ImageContent) for block in result[1:])
    assert all(block.mimeType == "image/png" for block in result[1:])


@pytest.mark.asyncio
async def test_render_pdf_pages_paths_mode_and_validation(monkeypatch, tmp_path):
    _patch_fitz(monkeypatch, page_count=20)
    monkeypatch.setattr(retrieval, "_resolve_pdf_attachment", lambda item_key, ctx: _async_resolved_pdf(tmp_path))

    result = await retrieval.render_pdf_pages("PARENT01", pages="1", return_mode="paths", ctx=DummyContext())
    assert isinstance(result, list)
    assert len(result) == 1

    assert "dpi" in await retrieval.render_pdf_pages("PARENT01", dpi=10, ctx=DummyContext())
    assert "image_format" in await retrieval.render_pdf_pages("PARENT01", image_format="gif", ctx=DummyContext())
    assert "return_mode" in await retrieval.render_pdf_pages("PARENT01", return_mode="bad", ctx=DummyContext())
    assert "Too many pages" in await retrieval.render_pdf_pages("PARENT01", pages="1-11", ctx=DummyContext())


@pytest.mark.asyncio
async def test_resolve_pdf_attachment_rejects_non_pdf_attachment(monkeypatch):
    class FakeZot:
        def item(self, key):
            return {"key": key, "data": {"itemType": "attachment", "contentType": "text/html"}}

    monkeypatch.setattr(retrieval._utils, "is_local_mode", lambda: False)
    monkeypatch.setattr(retrieval._client, "get_zotero_client", lambda: FakeZot())

    with pytest.raises(ValueError, match="not a PDF attachment"):
        await retrieval._resolve_pdf_attachment("ATTACH01", DummyContext())
