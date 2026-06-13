"""Tests for zotero_create_annotation, zotero_update_note, and zotero_delete_note."""

import os
import tempfile
from unittest.mock import Mock, patch

import pytest

from zotero_mcp.tools.annotations import (
    create_annotation,
    create_note,
    delete_note,
    update_note,
)


class MockContext:
    """Mock MCP context for testing."""

    def __init__(self):
        self.info_calls = []
        self.error_calls = []
        self.warning_calls = []

    async def info(self, message):
        self.info_calls.append(message)

    async def error(self, message):
        self.error_calls.append(message)

    async def warning(self, message):
        self.warning_calls.append(message)


@pytest.fixture
def mock_ctx():
    return MockContext()


@pytest.fixture
def mock_zotero_client():
    """Create a mock Zotero client."""
    client = Mock()
    client.item.return_value = {
        "key": "TEST1234",
        "version": 1,
        "data": {
            "title": "Test Item",
            "itemType": "journalArticle",
            "tags": []
        }
    }
    client.create_items.return_value = {"success": {"0": "NEWKEY12"}}
    client.update_item.return_value = {"success": {"0": "TEST1234"}}
    return client


@pytest.fixture
def mock_note_item():
    """Create a mock note item."""
    return {
        "key": "NOTE1234",
        "version": 1,
        "data": {
            "itemType": "note",
            "note": "<p>Original note content</p>",
            "parentItem": "PARENT12",
            "tags": []
        }
    }


@pytest.fixture
def mock_pdf_attachment():
    """Create a mock PDF attachment item."""
    return {
        "key": "PDF12345",
        "version": 1,
        "data": {
            "itemType": "attachment",
            "contentType": "application/pdf",
            "filename": "test.pdf",
            "parentItem": "PARENT12"
        }
    }


# ============================================================================
# Tests for create_note
# ============================================================================


@pytest.mark.asyncio
async def test_create_note_success(mock_ctx, mock_zotero_client):
    """Test successful note creation."""
    with patch('zotero_mcp.tools.annotations._client.get_zotero_client', return_value=mock_zotero_client):
        with patch('zotero_mcp.tools.annotations._utils.is_local_mode', return_value=False):
            result = await create_note(
                item_key="TEST1234",
                note_title="Test Note",
                note_text="This is the note content",
                ctx=mock_ctx
            )

    assert "Successfully created note" in result
    assert "NEWKEY12" in result
    mock_zotero_client.create_items.assert_called_once()


@pytest.mark.asyncio
async def test_create_note_item_not_found(mock_ctx, mock_zotero_client):
    """Test error when parent item doesn't exist."""
    mock_zotero_client.item.side_effect = Exception("Not found")

    with patch('zotero_mcp.tools.annotations._client.get_zotero_client', return_value=mock_zotero_client):
        result = await create_note(
            item_key="INVALID1",
            note_title="Test Note",
            note_text="Content",
            ctx=mock_ctx
        )

    assert "Error" in result
    assert "No item found" in result


@pytest.mark.asyncio
async def test_create_note_with_tags(mock_ctx, mock_zotero_client):
    """Test note creation with tags."""
    with patch('zotero_mcp.tools.annotations._client.get_zotero_client', return_value=mock_zotero_client):
        with patch('zotero_mcp.tools.annotations._utils.is_local_mode', return_value=False):
            result = await create_note(
                item_key="TEST1234",
                note_title="Tagged Note",
                note_text="Content with tags",
                tags=["tag1", "tag2"],
                ctx=mock_ctx
            )

    assert "Successfully created note" in result
    # Verify tags were passed to create_items
    call_args = mock_zotero_client.create_items.call_args[0][0][0]
    assert "tags" in call_args


