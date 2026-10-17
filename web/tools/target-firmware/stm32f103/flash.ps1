<#
  用 OpenOCD + CMSIS-DAP 把测试固件烧进 STM32F103
    pwsh -File flash.ps1            # 烧录 + 校验 + 复位运行
    pwsh -File flash.ps1 -Erase     # 先整片擦除
  注意：要认 CMSIS-DAP v2（bulk）的 OpenOCD，并且显式 "cmsis-dap backend usb_bulk"。
        ⚠️ 2026-09-10 起本机的 ESP-IDF 已卸载，原先写死的
           %USERPROFILE%\.espressif\tools\openocd-esp32\... 已不存在 ——
           现在按「PATH → xpack → 常见目录」顺序自动找，找不到再用 -OpenOcd 指定。
#>
param([switch]$Erase, [string]$OpenOcd, [string]$Scripts)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$elf = Join-Path $root 'build\fw.elf'
if (-not (Test-Path $elf)){ throw "先跑 build.ps1（找不到 $elf）" }

if (-not $OpenOcd){
  $cands = @()
  # 🚨 顺序要紧：PATH 里那份 0.11（rtt-debugger-support-package）**不认 CMSIS-DAP v2**，
  #    实测烧录直接失败；xpack 0.12 才是这份脚本验证过的。所以 xpack 优先。
  $cands += (Get-ChildItem 'E:\Share\env-windows\xpack-openocd-*\bin\openocd.exe' -ErrorAction SilentlyContinue | Sort-Object FullName -Descending | Select-Object -ExpandProperty FullName)
  $cands += (Get-ChildItem "$env:USERPROFILE\.espressif\tools\openocd-esp32\*\openocd-esp32\bin\openocd.exe" -ErrorAction SilentlyContinue | Sort-Object FullName | Select-Object -ExpandProperty FullName)
  $inPath = Get-Command openocd -ErrorAction SilentlyContinue
  if ($inPath){ $cands += $inPath.Source }
  $cands += (Get-ChildItem 'C:\Program Files*\openocd*\bin\openocd.exe' -ErrorAction SilentlyContinue | Select-Object -ExpandProperty FullName)
  $OpenOcd = $cands | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
  if (-not $OpenOcd){ throw '找不到 openocd.exe（用 -OpenOcd 指定，或把 openocd 加进 PATH）' }
}
if (-not $Scripts){
  # 脚本目录：先看 openocd 同级/上层的 share\openocd\scripts，再看 bin\scripts
  $base = Split-Path -Parent $OpenOcd
  foreach ($c in @((Join-Path (Split-Path -Parent $base) 'openocd\scripts'),
                   (Join-Path (Split-Path -Parent $base) 'share\openocd\scripts'),
                   (Join-Path $base 'scripts'))){
    if (Test-Path (Join-Path $c 'interface\cmsis-dap.cfg')){ $Scripts = $c; break }
  }
  if (-not $Scripts){ throw "找不到 OpenOCD scripts 目录（用 -Scripts 指定）" }
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
& $OpenOcd -s $Scripts -f "$Scripts\interface\cmsis-dap.cfg" -c "cmsis-dap backend usb_bulk" `
  -f "$Scripts\target\stm32f1x.cfg" -c ($cmds -join '; ') 2>&1 |
  ForEach-Object { $_ }
if ($LASTEXITCODE -ne 0){ throw "烧录失败 (exit $LASTEXITCODE)" }
Write-Output "烧录完成"
