# Token-Monitor

> Lokales Windows-Tool, das den Token-Verbrauch von [Claude Code](https://claude.com/claude-code) aufzeichnet, übersichtlich visualisiert und konkrete Optimierungs-Empfehlungen ausspricht.

![status](https://img.shields.io/badge/status-alpha-orange) ![python](https://img.shields.io/badge/python-3.10%2B-blue) ![license](https://img.shields.io/badge/license-MIT-green) ![platform](https://img.shields.io/badge/platform-Windows-lightgrey)

---

## Was es kann

- 📈 **Live-Dashboard** im Browser mit Token-Verlauf, Kosten pro Tag, Modell-Aufteilung, Top-Tools, Heatmap und Sessions
- 🧠 **Empfehlungs-Engine** mit 9 Heuristik-Regeln (Cache-Hit-Rate, Modellauswahl, Effort-Overuse, lange Sessions, Subagent-Overhead, …) inklusive geschätzter monatlicher Ersparnis
- 🔌 **OpenTelemetry-Receiver** (OTLP/HTTP) gemäß der [offiziellen Doku](https://code.claude.com/docs/en/monitoring-usage) — Claude Code sendet Metrics + Logs direkt an `http://localhost:8765`
- 📚 **JSONL-Importer** für deine bestehenden `~/.claude/projects/*.jsonl`-Transcripts — auch ohne aktivierte OTel-Pipeline siehst du _historische_ Daten ab dem ersten Start
- 💾 **SQLite-Backend** — keine Cloud, keine Auth, alles lokal in `data/events.db`

## Architektur

```
+------------------+    OTLP/HTTP    +-------------------------------+
| Claude Code CLI  | --------------> |  Token-Monitor (localhost)    |
+------------------+   :8765/v1/*    |                               |
                                     |  +-------------------------+  |
~/.claude/projects/*.jsonl --------> |  | JSONL Importer          |  |
                                     |  +-------------------------+  |
                                     |  +-------------------------+  |
                                     |  | OTLP Receiver (FastAPI) |  |
                                     |  +-------------------------+  |
                                     |              |                |
                                     |              v                |
                                     |  +-------------------------+  |
                                     |  | SQLite (events.db)      |  |
                                     |  +-------------------------+  |
                                     |              |                |
                                     |              v                |
                                     |  +-------------------------+  |
                                     |  | Dashboard API + Web UI  | <--- Browser
                                     |  | (Chart.js, Vanilla JS)  |  |
                                     |  +-------------------------+  |
                                     |  +-------------------------+  |
                                     |  | Optimizer-Engine        |  |
                                     |  +-------------------------+  |
                                     +-------------------------------+
```

## Schnellstart

### Voraussetzungen

- Windows 10 / 11
- Python 3.10+ ([Download](https://www.python.org/downloads/))
- PowerShell 5.1+ (vorinstalliert)

### Installation

```powershell
git clone https://github.com/thomas-lauer/Token-Monitor.git
cd Token-Monitor
.\scripts\setup.ps1
```

`setup.ps1` macht drei Dinge:

1. Legt ein `venv\` an und installiert die Python-Dependencies
2. Setzt die OpenTelemetry-Variablen im _Benutzer_-Scope (HKCU), damit Claude Code Telemetrie zu uns sendet:

   | Variable | Wert |
   |---|---|
   | `CLAUDE_CODE_ENABLE_TELEMETRY` | `1` |
   | `OTEL_METRICS_EXPORTER` | `otlp` |
   | `OTEL_LOGS_EXPORTER` | `otlp` |
   | `OTEL_EXPORTER_OTLP_PROTOCOL` | `http/protobuf` |
   | `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://localhost:8765` |
   | `OTEL_METRIC_EXPORT_INTERVAL` | `10000` |
   | `OTEL_LOGS_EXPORT_INTERVAL` | `5000` |

3. Setzt die gleichen Variablen auch in der aktuellen Session

### Starten

**Neues Terminal-Fenster** öffnen (damit die Env-Variablen greifen), dann:

```powershell
.\scripts\start.ps1
```

Der Browser öffnet sich automatisch auf <http://localhost:8765>. Beim ersten Start werden die historischen JSONL-Transcripts aus `~/.claude/projects/` importiert — das kann je nach Volumen ein paar Sekunden dauern.

### Stoppen

```powershell
.\scripts\stop.ps1
```

### Deinstallieren

Entfernt die OTel-Variablen (Daten bleiben erhalten):

```powershell
.\scripts\teardown.ps1
```

Komplett entfernen: zusätzlich den `Token-Monitor`-Ordner löschen.

## Datenquellen

Token-Monitor kombiniert _zwei_ Quellen, dedupliziert auf `request_id`:

### 1. OpenTelemetry (Live)

Sobald Claude Code die OTel-Variablen sieht, sendet es alle 5–10 Sekunden:

- **Metrics**: `claude_code.token.usage`, `claude_code.cost.usage`, `claude_code.session.count`
- **Logs/Events**: `claude_code.api_request`, `claude_code.tool_result`, `claude_code.user_prompt`, `claude_code.api_error`

Genaue Definitionen: <https://code.claude.com/docs/en/monitoring-usage>

### 2. JSONL-Transcripts (historisch)

Claude Code schreibt jede Session als JSONL nach `~/.claude/projects/<projekt>/<session>.jsonl`. Token-Monitor liest diese Dateien inkrementell (Cursor-basiert) und füttert sie in dieselben Tabellen. **So siehst du sofort beim ersten Start dein Verhalten der letzten Wochen.**

## Optimierungs-Regeln

| Regel | Trigger |
|---|---|
| **Low Cache-Hit-Rate** | `<50%` Cache-Anteil über das Zeitfenster |
| **Opus für Short Tasks** | Opus-Calls mit `<500` Output-Tokens > 5% Anteil |
| **High Effort Overuse** | `high/xhigh/max` Effort > 20% |
| **Long Session w/o Compact** | Session > 200k Input-Tokens ohne `/compact` |
| **Subagent Overload** | Subagents > 50% der Gesamtkosten |
| **Failing Tools** | Tool-Fehlerrate > 15% bei >20 Calls |
| **Oversize Reads** | Read-Tool Ergebnis > 200 KB |
| **Fast-Mode Overuse** | `speed=fast` > 50% Kostenanteil |
| **Output Heavy** | Output > 3× Input (Output ist 5× teurer als Input) |

Jede Empfehlung enthält:
- _Was_ konkret stört (Titel)
- _Warum_ es teuer ist (Beschreibung)
- _Wie_ du es ändern kannst (Hinweis)
- Geschätzte monatliche Ersparnis in USD

## Preise anpassen

`pricing.json` enthält die USD-Raten pro 1M Tokens je Modell. Standardwerte basieren auf den öffentlichen Anthropic-Preisen (Stand 2026-05). Wenn du andere Konditionen hast (z.B. Enterprise-Rabatt, Bedrock, Vertex), editiere die Datei und starte das Tool neu:

```json
{
  "models": {
    "claude-sonnet-4-6": { "input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_creation": 3.75 }
  },
  "fallback": "claude-sonnet-4-6"
}
```

> Hinweis: Wenn Claude Code via OTel `cost_usd` mitliefert, hat dieser Wert Vorrang. Die lokale Berechnung wird nur für JSONL-Daten verwendet.

## FAQ

**Kann ich es ohne OTel benutzen?** Ja. Wenn du `setup.ps1` _nicht_ ausführst oder per `teardown.ps1` die Variablen wieder entfernst, läuft trotzdem der JSONL-Importer und du siehst alle Session-Daten aus `~/.claude/projects/`.

**Geht das auch unter macOS/Linux?** Der Python-Code ist plattformneutral. Nur die `.ps1`-Skripte sind Windows-spezifisch — auf macOS/Linux müsstest du die Env-Variablen manuell exportieren und `uvicorn app.main:app --host 127.0.0.1 --port 8765` selber starten.

**Wo liegt die Datenbank?** `<repo>/data/events.db`. Reines SQLite, kann mit z.B. [DB Browser for SQLite](https://sqlitebrowser.org/) inspiziert werden.

**Werden meine Prompts gespeichert?** Nein. Standardmäßig _nicht_. Claude Code sendet `prompt` nur, wenn du `OTEL_LOG_USER_PROMPTS=1` setzt — diese Variable setzen wir bewusst nicht. JSONL enthält die User-Prompts unverschlüsselt, aber wir importieren nur die _Länge_, nicht den Inhalt.

**Hat das einen Performance-Impact auf Claude Code?** OTel exportiert asynchron in 5–10s Buckets, der Overhead ist minimal. Der JSONL-Importer läuft nur in unserem Prozess, nicht in Claude Code.

## Lizenz

[MIT](LICENSE) — siehe LICENSE-Datei.

## Beiträge

Issues und PRs sind willkommen, vor allem für:
- weitere Optimizer-Regeln
- Pricing-Updates für neue Modelle
- macOS/Linux-Setup-Skripte
- bessere Charts / Dashboard-Layouts
