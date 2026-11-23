# Un ciclo di raccolta, pensato per l'attivita' pianificata oraria.
# Gira anche a sessione bloccata o schermo spento, e il risveglio dalla
# sospensione lo gestisce il trigger dell'attivita'.
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
$logDir = Join-Path $root 'data\logs'
$log = Join-Path $logDir 'collect.log'

if (-not (Test-Path $python)) {
    throw "Python del venv non trovato: $python. Crea il venv come da README."
}
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }

$stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
Add-Content -Path $log -Encoding utf8 -Value "`n===== $stamp  avvio raccolta ====="

Push-Location $root
try {
    # 2>&1 sul nativo: stdout e stderr finiscono nello stesso log in ordine.
    & $python -u -m pricedrop collect 2>&1 | ForEach-Object {
        Add-Content -Path $log -Encoding utf8 -Value $_.ToString()
    }
    $code = $LASTEXITCODE
} finally {
    Pop-Location
}

$stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
Add-Content -Path $log -Encoding utf8 -Value "===== $stamp  fine raccolta (uscita $code) ====="

# Il log non cresce senza limite: oltre 2 MB tiene le ultime 2000 righe.
if ((Get-Item $log).Length -gt 2MB) {
    $tail = Get-Content $log -Tail 2000
    Set-Content -Path $log -Encoding utf8 -Value $tail
}

exit $code
