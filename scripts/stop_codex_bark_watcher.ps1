$ErrorActionPreference = 'Stop'

$DataDir = Join-Path $env:USERPROFILE '.codex-bark-notify'
$PidFile = Join-Path $DataDir 'watcher.pid'
$WebPidFile = Join-Path $DataDir 'web.pid'

function Stop-AgentWatcherProcess {
    param(
        [string]$Path,
        [string]$Name
    )

    if (!(Test-Path -LiteralPath $Path)) {
        Write-Output "[AgentWatcher] $Name PID file not found."
        return
    }

    $ExistingPid = (Get-Content -Raw -LiteralPath $Path).Trim()
    if (!$ExistingPid) {
        Remove-Item -LiteralPath $Path -Force
        Write-Output "[AgentWatcher] $Name PID file was empty."
        return
    }

    $Process = Get-Process -Id ([int]$ExistingPid) -ErrorAction SilentlyContinue
    if ($Process) {
        Stop-Process -Id $Process.Id -Force
        Write-Output "[AgentWatcher] $Name stopped. PID: $ExistingPid"
    } else {
        Write-Output "[AgentWatcher] $Name process was not running. PID: $ExistingPid"
    }

    Remove-Item -LiteralPath $Path -Force
}

Stop-AgentWatcherProcess -Path $PidFile -Name 'Watcher'
Stop-AgentWatcherProcess -Path $WebPidFile -Name 'Web console'
