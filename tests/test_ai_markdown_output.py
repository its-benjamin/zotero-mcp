from pathlib import Path

import pytest
from conftest import DummyContext, FakeZotero

from zotero_mcp.tools import retrieval
from zotero_mcp.utils import format_item_result


class ChildFakeZotero(FakeZotero):
    def item(self, key):
        return {"key": key, "data": {"title": "Parent Paper", "itemType": "journalArticle"}}

    def items(self, **kwargs):
        keys = (kwargs.get("itemKey") or "").split(",")
        return [{"key": key, "data": {"title": f"Parent {key}", "itemType": "journalArticle"}} for key in keys if key]

    def children(self, key, **kwargs):
        return [
            {
                "key": "ATTACH01",
                "data": {
                    "itemType": "attachment",
                    "title": "Paper PDF",
                    "filename": "paper.pdf",
                    "contentType": "application/pdf",
                    "linkMode": "imported_file",
                },
            },
            {
                "key": "NOTE0001",
                "data": {
                    "itemType": "note",
                    "title": "Reading note",
                    "note": "<p>Important finding<br/>Second line</p>",
                },
            },
        ]


@pytest.mark.asyncio
async def test_get_item_children_uses_ai_readable_field_labels(monkeypatch):
    monkeypatch.setattr(retrieval._client, "get_zotero_client", lambda: ChildFakeZotero())

    result = await retrieval.get_item_children("PARENT01", ctx=DummyContext())

    assert "# Child Items" in result
    assert "**Parent Key:** PARENT01" in result
    assert "**Parent Title:** Parent Paper" in result
    assert "**Child Key:** ATTACH01" in result
    assert "**Child Type:** attachment" in result
    assert "**Filename:** paper.pdf" in result
    assert "**Content Type:** application/pdf" in result
    assert "**Link Mode:** imported_file" in result
    assert 'zotero_get_pdf_outline(item_key="ATTACH01")' in result
    assert 'zotero_get_attachment_path(item_key="PARENT01")' in result
    assert "C:\\" not in result
    assert "**Child Key:** NOTE0001" in result
    assert "**Note Text:**" in result
    assert "Important finding" in result


@pytest.mark.asyncio
async def test_get_items_children_uses_same_labels_for_batch(monkeypatch):
    monkeypatch.setattr(retrieval._client, "get_zotero_client", lambda: ChildFakeZotero())

    result = await retrieval.get_items_children(["PARENT01"], ctx=DummyContext())

    assert "# Children for 1 Items" in result
    assert "**Parent Key:** PARENT01" in result
    assert "**Child Key:** ATTACH01" in result
    assert "**Child Type:** attachment" in result
    assert 'zotero_get_pdf_outline(item_key="ATTACH01")' in result
    assert 'zotero_get_attachment_path(item_key="PARENT01")' in result
    assert "[ATTACH01] Attachment" not in result

def test_metadata_includes_ai_next_steps():
    item = {
        "key": "META0001",
        "data": {"key": "META0001", "title": "Metadata Paper", "itemType": "journalArticle"},
    }

    result = retrieval._client.format_item_metadata(item, include_abstract=False)

    assert "## Next Steps" in result
    assert 'zotero_get_item_fulltext(item_key="META0001")' in result
    assert 'zotero_get_item_children(item_key="META0001")' in result
    assert 'zotero_get_attachment_path(item_key="META0001")' in result

def test_list_formatter_includes_stable_labels_and_next_line():
    item = {
        "key": "LIST0001",
        "data": {
            "key": "LIST0001",
            "title": "List Paper",
            "itemType": "journalArticle",
            "date": "2025",
            "creators": [{"firstName": "Ada", "lastName": "Lovelace"}],
            "DOI": "10.1234/list",
            "url": "https://example.org/list",
            "tags": [{"tag": "ai"}],
        },
    }

    result = "\n".join(format_item_result(item))

    assert "**Item Key:** LIST0001" in result
    assert "**Title:** List Paper" in result
    assert "**Creators:** Lovelace, Ada" in result
    assert "**Date:** 2025" in result
    assert "**DOI:** 10.1234/list" in result
    assert "**URL:** https://example.org/list" in result
    assert "**Tags:** `ai`" in result
    assert '**Next:** call `zotero_get_item_metadata(item_key="LIST0001")`' in result
    assert 'children: `zotero_get_item_children(item_key="LIST0001")`' in result

