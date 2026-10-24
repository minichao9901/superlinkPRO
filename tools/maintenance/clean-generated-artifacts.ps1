[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
param(
    [string]$Plan = (Join-Path $PSScriptRoot '../../docs/maintenance/storage-cleanup.plan.json'),
    [switch]$Apply
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '../..')).Path.TrimEnd('\', '/')
$planPath = (Resolve-Path -LiteralPath $Plan).Path
$planHash = (Get-Content -LiteralPath ($planPath + '.sha256') -Raw).Trim()
if ((Get-FileHash -LiteralPath $planPath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $planHash) {
    throw 'Plan fingerprint changed; regenerate and review the plan.'
}
$data = Get-Content -LiteralPath $planPath -Raw | ConvertFrom-Json
if ($data.summary.schema -ne 'repo-artifact-cleanup/1' -or
    [IO.Path]::GetFullPath($data.summary.root).TrimEnd('\', '/') -ne $repoRoot) {
    throw 'Plan belongs to a different workspace or schema.'
}
$repoHead = (& git -C $repoRoot rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $repoHead -ne $data.summary.revision) {
    throw 'Git revision changed; regenerate the cleanup plan before applying.'
}
$tracked = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
(& git -C $repoRoot -c core.quotepath=false ls-files) | ForEach-Object { [void]$tracked.Add($_) }
if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect tracked files.' }
$protectedPaths = @($data.summary.protectedPrefixes) + @($data.summary.protectedSyntheticSegments)
$checked = [Collections.Generic.List[object]]::new()
$checkedDirectories = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
$missing = 0
$visited = 0
foreach ($entry in $data.files) {
    $visited++
    if ($visited % 10000 -eq 0) { Write-Output "Preflight $visited / $($data.files.Count)" }
    $relative = [string]$entry[0]
    $parts = $relative -split '/'
    if ([IO.Path]::IsPathRooted($relative) -or $parts -contains '..' -or
        $parts -contains '.' -or $parts -contains '.git' -or $relative.Contains('\') -or
        $relative.Contains(':') -or $tracked.Contains($relative)) { throw "Unsafe or tracked target: $relative" }
    $allowed = $relative.StartsWith('tmp/') -or $relative.StartsWith('web/tmp/') -or
        $relative.StartsWith('web/validation/') -or $relative -match '(^|/)docs/validation/' -or
        $relative -match '^\.offline-reading-review-[\w-]+/' -or
        $relative -match '^etm/records/(_p4_|_r0_|_r1_|\.p4_test_cache/)'
    if (-not $allowed) { throw "Outside generated artifact scopes: $relative" }
    foreach ($prefix in $protectedPaths) {
        if ($relative -eq $prefix -or $relative.StartsWith($prefix + '/', [StringComparison]::OrdinalIgnoreCase)) {
            throw "Protected regression target: $relative"
        }
    }
    $target = [IO.Path]::GetFullPath((Join-Path $repoRoot $relative))
    if (-not $target.StartsWith($repoRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Target escapes workspace: $relative"
    }
    # Every ancestor is checked before any deletion. Directory links, nested
    # checkouts and external mounts must never redirect an allowed relative path.
    $ancestor = [IO.Path]::GetDirectoryName($target)
    while ($ancestor -ne $repoRoot -and -not $checkedDirectories.Contains($ancestor)) {
        if (Test-Path -LiteralPath $ancestor) {
            if ((Get-Item -LiteralPath $ancestor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "Linked ancestor: $ancestor"
            }
            if (Test-Path -LiteralPath (Join-Path $ancestor '.git')) { throw "Git checkout: $ancestor" }
        }
        [void]$checkedDirectories.Add($ancestor)
        $ancestor = [IO.Path]::GetDirectoryName($ancestor)
        if (-not $ancestor) { throw 'Invalid ancestor chain.' }
    }
    if (-not (Test-Path -LiteralPath $target)) { $missing++; continue }
    $item = Get-Item -LiteralPath $target -Force
    if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw "Expected an ordinary file: $relative"
    }
    # Python stores nanoseconds since epoch. On Windows file times have 100ns
    # precision; compare integer 100ns ticks rather than floating-point seconds.
    $ticks = [long]$item.LastWriteTimeUtc.ToFileTimeUtc() - 116444736000000000L
    $expectedTicks = [long]([System.Numerics.BigInteger]::Parse([string]$entry[2]) / 100)
    if ($item.Length -ne [long]$entry[1] -or $ticks -ne $expectedTicks) {
        throw "File changed after planning; no deletion started: $relative"
    }
    $checked.Add([pscustomobject]@{ Path = $target; Relative = $relative; Bytes = $item.Length; Ticks = $ticks })
}
$summary = [pscustomobject]@{
    Mode = $(if ($Apply) { 'Apply' } else { 'Preview; no deletion' })
    Files = $checked.Count
    ApparentGiB = [Math]::Round(($checked | Measure-Object -Property Bytes -Sum).Sum / 1GB, 3)
    MissingSincePlan = $missing
    Plan = $planPath
    PlanSHA256 = $planHash
}
$summary | Format-List
if (-not $Apply) { return }

$logPath = Join-Path ([IO.Path]::GetDirectoryName($planPath)) ('cleanup-applied-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.jsonl')
$writer = [IO.StreamWriter]::new($logPath, $false, [Text.UTF8Encoding]::new($false))
try {
    foreach ($entry in $checked) {
        $item = Get-Item -LiteralPath $entry.Path -Force
        $ticks = [long]$item.LastWriteTimeUtc.ToFileTimeUtc() - 116444736000000000L
        if ($item.Length -ne $entry.Bytes -or $ticks -ne $entry.Ticks) { throw "Concurrent modification: $($entry.Relative)" }
        if ($PSCmdlet.ShouldProcess($entry.Path, 'Delete reviewed generated artifact')) {
            # Explicit ordinary files only. No recursive directory deletion,
            # globs, git clean/reset, another shell or computed external paths.
            Remove-Item -LiteralPath $entry.Path -Force
            $writer.WriteLine((@{ path = $entry.Relative; bytes = $entry.Bytes } | ConvertTo-Json -Compress))
        }
    }
} finally { $writer.Dispose() }
Write-Output "Deletion log: $logPath"
