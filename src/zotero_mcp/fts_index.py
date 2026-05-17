"""FTS5 sidecar index for fast local search over notes and annotations.

Builds a separate SQLite database with FTS5 virtual tables populated from
Zotero's read-only database. Falls back gracefully if the sidecar doesn't
exist or can't be built.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from pathlib import Path

_logger = logging.getLogger(__name__)

_SCHEMA_VERSION = 1


def _get_sidecar_path() -> Path:
    """Return path to the FTS sidecar database."""
    env_dir = os.environ.get("ZOTERO_MCP_CONFIG_DIR", "").strip()
    config_dir = Path(env_dir) if env_dir else (Path.home() / ".config" / "zotero-mcp")
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir / "fts_index.sqlite"


def _create_schema(conn: sqlite3.Connection) -> None:
    """Create FTS5 virtual tables and metadata."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS fts_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
            item_key,
            parent_key UNINDEXED,
            parent_title UNINDEXED,
            content,
            tokenize='porter unicode61'
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS annotations_fts USING fts5(
            item_key,
            parent_key UNINDEXED,
            parent_title UNINDEXED,
            attachment_key UNINDEXED,
            annotation_type UNINDEXED,
            color UNINDEXED,
            page_label UNINDEXED,
            text,
            comment,
            tokenize='porter unicode61'
        );
    """)
    conn.execute(
        "INSERT OR REPLACE INTO fts_meta (key, value) VALUES ('schema_version', ?)",
        (str(_SCHEMA_VERSION),),
    )
    conn.commit()


class FTSIndex:
    """FTS5 sidecar index for notes and annotations."""

    def __init__(self, sidecar_path: Path | None = None):
        self._path = sidecar_path or _get_sidecar_path()
        self._conn: sqlite3.Connection | None = None

    def _get_connection(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self._path))
            self._conn.execute("PRAGMA journal_mode=WAL")
            _create_schema(self._conn)
        return self._conn

    @property
    def exists(self) -> bool:
        """Check if the sidecar DB file exists and has content."""
        return self._path.exists() and self._path.stat().st_size > 0

    def populate_from_zotero_db(self, zotero_conn: sqlite3.Connection) -> int:
        """Populate FTS index from Zotero's read-only database.

        Returns number of rows indexed.
        """
        conn = self._get_connection()
        t0 = time.monotonic()

        # Clear existing data for full rebuild
        conn.execute("DELETE FROM notes_fts")
        conn.execute("DELETE FROM annotations_fts")

        # Index notes
        cursor = zotero_conn.execute("""
            SELECT i.key, n.note, n.title,
                   pi.key as parentKey,
                   pdv.value as parentTitle
            FROM itemNotes n
            JOIN items i ON n.itemID = i.itemID
            LEFT JOIN items pi ON n.parentItemID = pi.itemID
            LEFT JOIN itemData pd ON pi.itemID = pd.itemID AND pd.fieldID = 1
            LEFT JOIN itemDataValues pdv ON pd.valueID = pdv.valueID
            WHERE i.itemID NOT IN (SELECT itemID FROM deletedItems)
        """)

        note_count = 0
        for row in cursor.fetchall():
            note_html = row[1] or ""
            # Strip HTML for FTS content
            from zotero_mcp.utils import clean_html

            clean_text = clean_html(note_html)
            if not clean_text.strip():
                continue
            conn.execute(
                "INSERT INTO notes_fts (item_key, parent_key, parent_title, content) VALUES (?, ?, ?, ?)",
                (row[0], row[3], row[4] or "", clean_text),
            )
            note_count += 1

        # Index annotations
        cursor = zotero_conn.execute("""
            SELECT i.key, ia.text, ia.comment, ia.type, ia.color, ia.pageLabel,
                   att.key as attachmentKey,
                   gpi.key as parentKey,
                   gpdv.value as parentTitle
            FROM itemAnnotations ia
            JOIN items i ON ia.itemID = i.itemID
            LEFT JOIN items att ON ia.parentItemID = att.itemID
            LEFT JOIN itemAttachments iatt ON ia.parentItemID = iatt.itemID
            LEFT JOIN items gpi ON iatt.parentItemID = gpi.itemID
            LEFT JOIN itemData gpd ON gpi.itemID = gpd.itemID AND gpd.fieldID = 1
            LEFT JOIN itemDataValues gpdv ON gpd.valueID = gpdv.valueID
            WHERE i.itemID NOT IN (SELECT itemID FROM deletedItems)
        """)

        type_map = {1: "highlight", 2: "note", 3: "image", 4: "ink", 5: "underline"}
        ann_count = 0
        for row in cursor.fetchall():
            text = row[1] or ""
            comment = row[2] or ""
            if not text.strip() and not comment.strip():
                continue
            conn.execute(
                "INSERT INTO annotations_fts (item_key, parent_key, parent_title, "
                "attachment_key, annotation_type, color, page_label, text, comment) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row[0],
                    row[7],
                    row[8] or "",
                    row[6],
                    type_map.get(row[3], "unknown"),
                    row[4] or "",
                    row[5] or "",
                    text,
                    comment,
                ),
            )
            ann_count += 1

        # Record build timestamp
        conn.execute(
            "INSERT OR REPLACE INTO fts_meta (key, value) VALUES ('last_built', ?)",
            (str(time.time()),),
        )
        conn.commit()

        elapsed = time.monotonic() - t0
        _logger.info(f"FTS index built: {note_count} notes, {ann_count} annotations in {elapsed:.1f}s")
        return note_count + ann_count

    def search_notes(self, query: str, limit: int = 20) -> list[dict]:
        """Search notes using FTS5 MATCH."""
        conn = self._get_connection()
        # Escape special FTS5 characters
        fts_query = _escape_fts_query(query)
        if not fts_query:
            return []

        try:
            cursor = conn.execute(
                """
                SELECT item_key, parent_key, parent_title, content,
                       rank
                FROM notes_fts
                WHERE notes_fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (fts_query, limit),
            )
        except sqlite3.OperationalError:
            return []

        results = []
        for row in cursor.fetchall():
            results.append(
                {
                    "type": "note",
                    "key": row[0],
                    "text": row[3],
                    "parent_key": row[1] or None,
                    "parent_title": row[2] or ("Unknown" if row[1] else None),
                    "tags": [],
                }
            )
        return results

    def search_annotations(self, query: str, limit: int = 20) -> list[dict]:
        """Search annotations using FTS5 MATCH."""
        conn = self._get_connection()
        fts_query = _escape_fts_query(query)
        if not fts_query:
            return []

        try:
            cursor = conn.execute(
                """
                SELECT item_key, parent_key, parent_title, attachment_key,
                       annotation_type, color, page_label, text, comment,
                       rank
                FROM annotations_fts
                WHERE annotations_fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (fts_query, limit),
            )
        except sqlite3.OperationalError:
            return []

        results = []
        for row in cursor.fetchall():
            results.append(
                {
                    "type": "annotation",
                    "key": row[0],
                    "text": row[7],
                    "comment": row[8],
                    "annotation_type": row[4],
                    "color": row[5],
                    "page_label": row[6] or None,
                    "attachment_key": row[3],
                    "parent_key": row[1] or None,
                    "parent_title": row[2] or ("Unknown" if row[1] else None),
                }
            )
        return results

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None


def _escape_fts_query(query: str) -> str:
    """Convert user query to safe FTS5 query string.

    Wraps each word in quotes to avoid FTS5 syntax errors from special chars.
    """
    words = query.strip().split()
    if not words:
        return ""
    # Quote each term to handle special characters safely
    escaped = " ".join(f'"{w}"' for w in words)
    return escaped


_fts_index: FTSIndex | None = None


def get_fts_index() -> FTSIndex:
    """Return the global FTS index instance."""
    global _fts_index
    if _fts_index is None:
        _fts_index = FTSIndex()
    return _fts_index
