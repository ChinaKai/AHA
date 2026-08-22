[CmdletBinding()]
param(
    [string]$Python = "",
    [string]$AhaDir = (Join-Path $env:LOCALAPPDATA "AHA"),
    [string]$AhaHome = (Join-Path $env:USERPROFILE ".aha"),
    [ValidateNotNullOrEmpty()][string]$Bind = "127.0.0.1",
    [ValidateRange(1, 65535)][int]$Port = 8788,
    [string]$DownloadUrl = "https://github.com/ChinaKai/AHA/releases/latest/download/aha",
    [string]$ChecksumUrl = "",
    [ValidatePattern("^$|^[A-Fa-f0-9]{64}$")][string]$Sha256 = "",
    [string]$Artifact = "",
    [ValidateSet("Minimal", "Full", "Offline")][string]$Mode = "Full",
    [ValidateSet("Auto", "Codex", "Claude", "Both", "None")][string]$AgentBackend = "Auto",
    [ValidateSet("Browser", "Hardware", "Feishu")][string[]]$Modules = @(),
    [string]$OfflineDir = "",
    [switch]$Repair,
    [switch]$StrictModules,
    [switch]$WithBrowser,
    [switch]$SkipBrowserDownload,
    [switch]$EnableStartup,
    [System.Management.Automation.PSCredential]$StartupCredential = $null,
    [switch]$Uninstall,
    [switch]$NoShortcut,
    [switch]$NoStart,
    [switch]$NoAuth,
    [switch]$AllowUnsafeBind
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$script:AhaInstallResults = @()

function Add-AhaInstallResult {
    param(
        [string]$Name,
        [string]$Kind,
        [string]$Status,
        [string]$Detail,
        [bool]$Required = $false
    )

    $script:AhaInstallResults += [pscustomobject][ordered]@{
        name = $Name
        kind = $Kind
        status = $Status
        required = $Required
        detail = $Detail
    }
}

function Read-AhaChecksumFile {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return ""
    }
    foreach ($line in Get-Content -LiteralPath $Path) {
        if ($line -match '^([A-Fa-f0-9]{64})\s+\*?aha$') {
            return $Matches[1].ToLowerInvariant()
        }
    }
    return ""
}

function Assert-AhaArtifactHash {
    param(
        [string]$Path,
        [string]$Expected
    )

    if ([string]::IsNullOrWhiteSpace($Expected)) {
        Write-Warning "No SHA-256 checksum was available for the AHA artifact; version validation will still run"
        Add-AhaInstallResult -Name "aha-sha256" -Kind "integrity" -Status "skipped" -Detail "No checksum supplied"
        return
    }
    $actual = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $Expected.ToLowerInvariant()) {
        throw "AHA artifact SHA-256 mismatch: expected $Expected, got $actual"
    }
    Add-AhaInstallResult -Name "aha-sha256" -Kind "integrity" -Status "verified" -Detail $actual -Required $true
}

function Update-AhaProcessPath {
    $paths = @(
        $env:PATH,
        [Environment]::GetEnvironmentVariable("Path", "Machine"),
        [Environment]::GetEnvironmentVariable("Path", "User"),
        (Join-Path $env:APPDATA "npm"),
        (Join-Path $env:ProgramFiles "nodejs")
    ) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
    $env:PATH = ($paths -join ";")
}

function Install-AhaWingetPackage {
    param(
        [string]$PackageId,
        [string]$Label
    )

    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if (-not $winget) {
        throw "winget is required to install $Label automatically"
    }
    Write-Host "Installing $Label with winget..."
    $output = (& $winget.Source install --id $PackageId -e --accept-package-agreements --accept-source-agreements --silent 2>&1 | Out-String).Trim()
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        throw "Failed to install $Label with winget: $output"
    }
    Update-AhaProcessPath
}

function Find-AhaPythonExecutable {
    $systemPython = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($systemPython) {
        return $systemPython.Source
    }
    $pythonRoot = Join-Path $env:LOCALAPPDATA "Programs\Python"
    if (Test-Path -LiteralPath $pythonRoot -PathType Container) {
        $candidate = Get-ChildItem -LiteralPath $pythonRoot -Filter python.exe -File -Recurse -ErrorAction SilentlyContinue |
            Sort-Object FullName -Descending |
            Select-Object -First 1
        if ($candidate) {
            return $candidate.FullName
        }
    }
    return ""
}

function Test-AhaSupportedPython {
    param(
        [string]$Executable,
        [string[]]$Arguments = @()
    )

    if ([string]::IsNullOrWhiteSpace($Executable)) {
        return $false
    }
    $versionOutput = (& $Executable @Arguments -c "import sys; print('.'.join(map(str, sys.version_info[:3])))" 2>$null | Out-String).Trim()
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        return $false
    }
    try {
        return [Version]$versionOutput -ge [Version]"3.10"
    }
    catch {
        return $false
    }
}

