<#
  STM32H7B0 RTT 吞吐测试固件编译脚本（不需要 make，也不需要 Keil）

    pwsh -File build.ps1                # 默认（也是唯一）：HAL/SDK 版 → build\fw.elf，
                                        #   并复制一份到本目录 fw.elf（仓库给用户下载的那份）
    pwsh -File build.ps1 -Clean

  依赖：arm-none-eabi-gcc 在 PATH 里（本机在 E:\Share\env-windows\tools\gnu_gcc\arm_gcc\mingw\bin）

  只保留 **HAL/SDK 版**：时钟配置**原样用板子 demo 的 SystemClock_Config()**（ST 验证过，
  280MHz / VOS0），是最省心的正路。代价是要带一堆 HAL 源码（`sdk\`，51 个文件 / 4.3 MB）。

  ⚠️ 2026-10 用户定调：**早先那个"寄存器版"（`-Minimal` / `-SlowClock`）已删除**。
     它和 HAL 版长期双轨，两边各自漂过（寄存器版把 DIVM1 写成"值-1" → 实际 350MHz 却自报
     280MHz；`PWR_CR3` 偏移写成 0x08 → 读到 CR2），维护成本大于收益。要"只跑 HSI 64MHz 的
     保命档"就改 `sdk\Core\Src\main.c` 里的 `SystemClock_Config()`（或干脆用别的靶子目录），
     不再单独维护一份寄存器级实现。
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

$sdk = Join-Path $root 'sdk'
if (-not (Test-Path $sdk)){ throw "找不到 $sdk（HAL/SDK 版需要它；寄存器版已删除，见文件头说明）" }
$elf = Join-Path $build 'fw.elf'

# 参数一律加引号并用数组 splat：
# PowerShell 会把 -specs=nano.specs 按点号拆成两段（"-specs=nano" + ".specs"），
# 直接导致 "cannot read spec file 'nano'"（F103 那份脚本踩过，这里照抄修法）。
$cflags = @(
  '-mcpu=cortex-m7', '-mthumb', '-Os', '-g3',
  '-mfpu=fpv5-d16', '-mfloat-abi=hard',          # H7 的 FPU 是双精度；启动代码会打开它
  '-ffunction-sections', '-fdata-sections', '-fno-common',
  '-Wall', '-Wextra', '-Wno-unused-parameter',
  "-I$root\src", "-I$root\segger_rtt",
  "-T$root\ld\stm32h7b0.ld",
  '-nostartfiles', '-specs=nano.specs', '-specs=nosys.specs',
  '-Wl,--gc-sections', "-Wl,-Map=$build\fw.map",
  '-DUSE_HAL_DRIVER', '-DSTM32H7B0xx',
  "-I$sdk\Core\Inc",
  "-I$sdk\Drivers\STM32H7xx_HAL_Driver\Inc",
  "-I$sdk\Drivers\STM32H7xx_HAL_Driver\Inc\Legacy",
  "-I$sdk\Drivers\CMSIS\Device\ST\STM32H7xx\Include",
  "-I$sdk\Drivers\CMSIS\Include"
)

# ---- 源文件清单照抄 demo 的 keil 工程（去掉用不上的 USART/GPIO/BSP）----
$hal = Join-Path $sdk 'Drivers\STM32H7xx_HAL_Driver\Src'
$sources = @(
  (Join-Path $sdk 'Core\Src\main.c'),                     # ← 改过的：主循环换成 RTT 灌流
  (Join-Path $sdk 'Core\Src\stm32h7xx_it.c'),
  (Join-Path $sdk 'Core\Src\stm32h7xx_hal_msp.c'),
  (Join-Path $sdk 'Core\Src\system_stm32h7xx.c'),         # SystemInit / SystemCoreClock
  (Join-Path $root 'src\startup.c'),                      # 我们的 C 启动文件（向量表+SystemInit）
  (Join-Path $root 'segger_rtt\SEGGER_RTT.c')
)
foreach ($f in 'stm32h7xx_hal.c','stm32h7xx_hal_cortex.c','stm32h7xx_hal_rcc.c','stm32h7xx_hal_rcc_ex.c',
               'stm32h7xx_hal_pwr.c','stm32h7xx_hal_pwr_ex.c','stm32h7xx_hal_flash.c','stm32h7xx_hal_flash_ex.c',
               'stm32h7xx_hal_gpio.c','stm32h7xx_hal_exti.c'){
  $sources += (Join-Path $hal $f)
}

& $gcc @cflags @sources -o $elf
if ($LASTEXITCODE -ne 0) { throw "编译失败 (exit $LASTEXITCODE)" }

& $objcopy -O binary $elf (Join-Path $build 'fw.bin')
& $objcopy -O ihex   $elf (Join-Path $build 'fw.hex')
& $size $elf

Write-Output ""
Write-Output ("版本    ： HAL/SDK 版（时钟配置 = 板子 demo 的 SystemClock_Config() 原样）")
Write-Output ("时钟    ： HSE 25MHz→PLL1 280MHz（VOS0）")
Write-Output ("产物    ： {0}" -f $elf)
Write-Output ("          {0} ({1} B)" -f (Join-Path $build 'fw.bin'), (Get-Item (Join-Path $build 'fw.bin')).Length)

# 仓库约定：目录根上的 fw.elf 是"发货的那一份"（页面直接载入它取符号/DWARF）
$rootElf = Join-Path $root 'fw.elf'
Copy-Item $elf $rootElf -Force
Write-Output ("已复制给用户下载： {0}" -f $rootElf)
Write-Output ""
Write-Output "下一步："
Write-Output "  烧录（OpenOCD 兜底）： pwsh -File flash.ps1"
Write-Output "  零安装烧录：在网页「烧录器」里选 stm32h7b0 → 选本目录的 fw.elf → 烧录"
Write-Output "  测速：网页 RTT Viewer 连上后看「读取 KB/s」；或 node tools\selftest\rtt-speed.mjs"
