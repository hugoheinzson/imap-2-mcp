# imap-2-mcp — Spezifikation

Read-only MCP-Server, der IMAP-Postfächer (all-inkl./Kasserver) für KI-Clients
wie Claude durchsuch- und nutzbar macht. Hybrid aus Live-IMAP-Zugriff und einem
lokalen Volltext-Index inkl. Anhang-Inhalten.

## Ziel

Mails (inkl. Anhang-Inhalten) leicht durchsuchbar und in Claude als
MCP-Connector/Tool nutzbar machen — datensparsam, self-hosted, read-only.

## Entscheidungen (Stand der Anforderungsklärung)

| Aspekt            | Entscheidung |
|-------------------|--------------|
| Mailserver        | all-inkl. (Kasserver), IMAP über SSL (Port 993) |
| Postfächer        | 3 (Multi-Account) |
| Volumen           | > 50.000 Mails → Such-Performance ist kritisch |
| Zugriffsrechte    | **read-only** — kein Senden, Löschen, Verschieben, Markieren |
| Suche             | Lokaler **SQLite-FTS5-Volltextindex** über Header + Body + extrahierten Anhang-Text |
| Anhang-Extraktion | PDF, DOCX, XLSX (Text), Klartext-Anhänge |
| Sync              | **Initial-Sync + periodisch** (Default alle 15 Min, nur neue Mails) |
| Live-Zugriff      | Ordner/aktuelle Mails/Einzelmail/Thread direkt per IMAP |
| Transport         | **HTTP/MCP** ab Phase 1 (Claude-Client läuft auf anderem Rechner) |
| Stack             | **Python** (imapclient, SQLite FTS5, PyMuPDF/python-docx/openpyxl, MCP-Python-SDK / FastMCP) |
| Hosting           | Docker auf eigenem Heim-Server ("Rechenknecht") |
| Datenschutz       | Index liegt lokal; Mail-Inhalte verlassen das Heimnetz nicht |

## Warum dieser Ansatz

- **On-demand IMAP allein reicht nicht:** IMAP `SEARCH` durchsucht keine
  Anhang-Inhalte, und das Live-Herunterladen/Extrahieren von Anhängen über
  > 50k Mails wäre für eine Suchanfrage zu langsam. → Lokaler Index nötig.
- **Kein RAG/Embeddings zum Start:** Ein FTS5-Volltextindex deckt
  Schlagwort-/Phrasensuche ab, ohne Embedding-Kosten. Semantische Suche bleibt
  optionale spätere Erweiterung.
- **HTTP statt stdio ab Phase 1:** Der Claude-Client läuft auf einem anderen
  Rechner als der Docker-Host; stdio würde lokalen Prozess-Start erfordern.
  HTTP ist zugleich die Basis für den späteren Public-Connector.
- **Lambda verworfen:** MCP-Server sind langlebige, verbindungsorientierte
  Prozesse mit IMAP-Connection-Pooling und Hintergrund-Sync — passt nicht zum
  zustandslosen, kurzlebigen Lambda-Modell.

## Architektur

```
Claude Desktop / claude.ai  ──HTTP/MCP──►  imap-2-mcp (Docker, Heim-Server)
                                             │
                         ┌───────────────────┼───────────────────┐
                         ▼                   ▼                   ▼
                 Live-IMAP-Zugriff    SQLite-FTS5-Index    Sync-Worker
                 (Ordner, aktuelle    (Header + Body +     (initial + periodisch:
                  Mails, Einzelmail/   Anhang-Text)         neue Mails holen,
                  Thread lesen)                             Anhänge extrahieren,
                                                            indexieren)
```

## MCP-Tools (read-only)

Live (direkt per IMAP):
- `list_mailboxes` — Postfächer/Ordner auflisten
- `list_recent` — neueste Mails eines Ordners
- `get_email` — einzelne Mail per ID/UID inkl. Body + Anhang-Metadaten
- `get_thread` — Konversation/Thread zu einer Mail

Suche (über FTS5-Index):
- `search` — Volltextsuche über Header, Body und Anhang-Text; Filter für
  Account, Ordner, Absender, Zeitraum
- `search_attachments` — gezielte Suche nur in Anhang-Inhalten
- `get_attachment_text` — extrahierten Text eines Anhangs abrufen

Status:
- `sync_status` — letzter Sync, indexierte Mailanzahl pro Account

## Datenmodell (SQLite)

- `accounts` — konfigurierte Postfächer
- `messages` — UID, Account, Ordner, Message-ID, Von/An, Betreff, Datum,
  Thread-Key, Flags-Snapshot
- `attachments` — Message-FK, Dateiname, MIME-Typ, Größe, extrahierter Text
- `messages_fts` / `attachments_fts` — FTS5-Virtual-Tables für die Suche
- `sync_state` — pro Account/Ordner letzter gesehener UID, `UIDVALIDITY`

## Konfiguration

Per ENV / Config-Datei (kein Secret im Repo):
- Pro Account: `HOST`, `PORT`, `SSL`, `USER`, `PASSWORD`
- `SYNC_INTERVAL` (Default 900s), `INDEX_ATTACHMENTS` (true)
- `HTTP_HOST`/`HTTP_PORT`, `BIND` (Phase 1: nur Heimnetz)
- Anhang-Größenlimit, ausgeschlossene Ordner (z. B. Spam/Trash optional)

## Phasen

### Phase 1 — Lokaler Read-Only-Server (Heimnetz)
1. Docker-Service, HTTP/MCP-Transport, nur im Heimnetz erreichbar (noch keine
   Auth/keine öffentliche Exposition).
2. IMAP-Connect (read-only) zu den 3 Accounts.
3. Live-Tools: `list_mailboxes`, `list_recent`, `get_email`, `get_thread`.
4. Sync-Worker: Initial-Index + periodisch; Anhang-Extraktion (PDF/DOCX/XLSX).
5. Suchtools über FTS5: `search`, `search_attachments`, `get_attachment_text`.
6. Einbindung & Test in Claude Desktop.

### Phase 2 — Public Connector
7. OAuth 2.1 / Token-Auth vor dem HTTP-Endpoint.
8. Cloudflare Tunnel + TLS → als Custom Connector in claude.ai.

## Sicherheit

- Strikt **read-only** gegen IMAP — keine schreibenden IMAP/SMTP-Operationen.
- Credentials nur lokal (ENV/Secret), niemals im Repo.
- Phase 1 bindet nur ans Heimnetz; öffentliche Erreichbarkeit erst mit Auth.
- Index/Daten verbleiben auf dem Heim-Server.

## Vergleichbare Projekte (Referenzen)

- [codefuturist/email-mcp](https://github.com/codefuturist/email-mcp) — umfangreich, Docker, saubere Read/Write-Trennung (Live-IMAP, kein FTS-Index)
- [yunfeizhu/mcp-mail-server](https://github.com/yunfeizhu/mcp-mail-server) — schlank, TypeScript
- [nikolausm/imap-mcp-server](https://github.com/nikolausm/imap-mcp-server) — Account-Mgmt, Verschlüsselung
- [dominik1001/imap-mcp](https://github.com/dominik1001/imap-mcp) — schlank, gut dokumentiert

Kein vorhandenes Projekt deckt den lokalen FTS-Index mit Anhang-Extraktion ab —
dieser Teil ist Eigenbau.