function Resolve-AhaPython {
    param(
        [string]$Requested,
        [bool]$InstallMissing,
        [string]$OfflineRoot
    )

    $venvDir = Join-Path $env:USERPROFILE ".venvs\aha"
    $venvPython = Join-Path $venvDir "Scripts\python.exe"
    if ($Requested) {
        if (-not (Test-Path -LiteralPath $Requested -PathType Leaf)) {
            throw "Python executable not found: $Requested"
        }
        return (Resolve-Path -LiteralPath $Requested).Path
    }
    if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
        $existingVersion = (& $venvPython -c "import sys; print('.'.join(map(str, sys.version_info[:3])))" 2>&1 | Out-String).Trim()
        $existingExitCode = $LASTEXITCODE
        if ($existingExitCode -eq 0 -and [Version]$existingVersion -ge [Version]"3.10") {
            return $venvPython
        }
        if (-not $InstallMissing) {
            return $venvPython
        }
        Write-Warning "Replacing an unusable or unsupported AHA Python environment: $venvDir"
        Remove-Item -LiteralPath $venvDir -Recurse -Force
    }

    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    $systemPythonPath = Find-AhaPythonExecutable
    if ($launcher -and -not (Test-AhaSupportedPython -Executable $launcher.Source -Arguments @("-3"))) {
        $launcher = $null
    }
    if ($systemPythonPath -and -not (Test-AhaSupportedPython -Executable $systemPythonPath)) {
        $systemPythonPath = ""
    }
    if (-not $launcher -and -not $systemPythonPath -and $OfflineRoot) {
        $offlineInstaller = Join-Path $OfflineRoot "python-installer.exe"
        if (Test-Path -LiteralPath $offlineInstaller -PathType Leaf) {
            Write-Host "Installing Python from offline bundle..."
            $process = Start-Process -FilePath $offlineInstaller -ArgumentList @(
                "/quiet",
                "InstallAllUsers=0",
                "PrependPath=1",
                "Include_test=0"
            ) -Wait -PassThru
            if ($process.ExitCode -ne 0) {
                throw "Offline Python installer failed with exit code $($process.ExitCode)"
            }
            Update-AhaProcessPath
            $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
            $systemPythonPath = Find-AhaPythonExecutable
            if ($launcher -and -not (Test-AhaSupportedPython -Executable $launcher.Source -Arguments @("-3"))) {
                $launcher = $null
            }
            if ($systemPythonPath -and -not (Test-AhaSupportedPython -Executable $systemPythonPath)) {
                $systemPythonPath = ""
            }
        }
    }
    if (-not $launcher -and -not $systemPythonPath -and $OfflineRoot) {
        throw "Offline mode requires Python 3.10+ or $OfflineRoot\python-installer.exe; network installation is disabled"
    }
    if (-not $launcher -and -not $systemPythonPath -and $InstallMissing) {
        Install-AhaWingetPackage -PackageId "Python.Python.3.12" -Label "Python 3.12"
        $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
        $systemPythonPath = Find-AhaPythonExecutable
        if ($launcher -and -not (Test-AhaSupportedPython -Executable $launcher.Source -Arguments @("-3"))) {
            $launcher = $null
        }
        if ($systemPythonPath -and -not (Test-AhaSupportedPython -Executable $systemPythonPath)) {
            $systemPythonPath = ""
        }
    }
    if ($launcher) {
        & $launcher.Source -3 -m venv $venvDir 2>&1 | Out-Null
        $venvExitCode = $LASTEXITCODE
    }
    else {
        if (-not $systemPythonPath) {
            throw "Python 3.10+ is required. Install it with: winget install --id Python.Python.3.12 -e"
        }
        & $systemPythonPath -m venv $venvDir 2>&1 | Out-Null
        $venvExitCode = $LASTEXITCODE
    }
    if ($venvExitCode -ne 0 -or -not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        throw "Failed to create the AHA Python environment: $venvDir"
    }
    return $venvPython
}

function Get-AhaCommandPath {
    param([string[]]$Names)

    foreach ($name in $Names) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($command) {
            return $command.Source
        }
    }
    return ""
}

function Test-AhaPythonImport {
    param(
        [string]$PythonPath,
        [string]$ImportName
    )

    & $PythonPath -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('$ImportName') else 1)" 2>$null
    return $LASTEXITCODE -eq 0
}

function Install-AhaPythonModule {
    param(
        [string]$PythonPath,
        [string]$Name,
        [string]$Package,
        [string]$ImportName,
        [string]$OfflineRoot
    )

    if (Test-AhaPythonImport -PythonPath $PythonPath -ImportName $ImportName) {
        Add-AhaInstallResult -Name $Name -Kind "python-module" -Status "present" -Detail $Package
        return $true
    }
    $arguments = @("-m", "pip", "install", "--disable-pip-version-check")
    if ($OfflineRoot) {
        $wheelhouse = Join-Path $OfflineRoot "wheels"
        if (-not (Test-Path -LiteralPath $wheelhouse -PathType Container)) {
            Add-AhaInstallResult -Name $Name -Kind "python-module" -Status "missing" -Detail "Offline wheel directory not found: $wheelhouse"
            return $false
        }
        $arguments += @("--no-index", "--find-links", $wheelhouse)
    }
    $arguments += $Package
    Write-Host "Installing AHA module: $Name..."
    $output = (& $PythonPath @arguments 2>&1 | Out-String).Trim()
    $installExitCode = $LASTEXITCODE
    if ($installExitCode -ne 0 -or -not (Test-AhaPythonImport -PythonPath $PythonPath -ImportName $ImportName)) {
        Add-AhaInstallResult -Name $Name -Kind "python-module" -Status "failed" -Detail $output
        return $false
    }
    Add-AhaInstallResult -Name $Name -Kind "python-module" -Status "installed" -Detail $Package
    return $true
}

