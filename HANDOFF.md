# Handoff — Weiterarbeit auf dem Rechenknecht

Kurz-Übergabe, um in einer **neuen Session auf dem Heim-Server** weiterzumachen.

## Was das Projekt ist
Read-only MCP-Server, der IMAP-Postfächer (all-inkl./Kasserver) für Claude
durchsuchbar macht. Hybrid: Live-IMAP + lokaler SQLite-FTS5-Volltextindex
inkl. extrahierter Anhang-Inhalte (PDF/DOCX/XLSX/Text). Details: `SPEC.md`.

## Stand
- **Phase 1 ist implementiert und im Repo** (Branch `claude/eloquent-feynman-4uK17`):
  Config, IMAP-Client (read-only), FTS5-Index, Anhang-Extraktion, Sync-Worker,
  MCP-Server (HTTP) mit Tools, Dockerfile + docker-compose, README.
- **Noch NICHT** gegen ein echtes Postfach getestet (keine echten Zugangsdaten
  im Cloud-Container; bewusst, da Repo public ist).

## Getroffene Entscheidungen
- Mailserver: all-inkl./Kasserver, IMAP SSL (993); **4 Accounts**; **>50k Mails**.
- **read-only** (kein Senden/Löschen/Verschieben/Markieren).
- Suche: lokaler **FTS5-Volltextindex** über Header+Body+Anhang-Text (kein RAG/Embeddings).
- Sync: **Initial + periodisch** (Default 15 Min).
- Transport: **HTTP/MCP** schon ab Phase 1 (Client läuft auf anderem Rechner).
- Stack: **Python**; Hosting: **Docker** auf dem Rechenknecht.
- Lambda wurde verworfen (langlebiger, verbindungsorientierter Dienst).

## Nächste Schritte auf dem Rechenknecht
1. Voraussetzungen: `docker --version && docker compose version`, `git --version`.
2. Repo holen:
   ```bash
   git clone https://github.com/hugoheinzson/imap-2-mcp.git
   cd imap-2-mcp
   git checkout claude/eloquent-feynman-4uK17
   ```
3. Zugangsdaten (bleiben lokal, .env ist git-ignored):
   ```bash
   cp .env.example .env && nano .env
   ```
   `ACCOUNTS=...` + pro Account `ACCOUNT_<NAME>_HOST/PORT/SSL/USER/PASSWORD`.
   Tipp: für den ersten Test nur **einen** Account, große Archiv-Ordner via
   `EXCLUDED_FOLDERS` ausschließen (Initial-Sync über 50k dauert sonst lange).
4. Testlauf, dann Dienst:
   ```bash
   docker compose run --rm imap-2-mcp python -m imap2mcp --sync-once   # IMAP/Extraktion prüfen
   docker compose up -d --build
   docker compose logs -f
   ```
5. Claude anbinden (vom Client-Rechner):
   ```bash
   claude mcp add --transport http imap http://<rechenknecht-ip>:8000/mcp
   ```

## Bekannte offene Punkte / To-dos
- Live-Test gegen echtes Postfach; Initial-Sync-Dauer bei 50k+ beobachten.
- Performance-Tuning Sync (Batch-Fetch statt UID-einzeln, falls zu langsam).
- **Phase 2 (public Connector):** OAuth/Token-Auth + TLS-Tunnel (Cloudflare/Tailscale),
  dann als Custom Connector in claude.ai.
- Sicherheit: `index.db` enthält Mail-/Anhang-Inhalte → so schützenswert wie das
  Postfach; bleibt im Docker-Volume auf dem Server. Phase 1 NICHT öffentlich exponieren.
- Optional später: semantische Suche (Embeddings) zusätzlich zu FTS.

## Erster Prompt für die neue Session (Vorschlag)
> "Wir richten den read-only IMAP-MCP-Server aus diesem Repo (Branch
> `claude/eloquent-feynman-4uK17`) hier auf dem Server ein und testen ihn gegen
> mein all-inkl.-Postfach. Lies HANDOFF.md und SPEC.md und führe mich durch
> Setup + ersten Sync; danach Fehler beheben."
