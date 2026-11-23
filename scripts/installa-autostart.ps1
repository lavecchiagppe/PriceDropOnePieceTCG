# Registra le due attivita' pianificate del progetto, per l'utente corrente:
#
#   PriceDropOnePieceTCG            la dashboard, avviata al login, in background
#   PriceDropOnePieceTCG-Raccolta   un ciclo di raccolta ogni ora, che sveglia
#                                   il PC se e' sospeso
#
# Niente diritti di amministratore: le attivita' girano nella tua sessione,
# quindi funzionano a schermo spento e a sessione bloccata, ma non con
# l'utente disconnesso.
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$serveScript = Join-Path $PSScriptRoot 'avvia-in-background.ps1'
$collectScript = Join-Path $PSScriptRoot 'raccogli.ps1'
$serveTask = 'PriceDropOnePieceTCG'
$collectTask = 'PriceDropOnePieceTCG-Raccolta'

function New-HiddenAction([string]$script) {
    New-ScheduledTaskAction -Execute 'powershell.exe' `
        -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`"" `
        -WorkingDirectory $root
}

# --- Dashboard al login -----------------------------------------------------

$serveTrigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$serveTrigger.Delay = 'PT1M'

$serveSettings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $serveTask `
    -Action (New-HiddenAction $serveScript) -Trigger $serveTrigger -Settings $serveSettings `
    -Description 'Avvia la dashboard PriceDrop One Piece TCG in background al login.' `
    -Force | Out-Null

Write-Output "Attivita' '$serveTask' registrata: dashboard al login di $env:USERNAME."

# --- Raccolta oraria, indipendente dalla dashboard --------------------------

# L'intervallo segue config.toml, con il minimo di 5 minuti del progetto.
$minutes = 60
$configFile = Join-Path $root 'config.toml'
if (Test-Path $configFile) {
    $line = Select-String -Path $configFile -Pattern '^\s*interval_minutes\s*=\s*(\d+)' | Select-Object -First 1
    if ($line) { $minutes = [Math]::Max([int]$line.Matches[0].Groups[1].Value, 5) }
}

$collectTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).Date.AddMinutes(3) `
    -RepetitionInterval (New-TimeSpan -Minutes $minutes)

$collectSettings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -WakeToRun `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $collectTask `
    -Action (New-HiddenAction $collectScript) -Trigger $collectTrigger -Settings $collectSettings `
    -Description 'Un ciclo di raccolta PriceDrop ogni ora, anche a schermo spento; sveglia il PC se sospeso.' `
    -Force | Out-Null

Write-Output "Attivita' '$collectTask' registrata: raccolta ogni $minutes minuti, con risveglio del PC."

# --- Collegamento sul desktop ----------------------------------------------

$port = 8000
$envFile = Join-Path $root '.env'
if (Test-Path $envFile) {
    $line = Select-String -Path $envFile -Pattern '^\s*WEB_PORT\s*=\s*(\d+)' | Select-Object -First 1
    if ($line) { $port = [int]$line.Matches[0].Groups[1].Value }
}
$shortcut = Join-Path ([Environment]::GetFolderPath('Desktop')) 'PriceDrop One Piece.url'
Set-Content -Path $shortcut -Encoding ascii -Value @"
[InternetShortcut]
URL=http://127.0.0.1:$port/
"@
Write-Output "Collegamento sul desktop: $shortcut"

Write-Output ''
Write-Output "Il risveglio dalla sospensione funziona solo se i timer di riattivazione"
Write-Output "sono abilitati nelle opzioni di risparmio energia di Windows."
