[CmdletBinding()]
param(
    [string]$DataRoot,
    [string]$TaskName = 'SalesMobileIngest'
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $projectRoot '.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonw -PathType Leaf)) {
    throw "Expected virtual-environment launcher not found: $pythonw"
}
if ([string]::IsNullOrWhiteSpace($DataRoot)) {
    $configPath = Join-Path $projectRoot 'config.local.json'
    if (Test-Path -LiteralPath $configPath -PathType Leaf) {
        $DataRoot = (Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json).data_root
    }
}
if ([string]::IsNullOrWhiteSpace($DataRoot)) {
    $DataRoot = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'SalesMobileIngestData'
}
$taskAction = '"' + $pythonw + '" -m sales_mobile_ingest --data-root "' + $DataRoot + '" watch --interval 45'
$taskOutput = & schtasks.exe /Create /TN $TaskName /SC ONLOGON /TR $taskAction /F 2>&1
if ($LASTEXITCODE -eq 0) {
    & schtasks.exe /Query /TN $TaskName /XML | Out-File -LiteralPath (Join-Path $env:TEMP "$TaskName.xml") -Encoding utf8
    if ($LASTEXITCODE -ne 0) { throw "schtasks query failed with exit code $LASTEXITCODE" }
    Write-Output "Installed Task Scheduler task $TaskName for user-logon watch mode. Data root: $DataRoot"
    exit 0
}

# Some managed Windows installations deny normal users access to Task Scheduler.
# Startup-folder VBS is the removable per-user fallback and launches pythonw hidden.
$startup = [Environment]::GetFolderPath('Startup')
$fallbackPath = Join-Path $startup "$TaskName.vbs"
$escapedAction = $taskAction.Replace('"', '""')
$vbs = 'Set shell = CreateObject("WScript.Shell")' + [Environment]::NewLine + 'shell.Run "' + $escapedAction + '", 0, False'
Set-Content -LiteralPath $fallbackPath -Value $vbs -Encoding ASCII
if (-not (Test-Path -LiteralPath $fallbackPath -PathType Leaf)) { throw "Task Scheduler failed and Startup fallback was not created: $taskOutput" }
Write-Output "Task Scheduler denied access; installed Startup fallback $fallbackPath. Data root: $DataRoot"
