"""Tests for FTS5 sidecar index."""

import sqlite3
from pathlib import Path

import pytest

from zotero_mcp.fts_index import FTSIndex, _escape_fts_query, _get_sidecar_path


@pytest.fixture
def zotero_db():
    """Build an in-memory Zotero-like DB with notes and annotations."""
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE items (itemID INTEGER PRIMARY KEY, key TEXT);
        CREATE TABLE itemNotes (itemID INTEGER, note TEXT, title TEXT, parentItemID INTEGER);
        CREATE TABLE itemData (itemID INTEGER, fieldID INTEGER, valueID INTEGER);
        CREATE TABLE itemDataValues (valueID INTEGER PRIMARY KEY, value TEXT);
        CREATE TABLE itemAnnotations (
            itemID INTEGER, text TEXT, comment TEXT, type INTEGER,
            color TEXT, pageLabel TEXT, parentItemID INTEGER
        );
        CREATE TABLE itemAttachments (itemID INTEGER, parentItemID INTEGER);
        CREATE TABLE deletedItems (itemID INTEGER);

        INSERT INTO items VALUES (1, 'NOTE001'), (2, 'PARENT01'), (3, 'NOTE002'),
                                 (4, 'ANN001'), (5, 'ATT001'), (6, 'PARENT02');
        INSERT INTO itemNotes VALUES
            (1, '<p>Searching for quantum computing notes</p>', 'Note 1', 2),
            (3, '<p>Machine learning insights</p>', 'Note 2', NULL);
        INSERT INTO itemDataValues VALUES (100, 'Quantum Paper');
        INSERT INTO itemData VALUES (2, 1, 100);
        INSERT INTO itemAnnotations VALUES
            (4, 'highlighted text about quantum', 'my comment', 1, 'yellow', '5', 5);
        INSERT INTO itemAttachments VALUES (5, 6);
        INSERT INTO itemDataValues VALUES (101, 'Quantum Annotated');
        INSERT INTO itemData VALUES (6, 1, 101);
    """)
    return conn


@pytest.fixture
def fts_index(tmp_path):
    sidecar = tmp_path / "fts_index.sqlite"
    return FTSIndex(sidecar_path=sidecar)


def test_fts_index_creates_sidecar(fts_index):
    assert not fts_index.exists
    fts_index._get_connection()
    fts_index.close()
    assert fts_index.exists


def test_fts_index_populates_from_zotero_db(fts_index, zotero_db):
    count = fts_index.populate_from_zotero_db(zotero_db)
    assert count == 3  # 2 notes + 1 annotation


def test_fts_index_search_notes_finds_match(fts_index, zotero_db):
    fts_index.populate_from_zotero_db(zotero_db)
    results = fts_index.search_notes("quantum", limit=10)

    assert len(results) >= 1
    assert results[0]["type"] == "note"
    assert "quantum" in results[0]["text"].lower()
    assert results[0]["parent_key"] == "PARENT01"
    assert results[0]["parent_title"] == "Quantum Paper"


def test_fts_index_search_notes_strips_html(fts_index, zotero_db):
    fts_index.populate_from_zotero_db(zotero_db)
    results = fts_index.search_notes("quantum", limit=10)

    # FTS content should be stripped of HTML for matching
    for r in results:
        assert "<p>" not in r["text"]


def test_fts_index_search_annotations_finds_match(fts_index, zotero_db):
    fts_index.populate_from_zotero_db(zotero_db)
    results = fts_index.search_annotations("quantum", limit=10)

    assert len(results) >= 1
    assert results[0]["type"] == "annotation"
    assert results[0]["annotation_type"] == "highlight"
    assert results[0]["color"] == "yellow"
    assert results[0]["page_label"] == "5"


def test_fts_index_search_annotations_matches_comment(fts_index, zotero_db):
    fts_index.populate_from_zotero_db(zotero_db)
    results = fts_index.search_annotations("comment", limit=10)

    assert len(results) >= 1
    assert "comment" in results[0]["comment"].lower()


def test_fts_index_search_no_results(fts_index, zotero_db):
    fts_index.populate_from_zotero_db(zotero_db)
    results = fts_index.search_notes("nonexistentterm12345", limit=10)

    assert results == []


def test_fts_index_search_empty_query(fts_index, zotero_db):
    fts_index.populate_from_zotero_db(zotero_db)
    assert fts_index.search_notes("", limit=10) == []
    assert fts_index.search_notes("   ", limit=10) == []


def test_fts_index_repopulate_clears_old_data(fts_index, zotero_db):
    fts_index.populate_from_zotero_db(zotero_db)
    first_count = len(fts_index.search_notes("quantum", limit=10))

    # Repopulate
    fts_index.populate_from_zotero_db(zotero_db)
    second_count = len(fts_index.search_notes("quantum", limit=10))

    assert first_count == second_count  # No duplication


def test_escape_fts_query_handles_special_chars():
    assert _escape_fts_query("hello world") == '"hello" "world"'
    assert _escape_fts_query("") == ""
    assert _escape_fts_query("   ") == ""
    # Special chars get safely quoted
    result = _escape_fts_query("foo:bar")
    assert "foo:bar" in result or '"foo:bar"' in result


def test_get_sidecar_path_uses_env_when_set(tmp_path, monkeypatch):
    monkeypatch.setenv("ZOTERO_MCP_CONFIG_DIR", str(tmp_path))
    path = _get_sidecar_path()
    assert path == tmp_path / "fts_index.sqlite"
    assert tmp_path.exists()


def test_get_sidecar_path_falls_back_when_env_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("ZOTERO_MCP_CONFIG_DIR", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    path = _get_sidecar_path()
    assert path == tmp_path / ".config" / "zotero-mcp" / "fts_index.sqlite"
    assert (tmp_path / ".config" / "zotero-mcp").exists()


def test_get_sidecar_path_falls_back_when_env_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("ZOTERO_MCP_CONFIG_DIR", "   ")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    path = _get_sidecar_path()
    assert path == tmp_path / ".config" / "zotero-mcp" / "fts_index.sqlite"
