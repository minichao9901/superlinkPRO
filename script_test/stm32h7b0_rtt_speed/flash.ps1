<#
  用 OpenOCD + CMSIS-DAP 把 H7B0 测试固件烧进去（**兜底通道**；首选是网页里的零安装 WebUSB 烧录）
    pwsh -File flash.ps1                 # 烧录 + 校验 + 复位运行
    pwsh -File flash.ps1 -Erase          # 先整片擦除
    pwsh -File flash.ps1 -Cfg stm32h7x.cfg

  OpenOCD 选择顺序与 F103 那份一致：xpack 优先（0.12，认 CMSIS-DAP v2），其次 ESP-IDF 自带的，
  再其次 PATH 里的 —— 顺序有讲究：PATH 里那份 0.11 不认 v2，会直接失败。
#>
param([switch]$Erase, [string]$OpenOcd, [string]$Scripts, [string]$Cfg = 'stm32h7x.cfg')

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$elf = Join-Path $root 'build\fw.elf'
if (-not (Test-Path $elf)){ throw "先跑 build.ps1（找不到 $elf）" }

if (-not $OpenOcd){
  $cands = @()
  $cands += (Get-ChildItem 'E:\Share\env-windows\xpack-openocd-*\bin\openocd.exe' -ErrorAction SilentlyContinue | Sort-Object FullName -Descending | Select-Object -ExpandProperty FullName)
  $cands += (Get-ChildItem "$env:USERPROFILE\.espressif\tools\openocd-esp32\*\openocd-esp32\bin\openocd.exe" -ErrorAction SilentlyContinue | Sort-Object FullName -Descending | Select-Object -ExpandProperty FullName)
  $inPath = Get-Command openocd -ErrorAction SilentlyContinue
  if ($inPath){ $cands += $inPath.Source }
  $OpenOcd = $cands | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
  if (-not $OpenOcd){ throw '找不到 openocd.exe（用 -OpenOcd 指定）' }
}
if (-not $Scripts){
  $base = Split-Path -Parent $OpenOcd
  foreach ($c in @((Join-Path (Split-Path -Parent $base) 'openocd\scripts'),
                   (Join-Path (Split-Path -Parent $base) 'share\openocd\scripts'),
                   (Join-Path $base 'scripts'))){
    if (Test-Path (Join-Path $c 'interface\cmsis-dap.cfg')){ $Scripts = $c; break }
  }
  if (-not $Scripts){ throw '找不到 OpenOCD scripts 目录（用 -Scripts 指定）' }
}

$cmds = @('init')
if ($Erase){ $cmds += 'reset halt' ; $cmds += 'stm32h7x mass_erase 0' }
# 路径必须转成正斜杠：OpenOCD 的命令走 Tcl 解析，Windows 反斜杠会被当转义吃掉
$elfTcl = $elf -replace '\\', '/'
$cmds += "program `"$elfTcl`" verify reset exit"

Write-Output "openocd : $OpenOcd"
Write-Output "scripts : $Scripts"
Write-Output "target  : $Cfg"
Write-Output ("命令    : " + ($cmds -join '; '))

& $OpenOcd -s $Scripts -f "$Scripts\interface\cmsis-dap.cfg" -c "cmsis-dap backend usb_bulk" `
  -f "$Scripts\target\$Cfg" -c ($cmds -join '; ') 2>&1 | ForEach-Object { $_ }
if ($LASTEXITCODE -ne 0){ throw "烧录失败 (exit $LASTEXITCODE)" }

Write-Output ""
Write-Output "烧录完成。接下来："
Write-Output "  1) 网页 RTT Viewer → 后端 WebUSB → 连接探针（RAM 范围选 STM32H7B0 预设）"
Write-Output "  2) 看左下「读取 xxx KB/s」——那就是这块板子的 RTT 饱和吞吐"
Write-Output "  3) SWD 时钟建议先「自动」，再手工比 8/12/20MHz（H7 的 SWD 通常能跑更高）"
