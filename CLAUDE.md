# Token-Monitor — Entwickler-Dokumentation

Diese Datei liefert den vollständigen technischen Kontext für Entwickler und KI-Assistenten (Claude Code, Codex, Cursor, …), die an Token-Monitor weiterarbeiten. Für die User-Sicht: siehe `README.md`.

---

## Zweck

Lokales Windows-Tool, das den Token- und Kostenverbrauch von **Claude Code** und **OpenAI Codex CLI** aus drei Quellen einsammelt, in SQLite persistiert, im Browser visualisiert und konkrete Spar-Empfehlungen ableitet.

Designziele (Prioritätenreihenfolge):
1. **Lokal & autark** — kein Cloud-Service, keine Auth, keine externe DB.
2. **Robust gegen partielle Daten** — Sources dürfen sich überschneiden, NULL-Felder sind normal, Importer dürfen jederzeit abstürzen ohne Datenkorruption.
3. **Geringe Setup-Hürde** — ein Python venv + drei PS-Skripte, kein Build-Schritt fürs Frontend.
4. **Erweiterbar um weitere Provider** — Schema und API sind provider-neutral.

---

## Architektur-Überblick

```
                +-------------------------------------------------------+
                |                  Token-Monitor (uvicorn)              |
                |               Single Python process @ :8765           |
                |                                                       |
                |  +----------------+   +------------------------+      |
Claude Code OTel|  | OTLP Receiver  |   | Claude JSONL Importer  |      |
   :8765/v1/*  ->  | (otlp_receiver)|   | (jsonl_importer)       |      |
                |  +----------------+   +------------------------+      |
                |                                                       |
~/.claude/projects/*.jsonl   ~/.codex/sessions/.../rollout-*.jsonl       |
                |                       +------------------------+      |
                |                       | Codex JSONL Importer   |      |
                |                       | (codex_importer)       |      |
                |                       +------------------------+      |
                |                       v                               |
                |       +---------------------------+                   |
                |       | SQLite (data/events.db)   |                   |
                |       | WAL-Modus, single-writer  |                   |
                |       +---------------------------+                   |
                |                       |                               |
                |       +--------------------------------+              |
                |       | Dashboard API (api.py) — JSON  | <-- Browser  |
                |       | Optimizer (optimizer.py)       |              |
                |       | Static (index/app.js/styles)   |              |
                |       +--------------------------------+              |
                +-------------------------------------------------------+
```

**Drei Datenquellen, ein Schema** — alles landet in `api_requests`/`tool_results`/`prompts`/`api_errors` mit einer `source`-Spalte (`otel` | `jsonl` | `otel-metric` | `otel-cost` | `codex-jsonl`) und einer `provider`-Spalte (`anthropic` | `openai`).

Deduplizierung läuft über UNIQUE-Indexe:
- `api_requests.request_id` (Anthropic msg-ID; für Codex: `codex_<sha1(session+ts)>`)
- `tool_results.tool_use_id`
- `prompts.prompt_id`

Wenn dieselbe Zeile aus mehreren Quellen kommt, wird via `ON CONFLICT … DO UPDATE … COALESCE(excluded.col, col)` gemerged — die zuerst nicht-NULL Quelle gewinnt pro Feld.

---

## Projektstruktur

```
Token-Monitor/
├── app/
│   ├── __init__.py
│   ├── main.py              # FastAPI-App, lifespan, Background-Tasks, Static-Mount
│   ├── config.py            # Pfade, Env-Vars, Pricing-Loader
│   ├── db.py                # SQLite-Schema, Migrations, write_conn/read_conn, Upserts
│   ├── pricing.py           # cost_for(model, …, provider)
│   ├── jsonl_importer.py    # ~/.claude/projects/*.jsonl
│   ├── codex_importer.py    # ~/.codex/sessions/yyyy/mm/dd/rollout-*.jsonl
│   ├── otlp_receiver.py     # /v1/metrics, /v1/logs, /v1/traces (HTTP/protobuf)
│   ├── proto_decode.py      # opentelemetry-proto Wrapper
│   ├── api.py               # /api/* JSON-Endpoints fürs Dashboard
│   └── optimizer.py         # Heuristik-Regeln + Recommendation-Dataclass
├── static/
│   ├── index.html
│   ├── app.js               # Vanilla JS, Chart.js, kein Build
│   ├── styles.css           # Dark Theme
│   └── vendor/chart.umd.min.js
├── scripts/
│   ├── setup.ps1            # venv + pip + OTel-EnvVars (HKCU)
│   ├── start.ps1            # uvicorn + Browser
│   ├── stop.ps1             # Port-basiert killen
│   └── teardown.ps1         # OTel-EnvVars entfernen
├── data/                    # SQLite-DB landet hier (gitignored)
├── pricing.json             # USD/MTok pro Modell, anpassbar
├── requirements.txt
├── README.md                # User-Sicht
├── CLAUDE.md                # diese Datei
└── LICENSE                  # MIT
```

