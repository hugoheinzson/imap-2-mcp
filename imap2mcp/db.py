"""SQLite storage with FTS5 full-text indexes for messages and attachments.

The schema keeps lightweight message metadata plus extracted text so that
search never has to touch the IMAP server. Connections use WAL mode so the
sync worker can write while the MCP server reads.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    account     TEXT NOT NULL,
    folder      TEXT NOT NULL,
    uid         INTEGER NOT NULL,
    message_id  TEXT,
    thread_key  TEXT,
    from_addr   TEXT,
    to_addr     TEXT,
    subject     TEXT,
    date        TEXT,            -- ISO 8601
    body        TEXT,
    has_attach  INTEGER DEFAULT 0,
    UNIQUE(account, folder, uid)
);

CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_key);
CREATE INDEX IF NOT EXISTS idx_messages_date ON messages(date);

CREATE TABLE IF NOT EXISTS attachments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id  INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    filename    TEXT,
    mime_type   TEXT,
    size        INTEGER,
    text        TEXT             -- extracted text, may be NULL
);

CREATE INDEX IF NOT EXISTS idx_attachments_msg ON attachments(message_id);

CREATE TABLE IF NOT EXISTS sync_state (
    account     TEXT NOT NULL,
    folder      TEXT NOT NULL,
    last_uid    INTEGER NOT NULL DEFAULT 0,
    uidvalidity INTEGER,
    last_sync   TEXT,
    PRIMARY KEY (account, folder)
);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    subject, body, from_addr, to_addr,
    content='messages', content_rowid='id'
);

CREATE VIRTUAL TABLE IF NOT EXISTS attachments_fts USING fts5(
    filename, text,
    content='attachments', content_rowid='id'
);

-- Keep FTS tables in sync with their content tables.
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, subject, body, from_addr, to_addr)
    VALUES (new.id, new.subject, new.body, new.from_addr, new.to_addr);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, subject, body, from_addr, to_addr)
    VALUES ('delete', old.id, old.subject, old.body, old.from_addr, old.to_addr);
END;
CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, subject, body, from_addr, to_addr)
    VALUES ('delete', old.id, old.subject, old.body, old.from_addr, old.to_addr);
    INSERT INTO messages_fts(rowid, subject, body, from_addr, to_addr)
    VALUES (new.id, new.subject, new.body, new.from_addr, new.to_addr);
END;

CREATE TRIGGER IF NOT EXISTS attachments_ai AFTER INSERT ON attachments BEGIN
    INSERT INTO attachments_fts(rowid, filename, text)
    VALUES (new.id, new.filename, new.text);
END;
CREATE TRIGGER IF NOT EXISTS attachments_ad AFTER DELETE ON attachments BEGIN
    INSERT INTO attachments_fts(attachments_fts, rowid, filename, text)
    VALUES ('delete', old.id, old.filename, old.text);
END;
"""


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            conn.commit()

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Yield a thread-local connection (transactions managed by caller)."""
        yield self._conn()
