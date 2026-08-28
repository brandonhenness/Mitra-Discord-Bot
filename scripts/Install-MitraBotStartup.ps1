<#
.SYNOPSIS
Installs or replaces Mitra's Windows startup scheduled task.

.DESCRIPTION
Creates an elevated, single-instance task that starts Mitra after Windows has
booted. The task runs as the supplied Windows user whether or not that user is
logged on. Windows securely stores the credential required for that logon
type; this script does not write the password to disk.

.EXAMPLE
.\scripts\Install-MitraBotStartup.ps1

.EXAMPLE
.\scripts\Install-MitraBotStartup.ps1 -AccountName "Mitra"
#>

[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$TaskName = "Mitra Discord Bot",
    [string]$RepoPath,
    [string]$EnvFileName = ".env",
    [string]$AccountName = $env:USERNAME,
    [ValidateRange(0, 3600)]
    [int]$StartupDelaySeconds = 60
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($RepoPath)) {
    $RepoPath = Split-Path -Parent $PSScriptRoot
}
$RepoPath = [System.IO.Path]::GetFullPath($RepoPath)
$launcherPath = Join-Path $RepoPath "scripts\Start-MitraBot.ps1"
$envPath = Join-Path $RepoPath $EnvFileName

# Task Scheduler's password logon type requires a fully qualified UserId.
# Get-Credential accepts an unqualified local username, but passing that value
# through to Register-ScheduledTask produces HRESULT 0x80070057 on some
# Windows versions. Preserve explicitly qualified domain/UPN names and qualify
# simple local names with this computer's NetBIOS name.
if ($AccountName -notmatch '[\\@]') {
    $AccountName = "$env:COMPUTERNAME\$AccountName"
}

foreach ($requiredPath in @($launcherPath, $envPath, (Join-Path $RepoPath "pyproject.toml"))) {
    if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
        throw "Required file not found: '$requiredPath'."
    }
}

$powerShellPath = Join-Path $PSHOME "powershell.exe"
$quotedLauncher = '"' + $launcherPath.Replace('"', '\"') + '"'
$quotedRepo = '"' + $RepoPath.Replace('"', '\"') + '"'
$quotedEnv = '"' + $EnvFileName.Replace('"', '\"') + '"'
$arguments = "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $quotedLauncher -RepoPath $quotedRepo -EnvFileName $quotedEnv"

if ($PSCmdlet.ShouldProcess($TaskName, "Install Windows startup scheduled task")) {
    $action = New-ScheduledTaskAction `
        -Execute $powerShellPath `
        -Argument $arguments `
        -WorkingDirectory $RepoPath
    $trigger = New-ScheduledTaskTrigger -AtStartup
    if ($StartupDelaySeconds -gt 0) {
        $trigger.Delay = "PT${StartupDelaySeconds}S"
    }
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -MultipleInstances IgnoreNew `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -StartWhenAvailable

    $credential = Get-Credential -UserName $AccountName -Message (
        "Enter the Windows password for the account that will run '$TaskName'."
    )
    $plainPassword = $credential.GetNetworkCredential().Password
    if ([string]::IsNullOrEmpty($plainPassword)) {
        throw "A Windows account password is required to run the task before logon."
    }

    try {
        Register-ScheduledTask `
            -TaskName $TaskName `
            -Action $action `
            -Trigger $trigger `
            -Settings $settings `
            -Description "Starts the Mitra Discord bot after Windows boots." `
            -User $credential.UserName `
            -Password $plainPassword `
            -RunLevel Highest `
            -Force | Out-Null

        Write-Host "Installed scheduled task: $TaskName" -ForegroundColor Green
        Write-Host "Start now: Start-ScheduledTask -TaskName '$TaskName'"
        Write-Host "Stop:      Stop-ScheduledTask -TaskName '$TaskName'"
    }
    finally {
        $plainPassword = $null
        $credential = $null
    }
}
