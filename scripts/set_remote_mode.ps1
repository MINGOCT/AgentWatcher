param(
    [switch]$ReadOnly,

    [switch]$Reply,

    [switch]$Json
)

$ErrorActionPreference = 'Stop'

if ($ReadOnly -and $Reply) {
    throw 'Choose only one mode: -ReadOnly or -Reply.'
}

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = & (Join-Path $ScriptDir 'resolve_python.ps1')
$NotifyScript = Join-Path $ScriptDir 'codex_bark_notify.py'

$ArgsList = @($NotifyScript, 'remote-mode')
if ($ReadOnly) {
    $ArgsList += '--read-only'
} elseif ($Reply) {
    $ArgsList += '--reply'
}
if ($Json) {
    $ArgsList += @('--format', 'json')
}

& $Python @ArgsList
