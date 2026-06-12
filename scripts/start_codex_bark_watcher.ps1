$ErrorActionPreference = 'Stop'

$PluginRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = & (Join-Path $ScriptDir 'resolve_python.ps1')
$Script = Join-Path $PluginRoot 'scripts\codex_bark_notify.py'
$DataDir = Join-Path $env:USERPROFILE '.codex-bark-notify'
$SessionsDir = Join-Path $env:USERPROFILE '.codex\sessions'
$PidFile = Join-Path $DataDir 'watcher.pid'
$WebPidFile = Join-Path $DataDir 'web.pid'
$LogFile = Join-Path $DataDir 'watcher.log'
$ErrFile = Join-Path $DataDir 'watcher.err.log'
$WebLogFile = Join-Path $DataDir 'web.log'
$WebErrFile = Join-Path $DataDir 'web.err.log'

if (!(Test-Path -LiteralPath $DataDir)) {
    New-Item -ItemType Directory -Path $DataDir | Out-Null
}

if (Test-Path -LiteralPath $PidFile) {
    $ExistingPid = (Get-Content -Raw -LiteralPath $PidFile).Trim()
    if ($ExistingPid -and (Get-Process -Id ([int]$ExistingPid) -ErrorAction SilentlyContinue)) {
        Write-Output "[AgentWatcher] Watcher already running. PID: $ExistingPid"
        $Process = $null
    } else {
        $ExistingPid = ''
    }
}

if (!$ExistingPid) {
    & $Python $Script --data-dir $DataDir watch --sessions-dir $SessionsDir --baseline --once | Out-File -FilePath $LogFile -Append -Encoding utf8

    $ArgsList = @(
        $Script,
        '--data-dir', $DataDir,
        'watch',
        '--sessions-dir', $SessionsDir,
        '--interval', '2'
    )

    $Process = Start-Process -FilePath $Python -ArgumentList $ArgsList -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $LogFile -RedirectStandardError $ErrFile

    $Process.Id | Out-File -FilePath $PidFile -Encoding ascii
    Write-Output "[AgentWatcher] Watcher started. PID: $($Process.Id)"
    Write-Output "[AgentWatcher] Log: $LogFile"
    Write-Output "[AgentWatcher] Error log: $ErrFile"
}

if (Test-Path -LiteralPath $WebPidFile) {
    $ExistingWebPid = (Get-Content -Raw -LiteralPath $WebPidFile).Trim()
    if ($ExistingWebPid -and (Get-Process -Id ([int]$ExistingWebPid) -ErrorAction SilentlyContinue)) {
        Write-Output "[AgentWatcher] Web console already running. PID: $ExistingWebPid"
        exit 0
    }
}

$WebArgsList = @(
    $Script,
    '--data-dir', $DataDir,
    'serve'
)

$WebProcess = Start-Process -FilePath $Python -ArgumentList $WebArgsList -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput $WebLogFile -RedirectStandardError $WebErrFile

$WebProcess.Id | Out-File -FilePath $WebPidFile -Encoding ascii
Write-Output "[AgentWatcher] Web console started. PID: $($WebProcess.Id)"
Write-Output "[AgentWatcher] Web log: $WebLogFile"
Write-Output "[AgentWatcher] Web error log: $WebErrFile"
