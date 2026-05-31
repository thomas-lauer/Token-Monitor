# Token-Monitor: One-time setup.
# Creates a Python venv, installs dependencies, and sets the Claude Code
# OpenTelemetry environment variables under HKCU so new terminals pick them up.

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

Write-Host ""
Write-Host "===== Token-Monitor Setup =====" -ForegroundColor Cyan
Write-Host ""

# 1. Locate Python
$python = $null
foreach ($candidate in @("python", "py")) {
    $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
    if ($cmd) {
        $version = & $candidate --version 2>&1
        if ($version -match "Python 3\.(\d+)") {
            $minor = [int]$Matches[1]
            if ($minor -ge 10) { $python = $candidate; break }
        }
    }
}
if (-not $python) {
    Write-Host "ERROR: Python 3.10+ wurde nicht gefunden. Bitte installieren: https://www.python.org/downloads/" -ForegroundColor Red
    exit 1
}
Write-Host "Python OK: $($python) ($(& $python --version))"

# 2. Create venv
if (-not (Test-Path "$repoRoot\venv\Scripts\python.exe")) {
    Write-Host "Erstelle virtuelles Environment in .\venv ..."
    & $python -m venv "$repoRoot\venv"
}
$venvPython = "$repoRoot\venv\Scripts\python.exe"

# 3. Install requirements
Write-Host "Installiere Dependencies ..."
& $venvPython -m pip install --upgrade pip --disable-pip-version-check --quiet
& $venvPython -m pip install -r "$repoRoot\requirements.txt" --disable-pip-version-check
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: pip install fehlgeschlagen" -ForegroundColor Red
    exit 1
}

# 4. Set Claude Code OTel env vars (User scope)
Write-Host ""
Write-Host "Setze OpenTelemetry-Variablen (HKCU\Environment) ..."
$envVars = @{
    "CLAUDE_CODE_ENABLE_TELEMETRY"    = "1"
    "OTEL_METRICS_EXPORTER"           = "otlp"
    "OTEL_LOGS_EXPORTER"              = "otlp"
    "OTEL_EXPORTER_OTLP_PROTOCOL"     = "http/protobuf"
    "OTEL_EXPORTER_OTLP_ENDPOINT"     = "http://localhost:8765"
    "OTEL_METRIC_EXPORT_INTERVAL"     = "10000"
    "OTEL_LOGS_EXPORT_INTERVAL"       = "5000"
}
foreach ($key in $envVars.Keys) {
    [Environment]::SetEnvironmentVariable($key, $envVars[$key], "User")
    Write-Host "  $key=$($envVars[$key])"
}

# Also set in current session so an immediate `start.ps1` works without restart
foreach ($key in $envVars.Keys) {
    Set-Item -Path "Env:$key" -Value $envVars[$key]
}

Write-Host ""
Write-Host "===== Setup abgeschlossen =====" -ForegroundColor Green
Write-Host ""
Write-Host "Naechste Schritte:" -ForegroundColor Yellow
Write-Host "  1. Oeffne ein neues Terminal-Fenster (damit die Env-Vars greifen)"
Write-Host "  2. Starte den Monitor:  .\scripts\start.ps1"
Write-Host "  3. Verwende Claude Code wie gewohnt - Daten erscheinen automatisch."
Write-Host ""
