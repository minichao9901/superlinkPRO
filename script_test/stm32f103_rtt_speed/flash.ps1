<#
  用 OpenOCD + CMSIS-DAP（akaLinkPro 探针）把测试固件烧进 STM32F103

    pwsh -File flash.ps1                 # 默认烧 CB 那套（build-cb/fw.elf）
    pwsh -File flash.ps1 -Board ze       # 烧 ZET6 那套（build-ze/fw.elf）
    pwsh -File flash.ps1 -Board ze -Erase

  接线：探针 J5.9(PA06)→SWCLK、J5.7(PA07)→SWDIO、J5.3(PA08)→nRESET、GND 共地。

  用的是 sdk_env 自带的 OpenOCD（ESP-IDF 那份已随 ESP-IDF 卸载）+ 本仓库的
  script_test\openocd_stm32f1_swd.cfg（sdk_env 的 tcl 树**没有 target/ 目录**，
  所以 F1 的 target 定义写在这份 cfg 里）。

  🚨 路径必须转成正斜杠：OpenOCD 的命令走 Tcl 解析，Windows 反斜杠会被当转义吃掉
     （症状：couldn't open E:web-serial-rtt-tools<TAB>ools... —— \t 变 Tab、\b 变退格）
#>
param(
  [switch]$Erase,
  [ValidateSet('cb', 'c8', 'ze')][string]$Board = 'cb',
  [string]$OpenOcd,
  [string]$Scripts
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot                       # …\script_test\stm32f103_rtt_speed
$test = Split-Path -Parent $root            # …\script_test

$out = @{ cb = 'build-cb'; c8 = 'build-c8'; ze = 'build-ze' }[$Board]
$elf = Join-Path $root "$out\fw.elf"
if (-not (Test-Path $elf)) { throw "先跑 build.ps1 -Board $Board（找不到 $elf）" }

if (-not $OpenOcd) {
  $sdk = if ($env:HPM_SDK_ENV_DIR) { $env:HPM_SDK_ENV_DIR } else { 'E:\sdk_env_v1.11.0' }
  $cand = Join-Path $sdk 'tools\openocd\openocd.exe'
  if (Test-Path $cand) { $OpenOcd = $cand } else { throw "找不到 openocd.exe（用 -OpenOcd 指定）" }
}
if (-not $Scripts) { $Scripts = Join-Path (Split-Path -Parent $OpenOcd) 'tcl' }

$cfg = Join-Path $test 'openocd_stm32f1_swd.cfg'
if (-not (Test-Path $cfg)) { throw "找不到 $cfg" }

$cmds = @('init')
if ($Erase) { $cmds += 'reset halt'; $cmds += 'stm32f1x mass_erase 0' }
$elfTcl = $elf -replace '\\', '/'
$cmds += "program `"$elfTcl`" verify reset exit"

Write-Output "board   : $Board  ($out)"
Write-Output "openocd : $OpenOcd"
Write-Output "cfg     : $cfg"
# 🚨 PS 5.1 坑：EAP=Stop 时 `2>&1` 会把 native 程序写到 stderr 的**第一行**
#    （OpenOCD 的 banner 就走 stderr）变成终止错误，烧录还没开始就被打断 ——
#    手动用 pwsh 7 跑没有这个行为，回归脚本经 powershell 5.1 调用就踩到了。
#    这里临时降级，退出码照常判。
$ErrorActionPreference = 'Continue'
& $OpenOcd -s $Scripts -f $cfg -c ($cmds -join '; ') 2>&1 | ForEach-Object { "$_" }
$ErrorActionPreference = 'Stop'
if ($LASTEXITCODE -ne 0) { throw "烧录失败 (exit $LASTEXITCODE)" }
Write-Output "烧录完成"
