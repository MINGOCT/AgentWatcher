param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('PermissionRequest', 'Stop', 'PostToolUse')]
    [string]$Event
)

$ErrorActionPreference = 'Stop'

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = & (Join-Path $ScriptDir 'resolve_python.ps1')
$NotifyScript = Join-Path $ScriptDir 'codex_bark_notify.py'

& $Python $NotifyScript hook --event $Event
