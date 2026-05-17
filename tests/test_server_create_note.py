import pytest

from zotero_mcp import server


class DummyContext:
    async def info(self, *_args, **_kwargs):
        return None

    async def error(self, *_args, **_kwargs):
        return None

    async def warning(self, *_args, **_kwargs):
        return None


class _FakeResponse:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


class FakeZotero:
    def __init__(self, fail_on=None):
        self.created = []
        self._fail_on = fail_on or {}

    def _maybe_fail(self, method_name):
        status_code = self._fail_on.get(method_name)
        if status_code is not None:
            exc = Exception(f"HTTP {status_code}")
            exc.response = _FakeResponse(status_code)
            raise exc

    def item(self, _item_key):
        self._maybe_fail("item")
        return {"data": {"title": "Parent Item"}}

    def create_items(self, items):
        self._maybe_fail("create_items")
        self.created.extend(items)
        return {"success": {"0": "NOTEKEY01"}}


@pytest.mark.asyncio
async def test_create_note_includes_title_heading(monkeypatch):
    fake_zot = FakeZotero()
    monkeypatch.setattr("zotero_mcp.client.get_zotero_client", lambda: fake_zot)
    monkeypatch.setattr("zotero_mcp.tools.annotations._utils.is_local_mode", lambda: False)

    result = await server.create_note(
        item_key="ITEM0001",
        note_title="<Unsafe Title>",
        note_text="Line one\n\nLine two",
        tags=["t1"],
        ctx=DummyContext(),
    )

    assert "Successfully created note" in result
    assert len(fake_zot.created) == 1
    note_html = fake_zot.created[0]["note"]
    assert note_html.startswith("<h1>&lt;Unsafe Title&gt;</h1>")
    assert "<p>Line one</p>" in note_html


@pytest.mark.asyncio
async def test_create_note_parent_not_found(monkeypatch):
    fake_zot = FakeZotero(fail_on={"item": 404})
    monkeypatch.setattr("zotero_mcp.client.get_zotero_client", lambda: fake_zot)
    monkeypatch.setattr("zotero_mcp.tools.annotations._utils.is_local_mode", lambda: False)

    result = await server.create_note(
        item_key="MISSING1",
        note_title="Note",
        note_text="Text",
        ctx=DummyContext(),
    )

    assert "Error" in result or "No item found" in result
    assert fake_zot.created == []


@pytest.mark.asyncio
async def test_create_note_auth_failure_on_create(monkeypatch):
    fake_zot = FakeZotero(fail_on={"create_items": 403})
    monkeypatch.setattr("zotero_mcp.client.get_zotero_client", lambda: fake_zot)
    monkeypatch.setattr("zotero_mcp.tools.annotations._utils.is_local_mode", lambda: False)

    result = await server.create_note(
        item_key="ITEM0001",
        note_title="Note",
        note_text="Text",
        ctx=DummyContext(),
    )

    assert "Error" in result


@pytest.mark.asyncio
async def test_create_note_empty_body_still_succeeds(monkeypatch):
    """Empty note body is allowed — creates a note with no content."""
    fake_zot = FakeZotero()
    monkeypatch.setattr("zotero_mcp.client.get_zotero_client", lambda: fake_zot)
    monkeypatch.setattr("zotero_mcp.tools.annotations._utils.is_local_mode", lambda: False)

    result = await server.create_note(
        item_key="ITEM0001",
        note_title="",
        note_text="",
        ctx=DummyContext(),
    )

    assert "Successfully created note" in result
    assert len(fake_zot.created) == 1
