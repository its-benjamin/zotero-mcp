from unittest.mock import MagicMock

import pytest
import requests

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


class _PyzoteroError(Exception):
    """Stand-in for pyzotero.zotero_errors.HTTPError-style exceptions."""

    def __init__(self, message, response=None):
        super().__init__(message)
        self.response = response


def _silence_rate_limit_and_backoff(monkeypatch):
    monkeypatch.setattr("zotero_mcp.rate_limiter.rate_limit", lambda provider: None)
    monkeypatch.setattr("zotero_mcp.rate_limiter.ProviderRateLimiter.backoff", lambda self, seconds: None)


def test_rate_limited_zotero_retries_on_429(monkeypatch):
    _silence_rate_limit_and_backoff(monkeypatch)
    err = _PyzoteroError("rate limited", response=Response(429, {"Retry-After": "0"}))
    call = MagicMock(side_effect=[err, [{"key": "A"}]])

    class ZoteroLike:
        def items(self, **kwargs):
            return call(**kwargs)

    wrapped = RateLimitedZotero(ZoteroLike())
    assert wrapped.items() == [{"key": "A"}]
    assert call.call_count == 2


def test_rate_limited_zotero_retries_on_503(monkeypatch):
    _silence_rate_limit_and_backoff(monkeypatch)
    err = _PyzoteroError("service unavailable", response=Response(503))
    call = MagicMock(side_effect=[err, "ok"])

    class ZoteroLike:
        def collection(self, key):
            return call(key)

    wrapped = RateLimitedZotero(ZoteroLike())
    assert wrapped.collection("ABC") == "ok"
    assert call.call_count == 2


def test_rate_limited_zotero_retries_on_timeout(monkeypatch):
    _silence_rate_limit_and_backoff(monkeypatch)
    err = requests.exceptions.ConnectionError("connection reset")
    call = MagicMock(side_effect=[err, [{"key": "X"}]])

    class ZoteroLike:
        def items(self, **kwargs):
            return call(**kwargs)

    wrapped = RateLimitedZotero(ZoteroLike())
    assert wrapped.items() == [{"key": "X"}]
    assert call.call_count == 2


def test_rate_limited_zotero_no_retry_on_404(monkeypatch):
    _silence_rate_limit_and_backoff(monkeypatch)
    err = _PyzoteroError("not found", response=Response(404))
    call = MagicMock(side_effect=err)

    class ZoteroLike:
        def item(self, key):
            return call(key)

    wrapped = RateLimitedZotero(ZoteroLike())
    with pytest.raises(_PyzoteroError):
        wrapped.item("MISSING")
    assert call.call_count == 1


def test_rate_limited_zotero_no_retry_on_auth_error(monkeypatch):
    _silence_rate_limit_and_backoff(monkeypatch)
    err = _PyzoteroError("forbidden", response=Response(403))
    call = MagicMock(side_effect=err)

    class ZoteroLike:
        def items(self, **kwargs):
            return call(**kwargs)

    wrapped = RateLimitedZotero(ZoteroLike())
    with pytest.raises(_PyzoteroError):
        wrapped.items()
    assert call.call_count == 1


def test_rate_limited_zotero_max_retries_then_raises(monkeypatch):
    _silence_rate_limit_and_backoff(monkeypatch)
    err = _PyzoteroError("rate limited", response=Response(429))
    call = MagicMock(side_effect=err)

    class ZoteroLike:
        def items(self, **kwargs):
            return call(**kwargs)

    wrapped = RateLimitedZotero(ZoteroLike())
    with pytest.raises(_PyzoteroError):
        wrapped.items()
    # initial call + 2 retries = 3
    assert call.call_count == 3


def test_rate_limited_zotero_honors_retry_after_header(monkeypatch):
    monkeypatch.setattr("zotero_mcp.rate_limiter.rate_limit", lambda provider: None)
    backoff_calls = []
    monkeypatch.setattr(
        "zotero_mcp.rate_limiter.ProviderRateLimiter.backoff",
        lambda self, seconds: backoff_calls.append(seconds),
    )
    err = _PyzoteroError("rate limited", response=Response(429, {"Retry-After": "7"}))
    call = MagicMock(side_effect=[err, "ok"])

    class ZoteroLike:
        def items(self, **kwargs):
            return call(**kwargs)

    wrapped = RateLimitedZotero(ZoteroLike())
    assert wrapped.items() == "ok"
    assert backoff_calls == [7.0]
