[CmdletBinding()]
param(
    [string]$TaskName = 'SalesMobileIngest'
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$removed = $false
& schtasks.exe /Query /TN $TaskName *> $null
if ($LASTEXITCODE -eq 0) {
    & schtasks.exe /Delete /TN $TaskName /F | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "schtasks delete failed with exit code $LASTEXITCODE" }
    $removed = $true
}
$fallbackPath = Join-Path ([Environment]::GetFolderPath('Startup')) "$TaskName.vbs"
if (Test-Path -LiteralPath $fallbackPath -PathType Leaf) {
    Remove-Item -LiteralPath $fallbackPath -Force
    $removed = $true
}
$workers = Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'" | Where-Object {
    $_.CommandLine -like "*$projectRoot*sales_mobile_ingest*watch*"
}
foreach ($worker in $workers) {
    Stop-Process -Id $worker.ProcessId -Force
    $removed = $true
}
if ($removed) { Write-Output "Removed $TaskName auto-start registration and matching watcher processes" } else { Write-Output "$TaskName had no installed auto-start registration" }
