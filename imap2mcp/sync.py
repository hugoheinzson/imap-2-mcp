"""Sync worker: initial full index plus periodic incremental updates.

For each account/folder it fetches messages with a UID greater than the last
seen one, parses them, extracts attachment text, and writes everything into the
local SQLite/FTS index. Reading is strictly read-only on the IMAP side.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from .config import Account, Config
from .db import Database
from .extract import extract_text
from .imap_client import ImapConnection, ParsedEmail, parse_message

logger = logging.getLogger(__name__)


def _thread_key(parsed: ParsedEmail) -> str | None:
    """Group by the root of the reference chain, falling back to Message-ID."""
    if parsed.references:
        return parsed.references[0]
    if parsed.in_reply_to:
        return parsed.in_reply_to
    return parsed.message_id


class SyncWorker:
    def __init__(self, config: Config, db: Database) -> None:
        self.config = config
        self.db = db
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="sync", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.sync_once()
            except Exception:  # noqa: BLE001
                logger.exception("Sync run failed")
            self._stop.wait(self.config.sync_interval)

    def sync_once(self) -> None:
        for account in self.config.accounts:
            try:
                self._sync_account(account)
            except Exception:  # noqa: BLE001
                logger.exception("Sync failed for account %s", account.name)

    def _sync_account(self, account: Account) -> None:
        with ImapConnection(account) as conn:
            for folder in conn.list_folders():
                if folder in self.config.excluded_folders:
                    continue
                self._sync_folder(conn, account, folder)

    def _sync_folder(self, conn: ImapConnection, account: Account, folder: str) -> None:
        meta = conn.examine(folder)
        uidvalidity = meta.get(b"UIDVALIDITY")
        last_uid, stored_validity = self._sync_state(account.name, folder)

        # If UIDVALIDITY changed, the server reassigned UIDs: re-index from scratch.
        if stored_validity is not None and uidvalidity != stored_validity:
            logger.warning(
                "UIDVALIDITY changed for %s/%s; re-indexing", account.name, folder
            )
            self._purge_folder(account.name, folder)
            last_uid = 0

        new_uids = conn.search_uids_since(last_uid)
        if not new_uids:
            self._touch_sync_state(account.name, folder, last_uid, uidvalidity)
            return

        logger.info("Indexing %d new messages in %s/%s", len(new_uids), account.name, folder)
        max_uid = last_uid
        for uid in new_uids:
            raw = conn.fetch_raw(uid)
            if raw is None:
                continue
            parsed = parse_message(uid, raw)
            self._store_message(account.name, folder, parsed)
            max_uid = max(max_uid, uid)
        self._touch_sync_state(account.name, folder, max_uid, uidvalidity)

    # --- DB helpers -----------------------------------------------------

    def _sync_state(self, account: str, folder: str) -> tuple[int, int | None]:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT last_uid, uidvalidity FROM sync_state WHERE account=? AND folder=?",
                (account, folder),
            ).fetchone()
        if row is None:
            return 0, None
        return row["last_uid"], row["uidvalidity"]

    def _touch_sync_state(self, account: str, folder: str, last_uid: int, uidvalidity) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.db.connect() as conn:
            conn.execute(
                """INSERT INTO sync_state(account, folder, last_uid, uidvalidity, last_sync)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(account, folder) DO UPDATE SET
                     last_uid=excluded.last_uid,
                     uidvalidity=excluded.uidvalidity,
                     last_sync=excluded.last_sync""",
                (account, folder, last_uid, uidvalidity, now),
            )
            conn.commit()

    def _purge_folder(self, account: str, folder: str) -> None:
        with self.db.connect() as conn:
            conn.execute(
                "DELETE FROM messages WHERE account=? AND folder=?", (account, folder)
            )
            conn.commit()

    def _store_message(self, account: str, folder: str, parsed: ParsedEmail) -> None:
        with self.db.connect() as conn:
            cur = conn.execute(
                """INSERT OR IGNORE INTO messages
                   (account, folder, uid, message_id, thread_key, from_addr,
                    to_addr, subject, date, body, has_attach)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    account, folder, parsed.uid, parsed.message_id,
                    _thread_key(parsed), parsed.from_addr, parsed.to_addr,
                    parsed.subject, parsed.date, parsed.body,
                    1 if parsed.attachments else 0,
                ),
            )
            if cur.lastrowid and parsed.attachments:
                self._store_attachments(conn, cur.lastrowid, parsed)
            conn.commit()

    def _store_attachments(self, conn, message_row_id: int, parsed: ParsedEmail) -> None:
        for att in parsed.attachments:
            text = None
            if (
                self.config.index_attachments
                and 0 < att.size <= self.config.max_attachment_bytes
            ):
                text = extract_text(att.filename, att.mime_type, att.data)
            conn.execute(
                """INSERT INTO attachments(message_id, filename, mime_type, size, text)
                   VALUES (?,?,?,?,?)""",
                (message_row_id, att.filename, att.mime_type, att.size, text),
            )
