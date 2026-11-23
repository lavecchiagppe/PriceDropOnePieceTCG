# Avvia la dashboard PriceDrop in background, senza finestre.
# Se la porta e' gia' in ascolto non fa nulla: non parte un secondo server.
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
$logDir = Join-Path $root 'data\logs'

if (-not (Test-Path $python)) {
    throw "Python del venv non trovato: $python. Crea il venv come da README."
}

# Porta: WEB_PORT in .env se c'e', altrimenti 8000 (il default del progetto).
$port = 8000
$envFile = Join-Path $root '.env'
if (Test-Path $envFile) {
    $line = Select-String -Path $envFile -Pattern '^\s*WEB_PORT\s*=\s*(\d+)' | Select-Object -First 1
    if ($line) { $port = [int]$line.Matches[0].Groups[1].Value }
}

$listening = $null
try { $listening = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction Stop } catch {}
if ($listening) {
    Write-Output "Dashboard gia' in ascolto su http://127.0.0.1:$port"
    exit 0
}

if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }

Start-Process -FilePath $python `
    -ArgumentList '-u', '-m', 'pricedrop', 'serve' `
    -WorkingDirectory $root `
    -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $logDir 'serve.log') `
    -RedirectStandardError (Join-Path $logDir 'serve.err')

Write-Output "Avviata. Dashboard su http://127.0.0.1:$port (log in $logDir)"
