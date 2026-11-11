"""Extra attachment sources for drafts: a drop folder, URLs and inline content.

``create_draft`` can already attach files from indexed mails and from
Paperless. Files that only exist in a chat, on a phone or on the web have no
way into the server otherwise, because the model never holds their bytes. The
three sources here close that gap — everything is resolved server-side and the
result is the same ``(filename, mime_type, data)`` triple the other sources
produce:

* a **drop folder** (``ATTACHMENT_DIR``, mounted read-only; typically a synced
  cloud folder) that the user fills from any device,
* a **URL** the server downloads (share links of Dropbox are turned into direct
  downloads),
* **inline content** (text or base64) for small things the model writes itself,
  such as an ``.ics`` or ``.csv`` file.
"""

from __future__ import annotations

import base64
import binascii
import ipaddress
import mimetypes
import re
import socket
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit, urlunsplit

import httpx
from pydantic import BaseModel

from .paperless import _disposition_filename, clean_filename

Triple = tuple[str, str | None, bytes]

# The slim container image ships a small mime table; add what mail needs.
for _mime, _ext in (
    ("text/calendar", ".ics"),
    ("text/vcard", ".vcf"),
    ("text/csv", ".csv"),
    ("image/heic", ".heic"),
    ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx"),
    ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
    ("application/vnd.openxmlformats-officedocument.presentationml.presentation", ".pptx"),
):
    mimetypes.add_type(_mime, _ext)

URL_TIMEOUT_SECONDS = 30
MAX_REDIRECTS = 5

# Sync clients and office programs leave housekeeping files next to real ones.
_IGNORED_NAMES = {"desktop.ini", "thumbs.db", ".ds_store"}


class InlineAttachment(BaseModel):
    """A file whose content the caller supplies directly."""

    filename: str
    # Exactly one of the two: plain text (UTF-8) or base64-encoded bytes.
    content: str | None = None
    content_base64: str | None = None
    mime_type: str | None = None


def _size(num_bytes: float) -> str:
    return f"{num_bytes / 1048576:.1f} MB" if num_bytes >= 1048576 else f"{max(num_bytes, 0) / 1024:.0f} KB"


def _guess_mime(filename: str, declared: str | None = None) -> str:
    declared = (declared or "").split(";")[0].strip().lower()
    if declared and declared != "application/octet-stream":
        return declared
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


# --- drop folder ---------------------------------------------------------


def _ignored(path: Path) -> bool:
    name = path.name
    return (
        name.startswith((".", "~$", ".~lock"))
        or name.lower() in _IGNORED_NAMES
        or name.endswith(("~", ".tmp", ".part", ".crdownload"))
    )


def _walk(root: Path):
    """Yield regular, non-hidden files below ``root`` that stay inside it."""
    root_real = root.resolve()
    for path in root.rglob("*"):
        rel_parts = path.relative_to(root).parts
        if any(p.startswith(".") for p in rel_parts) or _ignored(path):
            continue
        try:
            real = path.resolve()
            if path.is_file() and real.is_relative_to(root_real):
                yield path
        except OSError:
            continue


def list_drop_files(root: str, query: str | None = None, limit: int = 100) -> list[dict]:
    """Files in the drop folder, newest first; ``query`` filters by path."""
    base = Path(root)
    if not base.is_dir():
        return []
    needle = unicodedata.normalize("NFC", query or "").casefold()
    rows = []
    for path in _walk(base):
        rel = unicodedata.normalize("NFC", path.relative_to(base).as_posix())
        if needle and needle not in rel.casefold():
            continue
        stat = path.stat()
        rows.append((stat.st_mtime, rel, stat.st_size))
    rows.sort(reverse=True)
    return [
        {
            "path": rel,
            "size": size,
            "mime_type": _guess_mime(rel),
            "modified": datetime.fromtimestamp(mtime, timezone.utc).isoformat(timespec="seconds"),
        }
        for mtime, rel, size in rows[: max(1, min(limit, 500))]
    ]


