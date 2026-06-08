param(
    [Parameter(Mandatory = $true)]
    [string]$BarkUrl,

    [switch]$SkipTest
)

$ErrorActionPreference = 'Stop'

$PluginRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = & (Join-Path $ScriptDir 'resolve_python.ps1')
$NotifyScript = Join-Path $PluginRoot 'scripts\codex_bark_notify.py'

& $Python $NotifyScript setup --bark-url $BarkUrl

if (!$SkipTest) {
    & $Python $NotifyScript test
}

Write-Output '[AgentWatcher] Bark configured.'
Write-Output '[AgentWatcher] To enable automatic task-complete notifications, run:'
Write-Output 'powershell -NoProfile -ExecutionPolicy Bypass -File ".\scripts\start_codex_bark_watcher.ps1"'
Write-Output '[AgentWatcher] To enable startup launch, run:'
Write-Output 'powershell -NoProfile -ExecutionPolicy Bypass -File ".\scripts\install_watcher_startup.ps1"'
