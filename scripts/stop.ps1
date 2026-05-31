# Token-Monitor: stop a running server by killing the process on the configured port.

$ErrorActionPreference = "SilentlyContinue"

$port = if ($env:TOKEN_MONITOR_PORT) { $env:TOKEN_MONITOR_PORT } else { "8765" }

$conns = Get-NetTCPConnection -LocalPort $port -State Listen
if (-not $conns) {
    Write-Host "Kein Token-Monitor laeuft auf Port $port."
    exit 0
}

foreach ($c in $conns) {
    $proc = Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue
    if ($proc) {
        Write-Host "Stoppe PID $($proc.Id) ($($proc.ProcessName)) ..."
        Stop-Process -Id $proc.Id -Force
    }
}
Write-Host "Token-Monitor gestoppt." -ForegroundColor Green
