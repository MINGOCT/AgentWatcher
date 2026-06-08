$ErrorActionPreference = 'Stop'

function Test-PythonCandidate {
    param([Parameter(Mandatory = $true)][string]$Candidate)

    if (!(Test-Path -LiteralPath $Candidate) -and !(Get-Command $Candidate -ErrorAction SilentlyContinue)) {
        return $false
    }

    try {
        & $Candidate -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)" 2>$null | Out-Null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

$ConfiguredPython = $env:CODEX_BARK_NOTIFY_PYTHON
if (!$ConfiguredPython) {
    $ConfiguredPython = [Environment]::GetEnvironmentVariable('CODEX_BARK_NOTIFY_PYTHON', 'User')
}

if ($ConfiguredPython -and (Test-PythonCandidate $ConfiguredPython)) {
    Write-Output $ConfiguredPython
    exit 0
}

$Commands = @('python', 'python3', 'py')
foreach ($Command in $Commands) {
    $Resolved = Get-Command $Command -ErrorAction SilentlyContinue
    if ($Resolved -and $Resolved.Source -and (Test-PythonCandidate $Resolved.Source)) {
        Write-Output $Resolved.Source
        exit 0
    }
}

Write-Error 'Python was not found. Install Python 3, add it to PATH, or set CODEX_BARK_NOTIFY_PYTHON to python.exe.'
