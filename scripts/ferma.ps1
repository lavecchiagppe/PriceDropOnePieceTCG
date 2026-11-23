# Ferma la dashboard PriceDrop avviata in background.
$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot
$python = (Join-Path $root '.venv\Scripts\python.exe')

$found = $false
Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" | ForEach-Object {
    if ($_.ExecutablePath -eq $python -and $_.CommandLine -like '*pricedrop*serve*') {
        Write-Output "Chiudo il processo $($_.ProcessId)"
        Stop-Process -Id $_.ProcessId -Force
        $found = $true
    }
}
if (-not $found) { Write-Output 'Nessun server PriceDrop in esecuzione.' }
