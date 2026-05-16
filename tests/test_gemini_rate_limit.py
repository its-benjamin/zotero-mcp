from unittest.mock import MagicMock

from zotero_mcp.chroma_client import GeminiEmbeddingFunction


def make_gemini_ef(mock_client, mock_types, *, output_dimensionality=None, max_retries=0):
    ef = GeminiEmbeddingFunction.__new__(GeminiEmbeddingFunction)
    ef.model_name = "gemini-embedding-001"
    ef.client = mock_client
    ef.types = mock_types
    ef.output_dimensionality = output_dimensionality
    ef.max_retries = max_retries
    ef.rate_limited = False
    return ef


def test_gemini_output_dimensionality_is_configurable():
    mock_client = MagicMock()
    mock_embedding = MagicMock()
    mock_embedding.values = [0.4, 0.5, 0.6]
    mock_response = MagicMock()
    mock_response.embeddings = [mock_embedding]
    mock_client.models.embed_content.return_value = mock_response
    mock_types = MagicMock()

    ef = make_gemini_ef(mock_client, mock_types, output_dimensionality=768)
    ef(["some document"])

    mock_types.EmbedContentConfig.assert_called_with(
        task_type="retrieval_document",
        title="Zotero library document",
        output_dimensionality=768,
    )


def test_gemini_rate_limit_retries_with_backoff(monkeypatch):
    sleeps = []

    class RateLimitError(Exception):
        status_code = 429

    mock_client = MagicMock()
    mock_embedding = MagicMock()
    mock_embedding.values = [0.4, 0.5, 0.6]
    mock_response = MagicMock()
    mock_response.embeddings = [mock_embedding]
    mock_client.models.embed_content.side_effect = [
        RateLimitError("429 Too Many Requests"),
        mock_response,
    ]
    mock_types = MagicMock()
    monkeypatch.setattr("zotero_mcp.chroma_client.rate_limit", lambda provider: None)
    monkeypatch.setattr("zotero_mcp.chroma_client.time.sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr("zotero_mcp.chroma_client.random.uniform", lambda start, end: 0.0)

    ef = make_gemini_ef(mock_client, mock_types, max_retries=1)
    ef.rate_limited = True

    assert [list(vec) for vec in ef(["some document"])] == [[0.4, 0.5, 0.6]]
    assert mock_client.models.embed_content.call_count == 2
    assert sleeps == [30.0]


def test_gemini_rate_limit_retry_has_no_max_cap(monkeypatch):
    class RateLimitError(Exception):
        status_code = 429

    mock_client = MagicMock()
    mock_embedding = MagicMock()
    mock_embedding.values = [0.4]
    mock_response = MagicMock()
    mock_response.embeddings = [mock_embedding]
    mock_client.models.embed_content.side_effect = [
        RateLimitError("429 Too Many Requests"),
        mock_response,
    ]
    mock_types = MagicMock()
    monkeypatch.setattr("zotero_mcp.chroma_client.rate_limit", lambda provider: None)
    monkeypatch.setattr("zotero_mcp.chroma_client.time.sleep", lambda seconds: None)
    monkeypatch.setattr("zotero_mcp.chroma_client.random.uniform", lambda start, end: 0.0)

    ef = make_gemini_ef(mock_client, mock_types, max_retries=0)

    assert [list(vec) for vec in ef(["some document"])] == [[0.4]]
    assert mock_client.models.embed_content.call_count == 2