function Install-AhaBrowserRuntime {
    param(
        [string]$PythonPath,
        [string]$OfflineRoot,
        [bool]$SkipDownload
    )

    if ($SkipDownload) {
        Add-AhaInstallResult -Name "chromium" -Kind "browser-runtime" -Status "skipped" -Detail "Browser download not requested; using an installed Chrome/Edge when available"
        return $true
    }
    $browserRoot = Join-Path $env:LOCALAPPDATA "ms-playwright"
    if (Test-Path -LiteralPath $browserRoot -PathType Container) {
        $existing = Get-ChildItem -LiteralPath $browserRoot -Directory -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -like "chromium-*" -or $_.Name -like "chromium_headless_shell-*" } |
            Select-Object -First 1
        if ($existing) {
            Add-AhaInstallResult -Name "chromium" -Kind "browser-runtime" -Status "present" -Detail $existing.FullName
            return $true
        }
    }
    if ($OfflineRoot) {
        $offlineBrowsers = Join-Path $OfflineRoot "ms-playwright"
        if (-not (Test-Path -LiteralPath $offlineBrowsers -PathType Container)) {
            Add-AhaInstallResult -Name "chromium" -Kind "browser-runtime" -Status "missing" -Detail "Offline browser directory not found: $offlineBrowsers"
            return $false
        }
        New-Item -ItemType Directory -Force -Path $browserRoot | Out-Null
        Copy-Item -Path (Join-Path $offlineBrowsers "*") -Destination $browserRoot -Recurse -Force
        $installed = Get-ChildItem -LiteralPath $browserRoot -Directory -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -like "chromium-*" -or $_.Name -like "chromium_headless_shell-*" } |
            Select-Object -First 1
        if (-not $installed) {
            Add-AhaInstallResult -Name "chromium" -Kind "browser-runtime" -Status "failed" -Detail "Offline browser payload did not contain a Chromium runtime"
            return $false
        }
        Add-AhaInstallResult -Name "chromium" -Kind "browser-runtime" -Status "installed" -Detail $browserRoot
        return $true
    }
    Write-Host "Installing Playwright Chromium..."
    $output = (& $PythonPath -m playwright install chromium 2>&1 | Out-String).Trim()
    $installExitCode = $LASTEXITCODE
    if ($installExitCode -ne 0) {
        Add-AhaInstallResult -Name "chromium" -Kind "browser-runtime" -Status "failed" -Detail $output
        return $false
    }
    Add-AhaInstallResult -Name "chromium" -Kind "browser-runtime" -Status "installed" -Detail $browserRoot
    return $true
}

function Ensure-AhaGit {
    param([bool]$Offline)

    $gitPath = Get-AhaCommandPath @("git.exe", "git")
    if ($gitPath) {
        Add-AhaInstallResult -Name "git" -Kind "external-tool" -Status "present" -Detail $gitPath
        return $true
    }
    if ($Offline) {
        Add-AhaInstallResult -Name "git" -Kind "external-tool" -Status "missing" -Detail "Install Git before using Knowledge sync in offline mode"
        return $false
    }
    try {
        Install-AhaWingetPackage -PackageId "Git.Git" -Label "Git"
        $gitPath = Get-AhaCommandPath @("git.exe", "git")
        Add-AhaInstallResult -Name "git" -Kind "external-tool" -Status $(if ($gitPath) { "installed" } else { "failed" }) -Detail $(if ($gitPath) { $gitPath } else { "Git command is still unavailable" })
        return [bool]$gitPath
    }
    catch {
        Add-AhaInstallResult -Name "git" -Kind "external-tool" -Status "failed" -Detail $_.Exception.Message
        return $false
    }
}

function Ensure-AhaNode {
    param([bool]$Offline)

    $nodePath = Get-AhaCommandPath @("node.exe", "node")
    $npmPath = Get-AhaCommandPath @("npm.cmd", "npm")
    if ($nodePath -and $npmPath) {
        Add-AhaInstallResult -Name "node" -Kind "external-tool" -Status "present" -Detail $nodePath
        return $npmPath
    }
    if ($Offline) {
        Add-AhaInstallResult -Name "node" -Kind "external-tool" -Status "missing" -Detail "Node.js/npm must be preinstalled for offline Codex installation"
        return ""
    }
    try {
        Install-AhaWingetPackage -PackageId "OpenJS.NodeJS.LTS" -Label "Node.js LTS"
        $nodePath = Get-AhaCommandPath @("node.exe", "node")
        $npmPath = Get-AhaCommandPath @("npm.cmd", "npm")
        Add-AhaInstallResult -Name "node" -Kind "external-tool" -Status $(if ($nodePath -and $npmPath) { "installed" } else { "failed" }) -Detail $(if ($nodePath) { $nodePath } else { "Node.js command is still unavailable" })
        return $npmPath
    }
    catch {
        Add-AhaInstallResult -Name "node" -Kind "external-tool" -Status "failed" -Detail $_.Exception.Message
        return ""
    }
}

