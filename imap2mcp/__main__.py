"""Entrypoint: load config, open the index, start the sync worker, serve MCP."""

from __future__ import annotations

import logging
import sys

from .config import Config
from .db import Database
from .server import build_app
from .sync import SyncWorker


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = Config.from_env()
    db = Database(config.db_path)
    sync = SyncWorker(config, db)

    if "--sync-once" in sys.argv:
        sync.sync_once()
        return 0

    sync.start()
    app = build_app(config, db, sync)
    logging.getLogger(__name__).info(
        "Serving MCP (streamable HTTP) on %s:%s for %d account(s)",
        config.http_host, config.http_port, len(config.accounts),
    )
    app.run(transport="streamable-http")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
