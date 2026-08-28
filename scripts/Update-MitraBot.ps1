<#
.SYNOPSIS
Safely updates a Windows Mitra deployment from GitHub.

.DESCRIPTION
This updater is intentionally backup-first. It requires the operator to stop
the bot, copies runtime data to a timestamped directory outside the repository,
records SHA-256 hashes, reports every dirty Git path, updates with ff-only Git
operations, synchronizes the locked uv environment, migrates a legacy
cache.json when present, and validates the updated installation.

It never starts the bot and never automatically restores a Git stash.

.EXAMPLE
.\scripts\Update-MitraBot.ps1 -ConfirmBotStopped

.EXAMPLE
.\scripts\Update-MitraBot.ps1 -ConfirmBotStopped -AutoStash

.EXAMPLE
.\Update-MitraBot.ps1 `
    -RepoPath "C:\Users\Mitra\Documents\GitHub\Mitra-Discord-Bot" `
    -EnvFileName ".env.production" -ConfirmBotStopped -AutoStash

.EXAMPLE
# Use this only when the bot's launch command explicitly loads .env.
.\scripts\Update-MitraBot.ps1 -EnvFileName ".env" -ConfirmBotStopped
#>

[CmdletBinding()]
param(
    # Optional when this script is in the repository's scripts directory or
    # immediately beside the Mitra-Discord-Bot repository.
    [string]$RepoPath,

    [string]$TargetBranch = "main",

    # Defaults to a sibling of the repository, never inside it.
    [string]$BackupRoot,

    # Relative to the repository. When omitted, the updater selects the only
    # existing .env/.env.production file, refuses ambiguity, or defaults a new
    # deployment to the documented production file (.env.production).
    [string]$EnvFileName,

    # Stash tracked and non-ignored untracked files. The resulting stash is
    # retained for manual review and is never popped automatically.
    [switch]$AutoStash,

    # Required acknowledgement. Stop the bot/service before invoking this
    # script so cache and SQLite files cannot change during backup/migration.
    [switch]$ConfirmBotStopped,

    # Read-only validation of script parsing, redaction, and native stderr/
    # exit-code handling. Does not require a repository or stopped bot.
    [switch]$SelfTest,

    # From the stopped production server, PATCH every configured Cloudflare A
    # record to this server's public IPv4 and require an API read-back match.
    [switch]$VerifyCloudflareWrite
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"

function Step([string]$Message) {
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Protect-OutputText([string]$Text) {
    if ($null -eq $Text) {
        return ""
    }

    $safe = $Text
    # Redact credentials embedded in URLs and common token assignment forms.
    $safe = [regex]::Replace(
        $safe,
        '(?i)(https?://)([^/\s@]+@)',
        '$1***@'
    )
    $safe = [regex]::Replace(
        $safe,
        '(?i)\b(DISCORD_APPLICATION_TOKEN|CLOUDFLARE_API_TOKEN|API_KEY|PASSWORD)\s*[:=]\s*\S+',
        '$1=***'
    )
    $safe = [regex]::Replace(
        $safe,
        '\bgh[pousr]_[A-Za-z0-9_]{20,}\b',
        '***REDACTED_GITHUB_TOKEN***'
    )
    $safe = [regex]::Replace(
        $safe,
        '\b[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{20,}\b',
        '***REDACTED_DISCORD_TOKEN***'
    )
    return $safe
}

function Write-SafeExternalOutput([object[]]$Output) {
    foreach ($line in @($Output)) {
        if ($null -ne $line) {
            Write-Host (Protect-OutputText ([string]$line))
        }
    }
}

function Invoke-NativeCapture(
    [string]$FilePath,
    [string[]]$Arguments,
    [string]$WorkingDirectory
) {
    # Windows PowerShell 5.1 can promote redirected native stderr records to a
    # terminating error when the caller uses ErrorActionPreference=Stop. Keep
    # that preference strict everywhere else, but scope native capture to
    # Continue and always inspect the real process exit code ourselves.
    $previousPreference = $ErrorActionPreference
    $locationPushed = $false
    $nativeOutput = @()
    $nativeExitCode = $null
    try {
        $ErrorActionPreference = "Continue"
        if (-not [string]::IsNullOrWhiteSpace($WorkingDirectory)) {
            Push-Location -LiteralPath $WorkingDirectory
            $locationPushed = $true
        }
        $global:LASTEXITCODE = $null
        $nativeOutput = @(& $FilePath @Arguments 2>&1)
        $nativeExitCode = $LASTEXITCODE
    } catch {
        # Invocation failures are captured for redacted diagnostics. They use
        # a synthetic exit code because no trustworthy native code exists.
        $nativeOutput = @($_)
        $nativeExitCode = -1
    } finally {
        if ($locationPushed) {
            Pop-Location
        }
        $ErrorActionPreference = $previousPreference
    }

    if ($null -eq $nativeExitCode) {
        $nativeExitCode = -1
    }
    return [pscustomobject]@{
        ExitCode = [int]$nativeExitCode
        Output = @($nativeOutput)
    }
}

function Invoke-UpdaterSelfTest {
    $tokens = $null
    $parseErrors = $null
    [System.Management.Automation.Language.Parser]::ParseFile(
        $PSCommandPath,
        [ref]$tokens,
        [ref]$parseErrors
    ) | Out-Null
    if (@($parseErrors).Count -ne 0) {
        throw "Updater syntax self-test failed."
    }

    $preferenceBefore = $ErrorActionPreference
    $powershellExe = Join-Path $PSHOME "powershell.exe"
    $probe = Invoke-NativeCapture $powershellExe @(
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        "[Console]::Error.WriteLine('mitra-native-stderr-probe'); exit 7"
    ) ""
    if ($probe.ExitCode -ne 7) {
        throw "Native exit-code self-test failed."
    }
    $probeText = (@($probe.Output) | ForEach-Object { [string]$_ }) -join "`n"
    if ($probeText -notmatch 'mitra-native-stderr-probe') {
        throw "Native stderr capture self-test failed."
    }
    if ($ErrorActionPreference -ne $preferenceBefore) {
        throw "Native capture did not restore ErrorActionPreference."
    }

    $fakeSecret = "DISCORD_APPLICATION_TOKEN=this-is-a-fake-self-test-secret"
    if ((Protect-OutputText $fakeSecret) -match 'fake-self-test-secret') {
        throw "Output redaction self-test failed."
    }

    $relationship = ConvertFrom-GitAheadBehind @("2`t3")
    if ($relationship.Ahead -ne 2 -or $relationship.Behind -ne 3) {
        throw "Git ahead/behind parser self-test failed."
    }
    Write-Host "Updater syntax/native-capture/redaction self-test: OK" -ForegroundColor Green
}

function Get-FullPath([string]$Path) {
    if ([string]::IsNullOrWhiteSpace($Path)) {
        throw "A required path was empty."
    }
    return [System.IO.Path]::GetFullPath($Path)
}

function Test-PathAtOrUnder([string]$Candidate, [string]$Parent) {
    $candidateFull = (Get-FullPath $Candidate).TrimEnd('\', '/')
    $parentFull = (Get-FullPath $Parent).TrimEnd('\', '/')
    if ($candidateFull.Equals($parentFull, [System.StringComparison]::OrdinalIgnoreCase)) {
        return $true
    }
    $prefix = $parentFull + [System.IO.Path]::DirectorySeparatorChar
    return $candidateFull.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)
}

function Resolve-RepoPath([string]$ExplicitPath) {
    $candidates = New-Object System.Collections.ArrayList
    if (-not [string]::IsNullOrWhiteSpace($ExplicitPath)) {
        [void]$candidates.Add($ExplicitPath)
    } else {
        if (-not [string]::IsNullOrWhiteSpace($PSScriptRoot)) {
            [void]$candidates.Add($PSScriptRoot)
            [void]$candidates.Add((Split-Path -Parent $PSScriptRoot))
            [void]$candidates.Add((Join-Path $PSScriptRoot "Mitra-Discord-Bot"))
        }
        [void]$candidates.Add((Get-Location).Path)
    }

    $seen = @{}
    foreach ($candidate in $candidates) {
        try {
            $full = Get-FullPath ([string]$candidate)
        } catch {
            continue
        }
        $key = $full.ToLowerInvariant()
        if ($seen.ContainsKey($key)) {
            continue
        }
        $seen[$key] = $true
        if (
            (Test-Path -LiteralPath $full -PathType Container) -and
            (Test-Path -LiteralPath (Join-Path $full ".git") -PathType Container)
        ) {
            return (Resolve-Path -LiteralPath $full).Path
        }
    }

    throw "Could not locate the Mitra Git repository. Pass -RepoPath explicitly."
}

function Assert-MitraRepository([string]$ResolvedRepoPath) {
    $pyprojectPath = Join-Path $ResolvedRepoPath "pyproject.toml"
    $lockPath = Join-Path $ResolvedRepoPath "uv.lock"
    $packageDirectory = Join-Path $ResolvedRepoPath "mitra_bot"
    $packageInit = Join-Path $packageDirectory "__init__.py"
    $packageMain = Join-Path $packageDirectory "main.py"

    foreach ($requiredPath in @($pyprojectPath, $lockPath, $packageInit, $packageMain)) {
        if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
            throw "Repository identity check failed: a required Mitra project file is missing."
        }
    }

    try {
        $pyprojectText = [System.IO.File]::ReadAllText($pyprojectPath)
        $projectSection = [regex]::Match(
            $pyprojectText,
            '(?ms)^\s*\[project\]\s*(.*?)(?=^\s*\[|\z)'
        )
        if (-not $projectSection.Success) {
            throw "project section missing"
        }
        $nameMatch = [regex]::Match(
            $projectSection.Groups[1].Value,
            '(?mi)^\s*name\s*=\s*["'']mitra-discord-bot["'']\s*(?:#.*)?$'
        )
        if (-not $nameMatch.Success) {
            throw "project name mismatch"
        }
    } catch {
        throw "Repository identity check failed: pyproject.toml is not the Mitra package."
    }
}

function Resolve-BackupRoot([string]$ExplicitPath, [string]$ResolvedRepoPath) {
    if ([string]::IsNullOrWhiteSpace($ExplicitPath)) {
        $repoParent = Split-Path -Parent $ResolvedRepoPath
        $repoName = Split-Path -Leaf $ResolvedRepoPath
        $full = Get-FullPath (Join-Path $repoParent ($repoName + "-backups"))
    } else {
        $full = Get-FullPath $ExplicitPath
    }

    if (Test-PathAtOrUnder $full $ResolvedRepoPath) {
        throw "BackupRoot must be outside the repository: $full"
    }
    return $full
}

function Resolve-RepoDestination([string]$ResolvedRepoPath, [string]$RelativePath) {
    if ([string]::IsNullOrWhiteSpace($RelativePath)) {
        throw "EnvFileName cannot be empty."
    }
    if ([System.IO.Path]::IsPathRooted($RelativePath)) {
        throw "EnvFileName must be relative to the repository."
    }
    if ((Split-Path -Leaf $RelativePath) -ne $RelativePath) {
        throw "EnvFileName must be a top-level file name such as .env or .env.production."
    }
    $full = Get-FullPath (Join-Path $ResolvedRepoPath $RelativePath)
    if (-not (Test-PathAtOrUnder $full $ResolvedRepoPath)) {
        throw "EnvFileName resolves outside the repository."
    }
    return $full
}

function Resolve-EnvFileName([string]$ResolvedRepoPath, [string]$ExplicitName) {
    if (-not [string]::IsNullOrWhiteSpace($ExplicitName)) {
        return $ExplicitName
    }

    $developmentEnv = Join-Path $ResolvedRepoPath ".env"
    $productionEnv = Join-Path $ResolvedRepoPath ".env.production"
    $hasDevelopmentEnv = Test-Path -LiteralPath $developmentEnv -PathType Leaf
    $hasProductionEnv = Test-Path -LiteralPath $productionEnv -PathType Leaf

    if ($hasDevelopmentEnv -and $hasProductionEnv) {
        throw "Both .env and .env.production exist. Pass -EnvFileName explicitly to match the bot's launch command."
    }
    if ($hasProductionEnv) {
        return ".env.production"
    }
    if ($hasDevelopmentEnv) {
        return ".env"
    }
    return ".env.production"
}

function ConvertFrom-EnvPathValue([string]$RawValue, [string]$VariableName) {
    $value = $RawValue.Trim()
    if ([string]::IsNullOrWhiteSpace($value)) {
        return ""
    }

    if ($value[0] -eq '"' -or $value[0] -eq "'") {
        $quote = $value[0]
        $closingIndex = $value.IndexOf($quote, 1)
        if ($closingIndex -lt 1) {
            throw "Selected runtime env file has an invalid quoted $VariableName value."
        }
        $trailing = $value.Substring($closingIndex + 1).Trim()
        if ($trailing -and -not $trailing.StartsWith("#")) {
            throw "Selected runtime env file has trailing text after $VariableName."
        }
        return $value.Substring(1, $closingIndex - 1)
    }

    $commentMatch = [regex]::Match($value, '\s+#')
    if ($commentMatch.Success) {
        $value = $value.Substring(0, $commentMatch.Index).TrimEnd()
    }
    return $value
}

function Read-RuntimePathOverrides([string]$ResolvedEnvPath) {
    $values = @{}
    $seen = @{}
    if (-not (Test-Path -LiteralPath $ResolvedEnvPath -PathType Leaf)) {
        return $values
    }

    try {
        foreach ($rawLine in [System.IO.File]::ReadAllLines($ResolvedEnvPath)) {
            $match = [regex]::Match(
                $rawLine,
                '^\s*(?:export\s+)?(?<key>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?<value>.*)$'
            )
            if (-not $match.Success) {
                continue
            }
            $key = $match.Groups["key"].Value.ToUpperInvariant()
            if ($key -ne "MITRA_CONFIG_PATH" -and $key -ne "MITRA_STATE_PATH") {
                continue
            }
            if ($seen.ContainsKey($key)) {
                throw "Selected runtime env file defines $key more than once."
            }
            $seen[$key] = $true
            $values[$key] = ConvertFrom-EnvPathValue $match.Groups["value"].Value $key
        }
    } catch {
        if ($_.Exception.Message -like "Selected runtime env file*") {
            throw
        }
        throw "Could not parse runtime path overrides from the selected env file."
    }
    return $values
}

function Read-SelectedEnvSecrets([string]$ResolvedEnvPath) {
    $values = @{}
    $seen = @{}
    if (-not (Test-Path -LiteralPath $ResolvedEnvPath -PathType Leaf)) {
        return $values
    }

    try {
        foreach ($rawLine in [System.IO.File]::ReadAllLines($ResolvedEnvPath)) {
            $match = [regex]::Match(
                $rawLine,
                '^\s*(?:export\s+)?(?<key>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?<value>.*)$'
            )
            if (-not $match.Success) {
                continue
            }
            $key = $match.Groups["key"].Value.ToUpperInvariant()
            if ($key -ne "DISCORD_APPLICATION_TOKEN" -and $key -ne "CLOUDFLARE_API_TOKEN") {
                continue
            }
            if ($seen.ContainsKey($key)) {
                throw "Selected runtime env file defines $key more than once."
            }
            $seen[$key] = $true
            $values[$key] = ConvertFrom-EnvPathValue $match.Groups["value"].Value $key
        }
    } catch {
        if ($_.Exception.Message -like "Selected runtime env file*") {
            throw
        }
        throw "Could not inspect required secrets in the selected env file."
    }
    return $values
}

function Assert-SelectedEnvSecretCompatibility([string]$ResolvedEnvPath) {
    $fileSecrets = Read-SelectedEnvSecrets $ResolvedEnvPath
    foreach ($key in @("DISCORD_APPLICATION_TOKEN", "CLOUDFLARE_API_TOKEN")) {
        $fromFile = if ($fileSecrets.ContainsKey($key)) { ([string]$fileSecrets[$key]).Trim() } else { "" }
        $fromProcess = ([string][System.Environment]::GetEnvironmentVariable($key, "Process")).Trim()
        if ($fromProcess -and -not $fromFile) {
            throw "Process environment defines $key but the selected env file does not. Put the service credential in the selected env file before updating."
        }
        if ($fromProcess -and $fromFile -and $fromProcess -ne $fromFile) {
            throw "$key differs between the selected env file and process environment. Resolve the conflict before updating."
        }
    }
}

function Resolve-RuntimeSettingPath(
    [string]$ResolvedRepoPath,
    [string]$VariableName,
    [string]$FileValue,
    [string]$ProcessValue,
    [string]$DefaultName
) {
    function Resolve-OneRuntimePath([string]$RawValue) {
        if ([string]::IsNullOrWhiteSpace($RawValue)) {
            return $null
        }
        try {
            if ([System.IO.Path]::IsPathRooted($RawValue)) {
                return (Get-FullPath $RawValue)
            }
            return (Get-FullPath (Join-Path $ResolvedRepoPath $RawValue))
        } catch {
            throw "$VariableName does not contain a valid filesystem path."
        }
    }

    $fromFile = Resolve-OneRuntimePath $FileValue
    $fromProcess = Resolve-OneRuntimePath $ProcessValue
    if (
        $fromFile -and $fromProcess -and
        -not $fromFile.Equals($fromProcess, [System.StringComparison]::OrdinalIgnoreCase)
    ) {
        throw "$VariableName differs between the selected env file and process environment. Refusing ambiguous runtime storage."
    }
    if ($fromProcess) {
        return $fromProcess
    }
    if ($fromFile) {
        return $fromFile
    }
    return (Get-FullPath (Join-Path $ResolvedRepoPath $DefaultName))
}

function Resolve-RuntimeDataPaths(
    [string]$ResolvedRepoPath,
    [string]$ResolvedEnvPath,
    [string]$ResolvedBackupRoot
) {
    $fileOverrides = Read-RuntimePathOverrides $ResolvedEnvPath
    $fileConfig = if ($fileOverrides.ContainsKey("MITRA_CONFIG_PATH")) { [string]$fileOverrides["MITRA_CONFIG_PATH"] } else { "" }
    $fileState = if ($fileOverrides.ContainsKey("MITRA_STATE_PATH")) { [string]$fileOverrides["MITRA_STATE_PATH"] } else { "" }
    $processConfig = [System.Environment]::GetEnvironmentVariable("MITRA_CONFIG_PATH", "Process")
    $processState = [System.Environment]::GetEnvironmentVariable("MITRA_STATE_PATH", "Process")

    $configPath = Resolve-RuntimeSettingPath $ResolvedRepoPath "MITRA_CONFIG_PATH" $fileConfig $processConfig "config.toml"
    $statePath = Resolve-RuntimeSettingPath $ResolvedRepoPath "MITRA_STATE_PATH" $fileState $processState "state.db"

    if ($configPath.Equals($statePath, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "MITRA_CONFIG_PATH and MITRA_STATE_PATH resolve to the same file."
    }
    if (
        $configPath.Equals($ResolvedEnvPath, [System.StringComparison]::OrdinalIgnoreCase) -or
        $statePath.Equals($ResolvedEnvPath, [System.StringComparison]::OrdinalIgnoreCase)
    ) {
        throw "A runtime config/state path collides with the selected env file."
    }
    $legacyCachePath = Join-Path $ResolvedRepoPath "cache.json"
    $protectedFiles = @(
        $ResolvedEnvPath,
        $legacyCachePath,
        (Join-Path $ResolvedRepoPath "pyproject.toml"),
        (Join-Path $ResolvedRepoPath "uv.lock")
    )
    $stateFamily = @(
        $statePath,
        ($statePath + "-wal"),
        ($statePath + "-shm"),
        ($statePath + "-journal")
    )
    foreach ($protectedFile in $protectedFiles) {
        if ($configPath.Equals($protectedFile, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Runtime config path collides with a protected project/runtime file."
        }
        foreach ($stateFile in $stateFamily) {
            if ($stateFile.Equals($protectedFile, [System.StringComparison]::OrdinalIgnoreCase)) {
                throw "Runtime state database family collides with a protected project/runtime file."
            }
        }
    }
    foreach ($stateFile in $stateFamily) {
        if ($stateFile.Equals($configPath, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Runtime config path collides with the state database family."
        }
    }
    $gitDirectory = Join-Path $ResolvedRepoPath ".git"
    if ((Test-PathAtOrUnder $configPath $gitDirectory) -or (Test-PathAtOrUnder $statePath $gitDirectory)) {
        throw "Runtime config/state paths cannot be stored inside .git."
    }
    if ((Test-PathAtOrUnder $configPath $ResolvedBackupRoot) -or (Test-PathAtOrUnder $statePath $ResolvedBackupRoot)) {
        throw "Runtime config/state paths cannot be stored inside BackupRoot."
    }
    foreach ($candidate in @($configPath, $statePath)) {
        if (Test-Path -LiteralPath $candidate -PathType Container) {
            throw "A runtime config/state destination resolves to a directory."
        }
    }

    return [pscustomobject]@{
        ConfigPath = $configPath
        StatePath = $statePath
    }
}

function Protect-BackupDirectoryAcl([string]$DirectoryPath) {
    try {
        $currentSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
        $systemSid = New-Object System.Security.Principal.SecurityIdentifier(
            [System.Security.Principal.WellKnownSidType]::LocalSystemSid,
            $null
        )
        $administratorsSid = New-Object System.Security.Principal.SecurityIdentifier(
            [System.Security.Principal.WellKnownSidType]::BuiltinAdministratorsSid,
            $null
        )

        $rights = [System.Security.AccessControl.FileSystemRights]::FullControl
        $inheritance = (
            [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
            [System.Security.AccessControl.InheritanceFlags]::ObjectInherit
        )
        $propagation = [System.Security.AccessControl.PropagationFlags]::None
        $allow = [System.Security.AccessControl.AccessControlType]::Allow

        $security = New-Object System.Security.AccessControl.DirectorySecurity
        $security.SetOwner($currentSid)
        # Disable inherited access and copy none of the previous inherited
        # rules. Backups can contain bot and Cloudflare credentials.
        $security.SetAccessRuleProtection($true, $false)
        foreach ($sid in @($currentSid, $systemSid, $administratorsSid)) {
            $rule = New-Object System.Security.AccessControl.FileSystemAccessRule -ArgumentList @(
                $sid, $rights, $inheritance, $propagation, $allow
            )
            [void]$security.AddAccessRule($rule)
        }
        Set-Acl -LiteralPath $DirectoryPath -AclObject $security

        $verified = Get-Acl -LiteralPath $DirectoryPath
        if (-not $verified.AreAccessRulesProtected) {
            throw "Backup directory still inherits access rules."
        }
        $currentSidFound = $false
        foreach ($rule in $verified.Access) {
            try {
                $ruleSid = $rule.IdentityReference.Translate(
                    [System.Security.Principal.SecurityIdentifier]
                )
                if (
                    $ruleSid.Value -eq $currentSid.Value -and
                    $rule.AccessControlType -eq $allow -and
                    (($rule.FileSystemRights -band $rights) -eq $rights)
                ) {
                    $currentSidFound = $true
                }
            } catch {
                continue
            }
        }
        if (-not $currentSidFound) {
            throw "Current operator does not have verified full control of the backup."
        }
    } catch {
        throw "Could not apply and verify a private Windows ACL on backup directory: $DirectoryPath"
    }
}

function Resolve-GitExe {
    $command = Get-Command git.exe -CommandType Application -ErrorAction SilentlyContinue
    if ($command -and $command.Source -and (Test-Path -LiteralPath $command.Source)) {
        return $command.Source
    }

    $whereResult = Invoke-NativeCapture "where.exe" @("git.exe") ""
    if ($whereResult.ExitCode -eq 0) {
        foreach ($candidate in @($whereResult.Output)) {
            if (Test-Path -LiteralPath ([string]$candidate) -PathType Leaf) {
                return ([string]$candidate)
            }
        }
    }
    throw "Could not find git.exe. Install Git for Windows and put it on PATH."
}

function Resolve-UvExe {
    $command = Get-Command uv.exe -CommandType Application -ErrorAction SilentlyContinue
    if ($command -and $command.Source -and (Test-Path -LiteralPath $command.Source)) {
        return $command.Source
    }

    $whereResult = Invoke-NativeCapture "where.exe" @("uv.exe") ""
    if ($whereResult.ExitCode -eq 0) {
        foreach ($candidate in @($whereResult.Output)) {
            if (Test-Path -LiteralPath ([string]$candidate) -PathType Leaf) {
                return ([string]$candidate)
            }
        }
    }

    $commonCandidates = @(
        (Join-Path $env:USERPROFILE ".local\bin\uv.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\uv\uv.exe")
    )
    foreach ($candidate in $commonCandidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return $candidate
        }
    }
    throw "Could not find uv.exe. Install uv before running this updater."
}

function Invoke-Git([string[]]$GitArgs) {
    $nativeArgs = @("-C", $script:ResolvedRepoPath) + $GitArgs
    $result = Invoke-NativeCapture $script:GitExe $nativeArgs ""
    Write-SafeExternalOutput $result.Output
    if ($result.ExitCode -ne 0) {
        $verb = if ($GitArgs.Count -gt 0) { $GitArgs[0] } else { "command" }
        throw "git $verb failed with exit code $($result.ExitCode)."
    }
}

function Get-GitOutput([string[]]$GitArgs) {
    $nativeArgs = @("-C", $script:ResolvedRepoPath) + $GitArgs
    $result = Invoke-NativeCapture $script:GitExe $nativeArgs ""
    if ($result.ExitCode -ne 0) {
        $verb = if ($GitArgs.Count -gt 0) { $GitArgs[0] } else { "command" }
        Write-SafeExternalOutput $result.Output
        throw "git $verb failed with exit code $($result.ExitCode)."
    }
    # Let PowerShell enumerate the lines. Callers wrap the invocation in @()
    # so zero, one, and many output lines remain distinguishable.
    return $result.Output
}

function Test-GitRef([string]$RefName) {
    $nativeArgs = @("-C", $script:ResolvedRepoPath, "show-ref", "--verify", "--quiet", $RefName)
    $result = Invoke-NativeCapture $script:GitExe $nativeArgs ""
    if ($result.ExitCode -eq 0) {
        return $true
    }
    if ($result.ExitCode -eq 1) {
        return $false
    }
    Write-SafeExternalOutput $result.Output
    throw "git show-ref failed with exit code $($result.ExitCode)."
}

function Get-GitCommitId([string]$RefName) {
    $commitLines = @(Get-GitOutput @("rev-parse", "--verify", ($RefName + "^{commit}")))
    if ($commitLines.Count -ne 1) {
        throw "Git did not return exactly one commit for the requested ref."
    }
    $commitId = ([string]$commitLines[0]).Trim()
    if ($commitId -notmatch '^[0-9a-fA-F]{40,64}$') {
        throw "Git returned an invalid commit ID for the requested ref."
    }
    return $commitId
}

function ConvertFrom-GitAheadBehind([object[]]$Output) {
    $lines = @(
        @($Output) |
            ForEach-Object { ([string]$_).Trim() } |
            Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
    )
    if ($lines.Count -ne 1) {
        throw "Git ahead/behind response was ambiguous."
    }
    $match = [regex]::Match($lines[0], '^(\d+)\s+(\d+)$')
    if (-not $match.Success) {
        throw "Git ahead/behind response was invalid."
    }
    return [pscustomobject]@{
        Ahead = [long]::Parse($match.Groups[1].Value)
        Behind = [long]::Parse($match.Groups[2].Value)
    }
}

function Get-GitAheadBehind([string]$LocalRef, [string]$RemoteRef) {
    $range = "{0}...{1}" -f $LocalRef, $RemoteRef
    $output = @(Get-GitOutput @("rev-list", "--left-right", "--count", $range))
    return ConvertFrom-GitAheadBehind $output
}

function Invoke-Uv([string[]]$UvArgs) {
    $result = Invoke-NativeCapture $script:UvExe $UvArgs ""
    Write-SafeExternalOutput $result.Output
    if ($result.ExitCode -ne 0) {
        $verb = if ($UvArgs.Count -gt 0) { $UvArgs[0] } else { "command" }
        throw "uv $verb failed with exit code $($result.ExitCode)."
    }
}

function Add-BackupCandidate(
    [System.Collections.ArrayList]$Candidates,
    [hashtable]$Seen,
    [string]$Source,
    [string]$BackupRelativePath
) {
    if (-not (Test-Path -LiteralPath $Source -PathType Leaf)) {
        return
    }
    $sourceFull = (Resolve-Path -LiteralPath $Source).Path
    $key = $sourceFull.ToLowerInvariant()
    if ($Seen.ContainsKey($key)) {
        return
    }
    $Seen[$key] = $true
    [void]$Candidates.Add([pscustomobject]@{
        Source = $sourceFull
        BackupRelativePath = $BackupRelativePath
    })
}

function Get-ConfiguredLegacyUpsLog([string]$LegacyCachePath, [string]$ResolvedRepoPath) {
    if (-not (Test-Path -LiteralPath $LegacyCachePath -PathType Leaf)) {
        return $null
    }
    try {
        $legacy = [System.IO.File]::ReadAllText($LegacyCachePath) | ConvertFrom-Json
        if ($legacy -and $legacy.ups -and $legacy.ups.log_file) {
            $raw = [string]$legacy.ups.log_file
            if ([System.IO.Path]::IsPathRooted($raw)) {
                return (Get-FullPath $raw)
            }
            return (Get-FullPath (Join-Path $ResolvedRepoPath $raw))
        }
    } catch {
        # The migration command will report malformed cache JSON after the raw
        # file has been backed up. Log discovery must not expose cache content.
    }
    return $null
}

function Get-ConfiguredTomlUpsLog([string]$ConfigPath, [string]$ResolvedRepoPath) {
    if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
        return $null
    }
    try {
        $insideUps = $false
        foreach ($line in [System.IO.File]::ReadAllLines($ConfigPath)) {
            if ($line -match '^\s*\[([^\]]+)\]\s*(?:#.*)?$') {
                $insideUps = ($matches[1].Trim().ToLowerInvariant() -eq "ups")
                continue
            }
            if ($insideUps -and $line -match '^\s*log_file\s*=\s*"([^"]+)"') {
                $raw = [string]$matches[1]
                if ([System.IO.Path]::IsPathRooted($raw)) {
                    return (Get-FullPath $raw)
                }
                return (Get-FullPath (Join-Path $ResolvedRepoPath $raw))
            }
        }
    } catch {
        # Config validation later produces a safe error. The known/default log
        # patterns below are still backed up.
    }
    return $null
}

function Backup-RuntimeFiles(
    [string]$ResolvedRepoPath,
    [string]$ResolvedBackupRoot,
    [string]$ResolvedEnvPath,
    [string]$ResolvedConfigPath,
    [string]$ResolvedStatePath
) {
    if (Test-Path -LiteralPath $ResolvedBackupRoot -PathType Leaf) {
        throw "BackupRoot is a file, not a directory: $ResolvedBackupRoot"
    }
    if (-not (Test-Path -LiteralPath $ResolvedBackupRoot -PathType Container)) {
        New-Item -ItemType Directory -Path $ResolvedBackupRoot -Force | Out-Null
    }

    $stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMdd-HHmmss")
    $backupDirectory = Join-Path $ResolvedBackupRoot $stamp
    if (Test-Path -LiteralPath $backupDirectory) {
        $backupDirectory = Join-Path $ResolvedBackupRoot ($stamp + "-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
    }
    New-Item -ItemType Directory -Path $backupDirectory -Force:$false | Out-Null
    Protect-BackupDirectoryAcl $backupDirectory

    $candidates = New-Object System.Collections.ArrayList
    $seen = @{}
    $legacyCache = Join-Path $ResolvedRepoPath "cache.json"
    $defaultConfigPath = Join-Path $ResolvedRepoPath "config.toml"
    $defaultStatePath = Join-Path $ResolvedRepoPath "state.db"

    Add-BackupCandidate $candidates $seen $legacyCache "runtime\cache.json"
    Add-BackupCandidate $candidates $seen $ResolvedConfigPath "runtime\config.toml"
    Add-BackupCandidate $candidates $seen $ResolvedStatePath "runtime\state.db"
    Add-BackupCandidate $candidates $seen ($ResolvedStatePath + "-wal") "runtime\state.db-wal"
    Add-BackupCandidate $candidates $seen ($ResolvedStatePath + "-shm") "runtime\state.db-shm"
    Add-BackupCandidate $candidates $seen ($ResolvedStatePath + "-journal") "runtime\state.db-journal"

    # Preserve stale/default-path files too when active runtime overrides point
    # elsewhere. They are not migration targets, but may still be useful for
    # recovery or diagnosing an older launch configuration.
    Add-BackupCandidate $candidates $seen $defaultConfigPath "runtime\repo-default-config.toml"
    Add-BackupCandidate $candidates $seen $defaultStatePath "runtime\repo-default-state.db"
    Add-BackupCandidate $candidates $seen ($defaultStatePath + "-wal") "runtime\repo-default-state.db-wal"
    Add-BackupCandidate $candidates $seen ($defaultStatePath + "-shm") "runtime\repo-default-state.db-shm"
    Add-BackupCandidate $candidates $seen ($defaultStatePath + "-journal") "runtime\repo-default-state.db-journal"
    Add-BackupCandidate $candidates $seen (Join-Path $ResolvedRepoPath "bot.log") "runtime\bot.log"

    # Back up every non-template top-level env variant without reading or
    # displaying its contents.
    $envFiles = @(
        Get-ChildItem -LiteralPath $ResolvedRepoPath -Force -File -Filter ".env*" |
            Where-Object { $_.Name -notlike "*.example" }
    )
    foreach ($envFile in $envFiles) {
        Add-BackupCandidate $candidates $seen $envFile.FullName ("runtime\env\" + $envFile.Name)
    }
    Add-BackupCandidate $candidates $seen $ResolvedEnvPath ("runtime\env\" + (Split-Path -Leaf $ResolvedEnvPath))

    # Include the default and commonly named UPS logs, plus any legacy/config
    # log path we can discover without evaluating configuration as code.
    $logFiles = New-Object System.Collections.ArrayList
    foreach ($pattern in @("*.jsonl", "ups*.log", "ups*.csv")) {
        foreach ($logFile in @(Get-ChildItem -Path (Join-Path $ResolvedRepoPath $pattern) -Force -File -ErrorAction SilentlyContinue)) {
            [void]$logFiles.Add($logFile)
        }
    }
    $logsDirectory = Join-Path $ResolvedRepoPath "logs"
    if (Test-Path -LiteralPath $logsDirectory -PathType Container) {
        foreach ($logFile in @(Get-ChildItem -LiteralPath $logsDirectory -Force -File -Recurse -ErrorAction SilentlyContinue)) {
            [void]$logFiles.Add($logFile)
        }
    }

    $legacyConfiguredLog = Get-ConfiguredLegacyUpsLog $legacyCache $ResolvedRepoPath
    if ($legacyConfiguredLog) {
        [void]$logFiles.Add([pscustomobject]@{ FullName = $legacyConfiguredLog; Name = (Split-Path -Leaf $legacyConfiguredLog) })
    }
    $tomlConfiguredLog = Get-ConfiguredTomlUpsLog $ResolvedConfigPath $ResolvedRepoPath
    if ($tomlConfiguredLog) {
        [void]$logFiles.Add([pscustomobject]@{ FullName = $tomlConfiguredLog; Name = (Split-Path -Leaf $tomlConfiguredLog) })
    }

    $logIndex = 0
    foreach ($logFile in $logFiles) {
        $logIndex += 1
        $safeName = ([string]$logFile.Name) -replace '[^A-Za-z0-9._-]', '_'
        Add-BackupCandidate $candidates $seen ([string]$logFile.FullName) ("runtime\logs\{0:D3}-{1}" -f $logIndex, $safeName)
    }

    $manifestFiles = New-Object System.Collections.ArrayList
    $backedUpLegacyCache = $null
    $backedUpLegacyCacheHash = $null
    foreach ($candidate in $candidates) {
        $destination = Join-Path $backupDirectory ([string]$candidate.BackupRelativePath)
        $destinationParent = Split-Path -Parent $destination
        if (-not (Test-Path -LiteralPath $destinationParent -PathType Container)) {
            New-Item -ItemType Directory -Path $destinationParent -Force | Out-Null
        }

        $sourceHashBefore = (Get-FileHash -LiteralPath $candidate.Source -Algorithm SHA256).Hash.ToLowerInvariant()
        Copy-Item -LiteralPath $candidate.Source -Destination $destination -Force:$false
        $destinationHash = (Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash.ToLowerInvariant()
        $sourceHashAfter = (Get-FileHash -LiteralPath $candidate.Source -Algorithm SHA256).Hash.ToLowerInvariant()

        if ($sourceHashBefore -ne $destinationHash -or $sourceHashAfter -ne $destinationHash) {
            throw "A runtime file changed during backup. Keep the partial backup, verify the bot is stopped, and retry."
        }

        $sourceLength = (Get-Item -LiteralPath $candidate.Source).Length
        $destinationLength = (Get-Item -LiteralPath $destination).Length
        if ($sourceLength -ne $destinationLength) {
            throw "A runtime backup size check failed. Keep the partial backup and stop."
        }

        [void]$manifestFiles.Add([ordered]@{
            backup_path = [string]$candidate.BackupRelativePath
            sha256 = $destinationHash
            size_bytes = [long]$destinationLength
            source_path = [string]$candidate.Source
        })

        if ($candidate.Source.Equals($legacyCache, [System.StringComparison]::OrdinalIgnoreCase)) {
            $backedUpLegacyCache = $destination
            $backedUpLegacyCacheHash = $destinationHash
        }
    }

    $manifest = [ordered]@{
        created_at_utc = (Get-Date).ToUniversalTime().ToString("o")
        repository = $ResolvedRepoPath
        files = @($manifestFiles)
    }
    $manifestPath = Join-Path $backupDirectory "manifest.json"
    $manifestTemp = $manifestPath + ".tmp"
    $manifest | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $manifestTemp -Encoding UTF8
    Move-Item -LiteralPath $manifestTemp -Destination $manifestPath -Force

    # Parse the manifest back before allowing any Git mutation.
    $verifiedManifest = [System.IO.File]::ReadAllText($manifestPath) | ConvertFrom-Json
    if ($null -eq $verifiedManifest -or $null -eq $verifiedManifest.files) {
        throw "Backup manifest verification failed. No Git changes were made."
    }
    if (@($verifiedManifest.files).Count -ne $manifestFiles.Count) {
        throw "Backup manifest file count verification failed. No Git changes were made."
    }

    return [pscustomobject]@{
        Directory = $backupDirectory
        ManifestPath = $manifestPath
        LegacyCachePath = $backedUpLegacyCache
        LegacyCacheSha256 = $backedUpLegacyCacheHash
        FileCount = $manifestFiles.Count
    }
}

function Retire-LegacyCache(
    [string]$ResolvedRepoPath,
    [string]$BackedUpCachePath,
    [string]$ExpectedCacheSha256,
    [string]$BackupDirectory
) {
    if (-not (Test-Path -LiteralPath $BackedUpCachePath -PathType Leaf)) {
        throw "Verified legacy-cache backup is missing; refusing retirement."
    }

    $repoCachePath = Join-Path $ResolvedRepoPath "cache.json"
    $backupHash = (Get-FileHash -LiteralPath $BackedUpCachePath -Algorithm SHA256).Hash.ToLowerInvariant()
    if (
        [string]::IsNullOrWhiteSpace($ExpectedCacheSha256) -or
        $backupHash -ne $ExpectedCacheSha256.ToLowerInvariant()
    ) {
        throw "Legacy-cache backup no longer matches its pre-update SHA-256 manifest entry."
    }
    $sourceWasPresent = Test-Path -LiteralPath $repoCachePath -PathType Leaf
    if ($sourceWasPresent) {
        $sourceHash = (Get-FileHash -LiteralPath $repoCachePath -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($sourceHash -ne $backupHash) {
            throw "Repository cache.json changed after backup; refusing retirement."
        }
    }

    $retiredDirectory = Join-Path $BackupDirectory "retired"
    if (Test-Path -LiteralPath $retiredDirectory) {
        throw "Legacy-cache retirement directory already exists unexpectedly."
    }
    New-Item -ItemType Directory -Path $retiredDirectory -Force:$false | Out-Null
    Protect-BackupDirectoryAcl $retiredDirectory

    $retiredCachePath = Join-Path $retiredDirectory "cache.json"
    $retirementSource = if ($sourceWasPresent) { $repoCachePath } else { $BackedUpCachePath }
    Copy-Item -LiteralPath $retirementSource -Destination $retiredCachePath -Force:$false
    $retiredHash = (Get-FileHash -LiteralPath $retiredCachePath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($retiredHash -ne $backupHash) {
        throw "Retired cache copy failed SHA-256 verification."
    }

    if ($sourceWasPresent) {
        # Recheck immediately before the only destructive step. This exact file
        # removal occurs only after migration and all validation have passed.
        $sourceHash = (Get-FileHash -LiteralPath $repoCachePath -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($sourceHash -ne $backupHash) {
            throw "Repository cache.json changed during retirement; original was preserved."
        }
        Remove-Item -LiteralPath $repoCachePath -Force
    }
    if (Test-Path -LiteralPath $repoCachePath) {
        throw "Repository cache.json is still present after retirement."
    }

    $receipt = [ordered]@{
        completed_at_utc = (Get-Date).ToUniversalTime().ToString("o")
        original_absence_verified = $true
        sha256 = $backupHash
        source_was_present_at_retirement = [bool]$sourceWasPresent
    }
    $receiptPath = Join-Path $retiredDirectory "retirement-receipt.json"
    $receiptTemp = $receiptPath + ".tmp"
    $receipt | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath $receiptTemp -Encoding UTF8
    Move-Item -LiteralPath $receiptTemp -Destination $receiptPath -Force
    Write-Host "Legacy cache retired to the restricted external backup."
}

function Write-DeploymentState(
    [System.Collections.IDictionary]$State,
    [string]$StatePath
) {
    $State["updated_at_utc"] = (Get-Date).ToUniversalTime().ToString("o")
    $tempPath = $StatePath + ".tmp"
    $State | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $tempPath -Encoding UTF8
    Move-Item -LiteralPath $tempPath -Destination $StatePath -Force
    $verified = [System.IO.File]::ReadAllText($StatePath) | ConvertFrom-Json
    if ($null -eq $verified -or [string]::IsNullOrWhiteSpace([string]$verified.pre_update_head)) {
        throw "Deployment-state receipt verification failed."
    }
}

function Get-CurrentGitHead {
    $headLines = @(Get-GitOutput @("rev-parse", "--verify", "HEAD"))
    if ($headLines.Count -ne 1) {
        throw "Could not record the current Git HEAD."
    }
    return ([string]$headLines[0]).Trim()
}

function Get-CurrentGitBranch {
    $args = @("-C", $script:ResolvedRepoPath, "symbolic-ref", "--quiet", "--short", "HEAD")
    $result = Invoke-NativeCapture $script:GitExe $args ""
    if ($result.ExitCode -eq 1) {
        return $null
    }
    if ($result.ExitCode -ne 0) {
        Write-SafeExternalOutput $result.Output
        throw "Could not record the current Git branch."
    }
    $lines = @($result.Output)
    if ($lines.Count -ne 1) {
        throw "Current Git branch response was ambiguous."
    }
    return ([string]$lines[0]).Trim()
}

function Validate-Installation(
    [string]$ResolvedRepoPath,
    [string]$EnvPath,
    [string]$ConfigPath,
    [string]$StatePath,
    [bool]$VerifyCloudflareWriteRequested
) {
    $validationCode = @'
from __future__ import annotations

import importlib.metadata
import ipaddress
import json
import logging
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

import mitra_bot
import mitra_bot.main
import requests
from mitra_bot.services.cloudflare_service import CloudflareService
from mitra_bot.storage.config_store import FileConfigModel
from mitra_bot.storage.state_store import StateStore

root = Path(sys.argv[1]).resolve()
env_path = Path(sys.argv[2]).resolve()
config_path = Path(sys.argv[3]).resolve()
state_path = Path(sys.argv[4]).resolve()
verify_cloudflare_write = sys.argv[5] == "1"

with (root / "pyproject.toml").open("rb") as source:
    expected_version = str(tomllib.load(source)["project"]["version"])
installed_version = importlib.metadata.version("mitra-discord-bot")
if installed_version != expected_version:
    raise SystemExit("Installed project version does not match pyproject.toml.")

if not config_path.is_file():
    raise SystemExit("config.toml is missing after update/migration.")
try:
    raw_config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    validated_config = FileConfigModel.model_validate(raw_config)
except Exception as exc:
    raise SystemExit(f"config.toml validation failed: {type(exc).__name__}") from exc

env_values: dict[str, str] = {}
if not env_path.is_file():
    raise SystemExit("Selected runtime env file is missing after update/migration.")
for raw_line in env_path.read_text(encoding="utf-8-sig").splitlines():
    line = raw_line.strip()
    if not line or line.startswith("#"):
        continue
    if line.startswith("export "):
        line = line[7:].lstrip()
    key, separator, value = line.partition("=")
    if not separator:
        continue
    key = key.strip()
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    if key in env_values:
        raise SystemExit(f"Selected runtime env file defines {key} more than once.")
    env_values[key] = value


def selected_secret(key: str) -> str:
    from_file = env_values.get(key, "").strip()
    from_process = (os.environ.get(key) or "").strip()
    if from_process and not from_file:
        raise SystemExit(
            f"Process environment defines {key} but selected runtime env file does not."
        )
    if from_file and from_process and from_file != from_process:
        raise SystemExit(
            f"{key} differs between selected env file and process environment."
        )
    return from_file


if not selected_secret("DISCORD_APPLICATION_TOKEN"):
    raise SystemExit(
        "Selected runtime env file is missing DISCORD_APPLICATION_TOKEN."
    )
cloudflare = validated_config.cloudflare
if cloudflare.enabled:
    cloudflare_token = selected_secret("CLOUDFLARE_API_TOKEN")
    if not cloudflare_token:
        raise SystemExit(
            "Cloudflare is enabled but selected runtime env file is missing CLOUDFLARE_API_TOKEN."
        )
    zone_id = str(cloudflare.zone_id or "").strip()
    record_ids = [str(value).strip() for value in cloudflare.record_ids if str(value).strip()]
    if not zone_id:
        raise SystemExit("Cloudflare is enabled but zone_id is missing.")
    if not record_ids:
        raise SystemExit("Cloudflare is enabled but record_ids is empty.")
    if len(set(record_ids)) != len(record_ids):
        raise SystemExit("Cloudflare record_ids contains duplicates.")

    # CloudflareService logs raw API failure payloads at ERROR. Suppress those
    # during updater verification so neither record contents nor public IPs can
    # reach the console; emit only generic validation failures below.
    logging.disable(logging.CRITICAL)
    service = CloudflareService(api_token=cloudflare_token)
    try:
        initial_records = service.get_dns_records(zone_id)
    except Exception:
        raise SystemExit(
            "Cloudflare live read failed; token, zone, or network validation was unsuccessful."
        ) from None

    records_by_id = {str(record.get("id", "")): record for record in initial_records}
    if any(record_id not in records_by_id for record_id in record_ids):
        raise SystemExit("Cloudflare live read did not return every configured record ID.")
    if any(
        str(records_by_id[record_id].get("type", "")).upper() != "A"
        for record_id in record_ids
    ):
        raise SystemExit("Every configured Cloudflare record must be an IPv4 A record.")

    if verify_cloudflare_write:
        try:
            response = requests.get("https://api.ipify.org", timeout=10)
            response.raise_for_status()
            public_address = ipaddress.ip_address(response.text.strip())
            if public_address.version != 4:
                raise ValueError("public address was not IPv4")
            public_ip = str(public_address)

            for record_id in record_ids:
                record = records_by_id[record_id]
                record_name = str(record.get("name", "")).strip()
                if not record_name:
                    raise ValueError("record name was missing")
                try:
                    ttl = int(record.get("ttl", 1))
                except (TypeError, ValueError):
                    ttl = 1
                service.update_dns_record(
                    zone_id,
                    record_id,
                    name=record_name,
                    record_type="A",
                    content=public_ip,
                    ttl=ttl,
                    proxied=bool(record.get("proxied", False)),
                )

            readback_records = service.get_dns_records(zone_id)
            readback_by_id = {
                str(record.get("id", "")): record for record in readback_records
            }
            if any(
                record_id not in readback_by_id
                or str(readback_by_id[record_id].get("content", "")).strip() != public_ip
                for record_id in record_ids
            ):
                raise ValueError("record read-back did not match")
        except Exception:
            raise SystemExit(
                "Cloudflare write/read-back verification failed; DNS sign-off was not completed."
            ) from None
        print(
            f"Cloudflare live write/read-back verification OK ({len(record_ids)} A record(s))"
        )
    else:
        print(
            "Cloudflare live read validation OK; DNS write verification was not attempted."
        )
elif verify_cloudflare_write:
    raise SystemExit(
        "Cloudflare write verification was requested, but Cloudflare is disabled in config."
    )

# Creating a missing empty state database is safe because there was no prior
# state to preserve. Existing databases are opened and checked in place.
if not state_path.exists():
    StateStore(state_path)

uri = f"file:{state_path.as_posix()}?mode=ro"
with closing(sqlite3.connect(uri, uri=True)) as conn:
    integrity = conn.execute("PRAGMA integrity_check").fetchone()
    if not integrity or str(integrity[0]).lower() != "ok":
        raise SystemExit("state.db failed SQLite integrity_check.")
    tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('meta', 'state_kv')"
        )
    }
    if tables != {"meta", "state_kv"}:
        raise SystemExit("state.db is missing required tables.")

print(f"Mitra import/version OK: {installed_version}")
print("Selected runtime env required-key validation OK")
print("config.toml schema validation OK")
print("state.db SQLite integrity/schema validation OK")
'@

    Invoke-Uv @(
        "run", "--no-sync", "python", "-c", $validationCode,
        $ResolvedRepoPath, $EnvPath, $ConfigPath, $StatePath,
        $(if ($VerifyCloudflareWriteRequested) { "1" } else { "0" })
    )
}

$deploymentState = $null
$deploymentStatePath = $null

try {
    if ($SelfTest) {
        Invoke-UpdaterSelfTest
        exit 0
    }

    if (-not $ConfirmBotStopped) {
        throw "Refusing to continue. Stop the Mitra bot/service, then re-run with -ConfirmBotStopped."
    }

    Step "Preflight"
    $script:ResolvedRepoPath = Resolve-RepoPath $RepoPath
    Assert-MitraRepository $script:ResolvedRepoPath
    $resolvedBackupRoot = Resolve-BackupRoot $BackupRoot $script:ResolvedRepoPath
    $resolvedEnvFileName = Resolve-EnvFileName $script:ResolvedRepoPath $EnvFileName
    $resolvedEnvPath = Resolve-RepoDestination $script:ResolvedRepoPath $resolvedEnvFileName
    Assert-SelectedEnvSecretCompatibility $resolvedEnvPath
    $runtimePaths = Resolve-RuntimeDataPaths $script:ResolvedRepoPath $resolvedEnvPath $resolvedBackupRoot
    $configPath = $runtimePaths.ConfigPath
    $statePath = $runtimePaths.StatePath

    $script:GitExe = Resolve-GitExe
    $script:UvExe = Resolve-UvExe
    Write-Host "Repository: $script:ResolvedRepoPath"
    Write-Host "Target branch: $TargetBranch"
    Write-Host "Backup root: $resolvedBackupRoot"
    Write-Host "Runtime env destination: $resolvedEnvPath"
    Write-Host "Runtime config/state overrides resolved (values suppressed)."
    Write-Host "Using git: $script:GitExe"
    Write-Host "Using uv: $script:UvExe"

    if ([string]::IsNullOrWhiteSpace($TargetBranch) -or $TargetBranch.StartsWith("-")) {
        throw "TargetBranch is invalid."
    }
    $branchCheck = Invoke-NativeCapture $script:GitExe @("check-ref-format", "--branch", $TargetBranch) ""
    if ($branchCheck.ExitCode -ne 0) {
        throw "TargetBranch is not a valid Git branch name."
    }

    Step "Backing up runtime data before Git changes"
    $backup = Backup-RuntimeFiles $script:ResolvedRepoPath $resolvedBackupRoot $resolvedEnvPath $configPath $statePath
    Write-Host ("Backed up {0} runtime file(s)." -f $backup.FileCount)
    Write-Host "Backup: $($backup.Directory)"
    Write-Host "Manifest: $($backup.ManifestPath)"
    Write-Host "This backup can contain credentials; keep it private." -ForegroundColor Yellow

    $preUpdateHead = Get-CurrentGitHead
    $preUpdateBranch = Get-CurrentGitBranch
    $deploymentStatePath = Join-Path $backup.Directory "deployment-state.json"
    $deploymentState = [ordered]@{
        created_at_utc = (Get-Date).ToUniversalTime().ToString("o")
        stage = "backup_complete"
        repository = $script:ResolvedRepoPath
        target_branch = $TargetBranch
        pre_update_branch = $preUpdateBranch
        pre_update_detached = [bool](-not $preUpdateBranch)
        pre_update_head = $preUpdateHead
        updater_stash_commit = $null
        fetched_remote_target_sha = $null
        remote_target_sha = $null
        post_update_head = $null
        migration_completed = $false
        validation_completed = $false
        cloudflare_write_requested = [bool]$VerifyCloudflareWrite
        cloudflare_write_verified = $false
        legacy_cache_retired = $false
        recovery_guidance = @(
            "Keep the bot stopped while reviewing recovery options.",
            "Use manifest.json to locate and verify runtime-file backups.",
            "Inspect any updater_stash_commit before applying individual changes.",
            "This updater never performs an automatic reset, rollback, or stash apply."
        )
    }
    Write-DeploymentState $deploymentState $deploymentStatePath
    Write-Host "Deployment state: $deploymentStatePath"

    Step "Checking exact repository status"
    $dirtyLines = @(Get-GitOutput @("status", "--porcelain=v1", "--untracked-files=all"))
    if ($dirtyLines.Count -eq 0) {
        Write-Host "Working tree is clean."
    } else {
        Write-Host "Local Git changes:" -ForegroundColor Yellow
        Write-SafeExternalOutput $dirtyLines
    }

    $stashCommit = $null
    if ($dirtyLines.Count -gt 0) {
        if (-not $AutoStash) {
            throw "Local Git changes exist. They are backed up only where listed in the runtime manifest. Review them, or re-run with -AutoStash."
        }

        Step "Stashing tracked and non-ignored untracked files"
        Write-Host "Ignored files are not included in Git stashes; critical runtime files are in the external backup." -ForegroundColor Yellow
        $stashMessage = "Mitra updater before " + (Get-Date).ToUniversalTime().ToString("o")
        Invoke-Git @("stash", "push", "-u", "-m", $stashMessage)
        $stashCommitLines = @(Get-GitOutput @("rev-parse", "--verify", "refs/stash"))
        if ($stashCommitLines.Count -ne 1) {
            throw "Could not identify the updater-created stash."
        }
        $stashCommit = ([string]$stashCommitLines[0]).Trim()
        $deploymentState["updater_stash_commit"] = $stashCommit
        $deploymentState["stage"] = "local_changes_stashed"
        Write-DeploymentState $deploymentState $deploymentStatePath
        $remainingDirty = @(Get-GitOutput @("status", "--porcelain=v1", "--untracked-files=all"))
        if ($remainingDirty.Count -gt 0) {
            Write-Host "Files still dirty after stash:" -ForegroundColor Yellow
            Write-SafeExternalOutput $remainingDirty
            throw "Working tree is still dirty after AutoStash."
        }
        Write-Host "Stash retained for manual review: $stashCommit"
    }

    Step "Fetching origin"
    Invoke-Git @("fetch", "origin", "--prune")
    $remoteRef = "refs/remotes/origin/$TargetBranch"
    if (-not (Test-GitRef $remoteRef)) {
        throw "Remote branch origin/$TargetBranch was not found after fetch."
    }
    $fetchedRemoteTargetSha = Get-GitCommitId $remoteRef
    $deploymentState["fetched_remote_target_sha"] = $fetchedRemoteTargetSha
    $deploymentState["remote_target_sha"] = $fetchedRemoteTargetSha
    $deploymentState["stage"] = "remote_fetched"
    Write-DeploymentState $deploymentState $deploymentStatePath

    Step "Checking out $TargetBranch"
    $localRef = "refs/heads/$TargetBranch"
    if (Test-GitRef $localRef) {
        Invoke-Git @("checkout", $TargetBranch)
    } else {
        Invoke-Git @("checkout", "-b", $TargetBranch, "--track", "origin/$TargetBranch")
    }

    $relationship = Get-GitAheadBehind "HEAD" $remoteRef
    if ($relationship.Ahead -gt 0) {
        if ($relationship.Behind -gt 0) {
            throw "Local target branch has diverged from origin/$TargetBranch. Preserve and reconcile its local commits manually."
        }
        throw "Local target branch is ahead of origin/$TargetBranch. Preserve and reconcile its local commits manually."
    }

    Step "Pulling origin/$TargetBranch with fast-forward only"
    Invoke-Git @("pull", "--ff-only", "origin", $TargetBranch)
    $postUpdateHead = Get-CurrentGitHead
    $remoteTargetSha = Get-GitCommitId $remoteRef
    $deploymentState["post_update_head"] = $postUpdateHead
    $deploymentState["remote_target_sha"] = $remoteTargetSha
    $deploymentState["stage"] = "source_update_verifying"
    Write-DeploymentState $deploymentState $deploymentStatePath
    if (-not $postUpdateHead.Equals($remoteTargetSha, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Updated HEAD does not exactly match refs/remotes/origin/$TargetBranch. Refusing to install or migrate."
    }
    $deploymentState["stage"] = "source_updated"
    Write-DeploymentState $deploymentState $deploymentStatePath

    Step "Synchronizing locked production dependencies"
    Invoke-Uv @("--version")
    Push-Location $script:ResolvedRepoPath
    try {
        Invoke-Uv @("sync", "--no-dev", "--frozen")
    } finally {
        Pop-Location
    }

    if ($backup.LegacyCachePath) {
        Step "Migrating backed-up legacy cache into current storage"
        $migrationBackup = Join-Path $backup.Directory "migration"
        New-Item -ItemType Directory -Path $migrationBackup -Force:$false | Out-Null
        Protect-BackupDirectoryAcl $migrationBackup
        Push-Location $script:ResolvedRepoPath
        try {
            Invoke-Uv @(
                "run", "--no-sync", "mitra-migrate-cache",
                "--source", $backup.LegacyCachePath,
                "--root", $script:ResolvedRepoPath,
                "--env-file", $resolvedEnvPath,
                "--config", $configPath,
                "--state", $statePath,
                "--backup-dir", $migrationBackup,
                "--apply", "--confirm-bot-stopped"
            )
        } finally {
            Pop-Location
        }
        $deploymentState["migration_completed"] = $true
        $deploymentState["stage"] = "legacy_cache_migrated"
        Write-DeploymentState $deploymentState $deploymentStatePath
    } else {
        Step "Legacy cache migration"
        Write-Host "No cache.json was present at backup time; migration is not required."
    }

    Step "Validating updated installation"
    Push-Location $script:ResolvedRepoPath
    try {
        Validate-Installation $script:ResolvedRepoPath $resolvedEnvPath $configPath $statePath ([bool]$VerifyCloudflareWrite)
    } finally {
        Pop-Location
    }
    $deploymentState["validation_completed"] = $true
    if ($VerifyCloudflareWrite) {
        $deploymentState["cloudflare_write_verified"] = $true
    }
    $deploymentState["stage"] = "validated"
    Write-DeploymentState $deploymentState $deploymentStatePath

    if ($backup.LegacyCachePath) {
        Step "Retiring legacy cache after successful migration and validation"
        Retire-LegacyCache $script:ResolvedRepoPath $backup.LegacyCachePath $backup.LegacyCacheSha256 $backup.Directory
        $deploymentState["legacy_cache_retired"] = $true
    }

    $deploymentState["stage"] = "complete"
    Write-DeploymentState $deploymentState $deploymentStatePath

    Step "Update complete"
    if ($VerifyCloudflareWrite) {
        Write-Host "Update and live Cloudflare write/read-back verification completed." -ForegroundColor Green
    } else {
        Write-Host "Update completed; Cloudflare DNS write verification was not attempted." -ForegroundColor Yellow
        Write-Host "Run this updater on the server with -VerifyCloudflareWrite before full DNS sign-off."
    }
    Write-Host "The bot remains stopped. Restart it manually after reviewing this result." -ForegroundColor Green
    Write-Host "Backup retained at: $($backup.Directory)"
    Write-Host "Deployment state retained at: $deploymentStatePath"
    if ($stashCommit) {
        Write-Host "Local changes remain safely stashed at commit: $stashCommit" -ForegroundColor Yellow
        Write-Host "Review with: git -C `"$script:ResolvedRepoPath`" stash show --stat $stashCommit"
        Write-Host "Do not apply the entire stash blindly; it may contain obsolete code or cache.json."
    }
} catch {
    $safeMessage = Protect-OutputText ([string]$_.Exception.Message)
    if ($deploymentState -and $deploymentStatePath) {
        try {
            $deploymentState["failure_after_stage"] = $deploymentState["stage"]
            $deploymentState["failure_message"] = $safeMessage
            $deploymentState["stage"] = "failed"
            Write-DeploymentState $deploymentState $deploymentStatePath
        } catch {
            # The previously durable state remains useful even if the final
            # failure marker itself cannot be written.
        }
    }
    Write-Host ""
    Write-Host "Update stopped safely: $safeMessage" -ForegroundColor Red
    Write-Host "The bot was not restarted. Preserve any backup/stash paths printed above."
    if ($deploymentStatePath -and (Test-Path -LiteralPath $deploymentStatePath -PathType Leaf)) {
        Write-Host "Deployment state: $deploymentStatePath" -ForegroundColor Yellow
        Write-Host "Keep the bot stopped; review deployment-state.json and manifest.json before manual recovery."
        Write-Host "No automatic rollback, reset, or stash apply was performed."
    }
    exit 1
}
