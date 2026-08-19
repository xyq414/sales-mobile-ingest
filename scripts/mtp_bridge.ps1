[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$InputJsonBase64
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding

function Emit-Json([object]$Value) {
    [Console]::Out.Write(($Value | ConvertTo-Json -Compress -Depth 12))
}

function Decode-Payload([string]$Value) {
    $bytes = [Convert]::FromBase64String($Value)
    return ([System.Text.Encoding]::UTF8.GetString($bytes) | ConvertFrom-Json)
}

function Get-PortableDevices([object]$Shell) {
    $thisPc = $Shell.Namespace(17)
    $portableCn = ([char]0x4FBF).ToString() + ([char]0x643A).ToString()
    $mobileCn = ([char]0x79FB).ToString() + ([char]0x52A8).ToString()
    $result = @()
    foreach ($item in @($thisPc.Items())) {
        $type = [string]$item.Type
        $name = [string]$item.Name
        if ($type -match 'Portable|Mobile' -or $type.Contains($portableCn) -or $type.Contains($mobileCn) -or $name -match 'OPPO|Android') {
            if ($item.IsFolder) {
                $result += $item
            }
        }
    }
    return $result
}

function Get-Children([object]$FolderItem) {
    try {
        $folder = $FolderItem.GetFolder
        return @($folder.Items())
    } catch {
        return @()
    }
}

function Join-Relative([string]$Parent, [string]$Name) {
    if ([string]::IsNullOrWhiteSpace($Parent)) { return $Name }
    return "$Parent/$Name"
}

function Candidate-Names {
    $tongHuaLuYin = ([char]0x901A).ToString() + ([char]0x8BDD).ToString() + ([char]0x5F55).ToString() + ([char]0x97F3).ToString()
    $luYin = ([char]0x5F55).ToString() + ([char]0x97F3).ToString()
    return @('Recordings', 'Recorder', 'Record', 'Call Recordings', 'CallRecord', 'Call Recording', $tongHuaLuYin, $luYin)
}

function Is-CandidateName([string]$Name) {
    foreach ($candidate in @(Candidate-Names)) {
        if ($Name.Equals($candidate, [System.StringComparison]::OrdinalIgnoreCase)) { return $true }
    }
    return $false
}

function Get-ItemSize([object]$Item) {
    try {
        $raw = $Item.ExtendedProperty('System.Size')
        if ($null -ne $raw -and "$raw" -match '^\d+$') { return [Int64]$raw }
    } catch {}
    try {
        $raw = [string]$Item.Size
        $digits = $raw -replace '[^0-9]', ''
        if ($digits) { return [Int64]$digits }
    } catch {}
    return 0
}

function Get-CanonicalFileName([object]$Item) {
    try {
        $fileName = [string]$Item.ExtendedProperty('System.FileName')
        if (-not [string]::IsNullOrWhiteSpace($fileName)) { return $fileName }
    } catch {}
    $name = [string]$Item.Name
    try {
        $extension = [string]$Item.ExtendedProperty('System.FileExtension')
        if (-not [string]::IsNullOrWhiteSpace($extension) -and -not $name.EndsWith($extension, [System.StringComparison]::OrdinalIgnoreCase)) {
            return "$name$extension"
        }
    } catch {}
    return $name
}

function Get-ItemExtension([object]$Item, [string]$FileName) {
    try {
        $extension = [string]$Item.ExtendedProperty('System.FileExtension')
        if (-not [string]::IsNullOrWhiteSpace($extension)) { return $extension.ToLowerInvariant() }
    } catch {}
    return [System.IO.Path]::GetExtension($FileName).ToLowerInvariant()
}

function Get-ItemModifiedAt([object]$Item) {
    try {
        $raw = $Item.ExtendedProperty('System.DateModified')
        if ($raw -is [datetime]) { return $raw.ToUniversalTime().ToString('o') }
        if ($null -ne $raw -and -not [string]::IsNullOrWhiteSpace([string]$raw)) {
            return ([datetime]::Parse([string]$raw, [System.Globalization.CultureInfo]::CurrentCulture)).ToUniversalTime().ToString('o')
        }
    } catch {
    }
    try {
        $raw = [string]$Item.ModifyDate
        if ([string]::IsNullOrWhiteSpace($raw)) { return $null }
        $parsed = [datetime]::Parse($raw, [System.Globalization.CultureInfo]::CurrentCulture)
        if ($parsed.Year -lt 2000) { return $null }
        return $parsed.ToUniversalTime().ToString('o')
    } catch {}
    return $null
}

function Get-DurationSeconds([object]$Item) {
    try {
        $raw = $Item.ExtendedProperty('System.Media.Duration')
        if ($null -ne $raw -and [Int64]$raw -gt 0) {
            return [math]::Round(([double][Int64]$raw / 10000000.0), 3)
        }
    } catch {}
    return $null
}

function Get-AudioFiles([object]$FolderItem, [string]$RelativePath, [int]$Depth = 0) {
    $supported = @('.m4a', '.amr', '.mp3', '.wav', '.aac', '.ogg', '.flac', '.3gp', '.opus')
    $result = @()
    foreach ($child in @(Get-Children $FolderItem)) {
        $childRelative = Join-Relative $RelativePath ([string]$child.Name)
        if ($child.IsFolder) {
            if ($Depth -lt 2) {
                $result += @(Get-AudioFiles $child $childRelative ($Depth + 1))
            }
            continue
        }
        $fileName = Get-CanonicalFileName $child
        $extension = Get-ItemExtension $child $fileName
        if ($supported -contains $extension) {
            $result += [pscustomobject]@{
                name = $fileName
                extension = $extension
                relative_path = $childRelative
                size_bytes = Get-ItemSize $child
                modified_at = Get-ItemModifiedAt $child
                duration_seconds = Get-DurationSeconds $child
            }
        }
        if ($result.Count -ge 250) { break }
    }
    return $result
}

function Find-CandidateFolders([object]$FolderItem, [string]$RelativePath, [int]$Depth, [int]$MaxDepth) {
    $result = @()
    foreach ($child in @(Get-Children $FolderItem)) {
        if (-not $child.IsFolder) { continue }
        $childName = [string]$child.Name
        $childRelative = Join-Relative $RelativePath $childName
        if (Is-CandidateName $childName) {
            $result += [pscustomobject]@{ item = $child; relative_path = $childRelative; from_cached_directory = $false }
        }
        if ($Depth -lt $MaxDepth -and $childName -notmatch '^(Android|data|obb)$') {
            $result += @(Find-CandidateFolders $child $childRelative ($Depth + 1) $MaxDepth)
        }
    }
    return $result
}

function Resolve-RelativeItem([object]$Device, [string]$RelativePath) {
    $current = $Device
    foreach ($part in @($RelativePath -split '/')) {
        $next = $null
        foreach ($child in @(Get-Children $current)) {
            if ([string]$child.Name -ceq $part) {
                $next = $child
                break
            }
        }
        if ($null -eq $next) { return $null }
        $current = $next
    }
    return $current
}

function Get-CandidateRows([object]$Device, [object]$Payload) {
    $seen = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    $folders = @()
    foreach ($cached in @($Payload.cached_dirs)) {
        if ($null -eq $cached) { continue }
        if ($cached.device_key -eq $Device.Path -or $cached.device_name -eq $Device.Name) {
            $resolved = Resolve-RelativeItem $Device ([string]$cached.relative_path)
            if ($null -ne $resolved -and $resolved.IsFolder -and $seen.Add([string]$cached.relative_path)) {
                $folders += [pscustomobject]@{ item = $resolved; relative_path = [string]$cached.relative_path; from_cached_directory = $true }
            }
        }
    }
    foreach ($storage in @(Get-Children $Device)) {
        if (-not $storage.IsFolder) { continue }
        foreach ($found in @(Find-CandidateFolders $storage ([string]$storage.Name) 0 ([int]$Payload.search_depth))) {
            if ($seen.Add([string]$found.relative_path)) { $folders += $found }
        }
    }
    $rows = @()
    foreach ($found in $folders) {
        $rows += [pscustomobject]@{
            relative_path = $found.relative_path
            from_cached_directory = $found.from_cached_directory
            files = @(Get-AudioFiles $found.item $found.relative_path)
        }
    }
    return $rows
}

function Invoke-Probe([object]$Shell, [object]$Payload) {
    $devices = @()
    foreach ($device in @(Get-PortableDevices $Shell)) {
        $storageRoots = @()
        foreach ($child in @(Get-Children $device)) {
            if ($child.IsFolder) { $storageRoots += [string]$child.Name }
        }
        $devices += [pscustomobject]@{
            device_key = [string]$device.Path
            display_name = [string]$device.Name
            storage_roots = $storageRoots
            search_depth = [int]$Payload.search_depth
            candidates = @(Get-CandidateRows $device $Payload)
        }
    }
    return @{ ok = $true; observation = 'ok'; devices = $devices }
}

function Invoke-Copy([object]$Shell, [object]$Payload) {
    $source = $Payload.source
    $device = $null
    foreach ($candidate in @(Get-PortableDevices $Shell)) {
        if ($candidate.Path -eq $source.device_key -or $candidate.Name -eq $source.device_name) {
            $device = $candidate
            break
        }
    }
    if ($null -eq $device) { return @{ ok = $false; error = 'portable_device_not_present' } }
    $item = Resolve-RelativeItem $device ([string]$source.relative_path)
    if ($null -eq $item -or $item.IsFolder) { return @{ ok = $false; error = 'source_item_not_found' } }
    $destination = [string]$Payload.destination_dir
    New-Item -ItemType Directory -Path $destination -Force | Out-Null
    $destinationFolder = $Shell.Namespace($destination)
    if ($null -eq $destinationFolder) { return @{ ok = $false; error = 'destination_namespace_unavailable' } }
    $destinationFolder.CopyHere($item, 1044)
    $expected = [Int64]($source.size_bytes)
    $deadline = [datetime]::UtcNow.AddSeconds([int]$Payload.timeout_seconds)
    $stable = 0
    $lastLength = -1
    while ([datetime]::UtcNow -lt $deadline) {
        $copied = @(Get-ChildItem -LiteralPath $destination -File -ErrorAction SilentlyContinue)
        if ($copied.Count -eq 1) {
            $target = $copied[0].FullName
            $length = $copied[0].Length
            if (($expected -gt 0 -and $length -eq $expected) -or ($expected -eq 0 -and $length -eq $lastLength -and $length -gt 0)) {
                $stable += 1
                if ($stable -ge 2) { return @{ ok = $true; destination_path = $target } }
            } else {
                $stable = 0
            }
            $lastLength = $length
        }
        Start-Sleep -Milliseconds 500
    }
    return @{ ok = $false; error = 'copy_timeout_or_size_mismatch' }
}

try {
    $payload = Decode-Payload $InputJsonBase64
    $shell = New-Object -ComObject Shell.Application
    if ($payload.operation -eq 'probe') {
        Emit-Json (Invoke-Probe $shell $payload)
        exit 0
    }
    if ($payload.operation -eq 'copy') {
        Emit-Json (Invoke-Copy $shell $payload)
        exit 0
    }
    throw "unsupported_operation:$($payload.operation)"
} catch {
    Emit-Json @{ ok = $false; error = $_.Exception.Message }
    exit 1
}