---

## Datenbankschema (SQLite)

`data/events.db` — WAL-Modus, `synchronous=NORMAL`.

### `api_requests` — die zentrale Tabelle

Jede Zeile ist _ein_ API-Roundtrip zum Modell. Kosten und Token-Buckets summieren auf diese Tabelle.

| Spalte | Quelle | Hinweis |
|---|---|---|
| `id` | autoincrement | |
| `timestamp` | ISO 8601 UTC | indexiert |
| `session_id` | UUID | indexiert |
| `prompt_id` | UUID | Anthropic only — fasst alle Requests eines User-Prompts zusammen |
| `request_id` | `req_…` / `msg_…` / `codex_<hash>` | UNIQUE, Dedup-Key |
| `model` | `claude-opus-4-7`, `gpt-5.4`, … | indexiert |
| `input_tokens` | int | bei Codex: ohne cached, also raw_input − cached |
| `output_tokens` | int | bei Codex: inkl. reasoning_output_tokens |
| `cache_read_tokens` | int | Codex `cached_input_tokens` mappt hier rein |
| `cache_creation_tokens` | int | Anthropic only — Codex hat keinen separaten Cache-Write |
| `cost_usd` | float | aus OTel-Cost-Counter ODER lokal via pricing.py |
| `duration_ms` | int? | nur OTel |
| `query_source` | `main`/`subagent`/`auxiliary`/`compact`/… | |
| `agent_name`, `skill_name`, `plugin_name`, `mcp_server`, `mcp_tool` | Strings | Anthropic OTel only |
| `effort` | `low`/`medium`/`high`/`xhigh`/`max` | |
| `speed` | `fast`/`normal` | Anthropic only |
| `project_path` | Pfad | für Claude aus dem JSONL-Ordnernamen rekonstruiert |
| `source` | `otel`/`jsonl`/`otel-metric`/`otel-cost`/`codex-jsonl` | |
| `provider` | `anthropic`/`openai` | NOT NULL, Default `'anthropic'` |

### Weitere Tabellen

- **`tool_results`** — pro Tool-Aufruf. `tool_name` ist **nullable** (es gibt Orphan-`tool_result`-Records ohne vorherigen `tool_use`). UNIQUE auf `tool_use_id`.
- **`prompts`** — User-Prompts mit `prompt_length` (nicht der Inhalt — Privacy!).
- **`sessions`** — denormalisierte Session-Meta, aus api_requests aggregiert.
- **`api_errors`** — Fehlgeschlagene Requests (Anthropic OTel).
- **`jsonl_cursor`** — Pro Datei: `mtime` + `byte_offset`, für inkrementelles Lesen.
- **`meta`** — Key-Value: `last_jsonl_scan_at`, `last_codex_scan_at`, etc.

### Migrations

`db.init_db()` ruft `_migrate()`, das per `PRAGMA table_info` prüft, ob die `provider`-Spalte existiert, und sie sonst per `ALTER TABLE ADD COLUMN provider TEXT NOT NULL DEFAULT 'anthropic'` ergänzt. **Pattern beibehalten** bei zukünftigen Spaltenänderungen — kein Alembic, kein Schema-Versionsfeld nötig.

---

## Datenquellen im Detail

### 1. Claude Code OTLP (live, optional)

Aktiviert durch `setup.ps1`, das diese 7 EnvVars im HKCU setzt:
- `CLAUDE_CODE_ENABLE_TELEMETRY=1`
- `OTEL_METRICS_EXPORTER=otlp`
- `OTEL_LOGS_EXPORTER=otlp`
- `OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf`
- `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:8765`
- `OTEL_METRIC_EXPORT_INTERVAL=10000`
- `OTEL_LOGS_EXPORT_INTERVAL=5000`

