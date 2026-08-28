<#
.SYNOPSIS
Starts Mitra from its repository checkout.

.DESCRIPTION
Resolves the repository and uv executable, changes to the repository so all
relative runtime paths are correct, and starts the locked environment without
synchronizing dependencies. The Windows scheduled task installer uses this
same entrypoint.
#>

[CmdletBinding()]
param(
    [string]$RepoPath,
    [string]$EnvFileName = ".env",
    [string]$UvPath
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($RepoPath)) {
    $RepoPath = Split-Path -Parent $PSScriptRoot
}
$RepoPath = [System.IO.Path]::GetFullPath($RepoPath)
$envPath = Join-Path $RepoPath $EnvFileName

if (-not (Test-Path -LiteralPath (Join-Path $RepoPath "pyproject.toml") -PathType Leaf)) {
    throw "Mitra repository not found at '$RepoPath'."
}
if (-not (Test-Path -LiteralPath $envPath -PathType Leaf)) {
    throw "Runtime environment file not found: '$envPath'."
}

if ([string]::IsNullOrWhiteSpace($UvPath)) {
    $uvCommand = Get-Command uv.exe -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -ne $uvCommand) {
        $UvPath = $uvCommand.Source
    }
    else {
        $UvPath = Join-Path $env:USERPROFILE ".local\bin\uv.exe"
    }
}

if (-not (Test-Path -LiteralPath $UvPath -PathType Leaf)) {
    throw "uv.exe was not found. Expected '$UvPath' or an executable on PATH."
}

Push-Location -LiteralPath $RepoPath
try {
    & $UvPath run --no-sync --env-file $EnvFileName mitra-bot
    if ($LASTEXITCODE -ne 0) {
        throw "Mitra exited with code $LASTEXITCODE."
    }
}
finally {
    Pop-Location
}
