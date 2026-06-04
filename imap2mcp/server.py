"""MCP server exposing read-only search/read tools over the local index.

Search and reads are served from the SQLite/FTS index (fast, offline);
``list_mailboxes`` queries IMAP live. Transport is streamable HTTP so the
client may run on a different machine than this server.
"""

from __future__ import annotations

import logging

from mcp.server.fastmcp import FastMCP

from .config import Config
from .db import Database
from .imap_client import ImapConnection
from .sync import SyncWorker

logger = logging.getLogger(__name__)


def build_app(config: Config, db: Database, sync: SyncWorker) -> FastMCP:
    mcp = FastMCP("imap-2-mcp", host=config.http_host, port=config.http_port)

    def _rows(query: str, params: tuple) -> list[dict]:
        with db.connect() as conn:
            return [dict(r) for r in conn.execute(query, params).fetchall()]

    @mcp.tool()
    def list_mailboxes(account: str) -> list[str]:
        """List the IMAP folders/mailboxes of a configured account (live)."""
        acc = config.account(account)
        if acc is None:
            raise ValueError(f"Unknown account '{account}'")
        with ImapConnection(acc) as conn:
            return conn.list_folders()

    @mcp.tool()
    def list_recent(account: str, folder: str | None = None, limit: int = 20) -> list[dict]:
        """List the most recent indexed messages, newest first."""
        clauses = ["account = ?"]
        params: list = [account]
        if folder:
            clauses.append("folder = ?")
            params.append(folder)
        params.append(min(limit, 100))
        return _rows(
            f"""SELECT id, account, folder, uid, from_addr, to_addr, subject,
                       date, has_attach
                FROM messages WHERE {' AND '.join(clauses)}
                ORDER BY date DESC LIMIT ?""",
            tuple(params),
        )

    @mcp.tool()
    def get_email(message_id: int) -> dict | None:
        """Get a single indexed message (incl. body and attachment metadata)."""
        rows = _rows("SELECT * FROM messages WHERE id = ?", (message_id,))
        if not rows:
            return None
        msg = rows[0]
        msg["attachments"] = _rows(
            "SELECT id, filename, mime_type, size, (text IS NOT NULL) AS has_text "
            "FROM attachments WHERE message_id = ?",
            (message_id,),
        )
        return msg

    @mcp.tool()
    def get_thread(message_id: int) -> list[dict]:
        """Get all indexed messages sharing the thread of the given message."""
        rows = _rows("SELECT thread_key FROM messages WHERE id = ?", (message_id,))
        if not rows or not rows[0]["thread_key"]:
            return _rows("SELECT * FROM messages WHERE id = ?", (message_id,))
        return _rows(
            "SELECT * FROM messages WHERE thread_key = ? ORDER BY date ASC",
            (rows[0]["thread_key"],),
        )

    @mcp.tool()
    def search(
        query: str,
        account: str | None = None,
        folder: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        """Full-text search over subject, body, sender and attachment text.

        ``query`` uses SQLite FTS5 syntax (e.g. ``rechnung AND 2025``,
        ``"genaue phrase"``). Matches in message text and attachment text are
        merged and ranked.
        """
        limit = min(limit, 100)
        clauses = ["m.id IN (SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?) "
                   "OR m.id IN (SELECT a.message_id FROM attachments a "
                   "JOIN attachments_fts f ON f.rowid = a.id WHERE attachments_fts MATCH ?)"]
        params: list = [query, query]
        where = [f"({clauses[0]})"]
        if account:
            where.append("m.account = ?")
            params.append(account)
        if folder:
            where.append("m.folder = ?")
            params.append(folder)
        params.append(limit)
        return _rows(
            f"""SELECT m.id, m.account, m.folder, m.uid, m.from_addr, m.to_addr,
                       m.subject, m.date, m.has_attach
                FROM messages m WHERE {' AND '.join(where)}
                ORDER BY m.date DESC LIMIT ?""",
            tuple(params),
        )

    @mcp.tool()
    def search_attachments(query: str, limit: int = 20) -> list[dict]:
        """Search only within attachment contents/filenames (FTS5 syntax)."""
        return _rows(
            """SELECT a.id AS attachment_id, a.filename, a.mime_type, a.size,
                      m.id AS message_id, m.subject, m.from_addr, m.date
               FROM attachments_fts f
               JOIN attachments a ON a.id = f.rowid
               JOIN messages m ON m.id = a.message_id
               WHERE attachments_fts MATCH ?
               ORDER BY m.date DESC LIMIT ?""",
            (query, min(limit, 100)),
        )

    @mcp.tool()
    def get_attachment_text(attachment_id: int) -> str | None:
        """Return the extracted text of an attachment, or None if unavailable."""
        rows = _rows("SELECT text FROM attachments WHERE id = ?", (attachment_id,))
        return rows[0]["text"] if rows else None

    @mcp.tool()
    def sync_status() -> dict:
        """Report last sync time and indexed message counts per account."""
        state = _rows(
            "SELECT account, folder, last_uid, last_sync FROM sync_state "
            "ORDER BY account, folder",
            (),
        )
        counts = _rows(
            "SELECT account, COUNT(*) AS messages FROM messages GROUP BY account",
            (),
        )
        return {"folders": state, "counts": counts}

    return mcp
