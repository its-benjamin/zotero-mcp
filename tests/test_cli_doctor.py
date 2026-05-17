"""Tests for `zotero-mcp doctor` CLI command."""

import os
import subprocess
import sys


def test_doctor_runs_cleanly(tmp_path, monkeypatch):
    """The doctor command should run without crashing and print expected sections."""
    monkeypatch.setenv("ZOTERO_NO_CLAUDE", "1")
    monkeypatch.setenv("ZOTERO_MCP_CONFIG_DIR", str(tmp_path))

    result = subprocess.run(
        [sys.executable, "-m", "zotero_mcp.cli", "doctor"],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "ZOTERO_NO_CLAUDE": "1"},
    )

    output = result.stdout + result.stderr
    assert "Zotero MCP Doctor" in output
    assert "Mode:" in output
    assert "FTS sidecar" in output
    assert result.returncode == 0


def test_doctor_reports_config_warn_on_invalid_json(tmp_path, monkeypatch):
    """Doctor should surface a config warning when JSON is malformed."""
    fake_home = tmp_path
    config_dir = fake_home / ".config" / "zotero-mcp"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text("{not valid json", encoding="utf-8")

    env = {**os.environ, "ZOTERO_NO_CLAUDE": "1"}
    if sys.platform == "win32":
        env["USERPROFILE"] = str(fake_home)
    else:
        env["HOME"] = str(fake_home)

    result = subprocess.run(
        [sys.executable, "-m", "zotero_mcp.cli", "doctor"],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )

    output = result.stdout + result.stderr
    assert "Config" in output
    assert "Invalid JSON" in output or "WARN" in output or "could not be loaded" in output
