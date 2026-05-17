"""Tests for zotero_move_item and zotero_rename_tag tools."""

from unittest.mock import patch

import pytest
from conftest import DummyContext, FakeZotero, _FakeResponse


class FakeZoteroForMove(FakeZotero):
    def __init__(self, items=None, **kwargs):
        super().__init__(**kwargs)
        self._items = items or []
        self.update_calls = []

    def update_item(self, item, **kwargs):
        self._maybe_fail("update_item")
        self.update_calls.append(item)
        return _FakeResponse(204)


@pytest.fixture
def move_ctx():
    return DummyContext()


def _make_item(key, collections=None, tags=None):
    return {
        "key": key,
        "version": 1,
        "data": {
            "title": f"Item {key}",
            "collections": collections or [],
            "tags": [{"tag": t} for t in (tags or [])],
        },
    }


class TestMoveItem:
    @pytest.mark.asyncio
    async def test_move_item_between_collections(self, move_ctx):
        item = _make_item("ITEM0001", collections=["COLLSRC1"])
        zot = FakeZoteroForMove(items=[item])

        with patch("zotero_mcp.tools._helpers._get_write_client", return_value=(zot, zot)):
            from zotero_mcp.tools.write import move_item

            result = await move_item(
                item_key="ITEM0001",
                target_collection="COLLTGT1",
                source_collection="COLLSRC1",
                ctx=move_ctx,
            )

        assert "Moved" in result
        assert "COLLTGT1" in result
        updated = zot.update_calls[0]
        assert "COLLTGT1" in updated["data"]["collections"]
        assert "COLLSRC1" not in updated["data"]["collections"]

    @pytest.mark.asyncio
    async def test_move_item_add_only(self, move_ctx):
        item = _make_item("ITEM0002", collections=["EXISTING"])
        zot = FakeZoteroForMove(items=[item])

        with patch("zotero_mcp.tools._helpers._get_write_client", return_value=(zot, zot)):
            from zotero_mcp.tools.write import move_item

            result = await move_item(
                item_key="ITEM0002",
                target_collection="NEWTARGT",
                ctx=move_ctx,
            )

        assert "Moved" in result
        updated = zot.update_calls[0]
        assert "NEWTARGT" in updated["data"]["collections"]
        assert "EXISTING" in updated["data"]["collections"]

    @pytest.mark.asyncio
    async def test_move_item_not_in_source(self, move_ctx):
        item = _make_item("ITEM0003", collections=["OTHER123"])
        zot = FakeZoteroForMove(items=[item])

        with patch("zotero_mcp.tools._helpers._get_write_client", return_value=(zot, zot)):
            from zotero_mcp.tools.write import move_item

            result = await move_item(
                item_key="ITEM0003",
                target_collection="COLLTGT1",
                source_collection="NOTHERE1",
                ctx=move_ctx,
            )

        assert "not in collection" in result

    @pytest.mark.asyncio
    async def test_move_item_invalid_key(self, move_ctx):
        from zotero_mcp.tools.write import move_item

        result = await move_item(
            item_key="bad",
            target_collection="COLLTGT1",
            ctx=move_ctx,
        )
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_move_item_invalid_target(self, move_ctx):
        from zotero_mcp.tools.write import move_item

        result = await move_item(
            item_key="ITEM0001",
            target_collection="bad",
            ctx=move_ctx,
        )
        assert "Error" in result


class TestRenameTag:
    @pytest.mark.asyncio
    async def test_rename_tag_basic(self, move_ctx):
        items = [
            _make_item("ITEM0001", tags=["old-tag", "keep"]),
            _make_item("ITEM0002", tags=["old-tag"]),
        ]
        zot = FakeZoteroForMove(items=items)

        def fake_paginate(method, *args, **kwargs):
            return items

        with (
            patch("zotero_mcp.tools._helpers._get_write_client", return_value=(zot, zot)),
            patch("zotero_mcp.tools._helpers._paginate", side_effect=fake_paginate),
        ):
            from zotero_mcp.tools.write import rename_tag

            result = await rename_tag(
                old_tag="old-tag",
                new_tag="new-tag",
                ctx=move_ctx,
            )

        assert "2 item(s)" in result
        assert "new-tag" in result
        for call in zot.update_calls:
            tag_names = [t["tag"] for t in call["data"]["tags"]]
            assert "new-tag" in tag_names
            assert "old-tag" not in tag_names

    @pytest.mark.asyncio
    async def test_rename_tag_preserves_other_tags(self, move_ctx):
        items = [_make_item("ITEM0001", tags=["old-tag", "important"])]
        zot = FakeZoteroForMove(items=items)

        def fake_paginate(method, *args, **kwargs):
            return items

        with (
            patch("zotero_mcp.tools._helpers._get_write_client", return_value=(zot, zot)),
            patch("zotero_mcp.tools._helpers._paginate", side_effect=fake_paginate),
        ):
            from zotero_mcp.tools.write import rename_tag

            result = await rename_tag(
                old_tag="old-tag",
                new_tag="new-tag",
                ctx=move_ctx,
            )

        assert "1 item(s)" in result
        tag_names = [t["tag"] for t in zot.update_calls[0]["data"]["tags"]]
        assert "important" in tag_names
        assert "new-tag" in tag_names

    @pytest.mark.asyncio
    async def test_rename_tag_no_items(self, move_ctx):
        zot = FakeZoteroForMove(items=[])

        def fake_paginate(method, *args, **kwargs):
            return []

        with (
            patch("zotero_mcp.tools._helpers._get_write_client", return_value=(zot, zot)),
            patch("zotero_mcp.tools._helpers._paginate", side_effect=fake_paginate),
        ):
            from zotero_mcp.tools.write import rename_tag

            result = await rename_tag(
                old_tag="nonexistent",
                new_tag="new-tag",
                ctx=move_ctx,
            )

        assert "No items found" in result

    @pytest.mark.asyncio
    async def test_rename_tag_same_name(self, move_ctx):
        from zotero_mcp.tools.write import rename_tag

        result = await rename_tag(
            old_tag="same",
            new_tag="same",
            ctx=move_ctx,
        )
        assert "same" in result.lower()

    @pytest.mark.asyncio
    async def test_rename_tag_invalid_empty(self, move_ctx):
        from zotero_mcp.tools.write import rename_tag

        result = await rename_tag(
            old_tag="",
            new_tag="new-tag",
            ctx=move_ctx,
        )
        assert "Error" in result
