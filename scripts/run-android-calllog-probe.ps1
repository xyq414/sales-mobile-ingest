[CmdletBinding()]
param(
    [string]$DataRoot = (Join-Path $env:USERPROFILE "Documents\SalesMobileIngestData"),
    [string]$EventPath,
    [switch]$BuildOnly
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$RepositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ProbeRoot = Join-Path $RepositoryRoot "android\calllog-probe"
$ToolRoot = Join-Path $RepositoryRoot "android\.tooling"
$SdkRoot = Join-Path $ToolRoot "android-sdk"
$JavaHome = (Get-ChildItem -LiteralPath (Join-Path $ToolRoot "jdk-17") -Directory | Select-Object -First 1).FullName
$Adb = Join-Path $SdkRoot "platform-tools\adb.exe"
$LocalGradle = Join-Path $ToolRoot "gradle-8.13\bin\gradle.bat"
$PackageName = "com.salesmobileingest.calllogprobe"
$PermissionName = "android.permission.READ_CALL_LOG"
$DiagnosticsRoot = Join-Path $DataRoot "diagnostics\android-calllog-probe"

function Write-SafeStatus {
    param([hashtable]$Status)
    New-Item -ItemType Directory -Force -Path $DiagnosticsRoot | Out-Null
    $Status.probe_package = $PackageName
    $Status.generated_at = [DateTimeOffset]::UtcNow.ToString("o")
    $json = $Status | ConvertTo-Json -Depth 6
    [System.IO.File]::WriteAllText((Join-Path $DiagnosticsRoot "last-run-safe.json"), $json + [Environment]::NewLine, [System.Text.UTF8Encoding]::new($false))
    Write-Output $json
}

if (-not $JavaHome -or -not (Test-Path -LiteralPath (Join-Path $JavaHome "bin\java.exe"))) {
    Write-SafeStatus @{ status = "ANDROID_BUILD_TOOLCHAIN_UNAVAILABLE"; stage = "jdk" }
    exit 2
}
if (-not (Test-Path -LiteralPath (Join-Path $SdkRoot "platforms\android-36\android.jar"))) {
    Write-SafeStatus @{ status = "ANDROID_BUILD_TOOLCHAIN_UNAVAILABLE"; stage = "android_sdk" }
    exit 2
}

$env:JAVA_HOME = $JavaHome
$env:ANDROID_HOME = $SdkRoot
$env:ANDROID_SDK_ROOT = $SdkRoot
$env:Path = "$JavaHome\bin;$SdkRoot\platform-tools;$env:Path"

Push-Location $ProbeRoot
try {
    if (Test-Path -LiteralPath $LocalGradle) {
        & $LocalGradle --no-daemon assembleDebug
    } else {
        & .\gradlew.bat --no-daemon assembleDebug
    }
    if ($LASTEXITCODE -ne 0) {
        Write-SafeStatus @{ status = "APK_BUILD_FAILED"; stage = "gradle"; exit_code = $LASTEXITCODE }
        exit 2
    }
} finally {
    Pop-Location
}

$ApkPath = Join-Path $ProbeRoot "app\build\outputs\apk\debug\app-debug.apk"
if (-not (Test-Path -LiteralPath $ApkPath)) {
    Write-SafeStatus @{ status = "APK_BUILD_FAILED"; stage = "apk_output_missing" }
    exit 2
}
if ($BuildOnly) {
    Write-SafeStatus @{ status = "APK_BUILD_PASS"; stage = "build_only"; apk_sha256 = (Get-FileHash -LiteralPath $ApkPath -Algorithm SHA256).Hash.ToLower() }
    exit 0
}
if (-not (Test-Path -LiteralPath $Adb)) {
    Write-SafeStatus @{ status = "AUTOMATED_ANDROID_APP_INSTALL_UNAVAILABLE_IN_CURRENT_DEVICE_STATE"; stage = "adb_unavailable"; apk_build = "PASS" }
    exit 3
}

$deviceLines = @(& $Adb devices -l 2>&1)
$devices = @($deviceLines | Where-Object { $_ -match "^\S+\s+device(?:\s|$)" })
if ($devices.Count -ne 1) {
    $state = if (($deviceLines -join "`n") -match "unauthorized") { "UNAUTHORIZED" } elseif (($deviceLines -join "`n") -match "offline") { "OFFLINE" } else { "NONE" }
    Write-SafeStatus @{ status = "AUTOMATED_ANDROID_APP_INSTALL_UNAVAILABLE_IN_CURRENT_DEVICE_STATE"; stage = "adb_devices"; adb_device_state = $state; apk_build = "PASS" }
    exit 3
}
$serial = ($devices[0] -split "\s+")[0]
$existing = @(& $Adb -s $serial shell pm path $PackageName 2>&1)
if ($LASTEXITCODE -eq 0 -and ($existing -join "`n") -match "^package:") {
    Write-SafeStatus @{ status = "PROBE_PACKAGE_ALREADY_PRESENT_REFUSE_OVERWRITE"; stage = "preinstall"; apk_build = "PASS" }
    exit 4
}

& $Adb -s $serial install --replace $ApkPath | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-SafeStatus @{ status = "APK_INSTALL_FAILED"; stage = "adb_install"; apk_build = "PASS" }
    exit 5
}

