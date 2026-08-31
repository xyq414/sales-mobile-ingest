param(
    [string]$Python = "",
    [string]$ReleaseName = "SalesMobileIngest-Pilot-win64",
    [switch]$SkipSmoke
)

$ErrorActionPreference = "Stop"
$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
if ([string]::IsNullOrWhiteSpace($Python)) {
    $Python = Join-Path $projectRoot ".venv\Scripts\python.exe"
}
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Build Python was not found. Prepare the repository development environment first."
}
if ($ReleaseName -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$') {
    throw "ReleaseName must be a short filename-safe value without path separators."
}

function Assert-SafeChildPath([string]$Target, [string]$Parent) {
    $resolvedTarget = [System.IO.Path]::GetFullPath($Target).TrimEnd([System.IO.Path]::DirectorySeparatorChar)
    $resolvedParent = [System.IO.Path]::GetFullPath($Parent).TrimEnd([System.IO.Path]::DirectorySeparatorChar)
    $prefix = $resolvedParent + [System.IO.Path]::DirectorySeparatorChar
    if (-not $resolvedTarget.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to modify a path outside the intended release directory: $resolvedTarget"
    }
}

$distParent = Join-Path $projectRoot "dist"
$distFolder = Join-Path $distParent "SalesMobileIngest"
$releaseParent = Join-Path $projectRoot "release"
$releaseFolder = Join-Path $releaseParent $ReleaseName
$releaseZip = Join-Path $releaseParent ($ReleaseName + ".zip")
New-Item -ItemType Directory -Path $distParent -Force | Out-Null
New-Item -ItemType Directory -Path $releaseParent -Force | Out-Null

Push-Location $projectRoot
try {
    & $Python -m PyInstaller --noconfirm --clean --distpath $distParent --workpath (Join-Path $projectRoot "build") (Join-Path $projectRoot "SalesMobileIngest.spec")
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed with exit code $LASTEXITCODE" }
} finally {
    Pop-Location
}

if (-not (Test-Path -LiteralPath (Join-Path $distFolder "SalesMobileIngest.exe") -PathType Leaf)) {
    throw "The packaged desktop executable was not produced."
}

Assert-SafeChildPath $releaseFolder $releaseParent
if (Test-Path -LiteralPath $releaseFolder) {
    Remove-Item -LiteralPath $releaseFolder -Recurse -Force
}
Copy-Item -LiteralPath $distFolder -Destination $releaseFolder -Recurse
Compress-Archive -LiteralPath $releaseFolder -DestinationPath $releaseZip -CompressionLevel Optimal -Force

if (-not $SkipSmoke) {
    $smokeRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("sales-mobile-ingest-release-smoke-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $smokeRoot | Out-Null
    $cleanRelease = Join-Path $smokeRoot $ReleaseName
    Copy-Item -LiteralPath $releaseFolder -Destination $cleanRelease -Recurse
    $smokeState = Join-Path $smokeRoot "state"
    New-Item -ItemType Directory -Path $smokeState | Out-Null
    $smokeConfig = Join-Path $smokeState "config.json"
    $smokeData = Join-Path $smokeState "data"
    @{ data_root = $smokeData } | ConvertTo-Json | Set-Content -LiteralPath $smokeConfig -Encoding UTF8
    $previousConfig = $env:SALES_MOBILE_INGEST_CONFIG_PATH
    $env:SALES_MOBILE_INGEST_CONFIG_PATH = $smokeConfig
    try {
        Push-Location $smokeRoot
        try {
            $exe = Join-Path $cleanRelease "SalesMobileIngest.exe"
            $firstReport = Join-Path $smokeState "smoke-first.json"
            $secondReport = Join-Path $smokeState "smoke-second.json"
            $screenshot = Join-Path $smokeState "packaged-window.png"
            $firstProcess = Start-Process -FilePath $exe -ArgumentList @("--smoke-report", $firstReport, "--screenshot", $screenshot) -Wait -PassThru -WindowStyle Hidden
            if ($firstProcess.ExitCode -ne 0) { throw "First packaged smoke failed with exit code $($firstProcess.ExitCode)" }
            $secondProcess = Start-Process -FilePath $exe -ArgumentList @("--smoke-report", $secondReport) -Wait -PassThru -WindowStyle Hidden
            if ($secondProcess.ExitCode -ne 0) { throw "Second packaged smoke failed with exit code $($secondProcess.ExitCode)" }
            $first = Get-Content -LiteralPath $firstReport -Raw | ConvertFrom-Json
            $second = Get-Content -LiteralPath $secondReport -Raw | ConvertFrom-Json
            if ($first.status -ne "PASS" -or $second.status -ne "PASS") { throw "Packaged smoke report did not pass" }
            if ($second.config_persisted_launch_count -ne 2) { throw "Packaged config did not persist across launches" }
            if (-not $first.screenshot_saved -or -not (Test-Path -LiteralPath $screenshot -PathType Leaf)) {
                throw "Packaged GUI screenshot was not produced"
            }
            Copy-Item -LiteralPath $firstReport -Destination (Join-Path $releaseParent "packaged-smoke-first.json") -Force
            Copy-Item -LiteralPath $secondReport -Destination (Join-Path $releaseParent "packaged-smoke-second.json") -Force
            Copy-Item -LiteralPath $screenshot -Destination (Join-Path $releaseParent "packaged-window.png") -Force
        } finally {
            Pop-Location
        }
    } finally {
        $env:SALES_MOBILE_INGEST_CONFIG_PATH = $previousConfig
    }
}

$hash = (Get-FileHash -LiteralPath $releaseZip -Algorithm SHA256).Hash
Write-Output "RELEASE_FOLDER=$releaseFolder"
Write-Output "RELEASE_ZIP=$releaseZip"
Write-Output "RELEASE_SHA256=$hash"
