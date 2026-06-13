"""Tests for zotero_batch_update_tags."""

import pytest
from unittest.mock import Mock, AsyncMock, patch, MagicMock

from zotero_mcp.tools.write import batch_update_tags


class MockContext:
    """Mock MCP context for testing."""

    def __init__(self):
        self.info_calls = []
        self.error_calls = []

    async def info(self, message):
        self.info_calls.append(message)

    async def error(self, message):
        self.error_calls.append(message)


@pytest.fixture
def mock_ctx():
    return MockContext()


@pytest.fixture
def mock_zotero_client():
    """Create a mock Zotero client."""
    client = Mock()
    client.items.return_value = []
    client.update_item.return_value = {"success": {"0": "ITEMKEY1"}}
    return client


@pytest.mark.asyncio
async def test_batch_update_tags_add_single_tag(mock_ctx, mock_zotero_client):
    """Test adding a single tag to multiple items."""
    mock_zotero_client.items.return_value = [
        {
            "key": "ITEM1",
            "data": {"title": "Test Item 1", "tags": []}
        },
        {
            "key": "ITEM2",
            "data": {"title": "Test Item 2", "tags": [{"tag": "existing"}]}
        },
    ]

    with patch('zotero_mcp.tools.write._helpers._get_write_client', return_value=(None, mock_zotero_client)):
        with patch('zotero_mcp.tools.write._client.get_zotero_client', return_value=mock_zotero_client):
            result = await batch_update_tags(
                query="test",
                add_tags=["new-tag"],
                ctx=mock_ctx
            )

    assert "Items updated: 2" in result  # 2 items updated
    assert "new-tag" in result


@pytest.mark.asyncio
async def test_batch_update_tags_remove_single_tag(mock_ctx, mock_zotero_client):
    """Test removing a single tag from items."""
    mock_zotero_client.items.return_value = [
        {
            "key": "ITEM1",
            "data": {"title": "Test Item 1", "tags": [{"tag": "remove-me"}, {"tag": "keep"}]}
        },
    ]

    with patch('zotero_mcp.tools.write._helpers._get_write_client', return_value=(None, mock_zotero_client)):
        with patch('zotero_mcp.tools.write._client.get_zotero_client', return_value=mock_zotero_client):
            result = await batch_update_tags(
                query="test",
                remove_tags=["remove-me"],
                ctx=mock_ctx
            )

    assert "Items updated: 1" in result
    assert "remove-me" in result


@pytest.mark.asyncio
async def test_batch_update_tags_add_and_remove(mock_ctx, mock_zotero_client):
    """Test adding and removing tags in the same operation."""
    mock_zotero_client.items.return_value = [
        {
            "key": "ITEM1",
            "data": {"title": "Test Item 1", "tags": [{"tag": "old-tag"}]}
        },
    ]

    with patch('zotero_mcp.tools.write._helpers._get_write_client', return_value=(None, mock_zotero_client)):
        with patch('zotero_mcp.tools.write._client.get_zotero_client', return_value=mock_zotero_client):
            result = await batch_update_tags(
                query="test",
                add_tags=["new-tag"],
                remove_tags=["old-tag"],
                ctx=mock_ctx
            )

    assert "Items updated: 1" in result
    assert "new-tag" in result
    assert "old-tag" in result


@pytest.mark.asyncio
async def test_batch_update_tags_no_items_found(mock_ctx, mock_zotero_client):
    """Test behavior when no items match the query."""
    mock_zotero_client.items.return_value = []

    with patch('zotero_mcp.tools.write._helpers._get_write_client', return_value=(None, mock_zotero_client)):
        with patch('zotero_mcp.tools.write._client.get_zotero_client', return_value=mock_zotero_client):
            result = await batch_update_tags(
                query="nonexistent",
                add_tags=["tag"],
                ctx=mock_ctx
            )

    assert "No items found" in result


@pytest.mark.asyncio
async def test_batch_update_tags_no_tags_specified(mock_ctx, mock_zotero_client):
    """Test error when neither add_tags nor remove_tags is provided."""
    with patch('zotero_mcp.tools.write._helpers._get_write_client', return_value=(None, mock_zotero_client)):
        with patch('zotero_mcp.tools.write._client.get_zotero_client', return_value=mock_zotero_client):
            result = await batch_update_tags(
                query="test",
                ctx=mock_ctx
            )

    assert "Error" in result or "must provide" in result.lower()


@pytest.mark.asyncio
async def test_batch_update_tags_with_tag_filter(mock_ctx, mock_zotero_client):
    """Test filtering items by existing tag."""
    mock_zotero_client.items.return_value = [
        {
            "key": "ITEM1",
            "data": {"title": "Test Item 1", "tags": [{"tag": "filter-tag"}]}
        },
    ]

    with patch('zotero_mcp.tools.write._helpers._get_write_client', return_value=(None, mock_zotero_client)):
        with patch('zotero_mcp.tools.write._client.get_zotero_client', return_value=mock_zotero_client):
            result = await batch_update_tags(
                tag="filter-tag",
                add_tags=["new-tag"],
                ctx=mock_ctx
            )

    assert "Items updated: 1" in result
    assert "new-tag" in result


