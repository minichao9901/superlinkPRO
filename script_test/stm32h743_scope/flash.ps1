<#
  用 OpenOCD + CMSIS-DAP 把 scope 测试固件烧进 STM32H743
    pwsh -File flash.ps1              # 烧录 + 校验 + 复位运行
    pwsh -File flash.ps1 -Erase       # 先整片擦除
    pwsh -File flash.ps1 -OpenOcd <path\to\openocd.exe> -Scripts <path\to\scripts>

  ⚠️ 本机那块阿波罗 H743 板子的 **flash 算法跑不起来**（写 flash 时
     "timed out while waiting for target halted"，SRST 也没接到探针）——
     见 ../stm32h743_rtt_speed/README.md。所以这条烧录路径**在本机并未跑通**，
     真要用请走 `build.ps1 -Ram` + 网页「烧录器」（AHB-AP 直接写 AXI SRAM 再指 SP/PC）。

  OpenOCD 自动查找顺序：
    1) E:\Share\env-windows\xpack-openocd-*\bin\openocd.exe     （xPack 0.12，scripts 在 <root>\openocd\scripts）
    2) %USERPROFILE%\.espressif\tools\openocd-esp32\*\...\bin\openocd.exe
    3) PATH 里的 openocd
  🚨 必须 0.12+ 并且显式 "cmsis-dap backend usb_bulk" —— CMSIS-DAP v2（bulk）只有 0.12 才认。
#>
param([switch]$Erase, [string]$OpenOcd, [string]$Scripts, [string]$Target = 'stm32h7x.cfg')

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$elf = Join-Path $root 'build\fw.elf'
if (-not (Test-Path $elf)){ throw "先跑 build.ps1（找不到 $elf）" }

function Find-OpenOcd {
  $cands = @()
  $cands += Get-ChildItem 'E:\Share\env-windows\xpack-openocd-*\bin\openocd.exe' -ErrorAction SilentlyContinue |
            Sort-Object FullName | ForEach-Object { $_.FullName }
  $cands += Get-ChildItem "$env:USERPROFILE\.espressif\tools\openocd-esp32\*\openocd-esp32\bin\openocd.exe" -ErrorAction SilentlyContinue |
            Sort-Object FullName | ForEach-Object { $_.FullName }
  $p = (Get-Command openocd -ErrorAction SilentlyContinue).Source
  if ($p){ $cands += $p }
  foreach ($c in $cands){
    $up2 = Split-Path -Parent (Split-Path -Parent $c)
    foreach ($s in @((Join-Path $up2 'openocd\scripts'), (Join-Path $up2 'share\openocd\scripts'))){
      if (Test-Path (Join-Path $s 'interface\cmsis-dap.cfg')){ return @{ exe = $c; scripts = $s } }
    }
  }
  throw '找不到带 cmsis-dap.cfg 的 OpenOCD（用 -OpenOcd / -Scripts 指定）'
}

if (-not $OpenOcd -or -not $Scripts){
  $f = Find-OpenOcd
  if (-not $OpenOcd){ $OpenOcd = $f.exe }
  if (-not $Scripts){ $Scripts = $f.scripts }
}

$cmds = @('init')
if ($Erase){ $cmds += 'reset halt'; $cmds += 'flash erase_sector 0 0 last' }
# 🚨 路径必须转成正斜杠：OpenOCD 的命令走 Tcl 解析，Windows 反斜杠会被当转义吃掉
$elfTcl = $elf -replace '\\', '/'
$cmds += "program `"$elfTcl`" verify reset exit"

Write-Output "openocd : $OpenOcd"
Write-Output "scripts : $Scripts"
Write-Output ("target  : {0}（H743 也吃 stm32h7x.cfg）" -f $Target)
Write-Output ("命令    : " + ($cmds -join '; '))
& $OpenOcd -s $Scripts -f "$Scripts\interface\cmsis-dap.cfg" -c "cmsis-dap backend usb_bulk" `
  -f (Join-Path $Scripts "target\$Target") -c ($cmds -join '; ') 2>&1 |
  ForEach-Object { $_ }
if ($LASTEXITCODE -ne 0){ throw "烧录失败 (exit $LASTEXITCODE)" }
Write-Output "烧录完成"
