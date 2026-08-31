"""Compose RFC 5322 draft messages.

Builds plain-text drafts (optionally threaded as replies onto an existing
message) for storage via IMAP APPEND. Nothing in this module can send mail;
there is no SMTP anywhere in this project.
"""

from __future__ import annotations

from email.message import EmailMessage
from email.utils import formatdate, make_msgid


def build_draft(
    from_addr: str,
    to: str,
    subject: str,
    body: str,
    cc: str | None = None,
    bcc: str | None = None,
    in_reply_to: str | None = None,
    references: str | None = None,
) -> tuple[bytes, str]:
    """Return the raw draft message and its generated Message-ID."""
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    if bcc:
        msg["Bcc"] = bcc
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    message_id = make_msgid()
    msg["Message-ID"] = message_id
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = references or in_reply_to
    msg.set_content(body)
    return msg.as_bytes(), message_id