Empfangene Metriken (in `_METRIC_HANDLERS`):
- `claude_code.token.usage` (split nach `type`: input/output/cacheRead/cacheCreation) → `api_requests`
- `claude_code.cost.usage` → `cost_usd`

Empfangene Events (in `_LOG_HANDLERS`):
- `claude_code.api_request` → primäre Quelle pro Request, inkl. duration
- `claude_code.tool_result` → `tool_results`
- `claude_code.user_prompt` → `prompts`
- `claude_code.api_error` → `api_errors`

Anwendungs-Kontext: Vollständige Feld-Definitionen unter <https://code.claude.com/docs/en/monitoring-usage>.

### 2. Claude Code JSONL-Transcripts (historisch + live)

Pfad: `~/.claude/projects/<projekt-slug>/<session-uuid>.jsonl` (eine JSON-Zeile pro Nachricht).

**Wichtige Record-Typen:**
- `type=assistant` mit `message.usage` → Token-bearing Zeilen
  - `message.usage.input_tokens` / `output_tokens` / `cache_read_input_tokens` / `cache_creation_input_tokens`
  - `message.id` ist der Anthropic `msg_…`-Identifier → wird als `request_id` benutzt
  - `message.content[].type == "tool_use"` → tool_results-Insert (Name + Input-Größe)
- `type=user` mit `message.content[].type == "tool_result"` → tool_results-Update (Success + Result-Größe)
- `type=user` ohne tool_result → `prompts`

**Project-Path-Decodierung** (lossy, nur für Display): `_decode_project_path()` rekonstruiert aus `D--OneDrive---TLDS--KI-Projekte_-Claude-Token-Monitor` einen Pfad — das Mapping ist nicht reversibel, weil Slashes _und_ Spaces beide zu Dashes werden.

### 3. OpenAI Codex CLI JSONL-Rollouts

Pfad: `~/.codex/sessions/<YYYY>/<MM>/<DD>/rollout-<ts>-<uuid>.jsonl`.

**Record-Typen:**
- `type=session_meta` → Session-ID, cwd, cli_version
- `type=turn_context` → Modell (z.B. `gpt-5.4`), effort, reasoning_effort
- `type=event_msg`:
  - `payload.type=token_count` mit `info.last_token_usage` → ein api_request je Turn
  - `payload.type=exec_command_end` → Bash tool_result (mit duration aus secs+nanos)
- `type=response_item`:
  - `payload.type=function_call` (außer `name=shell_command`, das ist schon via exec_command_end abgedeckt)
  - `payload.type=custom_tool_call`

**Token-Mapping** (`_handle_token_count`):

| Codex-Feld | Token-Monitor-Feld |
|---|---|
| `input_tokens − cached_input_tokens` | `input_tokens` (der "neue" Input) |
| `cached_input_tokens` | `cache_read_tokens` |
| `output_tokens + reasoning_output_tokens` | `output_tokens` |
| — | `cache_creation_tokens` (immer 0) |

`request_id` wird synthetisiert als `codex_<sha1(session_id + timestamp)[:16]>` — stabil über Re-Imports.

**Wichtig**: Die _ersten_ token_count Events haben `info: null` (nur Rate-Limit-Heartbeat) → werden übersprungen.

**Cursor-Pattern**: Der Codex-Importer nutzt `db.get_cursor("codex::<path>")` mit Prefix, damit er sich nicht mit dem Claude-Importer am gleichen file_path stört.

---

## Wichtige Code-Konventionen

### SQLite-Pitfalls

- **Partial UNIQUE Indexes funktionieren nicht mit `ON CONFLICT(spalte)`** — SQLite findet einen `CREATE UNIQUE INDEX … WHERE col IS NOT NULL` nicht als Konflikt-Target. Lösung: voller UNIQUE Index, SQLite behandelt NULL ohnehin als ungleich. **Nicht** `WHERE …`-Klauseln in UNIQUE Indexes verwenden.
- **Single-writer**: `db.write_conn()` ist durch `_write_lock` global serialisiert. Alle Background-Tasks und die OTel-Endpoints gehen darüber. Reads (`read_conn`) sind frei nebenläufig dank WAL.
- **`isolation_level=None` + explizites Commit**: Wir nutzen Autocommit-Mode. Wenn du ein Multi-Statement-Transaktion brauchst, explizit `conn.execute("BEGIN")` / `COMMIT`.
- **`tool_name` ist nullable**: Orphan-`tool_result`-Records (ohne vorheriges `tool_use` im gleichen File) treten in der Praxis auf, vor allem bei langen Sessions mit Tool-Result vor dem JSONL-Append des tool_use.

