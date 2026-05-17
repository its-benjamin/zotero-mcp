"""Small synchronous rate limiter for external API calls.

The MCP server is mostly synchronous, so a process-local limiter is enough to
avoid bursty "all at once" behavior from tool calls and indexing runs. Defaults
are intentionally conservative and can be overridden with environment variables.
"""

from __future__ import annotations

import logging
import os
import random
import threading
import time
from collections.abc import Callable
from email.utils import parsedate_to_datetime
from typing import Any

import requests
from requests.adapters import HTTPAdapter

DEFAULT_PROVIDER_LIMITS: dict[str, tuple[float, float]] = {
    # provider: (requests per window, window seconds)
    "zotero": (6, 1),
    "crossref": (1, 1),
    "arxiv": (1, 3),
    "unpaywall": (1, 1),
    "semantic_scholar": (1, 1),
    "pmc": (3, 1),
    "scite": (1, 1),
    "openai": (5, 1),
    "gemini": (1, 1),
    # Voyage free-tier projects are limited to 3 RPM. Users on paid tiers can
    # raise this with ZOTERO_MCP_RATE_VOYAGE_REQUESTS / WINDOW_SECONDS.
    "voyage": (3, 60),
}


def _env_key(provider: str, suffix: str) -> str:
    return f"ZOTERO_MCP_RATE_{provider.upper()}_{suffix}"


def _get_provider_limit(provider: str) -> tuple[float, float]:
    default_requests, default_window = DEFAULT_PROVIDER_LIMITS.get(provider, (1, 1))
    requests_per_window = float(os.getenv(_env_key(provider, "REQUESTS"), default_requests))
    window_seconds = float(os.getenv(_env_key(provider, "WINDOW_SECONDS"), default_window))
    if requests_per_window <= 0 or window_seconds <= 0:
        return default_requests, default_window
    return requests_per_window, window_seconds


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        return max(0.0, (parsedate_to_datetime(value).timestamp() - time.time()))
    except Exception:
        return None


class ProviderRateLimiter:
    """Minimum-spacing limiter with server-directed backoff support."""

    def __init__(self, provider: str):
        self.provider = provider
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def wait(self) -> None:
        requests_per_window, window_seconds = _get_provider_limit(self.provider)
        interval = window_seconds / requests_per_window
        with self._lock:
            now = time.monotonic()
            if self._next_allowed > now:
                time.sleep(self._next_allowed - now)
                now = time.monotonic()
            self._next_allowed = now + interval

    def backoff(self, seconds: float) -> None:
        if seconds <= 0:
            return
        with self._lock:
            self._next_allowed = max(self._next_allowed, time.monotonic() + seconds)


_LIMITERS: dict[str, ProviderRateLimiter] = {}
_LIMITERS_LOCK = threading.Lock()
_THREAD_LOCAL = threading.local()
_ORIGINAL_REQUESTS_GET = requests.get
_ORIGINAL_REQUESTS_POST = requests.post


def _get_thread_session(provider: str) -> requests.Session:
    sessions = getattr(_THREAD_LOCAL, "sessions", None)
    if sessions is None:
        sessions = {}
        _THREAD_LOCAL.sessions = sessions
    session = sessions.get(provider)
    if session is None:
        session = requests.Session()
        adapter = HTTPAdapter(pool_connections=16, pool_maxsize=16)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        sessions[provider] = session
    return session


def _session_get(provider: str, url: str, **kwargs: Any) -> requests.Response:
    if requests.get is not _ORIGINAL_REQUESTS_GET:
        return requests.get(url, **kwargs)
    return _get_thread_session(provider).get(url, **kwargs)


def _session_post(provider: str, url: str, **kwargs: Any) -> requests.Response:
    if requests.post is not _ORIGINAL_REQUESTS_POST:
        return requests.post(url, **kwargs)
    return _get_thread_session(provider).post(url, **kwargs)


def get_limiter(provider: str) -> ProviderRateLimiter:
    with _LIMITERS_LOCK:
        limiter = _LIMITERS.get(provider)
        if limiter is None:
            limiter = ProviderRateLimiter(provider)
            _LIMITERS[provider] = limiter
        return limiter


