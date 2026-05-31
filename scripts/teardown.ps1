# Token-Monitor: remove the OpenTelemetry env vars so Claude Code stops sending
# telemetry to us. Does NOT delete the data/ directory or the venv.

$keys = @(
    "CLAUDE_CODE_ENABLE_TELEMETRY",
    "OTEL_METRICS_EXPORTER",
    "OTEL_LOGS_EXPORTER",
    "OTEL_EXPORTER_OTLP_PROTOCOL",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_METRIC_EXPORT_INTERVAL",
    "OTEL_LOGS_EXPORT_INTERVAL"
)

Write-Host "Entferne OpenTelemetry-Variablen (HKCU\Environment) ..." -ForegroundColor Cyan
foreach ($key in $keys) {
    [Environment]::SetEnvironmentVariable($key, $null, "User")
    Remove-Item -Path "Env:$key" -ErrorAction SilentlyContinue
    Write-Host "  $key entfernt"
}
Write-Host ""
Write-Host "Fertig. Claude Code sendet keine Telemetrie mehr." -ForegroundColor Green
Write-Host "Tipp: Lokale JSONL-Transcripts werden weiterhin importiert solange das Tool laeuft."
