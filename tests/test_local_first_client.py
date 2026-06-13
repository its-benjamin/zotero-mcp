import os
import time

import pytest
from conftest import DummyContext

from zotero_mcp import client, server
from zotero_mcp.cli import setup_zotero_environment


class _FakePyzotero:
    calls = []
    fail_local = False

    def __init__(self, library_id, library_type, api_key=None, local=False):
        self.library_id = library_id
        self.library_type = library_type
        self.api_key = api_key
        self.local = local
        self.calls.append(
            {
                "library_id": library_id,
                "library_type": library_type,
                "api_key": api_key,
                "local": local,
            }
        )

    def items(self, **_kwargs):
        if self.local and self.fail_local:
            raise RuntimeError("local unavailable")
        return []


def test_setup_zotero_environment_defaults_to_auto_local(monkeypatch):
    monkeypatch.delenv("ZOTERO_LOCAL", raising=False)
    monkeypatch.delenv("ZOTERO_LOCAL_AUTO", raising=False)
    monkeypatch.delenv("ZOTERO_LIBRARY_ID", raising=False)
    monkeypatch.delenv("ZOTERO_API_KEY", raising=False)
    monkeypatch.delenv("ZOTERO_NO_CLAUDE", raising=False)
    monkeypatch.setattr("zotero_mcp.cli.load_standalone_env_vars", lambda: {})
    monkeypatch.setattr("zotero_mcp.cli.load_claude_desktop_env_vars", lambda: {})

    setup_zotero_environment()

    assert os.environ["ZOTERO_LOCAL"] == "true"
    assert os.environ["ZOTERO_LOCAL_AUTO"] == "true"
    assert os.environ["ZOTERO_LIBRARY_ID"] == "0"


def test_setup_zotero_environment_respects_explicit_web_mode(monkeypatch):
    monkeypatch.setenv("ZOTERO_LOCAL", "false")
    monkeypatch.delenv("ZOTERO_LOCAL_AUTO", raising=False)
    monkeypatch.setattr("zotero_mcp.cli.load_standalone_env_vars", lambda: {})
    monkeypatch.setattr("zotero_mcp.cli.load_claude_desktop_env_vars", lambda: {})

    setup_zotero_environment()

    assert os.environ["ZOTERO_LOCAL"] == "false"
    assert "ZOTERO_LOCAL_AUTO" not in os.environ


def test_get_zotero_client_auto_local_uses_local_user_id(monkeypatch):
    _FakePyzotero.calls = []
    _FakePyzotero.fail_local = False
    monkeypatch.setattr(client.zotero, "Zotero", _FakePyzotero)
    monkeypatch.setenv("ZOTERO_LOCAL", "true")
    monkeypatch.setenv("ZOTERO_LOCAL_AUTO", "true")
    monkeypatch.setenv("ZOTERO_LIBRARY_ID", "999999")
    monkeypatch.setenv("ZOTERO_LIBRARY_TYPE", "user")
    monkeypatch.setenv("ZOTERO_API_KEY", "secret")
    client.clear_active_library()

    zot = client.get_zotero_client()

    assert zot.library_id == "0"
    assert zot.local is True
    assert _FakePyzotero.calls[0]["library_id"] == "0"


def test_get_zotero_client_auto_local_falls_back_to_web(monkeypatch):
    _FakePyzotero.calls = []
    _FakePyzotero.fail_local = True
    monkeypatch.setattr(client.zotero, "Zotero", _FakePyzotero)
    monkeypatch.setenv("ZOTERO_LOCAL", "true")
    monkeypatch.setenv("ZOTERO_LOCAL_AUTO", "true")
    monkeypatch.setenv("ZOTERO_LIBRARY_ID", "999999")
    monkeypatch.setenv("ZOTERO_LIBRARY_TYPE", "user")
    monkeypatch.setenv("ZOTERO_API_KEY", "secret")
    client.clear_active_library()

    zot = client.get_zotero_client()

    assert zot.library_id == "999999"
    assert zot.local is False
    assert _FakePyzotero.calls == [
        {"library_id": "0", "library_type": "user", "api_key": "secret", "local": True},
        {"library_id": "999999", "library_type": "user", "api_key": "secret", "local": False},
    ]


@pytest.mark.asyncio
async def test_run_zotero_call_times_out(monkeypatch):
    monkeypatch.setenv("ZOTERO_MCP_CALL_TIMEOUT_SECONDS", "0.01")

    def slow_call():
        time.sleep(0.05)

    with pytest.raises(client.ZoteroAPITimeout, match="timed out after"):
        await client.run_zotero_call(slow_call, operation="slow_call")


@pytest.mark.asyncio
async def test_get_item_metadata_returns_timeout_error(monkeypatch):
    monkeypatch.setenv("ZOTERO_MCP_CALL_TIMEOUT_SECONDS", "0.01")

    class SlowZotero:
        def item(self, _key):
            time.sleep(0.05)
            return {"key": "SLOWITEM", "data": {"title": "Too Late"}}

    monkeypatch.setattr(client, "get_zotero_client", lambda: SlowZotero())

    result = await server.get_item_metadata(item_key="SLOWITEM", include_abstract=False, ctx=DummyContext())

    assert "Zotero API call timed out" in result
    assert "zot.item(SLOWITEM)" in result
