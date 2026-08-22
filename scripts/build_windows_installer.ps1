[CmdletBinding()]
param(
    [string]$Artifact = "dist\aha",
    [string]$Output = "dist\AHA-Setup-x64.exe",
    [string]$Python = "python",
    [string]$Icon = "src\aha_cli\assets\aha.ico",
    [string]$SignTool = "",
    [string]$CertificateThumbprint = $env:AHA_WINDOWS_SIGN_CERT_SHA1,
    [string]$TimestampUrl = "http://timestamp.digicert.com"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$RepoRoot = Split-Path -Parent $PSScriptRoot
$ArtifactPath = [IO.Path]::GetFullPath((Join-Path $RepoRoot $Artifact))
$OutputPath = [IO.Path]::GetFullPath((Join-Path $RepoRoot $Output))
$BootstrapPath = Join-Path $PSScriptRoot "windows_installer_bootstrap.py"
$InstallerPath = Join-Path $PSScriptRoot "install_windows.ps1"
$IconPath = [IO.Path]::GetFullPath((Join-Path $RepoRoot $Icon))

foreach ($path in @($ArtifactPath, $BootstrapPath, $InstallerPath, $IconPath)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Required installer input not found: $path"
    }
}

& $Python -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller is required. Install it with: $Python -m pip install 'pyinstaller>=6,<7'"
}

$BuildRoot = Join-Path $RepoRoot "build\windows-installer"
$DistRoot = Join-Path $BuildRoot "dist"
$WorkRoot = Join-Path $BuildRoot "work"
$SpecRoot = Join-Path $BuildRoot "spec"
$PayloadRoot = Join-Path $BuildRoot "payload"
New-Item -ItemType Directory -Force -Path $DistRoot, $WorkRoot, $SpecRoot, $PayloadRoot | Out-Null
$BundledArtifact = Join-Path $PayloadRoot "aha"
Copy-Item -LiteralPath $ArtifactPath -Destination $BundledArtifact -Force

$dataSeparator = ";"
$arguments = @(
    "-m", "PyInstaller",
    "--noconfirm",
    "--clean",
    "--onefile",
    "--windowed",
    "--name", "AHA-Setup",
    "--icon", $IconPath,
    "--distpath", $DistRoot,
    "--workpath", $WorkRoot,
    "--specpath", $SpecRoot,
    "--add-data", ($BundledArtifact + $dataSeparator + "payload"),
    "--add-data", ($InstallerPath + $dataSeparator + "payload"),
    "--add-data", ($IconPath + $dataSeparator + "payload"),
    $BootstrapPath
)
& $Python @arguments
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE"
}

$BuiltExe = Join-Path $DistRoot "AHA-Setup.exe"
if (-not (Test-Path -LiteralPath $BuiltExe -PathType Leaf)) {
    throw "PyInstaller output not found: $BuiltExe"
}
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $OutputPath) | Out-Null
Copy-Item -LiteralPath $BuiltExe -Destination $OutputPath -Force

if (-not [string]::IsNullOrWhiteSpace($CertificateThumbprint)) {
    if ([string]::IsNullOrWhiteSpace($SignTool)) {
        $resolved = Get-Command signtool.exe -ErrorAction SilentlyContinue
        if (-not $resolved) {
            throw "signtool.exe is required when CertificateThumbprint is configured"
        }
        $SignTool = $resolved.Source
    }
    & $SignTool sign /sha1 $CertificateThumbprint /fd SHA256 /tr $TimestampUrl /td SHA256 $OutputPath
    if ($LASTEXITCODE -ne 0) {
        throw "signtool failed with exit code $LASTEXITCODE"
    }
    Write-Host "Signed Windows installer: $OutputPath"
}
else {
    Write-Warning "Windows installer is unsigned; configure AHA_WINDOWS_SIGN_CERT_SHA1 for release signing"
}

Write-Host "Built Windows installer: $OutputPath"
