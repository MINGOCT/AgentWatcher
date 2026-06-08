$ErrorActionPreference = 'Stop'

$StartupDir = [Environment]::GetFolderPath('Startup')
$ShortcutPath = Join-Path $StartupDir 'AgentWatcher.lnk'

if (Test-Path -LiteralPath $ShortcutPath) {
    Remove-Item -LiteralPath $ShortcutPath -Force
    Write-Output "[AgentWatcher] Startup shortcut removed: $ShortcutPath"
} else {
    Write-Output '[AgentWatcher] Startup shortcut not found.'
}
