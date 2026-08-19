[CmdletBinding()]
param(
    [string]$TaskName = 'SalesMobileIngest'
)

$ErrorActionPreference = 'Stop'
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
if ($removed) { Write-Output "Removed $TaskName auto-start registration" } else { Write-Output "$TaskName had no installed auto-start registration" }
