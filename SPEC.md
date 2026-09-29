# imap-2-mcp — Specification

A read-only MCP server that makes IMAP mailboxes searchable and usable for AI
clients such as Claude. It is a hybrid of live IMAP access and a local
full-text index that also covers attachment contents.

## Goal

Make email (including attachment contents) easy to search and usable in Claude
as an MCP connector/tool — data-minimal, self-hosted, and read-only.

## Design decisions

| Aspect             | Decision |
|--------------------|----------|
| Mail server        | Any standard IMAP server over SSL (port 993) |
| Mailboxes          | Multiple (multi-account) |
| Volume             | Designed for large mailboxes (50k+ messages) — search performance is critical |
| Access rights      | **Read-only** — never sends, deletes, moves, or flags |
| Search             | Local **SQLite FTS5 full-text index** over headers + body + extracted attachment text |
| Attachment extract | PDF, DOCX, XLSX (text), plain-text attachments |
| Sync               | **Initial sync + periodic** (default every 15 min, new messages only) |
| Live access        | Folders / recent messages / single message / thread directly via IMAP |
| Transport          | **HTTP/MCP** from Phase 1 (the Claude client may run on a different machine) |
| Stack              | **Python** (imapclient, SQLite FTS5, PyMuPDF/python-docx/openpyxl, MCP Python SDK / FastMCP) |
| Hosting            | Docker on a self-hosted server |
| Privacy            | The index stays local; mail contents never leave the host network |

## Why this approach

- **On-demand IMAP alone is not enough:** IMAP `SEARCH` does not search
  attachment contents, and downloading/extracting attachments live across 50k+
  messages would be too slow for a single query. A local index is required.
- **No RAG/embeddings to start:** An FTS5 full-text index covers keyword and
  phrase search without embedding cost. Semantic search remains an optional
  later extension.
- **HTTP instead of stdio from Phase 1:** The Claude client may run on a
  different machine than the Docker host, so stdio (which requires launching a
  local process) is not a fit. HTTP is also the basis for a later public
  connector.
- **Long-lived process, not serverless:** An MCP server is a long-lived,
  connection-oriented process with IMAP connection pooling and a background
  sync worker — a poor fit for a stateless, short-lived function model.

## Architecture

```
Claude Desktop / claude.ai  ──HTTP/MCP──►  imap-2-mcp (Docker, self-hosted)
                                             │
                         ┌───────────────────┼───────────────────┐
                         ▼                   ▼                   ▼
                   Live IMAP access    SQLite FTS5 index     Sync worker
                   (folders, recent    (headers + body +     (initial + periodic:
                    messages, single    attachment text)     fetch new mail,
                    message/thread)                          extract attachments,
                                                             index)
```

## MCP tools

Live (directly via IMAP):
- `list_mailboxes` — list mailboxes/folders
- `list_recent` — most recent messages in a folder
- `get_email` — a single message by ID/UID incl. body + attachment metadata
- `get_thread` — the conversation/thread of a message

Search (over the FTS5 index):
- `search` — full-text search over headers, body, and attachment text; filters
  for account, folder, sender, and date range
- `search_attachments` — targeted search within attachment contents only
- `get_attachment_text` — retrieve the extracted text of an attachment

Status:
- `sync_status` — last sync time and indexed message count per account

Drafts (the only writing tool):
- `create_draft` — build an RFC 5322 draft (plain text; optionally threaded
  as a reply to an indexed message via `In-Reply-To`/`References`) and store
  it in the account's drafts folder via IMAP APPEND with the `\Draft` flag.
  The drafts folder is detected via the SPECIAL-USE `\Drafts` flag, with a
  fallback to well-known names. Never sends — there is no SMTP.

Archiving (optional, only registered when Paperless is configured):
- `send_attachment_to_paperless` — the index stores attachment text only, so
  the original message is re-fetched via EXAMINE (read-only) at its recorded
  folder/UID — falling back to other indexed copies and a `Message-ID` header
  search if it moved — and the attachment is identified by position,
  filename and size. It is posted server-side to Paperless'
  `post_document` endpoint with optional title, date, correspondent,
  document type and tags; the tool then waits briefly for the consumer task
  and returns the new document id or the failure (e.g. duplicate).
  `dry_run` fetches and checks the file without uploading.

## Data model (SQLite)

- `accounts` — configured mailboxes
- `messages` — UID, account, folder, Message-ID, From/To, subject, date,
  thread key, flags snapshot
- `attachments` — message FK, filename, MIME type, size, extracted text
- `messages_fts` / `attachments_fts` — FTS5 virtual tables for search
- `sync_state` — per account/folder: last seen UID, `UIDVALIDITY`

## Configuration

Via environment variables (no secrets in the repo):
- Per account: `HOST`, `PORT`, `SSL`, `USER`, `PASSWORD`, and optional `FROM`
  (From header for drafts, e.g. `Jane Doe <jane@example.org>`; defaults to
  `USER`)
- `SYNC_INTERVAL` (default 900s), `INDEX_ATTACHMENTS` (true)
- `HTTP_HOST`/`HTTP_PORT` (Phase 1: bind to the local network only)
- Attachment size limit, excluded folders (e.g. Spam/Trash)
- `API_TOKEN` (optional) — when set, requests must present a matching bearer
  token (see Security)
- `PAPERLESS_URL` + `PAPERLESS_API_TOKEN` (optional) — enable
  `send_attachment_to_paperless`

## Phases

### Phase 1 — Local read-only server (home network)
1. Docker service, HTTP/MCP transport, reachable on the local network.
2. Read-only IMAP connect to the configured accounts.
3. Live tools: `list_mailboxes`, `list_recent`, `get_email`, `get_thread`.
4. Sync worker: initial index + periodic; attachment extraction (PDF/DOCX/XLSX).
5. Search tools over FTS5: `search`, `search_attachments`, `get_attachment_text`.
6. Optional bearer-token auth (`API_TOKEN`) for exposure beyond the LAN.

### Phase 2 — Public connector
7. OAuth 2.1 in front of the HTTP endpoint.
8. TLS tunnel → usable as a custom connector in claude.ai.

## Security

- **Read-only against existing mail** — no deleting, moving, or flag changes,
  and no SMTP. The single write operation is the IMAP APPEND behind
  `create_draft`, which only ever adds a new message to the drafts folder.
- Credentials live only in the local environment, never in the repo.
- Without `API_TOKEN` the endpoint is unauthenticated and must stay on a
  trusted network; set `API_TOKEN` before exposing it more widely.
- Index and data remain on the host.
