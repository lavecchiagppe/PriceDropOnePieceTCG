# Rimuove le attivita' pianificate e il collegamento sul desktop.
$ErrorActionPreference = 'Continue'

foreach ($taskName in @('PriceDropOnePieceTCG', 'PriceDropOnePieceTCG-Raccolta')) {
    if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
        Write-Output "Attivita' '$taskName' rimossa."
    } else {
        Write-Output "Nessuna attivita' '$taskName' registrata."
    }
}

$shortcut = Join-Path ([Environment]::GetFolderPath('Desktop')) 'PriceDrop One Piece.url'
if (Test-Path $shortcut) {
    Remove-Item $shortcut -Force
    Write-Output 'Collegamento sul desktop rimosso.'
}
