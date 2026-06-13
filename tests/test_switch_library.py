"""Tests for zotero_switch_library functionality."""
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from zotero_mcp.tools.retrieval import switch_library


class MockContext:
    """Mock MCP context for testing."""

    async def info(self, msg):
        pass

    async def error(self, msg):
        pass


@pytest.fixture
def mock_ctx():
    return MockContext()


@pytest.mark.asyncio
async def test_switch_library_to_group_success(mock_ctx):
    """Test successful switch to a group library."""
    mock_zot = MagicMock()
    mock_zot.add_parameters = MagicMock()
    mock_zot.items = MagicMock(return_value=[])

    with patch("zotero_mcp.tools.retrieval._client") as mock_client, \
         patch("zotero_mcp.tools.retrieval.validate_library_switch", return_value=None):
        mock_client.set_active_library = MagicMock()
        mock_client.run_zotero_call = AsyncMock(side_effect=[mock_zot, None])

        result = await switch_library(library_id="12345", library_type="group", ctx=mock_ctx)

        assert "Successfully switched" in result
        assert "12345" in result
        assert "group" in result
        mock_client.set_active_library.assert_called_once_with("12345", "group")


@pytest.mark.asyncio
async def test_switch_library_to_user_success(mock_ctx):
    """Test successful switch to user library."""
    mock_zot = MagicMock()
    mock_zot.add_parameters = MagicMock()
    mock_zot.items = MagicMock(return_value=[])

    with patch("zotero_mcp.tools.retrieval._client") as mock_client, \
         patch("zotero_mcp.tools.retrieval.validate_library_switch", return_value=None):
        mock_client.set_active_library = MagicMock()
        mock_client.run_zotero_call = AsyncMock(side_effect=[mock_zot, None])

        result = await switch_library(library_id="0", library_type="user", ctx=mock_ctx)

        assert "Successfully switched" in result
        assert "0" in result
        assert "user" in result


@pytest.mark.asyncio
async def test_switch_library_to_default(mock_ctx):
    """Test switching back to default library."""
    with patch("zotero_mcp.tools.retrieval._client") as mock_client:
        mock_client.clear_active_library = MagicMock()

        with patch.dict(os.environ, {"ZOTERO_LIBRARY_ID": "99999", "ZOTERO_LIBRARY_TYPE": "group"}):
            result = await switch_library(library_id="ignored", library_type="default", ctx=mock_ctx)

        assert "default library configuration" in result
        assert "99999" in result
        assert "group" in result
        mock_client.clear_active_library.assert_called_once()


@pytest.mark.asyncio
async def test_switch_library_validation_failure(mock_ctx):
    """Test that validation errors prevent the switch."""
    with patch("zotero_mcp.tools.retrieval._client") as mock_client, \
         patch("zotero_mcp.tools.retrieval.validate_library_switch",
               return_value="Invalid library_type 'invalid'. Must be 'user', 'group', or 'feed'."):
        mock_client.set_active_library = MagicMock()

        result = await switch_library(library_id="12345", library_type="invalid", ctx=mock_ctx)

        assert "Invalid library_type" in result
        mock_client.set_active_library.assert_not_called()


@pytest.mark.asyncio
async def test_switch_library_api_failure_rollback(mock_ctx):
    """Test rollback when API call fails after setting library."""
    with patch("zotero_mcp.tools.retrieval._client") as mock_client, \
         patch("zotero_mcp.tools.retrieval.validate_library_switch", return_value=None):
        mock_client.set_active_library = MagicMock()
        mock_client.clear_active_library = MagicMock()
        mock_client.run_zotero_call = AsyncMock(side_effect=Exception("API connection failed"))

        result = await switch_library(library_id="12345", library_type="group", ctx=mock_ctx)

        assert "Could not access library" in result
        assert "Reverted to default library" in result
        mock_client.set_active_library.assert_called_once()
        mock_client.clear_active_library.assert_called_once()


@pytest.mark.asyncio
async def test_switch_library_verify_items_call(mock_ctx):
    """Test that switch_library makes a verification call to items()."""
    mock_zot = MagicMock()
    mock_zot.add_parameters = MagicMock()
    mock_zot.items = MagicMock(return_value=[{"key": "TEST123"}])

    call_count = 0

    async def mock_run_zotero_call(func, **_kwargs):
        nonlocal call_count
        call_count += 1
        # First call: get_zotero_client — return the mock zot client
        if call_count == 1:
            return mock_zot
        # Second call: _validate_lib — actually invoke it (it's sync)
        if call_count == 2:
            func()
        return None

    with patch("zotero_mcp.tools.retrieval._client") as mock_client, \
         patch("zotero_mcp.tools.retrieval.validate_library_switch", return_value=None):
        mock_client.get_zotero_client = MagicMock(return_value=mock_zot)
        mock_client.set_active_library = MagicMock()
        mock_client.run_zotero_call = AsyncMock(side_effect=mock_run_zotero_call)

        result = await switch_library(library_id="12345", library_type="group", ctx=mock_ctx)

        assert "Successfully switched" in result
        mock_zot.add_parameters.assert_called_once_with(limit=1)
        mock_zot.items.assert_called_once()


@pytest.mark.asyncio
async def test_switch_library_with_feed_type(mock_ctx):
    """Test switching to a feed library type."""
    mock_zot = MagicMock()
    mock_zot.add_parameters = MagicMock()
    mock_zot.items = MagicMock(return_value=[])

    with patch("zotero_mcp.tools.retrieval._client") as mock_client, \
         patch("zotero_mcp.tools.retrieval.validate_library_switch", return_value=None):
        mock_client.set_active_library = MagicMock()
        mock_client.run_zotero_call = AsyncMock(side_effect=[mock_zot, None])

        result = await switch_library(library_id="67890", library_type="feed", ctx=mock_ctx)

        assert "Successfully switched" in result
        assert "67890" in result
        assert "feed" in result


@pytest.mark.asyncio
async def test_switch_library_exception_handling(mock_ctx):
    """Test that exceptions are caught and reported."""
    with patch("zotero_mcp.tools.retrieval._client") as mock_client, \
         patch("zotero_mcp.tools.retrieval.validate_library_switch",
               side_effect=RuntimeError("Unexpected error")):
        mock_client.set_active_library = MagicMock()

        result = await switch_library(library_id="12345", library_type="group", ctx=mock_ctx)

        assert "Error switching library" in result
        assert "Unexpected error" in result
