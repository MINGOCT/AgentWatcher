param(
    [ValidateSet(5, 10, 15, 30, 60)]
    [int]$Interval = 15,

    [switch]$Off,

    [switch]$Json
)

$ErrorActionPreference = 'Stop'

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = & (Join-Path $ScriptDir 'resolve_python.ps1')
$NotifyScript = Join-Path $ScriptDir 'codex_bark_notify.py'

$ArgsList = @($NotifyScript, 'reply-heartbeat')
if ($Off) {
    $ArgsList += '--off'
} else {
    $ArgsList += @('--interval', [string]$Interval)
}
if ($Json) {
    $ArgsList += @('--format', 'json')
}

& $Python @ArgsList
