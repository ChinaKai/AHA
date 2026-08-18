[CmdletBinding()]
param(
    [string]$Python = "",
    [string]$AhaDir = (Join-Path $env:LOCALAPPDATA "AHA"),
    [string]$AhaHome = (Join-Path $env:USERPROFILE ".aha"),
    [ValidateNotNullOrEmpty()][string]$Bind = "127.0.0.1",
    [ValidateRange(1, 65535)][int]$Port = 8788,
    [string]$DownloadUrl = "https://github.com/ChinaKai/AHA/releases/latest/download/aha",
    [string]$Artifact = "",
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

function Resolve-AhaPython {
    param([string]$Requested)

    $venvDir = Join-Path $env:USERPROFILE ".venvs\aha"
    $venvPython = Join-Path $venvDir "Scripts\python.exe"
    if ($Requested) {
        if (-not (Test-Path -LiteralPath $Requested -PathType Leaf)) {
            throw "Python executable not found: $Requested"
        }
        return (Resolve-Path -LiteralPath $Requested).Path
    }
    if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
        return $venvPython
    }

    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) {
        & $launcher.Source -3 -m venv $venvDir 2>&1 | Out-Null
    }
    else {
        $systemPython = Get-Command python.exe -ErrorAction SilentlyContinue
        if (-not $systemPython) {
            throw "Python 3.10+ is required. Install it with: winget install --id Python.Python.3.12 -e"
        }
        & $systemPython.Source -m venv $venvDir 2>&1 | Out-Null
    }
    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        throw "Failed to create the AHA Python environment: $venvDir"
    }
    return $venvPython
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

$ExistingStartupTask = Get-AhaStartupTask -TaskPath $StartupTaskPath -TaskName $StartupTaskName
$StartupRequested = $EnableStartup -or ($null -ne $ExistingStartupTask)
if ($StartupRequested -and -not (Test-IsAdministrator)) {
    throw "Installing or upgrading pre-login startup requires an elevated PowerShell"
}
if ($null -ne $ExistingStartupTask) {
    Stop-AhaStartupTask -TaskPath $StartupTaskPath -TaskName $StartupTaskName
}

$PythonExe = Resolve-AhaPython -Requested $Python
$PythonVersion = (& $PythonExe -c "import sys; print('.'.join(map(str, sys.version_info[:3])))" 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or [Version]$PythonVersion -lt [Version]"3.10") {
    throw "AHA requires Python 3.10 or newer; found: $PythonVersion"
}
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
try {
    if ($Artifact) {
        if (-not (Test-Path -LiteralPath $Artifact -PathType Leaf)) {
            throw "AHA artifact not found: $Artifact"
        }
        Copy-Item -LiteralPath $Artifact -Destination $Candidate -Force
    }
    else {
        Invoke-WebRequest -Uri $DownloadUrl -OutFile $Candidate -UseBasicParsing
    }

    $versionOutput = (& $PythonExe $Candidate --version 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $versionOutput.StartsWith("aha ")) {
        throw "Downloaded AHA artifact failed validation: $versionOutput"
    }
    Move-Item -LiteralPath $Candidate -Destination $InstallBin -Force
}
finally {
    if (Test-Path -LiteralPath $Candidate) {
        Remove-Item -LiteralPath $Candidate -Force
    }
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
Write-Host "AHA home: $AhaHome"
Write-Host "Bind: $Bind"
Write-Host "Port: $Port"
Write-Host "Tray started: $(-not $NoStart)"
Write-Host "Pre-login startup enabled: $StartupRequested"
Write-Host "Startup task: $ConfiguredTaskName"
Write-Host "Start Menu shortcut: $ShortcutPath"
