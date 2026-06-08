$ErrorActionPreference = 'Stop'

$DataDir = Join-Path $env:USERPROFILE '.codex-bark-notify'
$PidFile = Join-Path $DataDir 'watcher.pid'

if (!(Test-Path -LiteralPath $PidFile)) {
    Write-Output '[AgentWatcher] Watcher PID file not found.'
    exit 0
}

$ExistingPid = (Get-Content -Raw -LiteralPath $PidFile).Trim()
if (!$ExistingPid) {
    Remove-Item -LiteralPath $PidFile -Force
    Write-Output '[AgentWatcher] Watcher PID file was empty.'
    exit 0
}

$Process = Get-Process -Id ([int]$ExistingPid) -ErrorAction SilentlyContinue
if ($Process) {
    Stop-Process -Id $Process.Id -Force
    Write-Output "[AgentWatcher] Watcher stopped. PID: $ExistingPid"
} else {
    Write-Output "[AgentWatcher] Watcher process was not running. PID: $ExistingPid"
}

Remove-Item -LiteralPath $PidFile -Force
