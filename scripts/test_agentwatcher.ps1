$ErrorActionPreference = 'Stop'

$PluginRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = & (Join-Path $ScriptDir 'resolve_python.ps1')
$NotifyScript = Join-Path $PluginRoot 'scripts\codex_bark_notify.py'

& $Python $NotifyScript test
