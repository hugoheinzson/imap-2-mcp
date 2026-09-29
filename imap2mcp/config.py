"""Configuration loaded from environment variables.

Accounts are declared via ``ACCOUNTS=name1,name2`` and one
``ACCOUNT_<NAME>_<FIELD>`` block per account.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Account:
    name: str
    host: str
    port: int
    ssl: bool
    user: str
    password: str
    # From header for drafts, e.g. "Jane Doe <jane@example.org>".
    # Falls back to the login user (which must then be an address).
    from_addr: str | None = None
    # Optional include-list of folders to sync for this account. When empty,
    # all selectable folders (minus global EXCLUDED_FOLDERS) are indexed.
    # Entries starting with "\" are matched against SPECIAL-USE flags rather
    # than names, e.g. "\All" selects Gmail's All-Mail folder regardless of
    # its localized name ("[Gmail]/All Mail" vs "[Gmail]/Alle Nachrichten").
    folders: tuple[str, ...] = ()


@dataclass(frozen=True)
class Config:
    http_host: str
    http_port: int
    db_path: str
    sync_interval: int
    index_attachments: bool
    max_attachment_bytes: int
    excluded_folders: tuple[str, ...]
    accounts: tuple[Account, ...] = field(default_factory=tuple)
    api_token: str | None = None
    # Optional Paperless-ngx hand-off (send_attachment_to_paperless). The
    # tool is only registered when both are set.
    paperless_url: str | None = None
    paperless_token: str | None = None

    @staticmethod
    def from_env() -> "Config":
        names = [
            n.strip()
            for n in os.environ.get("ACCOUNTS", "").split(",")
            if n.strip()
        ]
        accounts: list[Account] = []
        for name in names:
            prefix = f"ACCOUNT_{name.upper()}_"
            host = os.environ.get(prefix + "HOST")
            user = os.environ.get(prefix + "USER")
            password = os.environ.get(prefix + "PASSWORD")
            if not (host and user and password):
                raise ValueError(
                    f"Account '{name}' is missing HOST/USER/PASSWORD "
                    f"(expected {prefix}HOST etc.)"
                )
            accounts.append(
                Account(
                    name=name,
                    host=host,
                    port=int(os.environ.get(prefix + "PORT", "993")),
                    ssl=_bool(os.environ.get(prefix + "SSL"), True),
                    user=user,
                    password=password,
                    from_addr=os.environ.get(prefix + "FROM") or None,
                    folders=tuple(
                        f.strip()
                        for f in os.environ.get(prefix + "FOLDERS", "").split(",")
                        if f.strip()
                    ),
                )
            )
        if not accounts:
            raise ValueError("No accounts configured. Set ACCOUNTS=... in the environment.")

        excluded = tuple(
            f.strip()
            for f in os.environ.get("EXCLUDED_FOLDERS", "Trash,Spam,Junk").split(",")
            if f.strip()
        )
        return Config(
            http_host=os.environ.get("HTTP_HOST", "0.0.0.0"),
            http_port=int(os.environ.get("HTTP_PORT", "8000")),
            db_path=os.environ.get("DB_PATH", "./data/index.db"),
            sync_interval=int(os.environ.get("SYNC_INTERVAL", "900")),
            index_attachments=_bool(os.environ.get("INDEX_ATTACHMENTS"), True),
            max_attachment_bytes=int(os.environ.get("MAX_ATTACHMENT_BYTES", str(25 * 1024 * 1024))),
            excluded_folders=excluded,
            accounts=tuple(accounts),
            api_token=os.environ.get("API_TOKEN") or None,
            paperless_url=os.environ.get("PAPERLESS_URL") or None,
            paperless_token=os.environ.get("PAPERLESS_API_TOKEN") or None,
        )

    def account(self, name: str) -> Account | None:
        for acc in self.accounts:
            if acc.name == name:
                return acc
        return None
