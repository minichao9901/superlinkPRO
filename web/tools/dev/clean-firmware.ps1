<#
  只清理靶子固件的生成目录，保留其中已跟踪的 ELF。
  -List    只列出将处理的目录，不修改文件
  -WhatIf  按 PowerShell 语义预览删除

  保护范围：不会删除源码、任何已跟踪的 ELF、文档、tmp 或 bundle。
#>
param([switch]$List, [switch]$WhatIf)

$ErrorActionPreference = 'Stop'
$repo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$firmwareRoot = [IO.Path]::GetFullPath((Join-Path $repo 'tools/target-firmware'))
$prefix = $firmwareRoot.TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar

$targets = @(
  Get-ChildItem -LiteralPath $firmwareRoot -Directory -Force |
    ForEach-Object { Get-ChildItem -LiteralPath $_.FullName -Directory -Force -ErrorAction SilentlyContinue } |
    Where-Object { $_.Name -eq 'build' -or $_.Name -like 'build-*' }
)

if (-not $targets) {
  Write-Output '没有发现靶子固件生成目录。'
  exit 0
}

$trackedElfs = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
$trackedPaths = @(& git -C $repo ls-files -- '*.elf')
if ($LASTEXITCODE -ne 0) { throw '无法读取 Git 中已跟踪的 ELF 清单。' }
foreach ($path in $trackedPaths) {
  $full = [IO.Path]::GetFullPath((Join-Path $repo $path))
  if ($full.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
    [void]$trackedElfs.Add($full)
  }
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

foreach ($target in $targets) {
  $full = [IO.Path]::GetFullPath($target.FullName)
  if (-not $full.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw "拒绝处理越出固件目录的路径：$full"
  }
  $size = (Get-ChildItem -LiteralPath $full -Recurse -File -Force -ErrorAction SilentlyContinue | Measure-Object -Property Length -Sum).Sum
  Write-Output ("{0}  ({1:N0} B)" -f $full.Substring($repo.Length + 1), $size)
  $preserved = @($trackedElfs | Where-Object { $_.StartsWith($full + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase) })
  if ($preserved.Count) {
    foreach ($path in $preserved) { Write-Output ("保留入库 ELF：{0}" -f $path.Substring($repo.Length + 1)) }
  }
  if (-not $List) { Remove-GeneratedChildren $full }
}

if ($List) { Write-Output '仅列出，未修改文件。' }
elseif ($WhatIf) { Write-Output 'WhatIf 预览完成，未修改文件。' }
else { Write-Output ("已清理 {0} 个靶子固件生成目录。" -f $targets.Count) }
