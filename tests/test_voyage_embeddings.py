"""Voyage embedding provider wiring."""

from zotero_mcp.chroma_client import VoyageEmbeddingFunction


class _Response:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return {"data": [{"embedding": [0.1, 0.2]}, {"embedding": [0.3, 0.4]}]}


def test_voyage_embeddings_call_api(monkeypatch):
    calls = []

    def fake_post(provider, url, **kwargs):
        calls.append((provider, url, kwargs))
        return _Response()

    monkeypatch.setattr("zotero_mcp.rate_limiter.rate_limited_post", fake_post)

    monkeypatch.setattr(VoyageEmbeddingFunction, "_wait_for_token_budget", lambda self, texts: None)

    ef = VoyageEmbeddingFunction(
        api_key="test-key", request_batch_size=2, tokens_per_minute=10000, output_dimension=512
    )
    embeddings = ef(["doc one", "doc two"])

    assert [list(embedding) for embedding in embeddings] == [[0.1, 0.2], [0.3, 0.4]]
    assert calls[0][0] == "voyage"
    assert calls[0][1] == "https://api.voyageai.com/v1/embeddings"
    assert calls[0][2]["headers"]["Authorization"] == "Bearer test-key"
    assert calls[0][2]["json"] == {
        "input": ["doc one", "doc two"],
        "model": "voyage-4-lite",
        "input_type": "document",
        "output_dimension": 512,
    }
    assert ef.tokens_per_minute == 10000