@pytest.mark.asyncio
async def test_create_note_html_formatting(mock_ctx, mock_zotero_client):
    """Test that note text is properly HTML-formatted."""
    with patch('zotero_mcp.tools.annotations._client.get_zotero_client', return_value=mock_zotero_client):
        with patch('zotero_mcp.tools.annotations._utils.is_local_mode', return_value=False):
            result = await create_note(
                item_key="TEST1234",
                note_title="HTML Note",
                note_text="Line 1\n\nLine 2",
                ctx=mock_ctx
            )

    assert "Successfully created note" in result
    # Verify the note content was HTML-formatted
    call_args = mock_zotero_client.create_items.call_args[0][0][0]
    assert "<h1>" in call_args["note"]
    assert "<p>" in call_args["note"]


@pytest.mark.asyncio
async def test_create_note_existing_html(mock_ctx, mock_zotero_client):
    """Test that existing HTML is preserved."""
    with patch('zotero_mcp.tools.annotations._client.get_zotero_client', return_value=mock_zotero_client):
        with patch('zotero_mcp.tools.annotations._utils.is_local_mode', return_value=False):
            result = await create_note(
                item_key="TEST1234",
                note_title="HTML Note",
                note_text="<p>Already has HTML</p>",
                ctx=mock_ctx
            )

    assert "Successfully created note" in result
    call_args = mock_zotero_client.create_items.call_args[0][0][0]
    assert "<p>Already has HTML</p>" in call_args["note"]


# ============================================================================
# Tests for update_note
# ============================================================================


@pytest.mark.asyncio
async def test_update_note_success(mock_ctx, mock_zotero_client, mock_note_item):
    """Test successful note update."""
    mock_zotero_client.item.return_value = mock_note_item

    with patch('zotero_mcp.tools.annotations._get_note_write_client', return_value=(mock_zotero_client, None)):
        result = await update_note(
            item_key="NOTE1234",
            note_text="<p>Updated content</p>",
            ctx=mock_ctx
        )

    assert "Successfully updated note" in result
    mock_zotero_client.update_item.assert_called_once()


@pytest.mark.asyncio
async def test_update_note_append_mode(mock_ctx, mock_zotero_client, mock_note_item):
    """Test note update with append=True."""
    mock_zotero_client.item.return_value = mock_note_item

    with patch('zotero_mcp.tools.annotations._get_note_write_client', return_value=(mock_zotero_client, None)):
        result = await update_note(
            item_key="NOTE1234",
            note_text="<p>Appended content</p>",
            append=True,
            ctx=mock_ctx
        )

    assert "Successfully updated note" in result
    # Verify content was appended
    call_args = mock_zotero_client.update_item.call_args[0][0]
    assert "Original note content" in call_args["data"]["note"]
    assert "Appended content" in call_args["data"]["note"]


@pytest.mark.asyncio
async def test_update_note_replace_mode(mock_ctx, mock_zotero_client, mock_note_item):
    """Test note update with append=False (default)."""
    mock_zotero_client.item.return_value = mock_note_item

    with patch('zotero_mcp.tools.annotations._get_note_write_client', return_value=(mock_zotero_client, None)):
        result = await update_note(
            item_key="NOTE1234",
            note_text="<p>Replacement content</p>",
            append=False,
            ctx=mock_ctx
        )

    assert "Successfully updated note" in result
    call_args = mock_zotero_client.update_item.call_args[0][0]
    assert "Original note content" not in call_args["data"]["note"]
    assert "Replacement content" in call_args["data"]["note"]


@pytest.mark.asyncio
async def test_update_note_not_found(mock_ctx, mock_zotero_client):
    """Test error when note doesn't exist."""
    mock_zotero_client.item.side_effect = Exception("Not found")

    with patch('zotero_mcp.tools.annotations._get_note_write_client', return_value=(mock_zotero_client, None)):
        result = await update_note(
            item_key="INVALID1",
            note_text="<p>Content</p>",
            ctx=mock_ctx
        )

    assert "Error" in result
    assert "No item found" in result


@pytest.mark.asyncio
async def test_update_note_not_a_note(mock_ctx, mock_zotero_client):
    """Test error when item is not a note."""
    mock_zotero_client.item.return_value = {
        "key": "ARTICLE1",
        "version": 1,
        "data": {
            "itemType": "journalArticle",
            "title": "Not a note"
        }
    }

    with patch('zotero_mcp.tools.annotations._get_note_write_client', return_value=(mock_zotero_client, None)):
        result = await update_note(
            item_key="ARTICLE1",
            note_text="<p>Content</p>",
            ctx=mock_ctx
        )

    assert "Error" in result
    assert "not a note" in result


