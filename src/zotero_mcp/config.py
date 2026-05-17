"""Shared configuration helpers for zotero-mcp."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class ConfigError(Exception):
    """Raised when configuration exists but cannot be loaded."""


def get_config_path() -> Path:
    return Path.home() / ".config" / "zotero-mcp" / "config.json"


def load_config(path: Path | str | None = None, *, missing_ok: bool = True) -> dict[str, Any]:
    config_path = Path(path) if path else get_config_path()
    if not config_path.exists():
        if missing_ok:
            return {}
        raise ConfigError(f"Config file not found: {config_path}")
    try:
        with open(config_path, encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"Invalid JSON in config file {config_path}: {exc.msg} at line {exc.lineno}, column {exc.colno}"
        ) from exc
    except OSError as exc:
        raise ConfigError(f"Could not read config file {config_path}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"Config file {config_path} must contain a JSON object")
    return data