$grantOutput = @(& $Adb -s $serial shell pm grant $PackageName $PermissionName 2>&1)
$grantStatus = if ($LASTEXITCODE -eq 0) { "GRANTED" } else { "DENIED" }
$permissionEvidence = @(& $Adb -s $serial shell dumpsys package $PackageName 2>&1 | Select-String -Pattern "READ_CALL_LOG|granted=|flags=")
$appOpsEvidence = @(& $Adb -s $serial shell appops get $PackageName $PermissionName 2>&1)
New-Item -ItemType Directory -Force -Path $DiagnosticsRoot | Out-Null
[System.IO.File]::WriteAllText((Join-Path $DiagnosticsRoot "permission-evidence.txt"), (($grantOutput + $permissionEvidence + $appOpsEvidence) -join [Environment]::NewLine), [System.Text.UTF8Encoding]::new($false))

if (-not $EventPath) {
    $eventFiles = @(Get-ChildItem -LiteralPath (Join-Path $DataRoot "ready\events") -Filter "*.json" -File)
    if ($eventFiles.Count -ne 1) {
        Write-SafeStatus @{ status = "CALL_LOG_PROBE_EVENT_TARGET_UNAVAILABLE"; stage = "event_selection"; apk_install = "PASS"; runtime_grant = $grantStatus }
        exit 6
    }
    $EventPath = $eventFiles[0].FullName
}
$event = Get-Content -Raw -LiteralPath $EventPath | ConvertFrom-Json
$targetOccurredAt = [DateTimeOffset]::Parse([string]$event.occurred_at).ToUnixTimeMilliseconds()
$targetDuration = [math]::Round([double]$event.duration_seconds)
& $Adb -s $serial shell am start -n "$PackageName/.MainActivity" --el targetOccurredAtMillis $targetOccurredAt --el targetDurationSeconds $targetDuration --el windowSeconds 900 | Out-Null
Start-Sleep -Seconds 2
$rawResult = @(& $Adb -s $serial shell run-as $PackageName cat files/probe-result.json 2>&1)
if ($LASTEXITCODE -ne 0 -or -not ($rawResult -join "")) {
    Write-SafeStatus @{ status = "CALL_LOG_PROBE_RESULT_RETRIEVAL_FAILED"; stage = "run_as"; apk_install = "PASS"; runtime_grant = $grantStatus }
    exit 7
}

$rawPath = Join-Path $DiagnosticsRoot "probe-result-private.json"
$safePath = Join-Path $DiagnosticsRoot "probe-result-safe.json"
[System.IO.File]::WriteAllText($rawPath, ($rawResult -join "`n"), [System.Text.UTF8Encoding]::new($false))
$python = Join-Path $RepositoryRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) { $python = "python" }
& $python -m sales_mobile_ingest calllog-probe-summary --raw-result $rawPath --event $EventPath --safe-output $safePath | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-SafeStatus @{ status = "CALL_LOG_PROBE_SAFE_SUMMARY_FAILED"; stage = "windows_parser"; apk_install = "PASS"; runtime_grant = $grantStatus }
    exit 8
}

$safe = Get-Content -Raw -LiteralPath $safePath | ConvertFrom-Json
& $Adb -s $serial uninstall $PackageName | Out-Null
$cleanup = if ($LASTEXITCODE -eq 0) { "UNINSTALLED" } else { "CLEANUP_FAILED" }
Write-SafeStatus @{
    status = if ($safe.query_status -eq "PASS") { "CALL_LOG_PROBE_COMPLETED" } else { "CALL_LOG_PROBE_COMPLETED_WITH_QUERY_FAILURE" }
    stage = "complete"
    apk_build = "PASS"
    apk_install = "PASS"
    runtime_grant = $grantStatus
    query_status = $safe.query_status
    correlation_status = $safe.recording_correlation.status
    probe_apk = $cleanup
}
