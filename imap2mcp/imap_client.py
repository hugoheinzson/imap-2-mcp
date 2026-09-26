"""Read-only IMAP access built on IMAPClient.

Mailboxes are always opened with ``readonly=True`` (EXAMINE, not SELECT), so
flags such as \\Seen are not altered by reading or syncing. The single
deliberate exception to "no writing IMAP commands" is ``append_draft``: it
APPENDs a new message to the drafts folder and never modifies or deletes
existing messages (no SELECT in write mode, no STORE/EXPUNGE/COPY).
"""

from __future__ import annotations

import email
import html
import re
from dataclasses import dataclass
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

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


class _HtmlText(HTMLParser):
    """Collect visible text from HTML, dropping script/style/head content."""

    _SKIP = {"script", "style", "head", "title"}
    _BREAK = {"br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in self._BREAK:
            self.parts.append("\n")

    def handle_endtag(self, tag) -> None:
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag in self._BREAK or tag == "td":
            self.parts.append("\n" if tag in self._BREAK else " ")

    def handle_data(self, data) -> None:
        if not self._skip_depth:
            self.parts.append(data)


def html_to_text(markup: str) -> str:
    parser = _HtmlText()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:  # noqa: BLE001 - malformed HTML degrades to tag stripping
        return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", markup)).split())
    text = "".join(parser.parts).replace("\u00a0", " ").replace("\u200c", "")
    lines = (" ".join(line.split()) for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def _decode_text(part: Message) -> str:
    payload = part.get_payload(decode=True) or b""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:  # unknown charset label
        return payload.decode("utf-8", errors="replace")


def _body_and_attachments(msg: Message) -> tuple[str, list[Attachment]]:
    """Plain-text body, falling back to text extracted from HTML parts.

    Many senders (Apple, PayPal, …) ship HTML only or an empty text/plain
    alternative, so the HTML is used whenever no usable plain text exists.
    """
    body_parts: list[str] = []
    html_parts: list[str] = []
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
            body_parts.append(_decode_text(part))
        elif ctype == "text/html":
            html_parts.append(_decode_text(part))
    body = "\n".join(body_parts).strip()
    if not body and html_parts:
        body = "\n".join(html_to_text(h) for h in html_parts).strip()
    return (body, attachments)


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
