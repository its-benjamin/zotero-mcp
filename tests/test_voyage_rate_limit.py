from zotero_mcp.chroma_client import VoyageEmbeddingFunction


class FakeVoyageResult:
    def __init__(self, embeddings):
        self.embeddings = embeddings


class FakeVoyageClient:
    def __init__(self):
        self.calls = []

    def count_tokens(self, texts, model=None):
        return sum(max(1, len(text) // 3) for text in texts)

    def embed(self, texts, **kwargs):
        self.calls.append((list(texts), kwargs))
        return FakeVoyageResult([[float(i)] for i, _ in enumerate(texts)])


def make_fake_client(monkeypatch, client=None):
    client = client or FakeVoyageClient()
    monkeypatch.setattr(VoyageEmbeddingFunction, "_create_client", lambda self: client)
    monkeypatch.setattr("zotero_mcp.chroma_client.rate_limit", lambda provider: None)
    return client


def reset_voyage_budget():
    VoyageEmbeddingFunction._TOKEN_WINDOW_STARTED = 0.0
    VoyageEmbeddingFunction._TOKEN_WINDOW_USED = 0


def test_voyage_retries_429_with_backoff(monkeypatch):
    sleeps = []

    class Response:
        status_code = 429
        headers = {"Retry-After": "0"}

    class RateLimitError(Exception):
        response = Response()

    class RetryClient(FakeVoyageClient):
        def embed(self, texts, **kwargs):
            self.calls.append((list(texts), kwargs))
            if len(self.calls) == 1:
                raise RateLimitError("429 Too Many Requests")
            return FakeVoyageResult([[0.4]])

    client = make_fake_client(monkeypatch, RetryClient())
    monkeypatch.setattr("zotero_mcp.chroma_client.time.sleep", lambda seconds: sleeps.append(seconds))
    reset_voyage_budget()

    ef = VoyageEmbeddingFunction(model_name="voyage-4-lite", api_key="test-key", max_retries=1)
    assert [list(vec) for vec in ef(["doc"])] == [[0.4]]
    assert len(client.calls) == 2
    assert sleeps == [0.0]


def test_voyage_rate_limit_retry_has_no_max_cap(monkeypatch):
    class RateLimitError(Exception):
        response = type("Response", (), {"status_code": 429, "headers": {"Retry-After": "0"}})()

    class RetryClient(FakeVoyageClient):
        def embed(self, texts, **kwargs):
            self.calls.append((list(texts), kwargs))
            if len(self.calls) == 1:
                raise RateLimitError("429 Too Many Requests")
            return FakeVoyageResult([[0.4]])

    client = make_fake_client(monkeypatch, RetryClient())
    monkeypatch.setattr("zotero_mcp.chroma_client.time.sleep", lambda seconds: None)
    reset_voyage_budget()

    ef = VoyageEmbeddingFunction(model_name="voyage-4-lite", api_key="test-key", max_retries=0)
    assert [list(vec) for vec in ef(["doc"])] == [[0.4]]
    assert len(client.calls) == 2


def test_voyage_request_batch_size_is_configurable(monkeypatch):
    client = make_fake_client(monkeypatch)
    reset_voyage_budget()

    ef = VoyageEmbeddingFunction(
        model_name="voyage-4-lite",
        api_key="test-key",
        request_batch_size=2,
    )
    result = ef(["a", "b", "c", "d", "e"])

    assert len(result) == 5
    assert [len(call[0]) for call in client.calls] == [2, 2, 1]


def test_voyage_defaults_are_conservative(monkeypatch):
    monkeypatch.delenv("VOYAGE_REQUEST_BATCH_SIZE", raising=False)
    monkeypatch.delenv("VOYAGE_TOKENS_PER_MINUTE", raising=False)
    monkeypatch.delenv("VOYAGE_MAX_RETRIES", raising=False)
    monkeypatch.delenv("VOYAGE_OUTPUT_DIMENSION", raising=False)
    make_fake_client(monkeypatch)

    ef = VoyageEmbeddingFunction(api_key="test-key")

    assert ef.model_name == "voyage-4-lite"
    assert ef.request_batch_size == 16
    assert ef.tokens_per_minute == 10_000
    assert ef.output_dimension == 512
    assert ef.max_retries == 8


def test_voyage_splits_batches_by_token_budget(monkeypatch):
    client = make_fake_client(monkeypatch)
    reset_voyage_budget()

    ef = VoyageEmbeddingFunction(
        model_name="voyage-4-lite",
        api_key="test-key",
        request_batch_size=128,
        tokens_per_minute=10_000,
    )
    ef(["x" * 9000, "y" * 9000, "z" * 9000])

    assert [len(call[0]) for call in client.calls] == [2, 1]


def test_voyage_waits_for_token_budget(monkeypatch):
    sleeps = []
    make_fake_client(monkeypatch)

    times = iter([0.0, 0.0, 61.0])
    monkeypatch.setattr("zotero_mcp.chroma_client.time.monotonic", lambda: next(times, 61.0))
    monkeypatch.setattr("zotero_mcp.chroma_client.time.sleep", lambda seconds: sleeps.append(seconds))
    reset_voyage_budget()

    ef = VoyageEmbeddingFunction(
        model_name="voyage-4-lite",
        api_key="test-key",
        request_batch_size=1,
    )
    ef.tokens_per_minute = 1
    ef(["abcdef", "ghijkl"])

    assert sleeps == [60.0]


def test_voyage_passes_output_dimension(monkeypatch):
    client = make_fake_client(monkeypatch)
    reset_voyage_budget()

    ef = VoyageEmbeddingFunction(
        model_name="voyage-4-lite",
        api_key="test-key",
        output_dimension=256,
    )
    ef(["doc"])

    assert client.calls[0][1]["output_dimension"] == 256
