<# Guided setup using uv when installed, with a Python/venv fallback. #>
[CmdletBinding()]
param([switch]$NoBrowser, [switch]$Plain)
$ErrorActionPreference = "Stop"
$repoPath = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $repoPath
try {
    $setupArgs = @()
    if ($NoBrowser) { $setupArgs += "--no-browser" }
    if ($Plain) { $setupArgs += "--plain" }
    $uvCommand = Get-Command uv -CommandType Application -ErrorAction SilentlyContinue
    if ($uvCommand) {
        & $uvCommand.Source sync --frozen --no-dev
        if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }
        & $uvCommand.Source run --no-sync -m mitra_bot.setup_wizard @setupArgs
    } else {
        $pythonCommand = Get-Command py -CommandType Application -ErrorAction SilentlyContinue
        if (-not $pythonCommand) { $pythonCommand = Get-Command python -CommandType Application -ErrorAction SilentlyContinue }
        if (-not $pythonCommand) { throw "Install Python 3.10+ from https://www.python.org/downloads/ or uv from https://docs.astral.sh/uv/getting-started/installation/ and run setup again." }
        if (-not (Test-Path -LiteralPath ".venv\Scripts\python.exe")) {
            & $pythonCommand.Source -m venv .venv
            if ($LASTEXITCODE -ne 0) { throw "Could not create the Python environment." }
        }
        & .\.venv\Scripts\python.exe -m pip install -e .
        if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }
        & .\.venv\Scripts\python.exe -m mitra_bot.setup_wizard @setupArgs
    }
    if ($LASTEXITCODE -ne 0) { throw "Guided setup did not finish. Correct the reported issue and rerun it." }
} finally { Pop-Location }