function Ensure-AhaAgentBackend {
    param(
        [string]$Selection,
        [bool]$Offline
    )

    $codexPath = Get-AhaCommandPath @("codex.cmd", "codex.exe", "codex")
    $claudePath = Get-AhaCommandPath @("claude.exe", "claude.cmd", "claude")
    if ($codexPath) {
        Add-AhaInstallResult -Name "codex" -Kind "agent-cli" -Status "present" -Detail "$codexPath; login remains user-managed" -Required ($Selection -ne "None")
    }
    if ($claudePath) {
        Add-AhaInstallResult -Name "claude" -Kind "agent-cli" -Status "present" -Detail "$claudePath; login remains user-managed" -Required ($Selection -ne "None")
    }
    if ($Selection -eq "None") {
        if (-not $codexPath -and -not $claudePath) {
            Add-AhaInstallResult -Name "agent-cli" -Kind "agent-cli" -Status "skipped" -Detail "Skipped by -AgentBackend None"
        }
        return $true
    }
    if ($Selection -eq "Auto" -and ($codexPath -or $claudePath)) {
        return $true
    }
    $targets = switch ($Selection) {
        "Claude" { @("Claude") }
        "Both" { @("Codex", "Claude") }
        default { @("Codex") }
    }
    $success = $true
    foreach ($target in $targets) {
        if ($target -eq "Codex" -and -not $codexPath) {
            if ($Offline) {
                Add-AhaInstallResult -Name "codex" -Kind "agent-cli" -Status "missing" -Detail "Codex must be preinstalled in offline mode; login remains user-managed" -Required $true
                $success = $false
                continue
            }
            $npmPath = Get-AhaCommandPath @("npm.cmd", "npm")
            if (-not $npmPath) {
                $npmPath = Ensure-AhaNode -Offline $false
            }
            if (-not $npmPath) {
                Add-AhaInstallResult -Name "codex" -Kind "agent-cli" -Status "failed" -Detail "npm is unavailable" -Required $true
                $success = $false
                continue
            }
            Write-Host "Installing Codex CLI..."
            $output = (& $npmPath install --global @openai/codex 2>&1 | Out-String).Trim()
            $installExitCode = $LASTEXITCODE
            Update-AhaProcessPath
            $codexPath = Get-AhaCommandPath @("codex.cmd", "codex.exe", "codex")
            if ($installExitCode -ne 0 -or -not $codexPath) {
                Add-AhaInstallResult -Name "codex" -Kind "agent-cli" -Status "failed" -Detail $output -Required $true
                $success = $false
            }
            else {
                Add-AhaInstallResult -Name "codex" -Kind "agent-cli" -Status "installed" -Detail "$codexPath; run 'codex' once to sign in" -Required $true
            }
        }
        if ($target -eq "Claude" -and -not $claudePath) {
            if ($Offline) {
                Add-AhaInstallResult -Name "claude" -Kind "agent-cli" -Status "missing" -Detail "Claude Code must be preinstalled in offline mode; login remains user-managed" -Required $true
                $success = $false
                continue
            }
            try {
                Install-AhaWingetPackage -PackageId "Anthropic.ClaudeCode" -Label "Claude Code"
                $claudePath = Get-AhaCommandPath @("claude.exe", "claude.cmd", "claude")
                Add-AhaInstallResult -Name "claude" -Kind "agent-cli" -Status $(if ($claudePath) { "installed" } else { "failed" }) -Detail $(if ($claudePath) { "$claudePath; run 'claude' once to sign in" } else { "Claude command is still unavailable" }) -Required $true
                if (-not $claudePath) { $success = $false }
            }
            catch {
                Add-AhaInstallResult -Name "claude" -Kind "agent-cli" -Status "failed" -Detail $_.Exception.Message -Required $true
                $success = $false
            }
        }
    }
    return $success
}

function Write-AhaInstallReport {
    param(
        [string]$Path,
        [string]$InstallMode,
        [string]$InstalledVersion,
        [string]$PythonPath,
        [string]$InstallPath,
        [bool]$RepairRequested
    )

    $report = [ordered]@{
        schema_version = 1
        installed_at = [DateTimeOffset]::Now.ToString("o")
        mode = $InstallMode
        repair = $RepairRequested
        version = $InstalledVersion
        python = $PythonPath
        install_bin = $InstallPath
        results = @($script:AhaInstallResults)
        next_actions = @(
            "Run the installed Codex or Claude CLI once to complete login if needed.",
            "Open AHA and configure credentials or enterprise integrations in Settings."
        )
    }
    $json = $report | ConvertTo-Json -Depth 6
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $json + [Environment]::NewLine, $utf8NoBom)
}

function New-WebToken {
    $bytes = New-Object byte[] 32
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $rng.GetBytes($bytes)
    }
    finally {
        $rng.Dispose()
    }
    return ([System.BitConverter]::ToString($bytes) -replace "-", "").ToLowerInvariant()
}

function Quote-StartProcessArgument {
    param([string]$Value)
    return '"' + ($Value -replace '"', '\"') + '"'
}

function Write-AhaTrayConfig {
    param(
        [string]$Path,
        [string]$HomePath,
        [string]$BindAddress,
        [int]$WebPort,
        [string]$WebTokenFile,
        [string]$StartupTaskName
    )

    $settings = [ordered]@{
        aha_home = $HomePath
        bind = $BindAddress
        port = $WebPort
        web_token_file = $WebTokenFile
        startup_task_name = $StartupTaskName
    }
    $json = $settings | ConvertTo-Json
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $json + [Environment]::NewLine, $utf8NoBom)
}

function Quote-PowerShellLiteral {
    param([string]$Value)
    return "'" + $Value.Replace("'", "''") + "'"
}

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-AhaStartupTask {
    param(
        [string]$TaskPath,
        [string]$TaskName
    )
    return Get-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -ErrorAction SilentlyContinue
}

