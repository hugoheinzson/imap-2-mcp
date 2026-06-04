"""Entrypoint: load config, open the index, start the sync worker, serve MCP."""

from __future__ import annotations

import logging
import sys

import uvicorn

from .config import Config
from .db import Database
from .server import build_app
from .sync import SyncWorker


class _BearerMiddleware:
    def __init__(self, app, token: str) -> None:
        self._app = app
        self._token = token

    def _authorized(self, scope) -> bool:
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        auth = headers.get(b"authorization", b"").decode()
        if auth == f"Bearer {self._token}":
            return True
        # Fallback: token as query parameter (?token=...), for clients that
        # cannot send an Authorization header (e.g. the claude.ai connector).
        from urllib.parse import parse_qs
        qs = parse_qs(scope.get("query_string", b"").decode())
        return self._token in qs.get("token", [])

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] in ("http", "websocket") and not self._authorized(scope):
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"text/plain")]})
            await send({"type": "http.response.body", "body": b"Unauthorized"})
            return
        await self._app(scope, receive, send)


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
    mcp = build_app(config, db, sync)
    logging.getLogger(__name__).info(
        "Serving MCP (streamable HTTP) on %s:%s for %d account(s)%s",
        config.http_host, config.http_port, len(config.accounts),
        " [auth enabled]" if config.api_token else " [WARNING: no auth]",
    )
    asgi_app = mcp.streamable_http_app()
    if config.api_token:
        asgi_app = _BearerMiddleware(asgi_app, config.api_token)
    uvicorn.run(asgi_app, host=config.http_host, port=config.http_port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
