# imap-2-mcp

MCP server that makes IMAP mailboxes searchable for AI clients such as
Claude. It combines **live IMAP access** with a **local SQLite FTS5 index**
that also covers **attachment contents** (PDF/DOCX/XLSX/text), so full-text
search stays fast even for large mailboxes (50k+ messages).

> Read-only against existing mail: it never sends, deletes, moves, or flags
> anything. The single write operation is `create_draft`, which APPENDs a
> new draft to the drafts folder — sending stays a manual step in your mail
> client (there is no SMTP in this project).

See [`SPEC.md`](./SPEC.md) for the full design and rationale.

## Features (Phase 1)

- Multi-account IMAP (SSL), read-only against existing mail.
- Background sync worker: initial full index + periodic incremental updates.
- Attachment text extraction (PDF, DOCX, XLSX, plain text).
- Draft creation (`create_draft`): store a draft — optionally threaded as a
  reply — in the account's drafts folder; never sends.
- Optional Paperless-ngx hand-off (`send_attachment_to_paperless`): re-fetch
  an attachment from IMAP and upload it straight to Paperless — the file
  never passes through the client. Enabled via `PAPERLESS_URL` +
  `PAPERLESS_API_TOKEN`.
- MCP tools over streamable HTTP (client may run on another machine):
  `list_mailboxes`, `list_recent`, `get_email`, `get_thread`, `search`,
  `search_attachments`, `get_attachment_text`, `create_draft`, `sync_status`
  (+ `send_attachment_to_paperless` when configured).

## Quick start

```bash
cp .env.example .env      # fill in your IMAP account(s) — never commit this file
docker compose up --build
```

The server listens on `:8000` (streamable HTTP). The first sync runs on
startup; large mailboxes take a while to index initially.

To run a one-off index pass without serving:

```bash
docker compose run --rm imap-2-mcp python -m imap2mcp --sync-once
```

## Configuration

All config is via environment variables (see [`.env.example`](./.env.example)).
Declare accounts with `ACCOUNTS=name1,name2` and one `ACCOUNT_<NAME>_*` block
each (`HOST`, `PORT`, `SSL`, `USER`, `PASSWORD`). Secrets live only in your
local `.env`, which is git-ignored.

## Authentication

Set `API_TOKEN` in your `.env` to require a bearer token on every request.
Clients then authenticate either via header or via a query parameter (useful
for connector UIs that cannot set custom headers):

```
Authorization: Bearer <token>
# or
https://<host>/mcp?token=<token>
```

With `API_TOKEN` unset, the endpoint is unauthenticated — keep it on a trusted
network only.

## Connecting Claude

Add the streamable-HTTP endpoint as an MCP server, e.g. for Claude Code:

```bash
claude mcp add --transport http imap http://<home-server-ip>:8000/mcp
```

## Security notes

- Set `API_TOKEN` before exposing the server beyond your local network.
- Without a token, only expose it inside your home network (LAN/VPN such as
  Tailscale). Do not port-forward it to the internet.
- **Phase 2** (public connector) will add full OAuth and a TLS tunnel.

## Status

Phase 1 implementation. Not yet hardened or load-tested against a live mailbox.