@pytest.mark.asyncio
async def test_get_attachment_path_uses_labeled_ai_output(monkeypatch):
    class FakeReader:
        def __init__(self, db_path=None):
            self.db_path = db_path

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            return None

        def get_attachment_paths(self, item_key):
            assert item_key == "PARENT01"
            return [
                {
                    "key": "ATTACH01",
                    "content_type": "application/pdf",
                    "zotero_path": "storage:paper.pdf",
                    "resolved_path": Path("C:/Zotero/storage/ATTACH01/paper.pdf"),
                    "exists": True,
                }
            ]

    import zotero_mcp.local_db as local_db

    monkeypatch.setattr(retrieval._utils, "is_local_mode", lambda: True)
    monkeypatch.setattr(retrieval._helpers, "_load_zotero_mcp_config", lambda: {})
    monkeypatch.setattr(local_db, "LocalZoteroReader", FakeReader)

    result = await retrieval.get_attachment_path("PARENT01", ctx=DummyContext())

    assert "# Attachment Paths" in result
    assert "**Attachment Key:** ATTACH01" in result
    assert "**Content Type:** application/pdf" in result
    assert "**Filename:** paper.pdf" in result
    assert "**Zotero Path:** `storage:paper.pdf`" in result
    assert "**Local Path:**" in result
    assert "**Exists:** yes" in result
    assert 'zotero_get_pdf_outline(item_key="ATTACH01")' in result

@pytest.mark.asyncio
async def test_get_item_fulltext_includes_source_block(monkeypatch):
    class FulltextFakeZotero(ChildFakeZotero):
        def fulltext_item(self, key):
            assert key == "ATTACH01"
            return {"content": "Extracted body text"}

    monkeypatch.setattr(retrieval._client, "get_zotero_client", lambda: FulltextFakeZotero())
    monkeypatch.setattr(retrieval._utils, "is_local_mode", lambda: False)

    result = await retrieval.get_item_fulltext("PARENT01", ctx=DummyContext())

    assert "## Full Text Source" in result
    assert "**Item Key:** PARENT01" in result
    assert "**Attachment Key:** ATTACH01" in result
    assert "## Full Text" in result
    assert "Extracted body text" in result

@pytest.mark.asyncio
async def test_get_item_fulltext_local_source_block_guides_ocr_and_vision(monkeypatch):
    class LocalItem:
        item_id = 1

    class FakeReader:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            return None

        def get_item_by_key(self, item_key):
            assert item_key == "PARENT01"
            return LocalItem()

        def extract_fulltext_for_item(self, item_id):
            assert item_id == 1
            return ("Local markdown text", "pdf")

        def get_attachment_paths(self, item_key):
            return [{"key": "ATTACH01", "exists": True, "content_type": "application/pdf"}]

    import zotero_mcp.local_db as local_db

    monkeypatch.setattr(retrieval._client, "get_zotero_client", lambda: ChildFakeZotero())
    monkeypatch.setattr(retrieval._utils, "is_local_mode", lambda: True)
    monkeypatch.setattr(
        retrieval._helpers,
        "_load_zotero_mcp_config",
        lambda: {
            "semantic_search": {
                "extraction": {
                    "pdf_backend": "pymupdf4llm",
                    "fulltext_display_max_pages": 5,
                    "pdf_use_ocr": False,
                }
            }
        },
    )
    monkeypatch.setattr(local_db, "LocalZoteroReader", FakeReader)

    result = await retrieval.get_item_fulltext("PARENT01", ctx=DummyContext())

    assert "**PDF Backend:** pymupdf4llm" in result
    assert "**OCR:** disabled" in result
    assert "**Pages Extracted:** 5" in result
    assert "pdf_use_ocr=true" in result
    assert "vision model" in result
