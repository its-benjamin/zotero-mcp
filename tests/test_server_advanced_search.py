import pytest

from zotero_mcp import server


class DummyContext:
    async def info(self, *_args, **_kwargs):
        return None

    async def error(self, *_args, **_kwargs):
        return None

    async def warning(self, *_args, **_kwargs):
        return None


class FakeZotero:
    def __init__(self, items):
        self._items = items
        self.last_kwargs: dict = {}

    def items(self, start=0, limit=100, **kwargs):
        self.last_kwargs = kwargs
        items = self._items
        # Mirror Zotero API itemType filtering: "-type" excludes, "type" includes
        item_type = kwargs.get("itemType", "")
        if item_type:
            excluded = set()
            included = set()
            for t in item_type.split():
                if t.startswith("-"):
                    excluded.add(t[1:])
                else:
                    included.add(t)
            items = [
                it
                for it in items
                if (not excluded or it.get("data", {}).get("itemType") not in excluded)
                and (not included or it.get("data", {}).get("itemType") in included)
            ]
        # Server-side q/qmode filtering
        q = kwargs.get("q", "")
        if q:
            q_lower = q.lower()
            items = [
                it
                for it in items
                if q_lower in it.get("data", {}).get("title", "").lower()
                or any(
                    q_lower in (c.get("lastName", "") + " " + c.get("firstName", "")).lower()
                    for c in it.get("data", {}).get("creators", [])
                    if isinstance(c, dict)
                )
            ]
        # Server-side tag filter
        tag = kwargs.get("tag", "")
        if tag:
            tags = [tag] if isinstance(tag, str) else tag
            items = [
                it
                for it in items
                if all(
                    any(t.get("tag") == tg for t in it.get("data", {}).get("tags", []))
                    for tg in tags
                )
            ]
        return items[start : start + limit]


@pytest.mark.asyncio
async def test_advanced_search_filters_items(monkeypatch):
    fake_items = [
        {
            "key": "AAA11111",
            "data": {
                "itemType": "journalArticle",
                "title": "Quantum Networks and Learning",
                "date": "2024",
                "creators": [{"firstName": "Jane", "lastName": "Doe"}],
                "tags": [{"tag": "physics"}],
            },
        },
        {
            "key": "BBB22222",
            "data": {
                "itemType": "journalArticle",
                "title": "Classical Literature Review",
                "date": "2018",
                "creators": [{"firstName": "Alex", "lastName": "Smith"}],
                "tags": [{"tag": "history"}],
            },
        },
        {
            "key": "CCC33333",
            "data": {
                "itemType": "attachment",
                "title": "Ignored Attachment",
                "date": "2024",
                "creators": [],
                "tags": [],
            },
        },
    ]
    monkeypatch.setattr("zotero_mcp.client.get_zotero_client", lambda: FakeZotero(fake_items))

    result = await server.advanced_search(
        conditions=[
            {"field": "title", "operation": "contains", "value": "quantum"},
            {"field": "year", "operation": "isGreaterThan", "value": "2020"},
        ],
        join_mode="all",
        limit=10,
        ctx=DummyContext(),
    )

    assert "Quantum Networks and Learning" in result
    assert "Classical Literature Review" not in result
    assert "Ignored Attachment" not in result


@pytest.mark.asyncio
async def test_advanced_search_rejects_unknown_operation(monkeypatch):
    monkeypatch.setattr("zotero_mcp.client.get_zotero_client", lambda: FakeZotero([]))

    result = await server.advanced_search(
        conditions=[{"field": "title", "operation": "regex", "value": ".*"}],
        ctx=DummyContext(),
    )

    assert "Unsupported operation" in result
