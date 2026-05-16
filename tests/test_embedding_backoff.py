from zotero_mcp.semantic_search import ZoteroSemanticSearch


def test_upsert_documents_retries_embedding_rate_limit(monkeypatch):
    sleeps = []
    calls = []

    class FakeChroma:
        def upsert_documents(self, documents, metadatas, ids):
            calls.append(ids)
            if len(calls) == 1:
                raise RuntimeError("429 Client Error: Too Many Requests")

    search = ZoteroSemanticSearch.__new__(ZoteroSemanticSearch)
    search.chroma_client = FakeChroma()
    search._embed_throttle_lock = None
    search._last_embed_ts = 0.0

    monkeypatch.setattr(search, "_throttle_embedding_request", lambda: None)
    monkeypatch.setattr(search, "_embedding_429_wait", lambda attempt: 0.0)
    monkeypatch.setattr("zotero_mcp.semantic_search.time.sleep", lambda seconds: sleeps.append(seconds))

    search._upsert_documents_with_backoff(["doc"], [{}], ["A__0"])

    assert calls == [["A__0"], ["A__0"]]
    assert sleeps == [0.0]


def test_upsert_documents_keeps_retrying_rate_limits(monkeypatch):
    sleeps = []
    calls = []

    class FakeChroma:
        def upsert_documents(self, documents, metadatas, ids):
            calls.append(ids)
            if len(calls) <= 3:
                raise RuntimeError("429 Client Error: Too Many Requests")

    search = ZoteroSemanticSearch.__new__(ZoteroSemanticSearch)
    search.chroma_client = FakeChroma()
    search._embed_throttle_lock = None
    search._last_embed_ts = 0.0

    monkeypatch.setattr(search, "_throttle_embedding_request", lambda: None)
    monkeypatch.setattr(search, "_embedding_429_wait", lambda attempt: 0.0)
    monkeypatch.setattr("zotero_mcp.semantic_search.time.sleep", lambda seconds: sleeps.append(seconds))

    search._upsert_documents_with_backoff(["doc"], [{}], ["A__0"])

    assert calls == [["A__0"], ["A__0"], ["A__0"], ["A__0"]]
    assert sleeps == [0.0, 0.0, 0.0]


def test_upsert_documents_does_not_retry_non_rate_limit(monkeypatch):
    class FakeChroma:
        def upsert_documents(self, documents, metadatas, ids):
            raise RuntimeError("disk exploded")

    search = ZoteroSemanticSearch.__new__(ZoteroSemanticSearch)
    search.chroma_client = FakeChroma()
    monkeypatch.setattr(search, "_throttle_embedding_request", lambda: None)

    try:
        search._upsert_documents_with_backoff(["doc"], [{}], ["A__0"])
    except RuntimeError as exc:
        assert "disk exploded" in str(exc)
    else:
        raise AssertionError("Expected RuntimeError")
