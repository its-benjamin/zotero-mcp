"""Tests for shared config helpers."""

import json
from pathlib import Path

import pytest

from zotero_mcp.config import ConfigError, get_config_path, load_config


def test_get_config_path_uses_home(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert get_config_path() == tmp_path / ".config" / "zotero-mcp" / "config.json"


def test_load_config_missing_ok(tmp_path):
    assert load_config(tmp_path / "missing.json") == {}


def test_load_config_missing_not_ok(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "missing.json", missing_ok=False)


def test_load_config_valid(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"client_env": {"ZOTERO_LOCAL": "true"}}), encoding="utf-8")
    assert load_config(path) == {"client_env": {"ZOTERO_LOCAL": "true"}}


def test_load_config_invalid_json(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{bad json", encoding="utf-8")
    with pytest.raises(ConfigError, match="Invalid JSON"):
        load_config(path)


def test_load_config_non_object(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(ConfigError, match="JSON object"):
        load_config(path)
