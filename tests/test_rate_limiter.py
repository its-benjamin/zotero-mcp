from unittest.mock import MagicMock

from zotero_mcp.rate_limiter import RateLimitedZotero, _call_with_rate_limit


class Response:
    def __init__(self, status_code=200, headers=None):
        self.status_code = status_code
        self.headers = headers or {}


def test_call_with_rate_limit_retries_429_with_retry_after(monkeypatch):
    sleeps = []
    responses = [Response(429, {"Retry-After": "0"}), Response(200)]
    call = MagicMock(side_effect=responses)

    monkeypatch.setattr("zotero_mcp.rate_limiter.time.sleep", lambda seconds: sleeps.append(seconds))

    result = _call_with_rate_limit("crossref", call, "https://example.test", max_retries=1)

    assert result.status_code == 200
    assert call.call_count == 2


def test_rate_limited_zotero_wraps_methods(monkeypatch):
    calls = []

    class ZoteroLike:
        library_id = "123"

        def items(self, **kwargs):
            calls.append(kwargs)
            return [{"key": "A"}]

    monkeypatch.setattr("zotero_mcp.rate_limiter.rate_limit", lambda provider: calls.append({"provider": provider}))

    wrapped = RateLimitedZotero(ZoteroLike())
    assert wrapped.items(limit=1) == [{"key": "A"}]
    assert calls[0] == {"provider": "zotero"}
    assert calls[1] == {"limit": 1}
    assert wrapped.library_id == "123"