### Importer-Pattern

Jeder Importer (`jsonl_importer.py`, `codex_importer.py`) folgt demselben Pattern:

1. `scan_once()` durchläuft alle Dateien, ruft `import_file(path)` auf
2. `import_file(path)` prüft `db.get_cursor(key)`:
   - mtime + offset == size → skip (nichts neues)
   - mtime gleich, offset < size → inkrementell ab offset
   - mtime ungleich → full re-scan ab 0 (Datei wurde rewritten/truncated)
3. Pro Zeile: parse, dispatch, upsert. Exceptions auf Zeilenebene loggen, _nicht_ den ganzen Import abbrechen.
4. Am Ende: `db.set_cursor(key, mtime, last_offset, now_iso)`.

`importer_loop()` läuft als asyncio-Task im FastAPI-Lifespan und wartet `JSONL_POLL_INTERVAL_SECONDS` zwischen Scans. Der eigentliche I/O läuft via `asyncio.to_thread()` damit der Event-Loop nicht blockiert.

### Provider-Filter-Konvention

Alle `/api/*`-Endpoints akzeptieren optional `?provider=anthropic|openai`. Im SQL wird das via `_where_clause()` (für api_requests/tool_results) zu einer `AND provider = ?`-Klausel. **Niemand parametrisiert provider direkt in SQL-Strings** — der Validator in `_where_clause` lässt nur die Whitelist (`anthropic`, `openai`) durch.

Im Frontend ist die Provider-Auswahl in `state.provider` und wird über `pquery()` an jeden API-Call gehängt. Wenn du eine neue Dashboard-Section ergänzt, **vergiss nicht `${pquery()}`** an die URL anzuhängen — sonst zeigt deine Section irrtümlich alle Provider, während der Rest gefiltert ist.

### Pricing & Cost

`pricing.json` enthält USD-Raten pro 1M Tokens je Modell. `pricing.py::cost_for()` nimmt optional `provider=` und fällt auf `fallback_anthropic` bzw. `fallback_openai` zurück, wenn das Modell unbekannt ist. **Niemals harte Modell-Listen im Code** — alles geht durch `pricing.json`, damit User die Werte ohne Code-Änderung pflegen können.

Wenn OTel `cost_usd` mitliefert (Anthropic-API meldet das direkt), hat das Vorrang. Für JSONL-Quellen (beide Provider) wird lokal aus den Token-Counts berechnet.

### Frontend-Konventionen

- **Kein Build-Step**: `static/` wird direkt von FastAPI gemountet (`StaticFiles(html=True)`). Chart.js liegt als UMD-Bundle in `static/vendor/`.
- **Single Source of Truth ist `state`** (range, provider, charts). Bei UI-Änderungen: `refreshAll()` aufrufen, der schreibt `state` aus den DOM-Werten neu und triggert alle Endpoints parallel.
- **Charts via `Chart` global** — destroy + recreate, kein Update-Path. Bei vielen Daten irrelevant, weil unsere Buckets <100 Punkte sind.
- **Sprache**: UI-Strings auf Deutsch, Code-Kommentare auf Englisch.

---

## Häufige Tasks

### Lokal starten (DEV)

```powershell
.\venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8765 --reload
```

`--reload` lauscht auf Datei-Änderungen in `app/` und startet neu. Bei Frontend-Änderungen reicht ein Browser-Refresh.

### DB von Grund auf neu

```powershell
.\scripts\stop.ps1
Remove-Item data\events.db, data\events.db-wal, data\events.db-shm
.\scripts\start.ps1
```

Initialer Scan beider Importer dauert je nach Datenmenge 5–60 Sekunden.

### DB inspizieren

```powershell
.\venv\Scripts\python.exe -c "import sqlite3; c=sqlite3.connect('data/events.db'); [print(r) for r in c.execute('SELECT provider, COUNT(*), SUM(cost_usd) FROM api_requests GROUP BY provider')]"
```

