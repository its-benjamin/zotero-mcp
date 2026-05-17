import sys
from unittest.mock import MagicMock, patch

from zotero_mcp.chroma_client import OpenRouterEmbeddingFunction

# Ensure `openai` is importable even when the package is not installed (CI).
# We insert a MagicMock module so `patch("openai.OpenAI")` can resolve.
if "openai" not in sys.modules:
    sys.modules["openai"] = MagicMock()


def test_openrouter_default_model_name():
    assert OpenRouterEmbeddingFunction.DEFAULT_MODEL_NAME == "openai/text-embedding-3-small"


def test_openrouter_default_base_url():
    assert OpenRouterEmbeddingFunction.DEFAULT_BASE_URL == "https://openrouter.ai/api/v1"


def test_openrouter_default_api_key_env_var(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-or-key")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_BASE_URL", raising=False)

    with patch("openai.OpenAI"):
        ef = OpenRouterEmbeddingFunction()

    assert ef.api_key == "test-or-key"
    assert ef.base_url == "https://openrouter.ai/api/v1"
    assert ef.model_name == "openai/text-embedding-3-small"


def test_openrouter_custom_base_url_env(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "key")
    monkeypatch.setenv("OPENROUTER_BASE_URL", "https://custom.example.com/v1")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with patch("openai.OpenAI"):
        ef = OpenRouterEmbeddingFunction()

    assert ef.base_url == "https://custom.example.com/v1"


def test_openrouter_explicit_params_override_env(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "env-key")
    monkeypatch.setenv("OPENROUTER_BASE_URL", "https://env.example.com/v1")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with patch("openai.OpenAI"):
        ef = OpenRouterEmbeddingFunction(
            model_name="mistral/mistral-embed",
            api_key="explicit-key",
            base_url="https://explicit.example.com/v1",
        )

    assert ef.api_key == "explicit-key"
    assert ef.base_url == "https://explicit.example.com/v1"
    assert ef.model_name == "mistral/mistral-embed"


def test_openrouter_build_from_config(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_BASE_URL", raising=False)

    with patch("openai.OpenAI"):
        ef = OpenRouterEmbeddingFunction.build_from_config(
            {
                "model_name": "cohere/embed-english-v3.0",
                "api_key": "cfg-key",
                "base_url": "https://cfg.example.com/v1",
                "request_batch_size": 32,
                "rate_limit_rps": 5.0,
            }
        )

    assert ef.model_name == "cohere/embed-english-v3.0"
    assert ef.api_key == "cfg-key"
    assert ef.base_url == "https://cfg.example.com/v1"
    assert ef.request_batch_size == 32
    assert ef.rate_limit_rps == 5.0


def test_openrouter_name():
    assert OpenRouterEmbeddingFunction.name() == "openrouter"


def test_create_embedding_function_returns_openrouter(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_BASE_URL", raising=False)

    with patch("openai.OpenAI"):
        from zotero_mcp.chroma_client import ChromaClient

        client = ChromaClient.__new__(ChromaClient)
        client.embedding_model = "openrouter"
        client.embedding_config = {
            "api_key": "test-key",
            "model_name": "openai/text-embedding-3-small",
        }

        ef = client._create_embedding_function()

    assert isinstance(ef, OpenRouterEmbeddingFunction)
    assert ef.model_name == "openai/text-embedding-3-small"
    assert ef.base_url == "https://openrouter.ai/api/v1"
