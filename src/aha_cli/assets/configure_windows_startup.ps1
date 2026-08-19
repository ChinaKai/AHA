[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("Enable", "Disable")]
    [string]$Mode,
    [Parameter(Mandatory = $true)]
    [string]$TaskName,
    [string]$PythonwPath = "",
    [string]$ArtifactPath = "",
    [string]$ConfigPath = "",
    [string]$LauncherPath = "",
    [string]$WorkingDirectory = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$TaskPath = "\"
trap {
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show(
        $_.Exception.Message,
        "AHA startup configuration",
        [System.Windows.Forms.MessageBoxButtons]::OK,
        [System.Windows.Forms.MessageBoxIcon]::Error
    ) | Out-Null
    exit 1
}

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Quote-PowerShellLiteral {
    param([string]$Value)
    return "'" + $Value.Replace("'", "''") + "'"
}

function Quote-StartProcessArgument {
    param([string]$Value)
    return '"' + ($Value -replace '"', '\"') + '"'
}

function Get-AhaStartupTask {
    return Get-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -ErrorAction SilentlyContinue
}

function Stop-AhaStartupTask {
    $task = Get-AhaStartupTask
    if ($null -eq $task) {
        return
    }
    Stop-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -ErrorAction SilentlyContinue
}

function Remove-AhaStartupTask {
    $task = Get-AhaStartupTask
    if ($null -eq $task) {
        return
    }
    Stop-AhaStartupTask
    Unregister-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -Confirm:$false
}

function Resolve-AhaStartupCredential {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $credential = Get-Credential `
        -UserName $identity.Name `
        -Message "AHA needs the current Windows account password to start before sign-in. Task Scheduler stores it as an LSA-protected secret."
    if ($null -eq $credential) {
        throw "The current Windows account credential is required"
    }
    $account = New-Object Security.Principal.NTAccount($credential.UserName)
    $credentialSid = $account.Translate([Security.Principal.SecurityIdentifier]).Value
    if ($credentialSid -ne $identity.User.Value) {
        throw "The startup task must use the current Windows account"
    }
    return $credential
}

function Write-AhaServiceLauncher {
    $pythonLiteral = Quote-PowerShellLiteral $PythonwPath
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
    [System.IO.File]::WriteAllText($LauncherPath, $content, $utf8NoBom)
}

if (-not (Test-IsAdministrator)) {
    throw "Configuring pre-login startup requires administrator permission"
}

if ($Mode -eq "Disable") {
    Remove-AhaStartupTask
    exit 0
}

foreach ($requiredPath in @($PythonwPath, $ArtifactPath, $ConfigPath)) {
    if ([string]::IsNullOrWhiteSpace($requiredPath) -or -not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
        throw "Invalid AHA startup path: $requiredPath"
    }
}
if ([string]::IsNullOrWhiteSpace($LauncherPath) -or [string]::IsNullOrWhiteSpace($WorkingDirectory)) {
    throw "The AHA startup launcher or working directory is missing"
}
New-Item -ItemType Directory -Force -Path $WorkingDirectory | Out-Null
Write-AhaServiceLauncher

$credential = Resolve-AhaStartupCredential
$password = $credential.GetNetworkCredential().Password
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
Register-ScheduledTask `
    -TaskPath $TaskPath `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "Start the AHA Web service before Windows sign-in" `
    -User $credential.UserName `
    -Password $password `
    -RunLevel Limited `
    -Force | Out-Null
Enable-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName | Out-Null
