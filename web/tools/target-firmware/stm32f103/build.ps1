<#
  STM32F103 测试固件编译脚本（不需要 make，也不需要 Keil）
    pwsh -File build.ps1
  依赖：arm-none-eabi-gcc 在 PATH 里（本机在 E:\Share\env-windows\tools\gnu_gcc\arm_gcc\mingw\bin）
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

# 参数一律加引号并用数组 splat：
# PowerShell 会把 -specs=nano.specs 按点号拆成两段（"-specs=nano" + ".specs"），
# 直接导致 "cannot read spec file 'nano'"。这是本脚本第一版踩的坑。
$cflags = @(
  '-mcpu=cortex-m3', '-mthumb', '-Os', '-g3',
  '-ffunction-sections', '-fdata-sections', '-fno-common',
  '-Wall', '-Wextra', '-Wno-unused-parameter',
  "-I$root\src", "-I$root\segger_rtt",
  "-T$root\ld\stm32f103c8.ld",
  '-nostartfiles', '-specs=nano.specs', '-specs=nosys.specs',
  '-Wl,--gc-sections', "-Wl,-Map=$build\fw.map"
)

& $gcc @cflags @sources -o $elf
if ($LASTEXITCODE -ne 0) { throw "编译失败 (exit $LASTEXITCODE)" }

& $objcopy -O binary $elf (Join-Path $build 'fw.bin')
& $objcopy -O ihex   $elf (Join-Path $build 'fw.hex')
& $size $elf

Write-Output ""
Write-Output ("产物： {0}" -f $elf)
Write-Output ("       {0} ({1} B)" -f (Join-Path $build 'fw.bin'), (Get-Item (Join-Path $build 'fw.bin')).Length)
