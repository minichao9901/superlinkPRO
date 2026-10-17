<#
  Remove generated EVKLite build output while preserving ELF files tracked by Git.
  The checked-in probe ELF snapshots live under the build output directories so
  a clean must not delete the files users can load/flash without a toolchain.
#>
param([switch]$WhatIf)

$ErrorActionPreference = 'Stop'
$repo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$trackedElfs = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
$trackedPaths = @(& git -C $repo ls-files -- '*.elf')
if ($LASTEXITCODE -ne 0) { throw '无法读取 Git 中已跟踪的 ELF 清单。' }
foreach ($path in $trackedPaths) {
  [void]$trackedElfs.Add([IO.Path]::GetFullPath((Join-Path $repo $path)))
}

function Has-TrackedElfBelow([string]$Directory) {
  $prefix = [IO.Path]::GetFullPath($Directory) + [IO.Path]::DirectorySeparatorChar
  foreach ($tracked in $script:trackedElfs) {
    if ($tracked.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { return $true }
  }
  return $false
}

function Remove-GeneratedChildren([string]$Directory) {
  foreach ($item in @(Get-ChildItem -LiteralPath $Directory -Force -ErrorAction SilentlyContinue)) {
    $full = [IO.Path]::GetFullPath($item.FullName)
    if ($item.PSIsContainer) {
      if (Has-TrackedElfBelow $full) {
        Remove-GeneratedChildren $full
      } else {
        Remove-Item -LiteralPath $full -Recurse -Force -WhatIf:$WhatIf
      }
    } elseif (-not $script:trackedElfs.Contains($full)) {
      Remove-Item -LiteralPath $full -Force -WhatIf:$WhatIf
    }
  }
}

$targets = @(
  [IO.Path]::GetFullPath((Join-Path $repo 'firmware/application_5301/build_dfu_evklite')),
  [IO.Path]::GetFullPath((Join-Path $repo 'firmware/bootloader_dfu/build_xip_evklite'))
)
foreach ($target in $targets) {
  if (-not (Test-Path -LiteralPath $target -PathType Container)) { continue }
  Write-Output ("清理生成文件：{0}" -f $target.Substring($repo.Length + 1))
  Remove-GeneratedChildren $target
  foreach ($tracked in $trackedElfs) {
    if ($tracked.StartsWith($target + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
      Write-Output ("保留入库 ELF：{0}" -f $tracked.Substring($repo.Length + 1))
    }
  }
}
