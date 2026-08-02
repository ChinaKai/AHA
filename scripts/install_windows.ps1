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
        [string]$WebTokenFile
    )

    $settings = [ordered]@{
        aha_home = $HomePath
        bind = $BindAddress
        port = $WebPort
        web_token_file = $WebTokenFile
    }
    $json = $settings | ConvertTo-Json
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $json + [Environment]::NewLine, $utf8NoBom)
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

if ($EnableStartup -and $NoStart) {
    throw "-EnableStartup requires the tray to start; remove -NoStart"
}

$TrayConfig = Join-Path $AhaDir "tray.json"
$ConfiguredTokenFile = if ($NoAuth) { "" } else { $TokenFile }
Write-AhaTrayConfig -Path $TrayConfig -HomePath $AhaHome -BindAddress $Bind -WebPort $Port -WebTokenFile $ConfiguredTokenFile

$ShortcutPath = ""
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
    if ($EnableStartup) {
        $TrayArguments += "--enable-startup"
    }
    Start-Process -FilePath $PythonwExe -ArgumentList $TrayArguments -WindowStyle Hidden
}

Write-Host "Installed AHA: $InstallBin"
Write-Host "AHA home: $AhaHome"
Write-Host "Bind: $Bind"
Write-Host "Port: $Port"
Write-Host "Tray started: $(-not $NoStart)"
Write-Host "Startup enabled: $EnableStartup"
Write-Host "Start Menu shortcut: $ShortcutPath"
