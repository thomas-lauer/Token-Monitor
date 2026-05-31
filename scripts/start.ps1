# Token-Monitor: start the server and open the dashboard.

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$venvPython = "$repoRoot\venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "Venv nicht gefunden. Bitte zuerst .\scripts\setup.ps1 ausfuehren." -ForegroundColor Red
    exit 1
}

$port = if ($env:TOKEN_MONITOR_PORT) { $env:TOKEN_MONITOR_PORT } else { "8765" }
$bindHost = if ($env:TOKEN_MONITOR_HOST) { $env:TOKEN_MONITOR_HOST } else { "127.0.0.1" }
$url = "http://$($bindHost):$port"

Write-Host "Starte Token-Monitor auf $url ..." -ForegroundColor Cyan
Write-Host "(Strg+C zum Beenden)"
Write-Host ""

# Open browser after a short delay
Start-Job -ScriptBlock {
    param($u)
    Start-Sleep -Seconds 2
    Start-Process $u
} -ArgumentList $url | Out-Null

# Run uvicorn (blocks)
& $venvPython -m uvicorn app.main:app --host $bindHost --port $port