def rate_limit(provider: str) -> None:
    get_limiter(provider).wait()


_logger = logging.getLogger(__name__)


def _is_transient_pyzotero_error(exc: Exception) -> tuple[bool, float | None]:
    """Check if exception is transient and extract retry delay if available."""
    # pyzotero wraps HTTP errors; check for response attribute
    response = getattr(exc, "response", None)
    if response is not None:
        status = getattr(response, "status_code", None)
        if status in (429, 503):
            return True, _response_backoff_seconds(response)
        if status in (400, 401, 403, 404, 405, 410):
            return False, None
    # Check wrapped requests exceptions
    cause = getattr(exc, "__cause__", None) or getattr(exc, "__context__", None)
    if cause is not None:
        if isinstance(cause, (requests.exceptions.Timeout, requests.exceptions.ConnectionError)):
            return True, None
    # Direct requests exceptions
    if isinstance(exc, (requests.exceptions.Timeout, requests.exceptions.ConnectionError)):
        return True, None
    # Check exception message as fallback
    msg = str(exc).lower()
    if any(s in msg for s in ("429", "503", "rate limit", "temporarily unavailable", "timed out")):
        return True, None
    return False, None


def _response_backoff_seconds(response: Any) -> float | None:
    headers = getattr(response, "headers", {}) or {}
    retry_after = _parse_retry_after(headers.get("Retry-After"))
    if retry_after is not None:
        return retry_after
    backoff = _parse_retry_after(headers.get("Backoff"))
    if backoff is not None:
        return backoff
    return None


def _call_with_rate_limit(
    provider: str,
    call: Callable[..., Any],
    *args: Any,
    max_retries: int = 3,
    **kwargs: Any,
) -> Any:
    limiter = get_limiter(provider)
    attempt = 0
    while True:
        limiter.wait()
        response = call(*args, **kwargs)
        status_code = getattr(response, "status_code", None)
        backoff_seconds = _response_backoff_seconds(response)
        if backoff_seconds is not None:
            limiter.backoff(backoff_seconds)
        if status_code not in (429, 503) or attempt >= max_retries:
            return response
        wait = backoff_seconds if backoff_seconds is not None else min(60.0, (2**attempt) + random.random())
        limiter.backoff(wait)
        attempt += 1


def rate_limited_get(provider: str, url: str, **kwargs: Any) -> requests.Response:
    return _call_with_rate_limit(provider, _session_get, provider, url, **kwargs)


def rate_limited_post(provider: str, url: str, **kwargs: Any) -> requests.Response:
    return _call_with_rate_limit(provider, _session_post, provider, url, **kwargs)


class RateLimitedZotero:
    """Proxy pyzotero clients so all API method calls share one limiter."""

    def __init__(self, wrapped: Any, provider: str = "zotero", enabled: bool = True):
        object.__setattr__(self, "_wrapped", wrapped)
        object.__setattr__(self, "_provider", provider)
        object.__setattr__(self, "_enabled", enabled)

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._wrapped, name)
        if not callable(attr):
            return attr

        def limited_call(*args: Any, **kwargs: Any) -> Any:
            max_retries = 2
            for attempt in range(max_retries + 1):
                if self._enabled:
                    rate_limit(self._provider)
                try:
                    return attr(*args, **kwargs)
                except Exception as exc:
                    is_transient, backoff_hint = _is_transient_pyzotero_error(exc)
                    if not is_transient or attempt >= max_retries:
                        raise
                    wait = backoff_hint if backoff_hint is not None else min(10.0, (2**attempt) + random.random())
                    get_limiter(self._provider).backoff(wait)
                    _logger.debug(
                        "Retrying %s.%s after transient error (attempt %d/%d): %s",
                        type(self._wrapped).__name__,
                        name,
                        attempt + 1,
                        max_retries,
                        exc,
                    )

        return limited_call

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_"):
            object.__setattr__(self, name, value)
        else:
            setattr(self._wrapped, name, value)