def read_drop_file(root: str, rel: str, max_bytes: int) -> Triple:
    """Read one file of the drop folder by its relative path.

    The path must resolve to a regular file inside the folder: ``..`` and
    symlinks that leave it are refused. Unicode normalisation differences
    (macOS/Dropbox store umlauts decomposed) are tolerated.
    """
    base = Path(root)
    if not base.is_dir():
        raise ValueError("Attachment folder is not available on the server")
    base_real = base.resolve()
    wanted = unicodedata.normalize("NFC", rel.replace("\\", "/").strip("/"))
    target: Path | None = None
    for path in _walk(base):
        if unicodedata.normalize("NFC", path.relative_to(base).as_posix()) == wanted:
            target = path
            break
    if target is None:
        # Same name in another folder? Offer it instead of just failing.
        hits = [
            p.relative_to(base).as_posix()
            for p in _walk(base)
            if unicodedata.normalize("NFC", p.name).casefold()
            == unicodedata.normalize("NFC", Path(wanted).name).casefold()
        ]
        hint = f" Did you mean: {', '.join(hits[:5])}?" if hits else ""
        raise ValueError(
            f"No file '{rel}' in the attachment folder (see list_attachment_files).{hint}"
        )
    real = target.resolve()
    if not real.is_relative_to(base_real):  # belt and braces; _walk already checks
        raise ValueError(f"'{rel}' is outside the attachment folder")
    size = real.stat().st_size
    if size > max_bytes:
        raise ValueError(
            f"'{rel}' is {_size(size)}, more than the {_size(max_bytes)} left"
        )
    return (clean_filename(target.name), _guess_mime(target.name), real.read_bytes())


# --- URLs ----------------------------------------------------------------


def direct_download_url(url: str) -> str:
    """Turn a Dropbox share link into a direct download; others pass through."""
    parts = urlsplit(url)
    if parts.hostname and parts.hostname.lower() in {"www.dropbox.com", "dropbox.com"}:
        query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in {"dl", "raw"}]
        query.append(("dl", "1"))
        return urlunsplit(parts._replace(query=urlencode(query)))
    return url


def _require_public(url: str) -> None:
    """Refuse anything but http(s) to a public address (no SSRF into the LAN).

    Every redirect hop is checked again. Names that resolve to private,
    loopback, link-local or shared (CGNAT/Tailscale) ranges are rejected.
    """
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("Only http(s) URLs are supported")
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ValueError(f"Cannot resolve host '{parts.hostname}'") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global:
            raise ValueError(f"Refusing to fetch from a non-public address ({parts.hostname})")


def download_url(url: str, max_bytes: int) -> Triple:
    """Download ``url`` server-side: ``(filename, mime_type, data)``."""
    url = direct_download_url(url.strip())
    with httpx.Client(follow_redirects=False, timeout=URL_TIMEOUT_SECONDS) as client:
        for _ in range(MAX_REDIRECTS + 1):
            _require_public(url)
            with client.stream("GET", url, headers={"User-Agent": "imap-2-mcp"}) as r:
                if r.is_redirect and r.headers.get("location"):
                    url = urljoin(url, r.headers["location"])
                    continue
                if r.status_code >= 400:
                    raise ValueError(f"Download of {urlsplit(url).netloc} failed (HTTP {r.status_code})")
                declared = int(r.headers.get("content-length") or 0)
                if declared > max_bytes:
                    raise ValueError(
                        f"File is {_size(declared)}, more than the {_size(max_bytes)} left"
                    )
                data = bytearray()
                for chunk in r.iter_bytes():
                    data += chunk
                    if len(data) > max_bytes:
                        raise ValueError(
                            f"File is larger than the {_size(max_bytes)} left"
                        )
                filename = (
                    _disposition_filename(r.headers.get("content-disposition", ""))
                    or clean_filename(unquote(Path(urlsplit(url).path).name))
                    or "attachment"
                )
                ctype = r.headers.get("content-type", "")
                if ctype.lower().startswith("text/html") and not filename.lower().endswith((".html", ".htm")):
                    raise ValueError(
                        "The URL returned a web page, not a file (login or preview page?). "
                        "Use a direct download link."
                    )
                if "." not in filename and ctype:
                    filename += mimetypes.guess_extension(ctype.split(";")[0].strip()) or ""
                return (filename, _guess_mime(filename, ctype), bytes(data))
    raise ValueError("Too many redirects")


# --- inline content ------------------------------------------------------


def read_inline(spec: InlineAttachment, max_bytes: int) -> Triple:
    filename = clean_filename(spec.filename)
    if not filename:
        raise ValueError("Inline attachment needs a filename")
    if (spec.content is None) == (spec.content_base64 is None):
        raise ValueError(f"Inline attachment '{filename}': pass exactly one of content / content_base64")
    if spec.content is not None:
        data = spec.content.encode("utf-8")
    else:
        try:
            data = base64.b64decode(re.sub(r"\s+", "", spec.content_base64), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(f"Inline attachment '{filename}': invalid base64") from exc
    if len(data) > max_bytes:
        raise ValueError(f"Inline attachment '{filename}' is larger than the {_size(max_bytes)} left")
    return (filename, _guess_mime(filename, spec.mime_type), data)
