"""Read-only IMAP access built on IMAPClient.

This module never issues writing IMAP commands. Mailboxes are always opened
with ``readonly=True`` (EXAMINE, not SELECT), so flags such as \\Seen are not
altered by reading or syncing.
"""

from __future__ import annotations

import email
from dataclasses import dataclass
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parsedate_to_datetime

from imapclient import IMAPClient

from .config import Account


@dataclass
class Attachment:
    filename: str | None
    mime_type: str | None
    size: int
    data: bytes


@dataclass
class ParsedEmail:
    uid: int
    message_id: str | None
    in_reply_to: str | None
    references: list[str]
    from_addr: str
    to_addr: str
    subject: str
    date: str | None  # ISO 8601
    body: str
    attachments: list[Attachment]


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:  # noqa: BLE001
        return value


def _iso_date(msg: Message) -> str | None:
    raw = msg.get("Date")
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw).isoformat()
    except Exception:  # noqa: BLE001
        return None


def _body_and_attachments(msg: Message) -> tuple[str, list[Attachment]]:
    body_parts: list[str] = []
    attachments: list[Attachment] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        disposition = (part.get("Content-Disposition") or "").lower()
        ctype = part.get_content_type()
        filename = part.get_filename()
        is_attachment = "attachment" in disposition or filename is not None
        if is_attachment:
            payload = part.get_payload(decode=True) or b""
            attachments.append(
                Attachment(
                    filename=_decode(filename) or None,
                    mime_type=ctype,
                    size=len(payload),
                    data=payload,
                )
            )
        elif ctype == "text/plain":
            payload = part.get_payload(decode=True) or b""
            charset = part.get_content_charset() or "utf-8"
            body_parts.append(payload.decode(charset, errors="replace"))
    return ("\n".join(body_parts).strip(), attachments)


def parse_message(uid: int, raw: bytes) -> ParsedEmail:
    msg = email.message_from_bytes(raw)
    body, attachments = _body_and_attachments(msg)
    refs = (msg.get("References") or "").split()
    return ParsedEmail(
        uid=uid,
        message_id=(msg.get("Message-ID") or None),
        in_reply_to=(msg.get("In-Reply-To") or None),
        references=refs,
        from_addr=_decode(msg.get("From")),
        to_addr=_decode(msg.get("To")),
        subject=_decode(msg.get("Subject")),
        date=_iso_date(msg),
        body=body,
        attachments=attachments,
    )


class ImapConnection:
    """Thin read-only wrapper around IMAPClient as a context manager."""

    def __init__(self, account: Account) -> None:
        self.account = account
        self._client: IMAPClient | None = None

    def __enter__(self) -> "ImapConnection":
        self._client = IMAPClient(
            self.account.host, port=self.account.port, ssl=self.account.ssl
        )
        self._client.login(self.account.user, self.account.password)
        return self

    def __exit__(self, *exc) -> None:
        if self._client is not None:
            try:
                self._client.logout()
            except Exception:  # noqa: BLE001
                pass
            self._client = None

    @property
    def client(self) -> IMAPClient:
        assert self._client is not None, "connection not opened"
        return self._client

    def list_folders(self) -> list[str]:
        return [entry[2] for entry in self.client.list_folders()]

    def examine(self, folder: str) -> dict:
        """Open a folder read-only; returns SELECT/EXAMINE response."""
        return self.client.select_folder(folder, readonly=True)

    def search_uids_since(self, last_uid: int) -> list[int]:
        uids = self.client.search(["UID", f"{last_uid + 1}:*"])
        return [u for u in uids if u > last_uid]

    def fetch_raw(self, uid: int) -> bytes | None:
        resp = self.client.fetch([uid], ["RFC822"])
        item = resp.get(uid)
        return item.get(b"RFC822") if item else None