function Resolve-AhaStartupCredential {
    param([System.Management.Automation.PSCredential]$Credential)

    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    if ($null -eq $Credential) {
        $Credential = Get-Credential -UserName $identity.Name -Message "AHA needs the current Windows account password to start before sign-in. The Task Scheduler stores it as an LSA-protected secret."
    }
    if ($null -eq $Credential) {
        throw "A startup credential is required"
    }
    try {
        $account = New-Object Security.Principal.NTAccount($Credential.UserName)
        $credentialSid = $account.Translate([Security.Principal.SecurityIdentifier]).Value
    }
    catch {
        throw "Unable to resolve startup account: $($Credential.UserName)"
    }
    if ($credentialSid -ne $identity.User.Value) {
        throw "The startup task must use the current Windows account so it can access the selected AHA_HOME"
    }
    return $Credential
}

function Write-AhaServiceLauncher {
    param(
        [string]$Path,
        [string]$PythonPath,
        [string]$ArtifactPath,
        [string]$ConfigPath
    )

    $pythonLiteral = Quote-PowerShellLiteral $PythonPath
    $artifactLiteral = Quote-PowerShellLiteral $ArtifactPath
    $configLiteral = Quote-PowerShellLiteral $ConfigPath
    $content = @"
`$ErrorActionPreference = "Stop"
`$config = Get-Content -LiteralPath $configLiteral -Raw | ConvertFrom-Json
`$arguments = @(
    $artifactLiteral,
    "--home", [string]`$config.aha_home,
    "ui",
    "--host", [string]`$config.bind,
    "--port", [string]`$config.port,
    "--poll-interval", "1000"
)
if (-not [string]::IsNullOrWhiteSpace([string]`$config.web_token_file)) {
    `$arguments += @("--auth-token-file", [string]`$config.web_token_file)
}
& $pythonLiteral @arguments
exit `$LASTEXITCODE
"@
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $content, $utf8NoBom)
}

function Install-AhaStartupTask {
    param(
        [string]$TaskPath,
        [string]$TaskName,
        [string]$LauncherPath,
        [string]$WorkingDirectory,
        [System.Management.Automation.PSCredential]$Credential
    )

    if (-not (Test-IsAdministrator)) {
        throw "-EnableStartup requires an elevated PowerShell because an AtStartup task is machine-triggered"
    }
    $actionArguments = '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File ' + (Quote-StartProcessArgument $LauncherPath)
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $actionArguments -WorkingDirectory $WorkingDirectory
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $settings = New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -MultipleInstances IgnoreNew
    $existing = Get-AhaStartupTask -TaskPath $TaskPath -TaskName $TaskName
    if ($null -ne $existing) {
        if ($null -ne $Credential) {
            $Credential = Resolve-AhaStartupCredential $Credential
            $password = $Credential.GetNetworkCredential().Password
            Register-ScheduledTask `
                -TaskPath $TaskPath `
                -TaskName $TaskName `
                -Action $action `
                -Trigger $trigger `
                -Settings $settings `
                -Description "Start the AHA Web service before Windows sign-in" `
                -User $Credential.UserName `
                -Password $password `
                -RunLevel Limited `
                -Force | Out-Null
            return
        }
        Set-ScheduledTask `
            -TaskPath $TaskPath `
            -TaskName $TaskName `
            -Action $action `
            -Trigger $trigger `
            -Settings $settings | Out-Null
        Enable-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName | Out-Null
        return
    }
    $Credential = Resolve-AhaStartupCredential $Credential
    $password = $Credential.GetNetworkCredential().Password
    Register-ScheduledTask `
        -TaskPath $TaskPath `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Description "Start the AHA Web service before Windows sign-in" `
        -User $Credential.UserName `
        -Password $password `
        -RunLevel Limited `
        -Force | Out-Null
}

function Set-AhaLoginStartup {
    param(
        [bool]$Enabled,
        [string]$Command
    )

    $runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
    if ($Enabled) {
        New-Item -Path $runKey -Force | Out-Null
        New-ItemProperty -Path $runKey -Name "AHA" -Value $Command -PropertyType String -Force | Out-Null
        return
    }
    Remove-ItemProperty -Path $runKey -Name "AHA" -ErrorAction SilentlyContinue
}

function Remove-AhaStartupTask {
    param(
        [string]$TaskPath,
        [string]$TaskName
    )

    $existing = Get-AhaStartupTask -TaskPath $TaskPath -TaskName $TaskName
    if ($null -eq $existing) {
        return
    }
    if (-not (Test-IsAdministrator)) {
        throw "Uninstalling the AHA startup task requires an elevated PowerShell"
    }
    Stop-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -Confirm:$false
}

function Stop-AhaStartupTask {
    param(
        [string]$TaskPath,
        [string]$TaskName
    )

    Stop-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -ErrorAction SilentlyContinue
    for ($attempt = 0; $attempt -lt 50; $attempt++) {
        $task = Get-AhaStartupTask -TaskPath $TaskPath -TaskName $TaskName
        if ($null -eq $task -or $task.State -ne "Running") {
            return
        }
        Start-Sleep -Milliseconds 100
    }
    throw "The existing AHA startup task did not stop within five seconds"
}

function Install-AhaIcon {
    param(
        [string]$ArtifactPath,
        [string]$Destination
    )

    try {
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $archive = [System.IO.Compression.ZipFile]::OpenRead($ArtifactPath)
        try {
            $entry = $archive.GetEntry("aha_cli/assets/aha.ico")
            if ($null -eq $entry) {
                return $false
            }
            $inputStream = $entry.Open()
            try {
                $outputStream = [System.IO.File]::Create($Destination)
                try {
                    $inputStream.CopyTo($outputStream)
                }
                finally {
                    $outputStream.Dispose()
                }
            }
            finally {
                $inputStream.Dispose()
            }
        }
        finally {
            $archive.Dispose()
        }
        return $true
    }
    catch {
        Write-Warning "Failed to install the AHA shortcut icon: $($_.Exception.Message)"
        return $false
    }
}

function Install-AhaStartMenuShortcut {
    param(
        [string]$PythonwPath,
        [string]$ArtifactPath,
        [string]$WorkingDirectory,
        [string]$IconPath
    )

    $programs = [Environment]::GetFolderPath([Environment+SpecialFolder]::Programs)
    if ([string]::IsNullOrWhiteSpace($programs)) {
        throw "Unable to resolve the current user's Start Menu directory"
    }
    $shortcutDirectory = Join-Path $programs "AHA"
    New-Item -ItemType Directory -Force -Path $shortcutDirectory | Out-Null
    $shortcutPath = Join-Path $shortcutDirectory "AHA.lnk"
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = $PythonwPath
    $shortcut.Arguments = (Quote-StartProcessArgument $ArtifactPath) + " tray --open-browser"
    $shortcut.WorkingDirectory = $WorkingDirectory
    $shortcut.Description = "Start AHA in the Windows notification area"
    if (Test-Path -LiteralPath $IconPath -PathType Leaf) {
        $shortcut.IconLocation = $IconPath + ",0"
    }
    $shortcut.Save()
    return $shortcutPath
}

$StartupTaskPath = "\"
$StartupTaskName = "AHA Web"
$StartupTaskFullName = "\AHA Web"
$Programs = [Environment]::GetFolderPath([Environment+SpecialFolder]::Programs)
$ShortcutDirectory = if ([string]::IsNullOrWhiteSpace($Programs)) { "" } else { Join-Path $Programs "AHA" }
$ShortcutPath = if ($ShortcutDirectory) { Join-Path $ShortcutDirectory "AHA.lnk" } else { "" }

if ($Uninstall) {
    Remove-AhaStartupTask -TaskPath $StartupTaskPath -TaskName $StartupTaskName
    Set-AhaLoginStartup -Enabled $false -Command ""
    if ($ShortcutPath) {
        Remove-Item -LiteralPath $ShortcutPath -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $ShortcutDirectory -Force -ErrorAction SilentlyContinue
    }
    if (Test-Path -LiteralPath $AhaDir) {
        Remove-Item -LiteralPath $AhaDir -Recurse -Force
    }
    Write-Host "Removed AHA startup task, login startup, shortcut, and installed files"
    Write-Host "AHA home retained: $AhaHome"
    return
}

$OfflineRoot = ""
if ($Mode -eq "Offline") {
    if ([string]::IsNullOrWhiteSpace($OfflineDir)) {
        throw "-Mode Offline requires -OfflineDir containing aha, wheels, and optional ms-playwright/python-installer.exe assets"
    }
    if (-not (Test-Path -LiteralPath $OfflineDir -PathType Container)) {
        throw "Offline bundle directory not found: $OfflineDir"
    }
    $OfflineRoot = (Resolve-Path -LiteralPath $OfflineDir).Path
    if ([string]::IsNullOrWhiteSpace($Artifact)) {
        $offlineArtifact = Join-Path $OfflineRoot "aha"
        if (-not (Test-Path -LiteralPath $offlineArtifact -PathType Leaf)) {
            throw "Offline AHA artifact not found: $offlineArtifact"
        }
        $Artifact = $offlineArtifact
    }
}
$BrowserModuleExplicitlyRequested = $Modules -contains "Browser"
$RequestedModules = if ($Modules.Count -gt 0) {
    @($Modules | Select-Object -Unique)
}
elseif ($Mode -in @("Full", "Offline")) {
    @("Browser", "Hardware", "Feishu")
}
else {
    @()
}
if ($WithBrowser -and $RequestedModules -notcontains "Browser") {
    $RequestedModules = @($RequestedModules + "Browser")
}
$BrowserDownloadRequested = (-not [bool]$SkipBrowserDownload) -and ([bool]$WithBrowser -or $BrowserModuleExplicitlyRequested)
if ($RequestedModules -notcontains "Browser") {
    Add-AhaInstallResult -Name "browser" -Kind "optional-module" -Status "skipped" -Detail "Optional; rerun with -WithBrowser or -Modules Browser"
}

$ExistingStartupTask = Get-AhaStartupTask -TaskPath $StartupTaskPath -TaskName $StartupTaskName
$StartupRequested = $EnableStartup -or ($null -ne $ExistingStartupTask)
if ($StartupRequested -and -not (Test-IsAdministrator)) {
    throw "Installing or upgrading pre-login startup requires an elevated PowerShell"
}
if ($null -ne $ExistingStartupTask) {
    Stop-AhaStartupTask -TaskPath $StartupTaskPath -TaskName $StartupTaskName
}

$PythonExe = Resolve-AhaPython `
    -Requested $Python `
    -InstallMissing ($Mode -ne "Minimal") `
    -OfflineRoot $OfflineRoot
$PythonVersion = (& $PythonExe -c "import sys; print('.'.join(map(str, sys.version_info[:3])))" 2>&1 | Out-String).Trim()
$pythonVersionExitCode = $LASTEXITCODE
if ($pythonVersionExitCode -ne 0 -or [Version]$PythonVersion -lt [Version]"3.10") {
    throw "AHA requires Python 3.10 or newer; found: $PythonVersion"
}
Add-AhaInstallResult -Name "python" -Kind "runtime" -Status "present" -Detail "$PythonExe ($PythonVersion)" -Required $true
$PythonwExe = Join-Path (Split-Path -Parent $PythonExe) "pythonw.exe"
if (-not (Test-Path -LiteralPath $PythonwExe -PathType Leaf)) {
    $PythonwExe = $PythonExe
}

New-Item -ItemType Directory -Force -Path $AhaDir | Out-Null
New-Item -ItemType Directory -Force -Path $AhaHome | Out-Null
$AhaDir = (Resolve-Path -LiteralPath $AhaDir).Path
$AhaHome = (Resolve-Path -LiteralPath $AhaHome).Path
$InstallBin = Join-Path $AhaDir "aha"
$Candidate = Join-Path ([System.IO.Path]::GetTempPath()) ("aha-" + [Guid]::NewGuid().ToString("N"))
$ExpectedSha256 = $Sha256.ToLowerInvariant()
$DownloadedChecksum = ""
$EffectiveChecksumUrl = $ChecksumUrl
if (-not $Artifact -and [string]::IsNullOrWhiteSpace($EffectiveChecksumUrl) -and $DownloadUrl -match '/aha$') {
    $EffectiveChecksumUrl = $DownloadUrl.Substring(0, $DownloadUrl.Length - 3) + "SHA256SUMS"
}
try {
    if ($Artifact) {
        if (-not (Test-Path -LiteralPath $Artifact -PathType Leaf)) {
            throw "AHA artifact not found: $Artifact"
        }
        Copy-Item -LiteralPath $Artifact -Destination $Candidate -Force
    }
    else {
        Invoke-WebRequest -Uri $DownloadUrl -OutFile $Candidate -UseBasicParsing
        if (-not [string]::IsNullOrWhiteSpace($EffectiveChecksumUrl)) {
            $DownloadedChecksum = Join-Path ([System.IO.Path]::GetTempPath()) ("aha-sha256-" + [Guid]::NewGuid().ToString("N"))
            try {
                Invoke-WebRequest -Uri $EffectiveChecksumUrl -OutFile $DownloadedChecksum -UseBasicParsing
            }
            catch {
                Write-Warning "Failed to download SHA256SUMS: $($_.Exception.Message)"
            }
        }
    }
    if (-not $ExpectedSha256 -and $OfflineRoot) {
        $ExpectedSha256 = Read-AhaChecksumFile -Path (Join-Path $OfflineRoot "SHA256SUMS")
    }
    if (-not $ExpectedSha256 -and $DownloadedChecksum) {
        $ExpectedSha256 = Read-AhaChecksumFile -Path $DownloadedChecksum
    }

    Assert-AhaArtifactHash -Path $Candidate -Expected $ExpectedSha256
    $versionOutput = (& $PythonExe $Candidate --version 2>&1 | Out-String).Trim()
    $versionExitCode = $LASTEXITCODE
    if ($versionExitCode -ne 0 -or -not $versionOutput.StartsWith("aha ")) {
        throw "Downloaded AHA artifact failed validation: $versionOutput"
    }
    Move-Item -LiteralPath $Candidate -Destination $InstallBin -Force
    Add-AhaInstallResult -Name "aha" -Kind "core" -Status "installed" -Detail $versionOutput -Required $true
}
finally {
    if (Test-Path -LiteralPath $Candidate) {
        Remove-Item -LiteralPath $Candidate -Force
    }
    if ($DownloadedChecksum -and (Test-Path -LiteralPath $DownloadedChecksum)) {
        Remove-Item -LiteralPath $DownloadedChecksum -Force
    }
}

$ModuleInstallOk = $true
if ($RequestedModules -contains "Browser") {
    $browserModuleOk = Install-AhaPythonModule `
        -PythonPath $PythonExe `
        -Name "browser" `
        -Package "playwright>=1.45,<2" `
        -ImportName "playwright" `
        -OfflineRoot $OfflineRoot
    if ($browserModuleOk) {
        $browserRuntimeOk = Install-AhaBrowserRuntime `
            -PythonPath $PythonExe `
            -OfflineRoot $OfflineRoot `
            -SkipDownload (-not $BrowserDownloadRequested)
        $ModuleInstallOk = $ModuleInstallOk -and $browserRuntimeOk
    }
    else {
        $ModuleInstallOk = $false
    }
}
if ($RequestedModules -contains "Hardware") {
    $ModuleInstallOk = (Install-AhaPythonModule `
        -PythonPath $PythonExe `
        -Name "hardware" `
        -Package "pyserial>=3.5" `
        -ImportName "serial" `
        -OfflineRoot $OfflineRoot) -and $ModuleInstallOk
}
if ($RequestedModules -contains "Feishu") {
    $ModuleInstallOk = (Install-AhaPythonModule `
        -PythonPath $PythonExe `
        -Name "feishu" `
        -Package "lark-channel-sdk>=1.2,<2" `
        -ImportName "lark_channel" `
        -OfflineRoot $OfflineRoot) -and $ModuleInstallOk
}
if ($Mode -in @("Full", "Offline")) {
    $gitOk = Ensure-AhaGit -Offline ($Mode -eq "Offline")
    $agentOk = Ensure-AhaAgentBackend -Selection $AgentBackend -Offline ($Mode -eq "Offline")
    $ModuleInstallOk = $ModuleInstallOk -and $gitOk -and $agentOk
}
elseif ($AgentBackend -notin @("Auto", "None")) {
    $ModuleInstallOk = (Ensure-AhaAgentBackend -Selection $AgentBackend -Offline $false) -and $ModuleInstallOk
}

$TokenFile = Join-Path $AhaHome "web-token"
$LoopbackBinds = @("127.0.0.1", "localhost", "::1", "[::1]")
if ($NoAuth -and $LoopbackBinds -notcontains $Bind.ToLowerInvariant() -and -not $AllowUnsafeBind) {
    throw "-NoAuth with a network-visible -Bind requires -AllowUnsafeBind"
}
if (-not $NoAuth -and -not (Test-Path -LiteralPath $TokenFile -PathType Leaf)) {
    Set-Content -LiteralPath $TokenFile -Value (New-WebToken) -Encoding ASCII -NoNewline
}

$TrayConfig = Join-Path $AhaDir "tray.json"
$ServiceLauncher = Join-Path $AhaDir "start-web.ps1"
$ConfiguredTokenFile = if ($NoAuth) { "" } else { $TokenFile }
$ConfiguredTaskName = if ($StartupRequested) { $StartupTaskFullName } else { "" }
Write-AhaTrayConfig `
    -Path $TrayConfig `
    -HomePath $AhaHome `
    -BindAddress $Bind `
    -WebPort $Port `
    -WebTokenFile $ConfiguredTokenFile `
    -StartupTaskName $ConfiguredTaskName
Write-AhaServiceLauncher -Path $ServiceLauncher -PythonPath $PythonExe -ArtifactPath $InstallBin -ConfigPath $TrayConfig

if ($StartupRequested) {
    Install-AhaStartupTask `
        -TaskPath $StartupTaskPath `
        -TaskName $StartupTaskName `
        -LauncherPath $ServiceLauncher `
        -WorkingDirectory $AhaDir `
        -Credential $StartupCredential
    $LoginCommand = (Quote-StartProcessArgument $PythonwExe) + " " + (Quote-StartProcessArgument $InstallBin) + " tray"
    Set-AhaLoginStartup -Enabled $true -Command $LoginCommand
    Start-ScheduledTask -TaskPath $StartupTaskPath -TaskName $StartupTaskName
}

$InstallReport = Join-Path $AhaDir "install-report.json"
Write-AhaInstallReport `
    -Path $InstallReport `
    -InstallMode $Mode `
    -InstalledVersion $versionOutput `
    -PythonPath $PythonExe `
    -InstallPath $InstallBin `
    -RepairRequested ([bool]$Repair)
if ($StrictModules -and -not $ModuleInstallOk) {
    throw "AHA core and service configuration were installed, but one or more requested dependencies failed. See: $InstallReport"
}

if (-not $NoShortcut) {
    $IconPath = Join-Path $AhaDir "aha.ico"
    Install-AhaIcon -ArtifactPath $InstallBin -Destination $IconPath | Out-Null
    $ShortcutPath = Install-AhaStartMenuShortcut `
        -PythonwPath $PythonwExe `
        -ArtifactPath $InstallBin `
        -WorkingDirectory $AhaDir `
        -IconPath $IconPath
}

if (-not $NoStart) {
    $TrayArguments = @(
        (Quote-StartProcessArgument $InstallBin),
        "--home",
        (Quote-StartProcessArgument $AhaHome),
        "tray",
        "--host",
        $Bind,
        "--port",
        $Port.ToString(),
        "--open-browser"
    )
    if (-not $NoAuth) {
        $TrayArguments += @("--auth-token-file", (Quote-StartProcessArgument $TokenFile))
    }
    Start-Process -FilePath $PythonwExe -ArgumentList $TrayArguments -WindowStyle Hidden
}

Write-Host "Installed AHA: $InstallBin"
Write-Host "Install mode: $Mode"
Write-Host "Requested modules: $($RequestedModules -join ', ')"
Write-Host "Agent backend: $AgentBackend"
Write-Host "Repair requested: $([bool]$Repair)"
Write-Host "Install report: $InstallReport"
Write-Host "AHA home: $AhaHome"
Write-Host "Bind: $Bind"
Write-Host "Port: $Port"
Write-Host "Tray started: $(-not $NoStart)"
Write-Host "Pre-login startup enabled: $StartupRequested"
Write-Host "Startup task: $ConfiguredTaskName"
Write-Host "Start Menu shortcut: $ShortcutPath"
foreach ($result in $script:AhaInstallResults) {
    Write-Host ("[{0}] {1}: {2}" -f $result.status.ToUpperInvariant(), $result.name, $result.detail)
}
if (-not $ModuleInstallOk) {
    Write-Warning "AHA core is installed, but one or more optional or external modules need attention. See: $InstallReport"
}