Oder mit [DB Browser for SQLite](https://sqlitebrowser.org/) öffnen.

### Neuen API-Endpoint hinzufügen

1. In `app/api.py` einen `@router.get(...)` definieren.
2. Bei Querys auf api_requests/tool_results: `_where_clause(range, provider, ts_col)` benutzen statt manuelles WHERE.
3. Im Frontend `app.js::refreshAll()` einen `jget(...)` einreihen und das Rendering ergänzen.

### Neue Optimizer-Regel hinzufügen

1. In `app/optimizer.py` eine `_rule_<name>(since, window_days, provider)` Funktion definieren, die `list[Recommendation]` zurückgibt.
2. SQL über `_params_for(since, provider)` parametrisieren — der gibt `" AND timestamp >= ? AND provider = ?"` zurück.
3. Bei monetärer Empfehlung: `est_monthly_savings_usd=_scale_to_month(window_days, observed_saving)`.
4. Funktion in die `_RULES`-Liste am Ende der Datei eintragen.

### Neuen Provider unterstützen

1. Modell-Preise in `pricing.json` ergänzen, optional `fallback_<provider>` setzen.
2. Neuen Importer `app/<provider>_importer.py` nach dem Pattern von `codex_importer.py` schreiben:
   - Pro Datei `db.get_cursor(f"<provider>::<path>")` für Cursor-Isolation
   - Bei Upserts immer `provider="<name>"` mitschicken
   - `db.set_meta(f"last_<provider>_scan_at", …)` setzen
3. In `app/main.py::lifespan` einen `asyncio.create_task()` für die Importer-Loop ergänzen
4. In `app/api.py::_VALID_PROVIDERS` den Namen aufnehmen
5. Im Frontend `static/index.html` Provider-Select-Option + `PROVIDER_LABELS`/`PROVIDER_COLORS` in `app.js` ergänzen

---

## Bekannte Gotchas

- **`pricing.json`-Cache** in `pricing.py::_PRICING_CACHE` wird beim Prozessstart einmal geladen. Bei Änderungen an `pricing.json` Server neu starten ODER `pricing.reload_pricing()` aufrufen.
- **PowerShell-Reserved `$host`**: In `start.ps1` heißt die Variable `$bindHost`, nicht `$host` — PowerShell hat `$host` als Built-In.
- **OneDrive sync** auf dem Projektpfad: Wenn Token-Monitor in `OneDrive\...` liegt, kann SQLite-WAL gelegentlich auf File-Lock-Konflikte stoßen. Bei Produktiv-Setup besser ein Standard-Lokalpfad.
- **OTLP-Endpoint-Konflikt**: Claude Code's Default-OTLP-Port wäre 4317 (gRPC) / 4318 (HTTP). Wir nutzen 8765, damit Token-Monitor sowohl Dashboard als auch OTel auf einem Port serviert. Der `OTEL_EXPORTER_OTLP_ENDPOINT` muss daher explizit auf `http://localhost:8765` (ohne `/v1/...`-Suffix) gesetzt sein — der OTel-SDK hängt `/v1/logs` etc. selbst an.
- **Codex `info: null`**: Das erste `token_count`-Event jeder Codex-Session hat nur Rate-Limit-Info und kein `info`-Feld. Importer ignoriert das.
- **Project-Path bei Claude JSONL** ist verlustbehaftet (`/` und ` ` werden beide zu `-`). Nur fürs Display, niemals als FS-Path verwenden.

---

## Tech-Stack & Abhängigkeiten

- **Python 3.10+** (getestet auf 3.14)
- **FastAPI 0.115+** für HTTP-Layer
- **uvicorn[standard]** als ASGI-Server (mit `watchfiles` für `--reload`)
- **opentelemetry-proto 1.27+** für OTLP-Protobuf-Decoding
- **protobuf 4.25+**
- **SQLite** aus stdlib
- **Chart.js 4.4** (vendored)
- Keine ORM, keine Pydantic-Models (FastAPI Type-Hints reichen), kein Build-Tool, kein Test-Framework (yet)

`requirements.txt` ist intentional minimal.

---

## Lizenz & Beiträge

MIT. Issues/PRs willkommen — siehe README für die Wishlist (mehr Optimizer-Regeln, macOS/Linux-Skripte, weitere Provider).
