<#
  STM32H743 测试固件编译脚本（**只出 flash 版**）

    pwsh -File build.ps1
    pwsh -File build.ps1 -Clean

  依赖：arm-none-eabi-gcc 在 PATH 里（本机在 E:\Share\env-windows\tools\gnu_gcc\arm_gcc\mingw\bin）

  ⚠️ 用户定调：**H743 走 flash 版，不做纯 RAM 运行版**（2026-10）。
     早先那个 `-Ram` 分支与 `ld/stm32h743_ram.ld` 已删除 —— 需要时从 git 历史里取。
#>
param([switch]$Clean)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$build = Join-Path $root 'build'

$gcc = (Get-Command arm-none-eabi-gcc -ErrorAction SilentlyContinue).Source
if (-not $gcc){
  $guess = 'E:\Share\env-windows\tools\gnu_gcc\arm_gcc\mingw\bin\arm-none-eabi-gcc.exe'
  if (Test-Path $guess){ $gcc = $guess } else { throw 'arm-none-eabi-gcc 不在 PATH 里' }
}
$bin = Split-Path -Parent $gcc
$objcopy = Join-Path $bin 'arm-none-eabi-objcopy.exe'
$size    = Join-Path $bin 'arm-none-eabi-size.exe'

if ($Clean -and (Test-Path $build)) { Remove-Item $build -Recurse -Force }
New-Item -ItemType Directory -Force -Path $build | Out-Null

$sources = @(
  (Join-Path $root 'src\main.c'),
  (Join-Path $root 'src\startup.c'),
  (Join-Path $root 'segger_rtt\SEGGER_RTT.c')
)
$elf = Join-Path $build 'fw.elf'
$ld  = Join-Path $root 'ld\stm32h743.ld'

# Cortex-M7 + 双精度硬浮点（H743 有 FPU）
$cflags = @(
  '-mcpu=cortex-m7', '-mthumb', '-mfpu=fpv5-d16', '-mfloat-abi=hard', '-Os', '-g3',
  '-ffunction-sections', '-fdata-sections', '-fno-common',
  '-Wall', '-Wextra', '-Wno-unused-parameter',
  "-I$root\src", "-I$root\segger_rtt",
  "-T$ld",
  '-nostartfiles', '-specs=nano.specs', '-specs=nosys.specs',
  '-Wl,--gc-sections', "-Wl,-Map=$build\fw.map"
)

& $gcc @cflags @sources -o $elf
if ($LASTEXITCODE -ne 0) { throw "编译失败 (exit $LASTEXITCODE)" }

$binout = Join-Path $build 'fw.bin'
& $objcopy -O binary $elf $binout
& $size $elf

# 📦 把 ELF 复制到目录根：仓库里"给用户直接下载"的那份就是它（与 hpm6800evk_* 同约定）。
#    改了源码重跑本脚本，这份会被覆盖 —— 别让入库的 ELF 和源码漂开（页面靠它取符号地址）。
$pub = Join-Path $root 'fw.elf'
Copy-Item -Force $elf $pub

Write-Output ""
Write-Output ("产物： {0}" -f $elf)
Write-Output ("       {0} ({1} B)" -f $binout, (Get-Item $binout).Length)
Write-Output ("入库： {0} ({1} KB)" -f $pub, [int]((Get-Item $pub).Length / 1024))
