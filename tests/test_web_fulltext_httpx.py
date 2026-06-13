from zotero_mcp import semantic_search


class DummyChroma:
    pass


class DummyZotero:
    endpoint = "https://api.zotero.org"
    library_type = "users"
    library_id = "12345"


def _paper(key: str) -> dict:
    return {
        "key": key,
        "data": {
            "key": key,
            "itemType": "journalArticle",
            "title": key,
        },
    }


def _search(monkeypatch):
    monkeypatch.setattr(semantic_search, "get_zotero_client", lambda: DummyZotero())
    monkeypatch.setattr(semantic_search, "is_local_mode", lambda: False)
    return semantic_search.ZoteroSemanticSearch(chroma_client=DummyChroma())


def test_web_api_config_uses_zotero_client_attrs(monkeypatch):
    monkeypatch.setenv("ZOTERO_API_KEY", "secret")
    search = _search(monkeypatch)

    assert search._web_api_config() == (
        "https://api.zotero.org",
        "users",
        "12345",
        "secret",
    )


def test_attach_web_fulltext_uses_httpx_path(monkeypatch):
    monkeypatch.setenv("ZOTERO_API_KEY", "secret")
    items = [_paper("A"), _paper("B")]
    search = _search(monkeypatch)

    async def fake_attach(pending, config, workers):
        assert [item["key"] for item in pending] == ["A", "B"]
        assert config[0] == "https://api.zotero.org"
        assert workers == 2
        pending[0]["data"]["fulltext"] = "A body"
        pending[0]["data"]["fulltextSource"] = "web-api:parent"
        pending[1]["data"]["fulltext_attempted"] = True
        return 1

    monkeypatch.setattr(search, "_load_web_fulltext_workers", lambda: 2)
    monkeypatch.setattr(search, "_attach_web_fulltext_httpx_async", fake_attach)

    search._attach_web_fulltext(items)

    assert items[0]["data"]["fulltext"] == "A body"
    assert items[0]["data"]["fulltextSource"] == "web-api:parent"
    assert items[1]["data"]["fulltext_attempted"] is True


def test_attach_web_fulltext_falls_back_when_httpx_unavailable(monkeypatch):
    monkeypatch.setenv("ZOTERO_API_KEY", "secret")
    items = [_paper("A")]
    search = _search(monkeypatch)
    fallback_calls = []

    async def fake_attach(pending, config, workers):
        raise RuntimeError("asyncio event loop is already running")

    def fake_fallback(fallback_items, workers):
        fallback_calls.append((fallback_items, workers))

    monkeypatch.setattr(search, "_attach_web_fulltext_httpx_async", fake_attach)
    monkeypatch.setattr(search, "_attach_web_fulltext_pyzotero", fake_fallback)

    search._attach_web_fulltext(items)

    assert fallback_calls == [(items, 1)]

def test_attach_web_fulltext_invalid_rate_env_uses_safe_default(monkeypatch):
    monkeypatch.setenv("ZOTERO_MCP_RATE_ZOTERO_WINDOW_SECONDS", "-1")
    monkeypatch.setenv("ZOTERO_API_KEY", "secret")
    intervals = []
    search = _search(monkeypatch)

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    async def fake_fetch(client, base_items_url, key, limiter, min_interval, next_allowed):
        intervals.append(min_interval)
        return "Body text", "web-api:parent"

    monkeypatch.setattr(semantic_search.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(search, "_fetch_fulltext_via_httpx", fake_fetch)

    fetched = semantic_search.asyncio.run(
        search._attach_web_fulltext_httpx_async([_paper("A")], search._web_api_config(), workers=1)
    )

    assert fetched == 1
    assert intervals == [1 / 6]
