"""Optional hand-off of mail attachments to a Paperless-ngx instance.

The index only keeps the *text* of attachments, never the files. To archive
one, the original message is re-fetched from IMAP on demand (read-only
EXAMINE, exactly like the sync), the attachment is cut out of it and posted
server-side to Paperless' ``post_document`` endpoint. The file therefore never
passes through the MCP client or the model's context.

Enabled only when ``PAPERLESS_URL`` and ``PAPERLESS_API_TOKEN`` are set.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass

import httpx

from .config import Config
from .db import Database
from .imap_client import Attachment, ImapConnection, parse_message

logger = logging.getLogger(__name__)

# How long to wait for Paperless to consume the upload before returning the
# task id only. Consumption (OCR) of a small PDF usually takes a few seconds.
TASK_WAIT_SECONDS = 45


@dataclass
class FetchedAttachment:
    filename: str
    mime_type: str | None
    data: bytes
    account: str
    folder: str
    uid: int

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()

    @property
    def md5(self) -> str:
        return hashlib.md5(self.data).hexdigest()


def clean_filename(name: str) -> str:
    """Unfold header line breaks and strip path separators from a filename.

    Long RFC 2231/2047 filenames are often folded (a CRLF plus indent in the
    middle of the name), which would otherwise end up verbatim in the archive.
    """
    name = re.sub(r"\s*[\r\n]+\s*", " ", name)
    name = re.sub(r"[/\\]", "_", name)
    return re.sub(r" {2,}", " ", name).strip()


def _pick(attachments: list[Attachment], ordinal: int, filename: str | None,
          size: int | None) -> Attachment | None:
    """Find the indexed attachment in a freshly parsed message.

    Attachments are stored in parse order, so the ordinal normally matches;
    filename + size are checked so a different MIME layout can't hand over
    the wrong file.
    """
    def same(att: Attachment) -> bool:
        return att.filename == filename and (size is None or att.size == size)

    if 0 <= ordinal < len(attachments) and same(attachments[ordinal]):
        return attachments[ordinal]
    matches = [a for a in attachments if same(a)]
    return matches[0] if len(matches) == 1 else None


def fetch_attachment(config: Config, db: Database, attachment_id: int) -> FetchedAttachment:
    """Re-download one indexed attachment from the IMAP server."""
    with db.connect() as conn:
        row = conn.execute(
            """SELECT a.id, a.message_id AS msg_row, a.filename, a.mime_type, a.size,
                      m.account, m.folder, m.uid, m.message_id
               FROM attachments a JOIN messages m ON m.id = a.message_id
               WHERE a.id = ?""",
            (attachment_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"No indexed attachment with id {attachment_id}")
        ordinal = conn.execute(
            "SELECT COUNT(*) FROM attachments WHERE message_id = ? AND id < ?",
            (row["msg_row"], attachment_id),
        ).fetchone()[0]
        # The same message may be indexed in other folders too (e.g. moved to
        # an archive after indexing): those copies are fallback locations.
        candidates = [(row["folder"], row["uid"])]
        if row["message_id"]:
            candidates += [
                (r["folder"], r["uid"])
                for r in conn.execute(
                    """SELECT folder, uid FROM messages
                       WHERE account = ? AND message_id = ? AND id != ?
                       ORDER BY date DESC""",
                    (row["account"], row["message_id"], row["msg_row"]),
                )
            ]

    acc = config.account(row["account"])
    if acc is None:
        raise ValueError(f"Account '{row['account']}' is no longer configured")

    with ImapConnection(acc) as imap:
        for folder, uid in candidates:
            try:
                imap.examine(folder)
            except Exception:  # noqa: BLE001 - folder renamed/deleted
                continue
            raw = imap.fetch_raw(uid)
            if raw is None and row["message_id"]:
                # UID gone (moved/expunged or UIDVALIDITY reset): look the
                # message up by its Message-ID header in the same folder.
                found = imap.client.search(["HEADER", "Message-ID", row["message_id"]])
                if found:
                    uid = found[0]
                    raw = imap.fetch_raw(uid)
            if raw is None:
                continue
            parsed = parse_message(uid, raw)
            if row["message_id"] and parsed.message_id != row["message_id"]:
                continue  # UID now points at a different message
            att = _pick(parsed.attachments, ordinal, row["filename"], row["size"])
            if att is None:
                continue
            return FetchedAttachment(
                filename=clean_filename(att.filename or f"attachment-{attachment_id}"),
                mime_type=att.mime_type,
                data=att.data,
                account=row["account"],
                folder=folder,
                uid=uid,
            )
    raise ValueError(
        f"Attachment {attachment_id} ('{row['filename']}') could not be found on the "
        f"IMAP server anymore (message deleted or moved to an unindexed folder)"
    )


class PaperlessClient:
    def __init__(self, url: str, token: str) -> None:
        self.url = url.rstrip("/")
        self.headers = {"Authorization": f"Token {token}", "Accept": "application/json"}

    def find_by_checksum(self, att: FetchedAttachment) -> list[dict]:
        """Existing documents whose original file is byte-identical.

        Paperless 3.x stores SHA-256, 2.x MD5; both are asked. Since 3.x no
        longer rejects duplicates on upload, this is the only guard against
        archiving the same file twice.
        """
        found: dict[int, dict] = {}
        for checksum in (att.sha256, att.md5):
            r = httpx.get(
                f"{self.url}/api/documents/",
                params={"checksum__iexact": checksum, "fields": "id,title,created"},
                headers=self.headers, timeout=15,
            )
            r.raise_for_status()
            for doc in r.json().get("results", []):
                found[doc["id"]] = doc
        return sorted(found.values(), key=lambda d: d["id"])

    def upload(
        self,
        att: FetchedAttachment,
        *,
        filename: str | None = None,
        title: str | None = None,
        created: str | None = None,
        correspondent_id: int | None = None,
        document_type_id: int | None = None,
        tag_ids: list[int] | None = None,
    ) -> str:
        form: dict[str, str | list[str]] = {}
        if title:
            form["title"] = title
        if created:
            form["created"] = created
        if correspondent_id is not None:
            form["correspondent"] = str(correspondent_id)
        if document_type_id is not None:
            form["document_type"] = str(document_type_id)
        if tag_ids:
            form["tags"] = [str(t) for t in tag_ids]
        files = {
            "document": (
                clean_filename(filename) if filename else att.filename,
                att.data,
                att.mime_type or "application/octet-stream",
            )
        }
        r = httpx.post(
            f"{self.url}/api/documents/post_document/",
            headers=self.headers, data=form, files=files, timeout=60,
        )
        if r.status_code >= 400:
            raise ValueError(f"Paperless rejected the upload ({r.status_code}): {r.text[:500]}")
        return r.text.strip().strip('"')

    def wait_for_task(self, task_id: str, timeout: float = TASK_WAIT_SECONDS) -> dict:
        """Poll the consumer task; returns the final task or the last seen state."""
        deadline = time.monotonic() + timeout
        task: dict = {"task_id": task_id, "status": "PENDING"}
        while time.monotonic() < deadline:
            r = httpx.get(
                f"{self.url}/api/tasks/", params={"task_id": task_id},
                headers=self.headers, timeout=15,
            )
            r.raise_for_status()
            results = r.json()
            if isinstance(results, dict):  # paginated variant
                results = results.get("results", [])
            if results:
                task = results[0]
                if task_status(task) in ("SUCCESS", "FAILURE", "REVOKED"):
                    return task
            time.sleep(2)
        return task


# Paperless 2.x reports "SUCCESS" plus related_document/result; 3.x reports
# "success" plus result_data/related_document_ids. These helpers read both.

def task_status(task: dict) -> str:
    return str(task.get("status") or "PENDING").upper()


def task_document_id(task: dict) -> int | None:
    data = task.get("result_data") or {}
    if isinstance(data, dict) and data.get("document_id"):
        return int(data["document_id"])
    ids = task.get("related_document_ids") or []
    if ids:
        return int(ids[0])
    related = task.get("related_document")
    return int(related) if related else None


def task_error(task: dict) -> str | None:
    data = task.get("result_data")
    if isinstance(data, dict):
        for key in ("error", "message", "detail"):
            if data.get(key):
                return str(data[key])
    return task.get("result") or (str(data) if data else None)
