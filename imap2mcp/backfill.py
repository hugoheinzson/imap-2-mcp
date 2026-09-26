"""Re-parse messages whose indexed body is empty and update them in place.

Older index versions only read text/plain parts, so HTML-only mails were
stored without a body. Run once after upgrading:

    python -m imap2mcp.backfill

Only the ``body`` column is updated; the ``messages_au`` trigger keeps the
FTS index in sync. IMAP access stays read-only (EXAMINE + FETCH).
"""

from __future__ import annotations

import logging
from collections import defaultdict

from .config import Config
from .db import Database
from .imap_client import ImapConnection, parse_message

logger = logging.getLogger(__name__)

BATCH = 200


def backfill(config: Config, db: Database) -> int:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, account, folder, uid FROM messages"
            " WHERE body IS NULL OR trim(body) = ''"
        ).fetchall()
    todo: dict[str, dict[str, dict[int, int]]] = defaultdict(lambda: defaultdict(dict))
    for row in rows:
        todo[row["account"]][row["folder"]][row["uid"]] = row["id"]
    logger.info("%d messages with empty body", len(rows))

    updated = 0
    for account_name, folders in todo.items():
        account = config.account(account_name)
        if account is None:
            logger.warning("Account %s no longer configured, skipping", account_name)
            continue
        with ImapConnection(account) as imap:
            for folder, uid_map in folders.items():
                try:
                    imap.examine(folder)
                except Exception:  # noqa: BLE001
                    logger.warning("Cannot open %s/%s, skipping", account_name, folder)
                    continue
                uids = sorted(uid_map)
                for i in range(0, len(uids), BATCH):
                    chunk = uids[i : i + BATCH]
                    resp = imap.client.fetch(chunk, ["RFC822"])
                    with db.connect() as conn:
                        for uid, item in resp.items():
                            raw = item.get(b"RFC822")
                            if raw is None or uid not in uid_map:
                                continue
                            try:
                                body = parse_message(uid, raw).body
                            except Exception:  # noqa: BLE001
                                logger.warning("Parse failed for UID %d", uid, exc_info=True)
                                continue
                            if body:
                                conn.execute(
                                    "UPDATE messages SET body=? WHERE id=?",
                                    (body, uid_map[uid]),
                                )
                                updated += 1
                        conn.commit()
                logger.info("%s/%s: done (%d updated so far)", account_name, folder, updated)
    return updated


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = Config.from_env()
    n = backfill(config, Database(config.db_path))
    logger.info("Backfill finished: %d bodies filled", n)


if __name__ == "__main__":
    main()
