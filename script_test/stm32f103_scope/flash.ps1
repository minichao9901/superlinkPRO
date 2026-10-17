<#
  用 OpenOCD + CMSIS-DAP 把 scope 测试固件烧进 STM32F103
    pwsh -File flash.ps1              # 烧录 + 校验 + 复位运行
    pwsh -File flash.ps1 -Erase       # 先整片擦除
    pwsh -File flash.ps1 -OpenOcd <path\to\openocd.exe> -Scripts <path\to\scripts>

  OpenOCD 自动查找顺序（ESP-IDF 已卸载，所以不能只认它那一条路径）：
    1) E:\Share\env-windows\xpack-openocd-*\bin\openocd.exe     （xPack 0.12，scripts 在 <root>\openocd\scripts）
    2) %USERPROFILE%\.espressif\tools\openocd-esp32\*\openocd-esp32\bin\openocd.exe
    3) PATH 里的 openocd
  🚨 必须 0.12+ 并且显式 "cmsis-dap backend usb_bulk" —— CMSIS-DAP v2（bulk）只有 0.12 才认。
#>
param([switch]$Erase, [string]$OpenOcd, [string]$Scripts,
      [ValidateSet('c8', 'ze')][string]$Board = 'ze')

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$BOARDS = @{ ze = 'build'; c8 = 'build-c8' }
$elf = Join-Path $root ((Join-Path $BOARDS[$Board] 'fw.elf'))
if (-not (Test-Path $elf)){ throw "先跑 build.ps1 -Board $Board（找不到 $elf）" }

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
if ($Erase){ $cmds += 'reset halt' ; $cmds += 'stm32f1x mass_erase 0' }
# 🚨 路径必须转成正斜杠：OpenOCD 的命令走 Tcl 解析，Windows 反斜杠会被当转义吃掉
#    （症状：couldn't open E:web-serial-rtt-tools<TAB>ools... —— \t 变 Tab、\b 变退格）
$elfTcl = $elf -replace '\\', '/'
$cmds += "program `"$elfTcl`" verify reset exit"

Write-Output "openocd : $OpenOcd"
Write-Output "scripts : $Scripts"
Write-Output ("命令    : " + ($cmds -join '; '))
# 🚨 PS 5.1 坑：EAP=Stop 时 `2>&1` 会把 native 程序写到 stderr 的**第一行**
#    （OpenOCD 的 banner 就走 stderr）变成终止错误（pwsh 7 无此行为）。
$ErrorActionPreference = 'Continue'
& $OpenOcd -s $Scripts -f "$Scripts\interface\cmsis-dap.cfg" -c "cmsis-dap backend usb_bulk" `
  -f "$Scripts\target\stm32f1x.cfg" -c ($cmds -join '; ') 2>&1 |
  ForEach-Object { "$_" }
$ErrorActionPreference = 'Stop'
if ($LASTEXITCODE -ne 0){ throw "烧录失败 (exit $LASTEXITCODE)" }
Write-Output "烧录完成"
