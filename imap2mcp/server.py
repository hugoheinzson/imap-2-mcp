"""MCP server exposing search/read tools over the local index, plus drafts.

Search and reads are served from the SQLite/FTS index (fast, offline);
``list_mailboxes`` queries IMAP live. ``create_draft`` is the only tool that
writes to IMAP: it APPENDs a draft to the account's drafts folder — mail can
never be sent from here (there is no SMTP in this project). The optional
``send_attachment_to_paperless`` re-fetches an attachment read-only and posts
it to Paperless-ngx; it never changes the mailbox. Transport is streamable
HTTP so the client may run on a different machine than this server.
"""

from __future__ import annotations

import logging

import anyio

from mcp.server.fastmcp import FastMCP

from .compose import build_draft
from .config import Config
from .db import Database
from .imap_client import ImapConnection
from .paperless import (
    PaperlessClient,
    fetch_attachment,
    task_document_id,
    task_error,
    task_status,
)
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
    def create_draft(
        account: str,
        body: str,
        to: str | None = None,
        subject: str | None = None,
        cc: str | None = None,
        bcc: str | None = None,
        reply_to_message_id: int | None = None,
    ) -> dict:
        """Create a draft e-mail in the account's IMAP drafts folder.

        The draft is only stored (IMAP APPEND with the \\Draft flag) — it is
        NEVER sent; sending stays a manual step in the mail client. With
        ``reply_to_message_id`` (the id of an indexed message) the draft is
        threaded as a reply: ``to`` defaults to the original sender and
        ``subject`` to ``Re: <original subject>``.
        """
        acc = config.account(account)
        if acc is None:
            raise ValueError(f"Unknown account '{account}'")

        in_reply_to: str | None = None
        if reply_to_message_id is not None:
            rows = _rows(
                "SELECT message_id, from_addr, subject FROM messages WHERE id = ?",
                (reply_to_message_id,),
            )
            if not rows:
                raise ValueError(f"No indexed message with id {reply_to_message_id}")
            orig = rows[0]
            in_reply_to = orig["message_id"]  # may be None; then no threading headers
            if to is None:
                to = orig["from_addr"]
            if subject is None:
                subject = orig["subject"] or ""
                if not subject.lower().startswith("re:"):
                    subject = f"Re: {subject}"
        if not to or not subject:
            raise ValueError(
                "'to' and 'subject' are required unless replying to an indexed message"
            )

        raw, draft_message_id = build_draft(
            from_addr=acc.from_addr or acc.user,
            to=to,
            subject=subject,
            body=body,
            cc=cc,
            bcc=bcc,
            in_reply_to=in_reply_to,
        )
        with ImapConnection(acc) as conn:
            folder = conn.find_drafts_folder()
            uid = conn.append_draft(folder, raw)
        logger.info("Draft created: account=%s folder=%s uid=%s", account, folder, uid)
        return {
            "account": account,
            "folder": folder,
            "uid": uid,
            "message_id": draft_message_id,
            "to": to,
            "subject": subject,
        }

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

    if config.paperless_url and config.paperless_token:
        paperless = PaperlessClient(config.paperless_url, config.paperless_token)

        @mcp.tool()
        async def send_attachment_to_paperless(
            attachment_id: int,
            title: str | None = None,
            filename: str | None = None,
            created: str | None = None,
            correspondent_id: int | None = None,
            document_type_id: int | None = None,
            tag_ids: list[int] | None = None,
            dry_run: bool = False,
            allow_duplicate: bool = False,
        ) -> dict:
            """Archive an e-mail attachment in Paperless-ngx.

            The index holds only attachment *text*; this re-downloads the
            original file from the IMAP server (read-only) and uploads it
            directly to Paperless — the file never passes through the client.
            Find ``attachment_id`` via ``get_email`` or ``search_attachments``.

            Metadata is optional; Paperless' own matching fills in what is
            left out. ``correspondent_id``/``document_type_id``/``tag_ids`` are
            Paperless ids. ``created`` is an ISO date (YYYY-MM-DD).
            ``filename`` overrides the uploaded file name. With
            ``dry_run=True`` the file is fetched and checked but not uploaded.

            A byte-identical file already in Paperless is not uploaded again
            (status ``DUPLICATE`` with the existing document ids) unless
            ``allow_duplicate=True``.

            Waits briefly for Paperless to consume the file and returns the new
            document id, or the failure reason, or just the task id if
            consumption is still running.
            """
            # IMAP fetch, upload and task polling block for up to a minute:
            # keep them off the event loop so other tool calls stay responsive.
            return await anyio.to_thread.run_sync(
                lambda: _send_to_paperless(
                    attachment_id, title, filename, created, correspondent_id,
                    document_type_id, tag_ids, dry_run, allow_duplicate,
                )
            )

        def _send_to_paperless(
            attachment_id, title, filename, created, correspondent_id,
            document_type_id, tag_ids, dry_run, allow_duplicate,
        ) -> dict:
            att = fetch_attachment(config, db, attachment_id)
            info = {
                "attachment_id": attachment_id,
                "filename": filename or att.filename,
                "mime_type": att.mime_type,
                "size": len(att.data),
                "sha256": att.sha256,
                "source": f"{att.account}/{att.folder}/uid {att.uid}",
            }
            existing = paperless.find_by_checksum(att)
            if existing:
                info["existing_documents"] = existing
            if dry_run:
                return {**info, "status": "DRY_RUN", "uploaded": False}
            if existing and not allow_duplicate:
                return {
                    **info,
                    "status": "DUPLICATE",
                    "uploaded": False,
                    "note": "identical file already archived; pass allow_duplicate=True to upload anyway",
                }
            task_id = paperless.upload(
                att,
                filename=filename,
                title=title,
                created=created,
                correspondent_id=correspondent_id,
                document_type_id=document_type_id,
                tag_ids=tag_ids,
            )
            logger.info("Attachment %s sent to Paperless, task %s", attachment_id, task_id)
            task = paperless.wait_for_task(task_id)
            status = task_status(task)
            result = {**info, "uploaded": True, "task_id": task_id, "status": status}
            if status == "SUCCESS":
                result["document_id"] = task_document_id(task)
            elif status in ("FAILURE", "REVOKED"):
                result["error"] = task_error(task)
            else:
                result["note"] = "still processing in Paperless; check the task later"
            return result

    return mcp
