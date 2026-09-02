"""Read-only IMAP access built on IMAPClient.

Mailboxes are always opened with ``readonly=True`` (EXAMINE, not SELECT), so
flags such as \\Seen are not altered by reading or syncing. The single
deliberate exception to "no writing IMAP commands" is ``append_draft``: it
APPENDs a new message to the drafts folder and never modifies or deletes
existing messages (no SELECT in write mode, no STORE/EXPUNGE/COPY).
"""

from __future__ import annotations

import email
import re
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
        disposition = str(part.get("Content-Disposition") or "").lower()
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

    def list_folders_with_flags(self) -> list[tuple[str, set[str]]]:
        """Return ``(name, flags)`` per folder, flags decoded to a str set."""
        out: list[tuple[str, set[str]]] = []
        for flags, _delim, name in self.client.list_folders():
            decoded = {f.decode() if isinstance(f, bytes) else f for f in flags}
            out.append((name, decoded))
        return out

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

    DRAFTS_FALLBACK_NAMES = ("Entwürfe", "Drafts", "INBOX.Drafts", "INBOX/Drafts")

    def find_drafts_folder(self) -> str:
        """Locate the drafts folder via the \\Drafts SPECIAL-USE flag.

        Falls back to well-known folder names if the server does not
        advertise a flagged drafts folder.
        """
        folders = self.client.list_folders()
        for flags, _delim, name in folders:
            if b"\\Drafts" in flags:
                return name
        names = {name for _flags, _delim, name in folders}
        for candidate in self.DRAFTS_FALLBACK_NAMES:
            if candidate in names:
                return candidate
        raise ValueError(
            f"No drafts folder found for account '{self.account.name}'"
        )

    def append_draft(self, folder: str, raw: bytes) -> int | None:
        """Store a message in ``folder`` with the \\Draft flag (IMAP APPEND).

        The single write operation in this module: it only ever adds a new
        message and never touches existing ones. Returns the new UID when
        the server reports it (UIDPLUS APPENDUID), else None.
        """
        resp = self.client.append(folder, raw, flags=[b"\\Draft"])
        match = re.search(rb"APPENDUID \d+ (\d+)", resp or b"")
        return int(match.group(1)) if match else None
