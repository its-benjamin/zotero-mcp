"""CLI handling for missing semantic-search optional dependencies."""

import builtins

import pytest

from zotero_mcp import cli


def test_missing_semantic_extra_prints_actionable_error(monkeypatch, capsys):
    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "zotero_mcp.semantic_search":
            raise ImportError("No module named 'chromadb'")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(SystemExit) as exc:
        cli._import_create_semantic_search()

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "Semantic search dependencies are not installed" in err
    assert "uv tool install zotero-mcp-server[semantic] --force" in err
    assert "No module named 'chromadb'" in err
