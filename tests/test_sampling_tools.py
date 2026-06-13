"""Tests for MCP sampling helpers."""

import pytest

from zotero_mcp.tools.search import suggest_tags


class SamplingContext:
    def __init__(self, result_text):
        self.result_text = result_text
        self.messages = []

    async def info(self, *_args, **_kwargs):
        return None

    async def warning(self, *_args, **_kwargs):
        return None

    async def error(self, *_args, **_kwargs):
        return None

    async def sample(self, messages, **kwargs):
        self.messages.append((messages, kwargs))
        return type("SamplingResult", (), {"text": self.result_text, "result": self.result_text})()

@pytest.mark.asyncio
async def test_suggest_tags_uses_client_sampling(monkeypatch):
    fake_item = {
        "key": "ABCD1234",
        "data": {
            "title": "Attention Is All You Need",
            "abstractNote": "A transformer architecture for sequence transduction.",
            "tags": [{"tag": "deep learning"}],
        },
    }
    monkeypatch.setattr("zotero_mcp.client.get_zotero_client", lambda: type("Z", (), {"item": lambda self, key: fake_item})())

    ctx = SamplingContext('["transformers", "sequence models"]')
    result = await suggest_tags("ABCD1234", max_tags=2, ctx=ctx)

    assert "transformers" in result
    assert "sequence models" in result
    assert ctx.messages
    assert "Attention Is All You Need" in ctx.messages[0][0]
    assert ctx.messages[0][1]["max_tokens"] <= 300

@pytest.mark.asyncio
async def test_suggest_tags_handles_sampling_unavailable(monkeypatch):
    class NoSamplingContext(SamplingContext):
        async def sample(self, messages, **kwargs):
            raise RuntimeError("sampling unsupported")

    fake_item = {"key": "ABCD1234", "data": {"title": "Paper", "abstractNote": "", "tags": []}}
    monkeypatch.setattr("zotero_mcp.client.get_zotero_client", lambda: type("Z", (), {"item": lambda self, key: fake_item})())

    result = await suggest_tags("ABCD1234", ctx=NoSamplingContext(""))

    assert "Sampling is not available" in result
    assert "zotero_get_item_metadata" in result