@pytest.mark.asyncio
async def test_update_note_no_web_client(mock_ctx):
    """Test error when web client is not available."""
    with patch('zotero_mcp.tools.annotations._get_note_write_client', return_value=(None, "Error: Web API required")):
        result = await update_note(
            item_key="NOTE1234",
            note_text="<p>Content</p>",
            ctx=mock_ctx
        )

    assert "Error" in result
    assert "Web API required" in result


# ============================================================================
# Tests for delete_note
# ============================================================================


@pytest.mark.asyncio
async def test_delete_note_success(mock_ctx, mock_zotero_client, mock_note_item):
    """Test successful note deletion (move to trash)."""
    mock_zotero_client.item.return_value = mock_note_item

    # Mock the HTTP PATCH response
    mock_response = Mock()
    mock_response.status_code = 204
    mock_zotero_client.client.patch.return_value = mock_response

    with patch('zotero_mcp.tools.annotations._get_note_write_client', return_value=(mock_zotero_client, None)):
        with patch('zotero_mcp.tools.annotations.rate_limit'):
            with patch('pyzotero.zotero.build_url', return_value="http://test.com"):
                result = await delete_note(
                    item_key="NOTE1234",
                    ctx=mock_ctx
                )

    assert "Successfully trashed note" in result
    assert "recoverable" in result


@pytest.mark.asyncio
async def test_delete_note_not_found(mock_ctx, mock_zotero_client):
    """Test error when note doesn't exist."""
    mock_zotero_client.item.side_effect = Exception("Not found")

    with patch('zotero_mcp.tools.annotations._get_note_write_client', return_value=(mock_zotero_client, None)):
        result = await delete_note(
            item_key="INVALID1",
            ctx=mock_ctx
        )

    assert "Error" in result
    assert "No item found" in result


@pytest.mark.asyncio
async def test_delete_note_not_a_note(mock_ctx, mock_zotero_client):
    """Test error when item is not a note."""
    mock_zotero_client.item.return_value = {
        "key": "ARTICLE1",
        "version": 1,
        "data": {
            "itemType": "journalArticle",
            "title": "Not a note"
        }
    }

    with patch('zotero_mcp.tools.annotations._get_note_write_client', return_value=(mock_zotero_client, None)):
        result = await delete_note(
            item_key="ARTICLE1",
            ctx=mock_ctx
        )

    assert "Error" in result
    assert "not a note" in result


@pytest.mark.asyncio
async def test_delete_note_no_web_client(mock_ctx):
    """Test error when web client is not available."""
    with patch('zotero_mcp.tools.annotations._get_note_write_client', return_value=(None, "Error: Web API required")):
        result = await delete_note(
            item_key="NOTE1234",
            ctx=mock_ctx
        )

    assert "Error" in result
    assert "Web API required" in result


@pytest.mark.asyncio
async def test_delete_note_http_error(mock_ctx, mock_zotero_client, mock_note_item):
    """Test error handling when HTTP request fails."""
    mock_zotero_client.item.return_value = mock_note_item

    # Mock failed HTTP response
    mock_response = Mock()
    mock_response.status_code = 500
    mock_response.text = "Internal Server Error"
    mock_zotero_client.client.patch.return_value = mock_response

    with patch('zotero_mcp.tools.annotations._get_note_write_client', return_value=(mock_zotero_client, None)):
        with patch('zotero_mcp.tools.annotations.rate_limit'):
            with patch('pyzotero.zotero.build_url', return_value="http://test.com"):
                result = await delete_note(
                    item_key="NOTE1234",
                    ctx=mock_ctx
                )

    assert "Failed to trash note" in result
    assert "500" in result


# ============================================================================
# Tests for create_annotation
# ============================================================================