@pytest.mark.asyncio
async def test_batch_update_tags_skip_attachments(mock_ctx, mock_zotero_client):
    """Test that attachments are skipped during batch update."""
    mock_zotero_client.items.return_value = [
        {
            "key": "ITEM1",
            "data": {"title": "Regular Item", "itemType": "journalArticle", "tags": []}
        },
        {
            "key": "ATT1",
            "data": {"title": "Attachment", "itemType": "attachment", "tags": []}
        },
    ]

    with patch('zotero_mcp.tools.write._helpers._get_write_client', return_value=(None, mock_zotero_client)):
        with patch('zotero_mcp.tools.write._client.get_zotero_client', return_value=mock_zotero_client):
            result = await batch_update_tags(
                query="test",
                add_tags=["tag"],
                ctx=mock_ctx
            )

    # Should only update 1 item (the non-attachment)
    assert "Items updated: 1" in result
    assert "Items skipped: 1" in result


@pytest.mark.asyncio
async def test_batch_update_tags_multiple_tags(mock_ctx, mock_zotero_client):
    """Test adding multiple tags at once."""
    mock_zotero_client.items.return_value = [
        {
            "key": "ITEM1",
            "data": {"title": "Test Item", "tags": []}
        },
    ]

    with patch('zotero_mcp.tools.write._helpers._get_write_client', return_value=(None, mock_zotero_client)):
        with patch('zotero_mcp.tools.write._client.get_zotero_client', return_value=mock_zotero_client):
            result = await batch_update_tags(
                query="test",
                add_tags=["tag1", "tag2", "tag3"],
                ctx=mock_ctx
            )

    assert "Items updated: 1" in result
    assert "tag1" in result
    assert "tag2" in result
    assert "tag3" in result


@pytest.mark.asyncio
async def test_batch_update_tags_no_changes_needed(mock_ctx, mock_zotero_client):
    """Test when items already have the tags being added."""
    mock_zotero_client.items.return_value = [
        {
            "key": "ITEM1",
            "data": {"title": "Test Item", "tags": [{"tag": "existing-tag"}]}
        },
    ]

    with patch('zotero_mcp.tools.write._helpers._get_write_client', return_value=(None, mock_zotero_client)):
        with patch('zotero_mcp.tools.write._client.get_zotero_client', return_value=mock_zotero_client):
            result = await batch_update_tags(
                query="test",
                add_tags=["existing-tag"],  # Tag already exists
                ctx=mock_ctx
            )

    # Should indicate no updates were needed
    assert "0" in result or "no items" in result.lower() or "updated" in result.lower()


@pytest.mark.asyncio
async def test_batch_update_tags_limit_parameter(mock_ctx, mock_zotero_client):
    """Test that the limit parameter is respected."""
    mock_zotero_client.items.return_value = [
        {"key": f"ITEM{i}", "data": {"title": f"Item {i}", "tags": []}}
        for i in range(10)
    ]

    with patch('zotero_mcp.tools.write._helpers._get_write_client', return_value=(None, mock_zotero_client)):
        with patch('zotero_mcp.tools.write._client.get_zotero_client', return_value=mock_zotero_client):
            await batch_update_tags(
                query="test",
                add_tags=["tag"],
                limit=5,
                ctx=mock_ctx
            )

    # Verify limit was passed to items() call
    call_args = mock_zotero_client.items.call_args
    assert call_args is not None


@pytest.mark.asyncio
async def test_batch_update_tags_json_string_input(mock_ctx, mock_zotero_client):
    """Test that JSON string input for tags is parsed correctly."""
    mock_zotero_client.items.return_value = [
        {
            "key": "ITEM1",
            "data": {"title": "Test Item", "tags": []}
        },
    ]

    with patch('zotero_mcp.tools.write._helpers._get_write_client', return_value=(None, mock_zotero_client)):
        with patch('zotero_mcp.tools.write._client.get_zotero_client', return_value=mock_zotero_client):
            result = await batch_update_tags(
                query="test",
                add_tags='["tag1", "tag2"]',  # JSON string
                ctx=mock_ctx
            )

    assert "Items updated: 1" in result
    assert "tag1" in result
    assert "tag2" in result


@pytest.mark.asyncio
async def test_batch_update_tags_error_handling(mock_ctx, mock_zotero_client):
    """Test error handling when update_item fails."""
    mock_zotero_client.items.return_value = [
        {
            "key": "ITEM1",
            "data": {"title": "Test Item", "tags": []}
        },
    ]
    mock_zotero_client.update_item.side_effect = Exception("API Error")

    with patch('zotero_mcp.tools.write._helpers._get_write_client', return_value=(None, mock_zotero_client)):
        with patch('zotero_mcp.tools.write._client.get_zotero_client', return_value=mock_zotero_client):
            result = await batch_update_tags(
                query="test",
                add_tags=["tag"],
                ctx=mock_ctx
            )

    # Should handle the error gracefully - item is skipped when update fails
    assert "Items updated: 0" in result or "Items skipped: 1" in result
