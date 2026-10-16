"""SQLite storage: shows, episodes, subtitle lines and an FTS5 index over the lines."""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Sequence

SCHEMA = """
CREATE TABLE IF NOT EXISTS shows (
    id            INTEGER PRIMARY KEY,
    plex_key      TEXT NOT NULL UNIQUE,
    library_key   TEXT,
    library_title TEXT,
    slug          TEXT NOT NULL UNIQUE,
    title         TEXT NOT NULL,
    year          INTEGER,
    thumb         TEXT,
    leaf_count    INTEGER NOT NULL DEFAULT 0,
    enabled       INTEGER NOT NULL DEFAULT 0,
    user_toggled  INTEGER NOT NULL DEFAULT 0,
    last_indexed  REAL,
    last_seen     REAL
);

CREATE TABLE IF NOT EXISTS episodes (
    id          INTEGER PRIMARY KEY,
    plex_key    TEXT NOT NULL UNIQUE,
    show_id     INTEGER NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
    season      INTEGER NOT NULL,
    episode     INTEGER NOT NULL,
    title       TEXT,
    duration_ms INTEGER,
    part_key    TEXT,
    file_path   TEXT,
    file_size   INTEGER,
    hdr         INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending | indexed | no_subs | error
    sub_source  TEXT,
    error       TEXT,
    line_count  INTEGER NOT NULL DEFAULT 0,
    indexed_at  REAL
);
CREATE INDEX IF NOT EXISTS episodes_by_show ON episodes(show_id, season, episode);

CREATE TABLE IF NOT EXISTS lines (
    id         INTEGER PRIMARY KEY,
    episode_id INTEGER NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
    idx        INTEGER NOT NULL,
    start_ms   INTEGER NOT NULL,
    end_ms     INTEGER NOT NULL,
    text       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS lines_by_episode ON lines(episode_id, idx);

-- `context` holds the line plus the line after it, so quotes that span two
-- subtitle cues still match as a phrase.
CREATE VIRTUAL TABLE IF NOT EXISTS lines_fts USING fts5(
    text, context, tokenize = 'porter unicode61 remove_diacritics 2'
);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.Lock()
        self.conn().executescript(SCHEMA)

    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    def query(self, sql: str, params: Sequence = ()) -> list[sqlite3.Row]:
        return self.conn().execute(sql, params).fetchall()

    def one(self, sql: str, params: Sequence = ()) -> sqlite3.Row | None:
        return self.conn().execute(sql, params).fetchone()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        conn = self.conn()
        with self._write_lock:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    def execute(self, sql: str, params: Sequence = ()) -> sqlite3.Cursor:
        with self.tx() as conn:
            return conn.execute(sql, params)

    def replace_lines(self, episode_id: int, cues: Sequence) -> None:
        with self.tx() as conn:
            _delete_lines(conn, "episode_id = ?", (episode_id,))
            for i, cue in enumerate(cues):
                nxt = cues[i + 1].text if i + 1 < len(cues) else ""
                cur = conn.execute(
                    "INSERT INTO lines(episode_id, idx, start_ms, end_ms, text) VALUES (?, ?, ?, ?, ?)",
                    (episode_id, i, cue.start_ms, cue.end_ms, cue.text),
                )
                conn.execute(
                    "INSERT INTO lines_fts(rowid, text, context) VALUES (?, ?, ?)",
                    (cur.lastrowid, cue.text, f"{cue.text}\n{nxt}"),
                )

    def delete_episode_lines(self, where: str, params: Sequence) -> None:
        """Delete lines for episodes matching `where` (a clause over the episodes table)."""
        with self.tx() as conn:
            _delete_lines(conn, f"episode_id IN (SELECT id FROM episodes WHERE {where})", params)


def _delete_lines(conn: sqlite3.Connection, where: str, params: Sequence) -> None:
    conn.execute(f"DELETE FROM lines_fts WHERE rowid IN (SELECT id FROM lines WHERE {where})", params)
    conn.execute(f"DELETE FROM lines WHERE {where}", params)