@pytest.mark.asyncio
async def test_create_annotation_no_web_client(mock_ctx):
    """Test error when web client is not available."""
    with patch('zotero_mcp.tools.annotations._client.get_local_zotero_client', return_value=None):
        with patch('zotero_mcp.tools.annotations._client.get_web_zotero_client', return_value=None):
            result = await create_annotation(
                attachment_key="PDF12345",
                page=1,
                text="Test text",
                ctx=mock_ctx
            )

    assert "Error" in result
    assert "Web API credentials required" in result


@pytest.mark.asyncio
async def test_create_annotation_attachment_not_found(mock_ctx):
    """Test error when attachment doesn't exist."""
    mock_web_client = Mock()
    mock_web_client.item.side_effect = Exception("Not found")

    with patch('zotero_mcp.tools.annotations._client.get_local_zotero_client', return_value=None):
        with patch('zotero_mcp.tools.annotations._client.get_web_zotero_client', return_value=mock_web_client):
            result = await create_annotation(
                attachment_key="INVALID1",
                page=1,
                text="Test text",
                ctx=mock_ctx
            )

    assert "Error" in result
    assert "No attachment found" in result


@pytest.mark.asyncio
async def test_create_annotation_not_pdf(mock_ctx):
    """Test error when attachment is not a PDF."""
    mock_web_client = Mock()
    mock_web_client.item.return_value = {
        "key": "DOC12345",
        "version": 1,
        "data": {
            "itemType": "attachment",
            "contentType": "application/msword",
            "filename": "test.docx"
        }
    }

    with patch('zotero_mcp.tools.annotations._client.get_local_zotero_client', return_value=None):
        with patch('zotero_mcp.tools.annotations._client.get_web_zotero_client', return_value=mock_web_client):
            result = await create_annotation(
                attachment_key="DOC12345",
                page=1,
                text="Test text",
                ctx=mock_ctx
            )

    assert "Error" in result
    assert "not a PDF" in result


@pytest.mark.asyncio
async def test_create_annotation_cannot_download_pdf(mock_ctx, mock_pdf_attachment):
    """Test error when PDF cannot be downloaded."""
    mock_web_client = Mock()
    mock_web_client.item.return_value = mock_pdf_attachment
    mock_web_client.dump.side_effect = Exception("Download failed")

    with patch('zotero_mcp.tools.annotations._client.get_local_zotero_client', return_value=None):
        with patch('zotero_mcp.tools.annotations._client.get_web_zotero_client', return_value=mock_web_client):
            result = await create_annotation(
                attachment_key="PDF12345",
                page=1,
                text="Test text",
                ctx=mock_ctx
            )

    assert "Error" in result
    assert "Could not download PDF" in result


@pytest.mark.asyncio
async def test_create_annotation_text_not_found(mock_ctx, mock_pdf_attachment):
    """Test error when text is not found in PDF."""
    mock_web_client = Mock()
    mock_web_client.item.return_value = mock_pdf_attachment

    # Create a temporary PDF file
    with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp:
        tmp.write(b'%PDF-1.4\n%EOF')  # Minimal PDF header
        tmp_path = tmp.name

    try:
        def mock_dump(_key, filename, path):
            import shutil
            shutil.copy(tmp_path, os.path.join(path, filename))

        mock_web_client.dump.side_effect = mock_dump

        with patch('zotero_mcp.tools.annotations._client.get_local_zotero_client', return_value=None):
            with patch('zotero_mcp.tools.annotations._client.get_web_zotero_client', return_value=mock_web_client):
                with patch('zotero_mcp.pdf_utils.verify_pdf_attachment', return_value=True):
                    with patch('zotero_mcp.pdf_utils.find_text_position',
                              return_value={"error": "Text not found", "best_score": 0.3}):
                        result = await create_annotation(
                            attachment_key="PDF12345",
                            page=1,
                            text="Nonexistent text",
                            ctx=mock_ctx
                        )

        assert "Error" in result
        assert "not found" in result or "Text searched" in result
    finally:
        os.unlink(tmp_path)
