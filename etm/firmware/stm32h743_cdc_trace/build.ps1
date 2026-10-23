# STM32H743 (阿波罗, HSE 25MHz) + CherryUSB CDC ACM + etm-trace 钩子 —— GCC 构建脚本
#
# 用法：
#   pwsh -NoProfile -File .\build.ps1                    # CPU 480MHz / TRACEPORTCK 480MHz(VCO 960/R=2)
#   pwsh -NoProfile -File .\build.ps1 -TraceR 4          # TRACEPORTCK 改 240MHz
#   pwsh -File .\build.ps1 -CoreMHz 60 -TraceR 8  # 推荐 CPU60 / TRACECLK30
#
# 产物：build\fw.elf / fw.bin / fw.hex，并在根目录放一份副本。
# 只编译，不烧录；固件**不配置 ETM**，trace 由外部 SWD 工具配置。
param(
    [ValidateSet('480', '240', '120', '100', '90', '80', '70', '60', '50', '45', '40', '35', '30', '25')][string]$CoreMHz = '480',
    [ValidateSet(0, 4, 5, 6, 8, 10, 12, 16, 20, 24, 32, 40, 48, 64, 80, 128)][int]$TraceR = 0,
    [ValidateSet('External', 'Internal')][string]$UsbSupply = 'External',
    # AHB/HCLK 分频（测试"AHB 太低 USB 不工作"）：HCLK = SYSCLK / AhbDiv
    [ValidateSet(0, 1, 2, 4, 8, 16)][int]$AhbDiv = 0,
    # TRDT 用哪个频率算：Hclk=按 AHB 查表（正确）；Core=沿用 SystemCoreClock（旧行为，A/B 用）
    [ValidateSet('Hclk', 'Core')][string]$Trdt = 'Hclk',
    # 0 保留一次 banner；验收时可显式开启周期负载，不改变常规固件默认行为。
    [ValidateRange(0, 60000)][int]$BannerEveryMs = 0,
    [string]$Gcc,
    [string]$SdkRoot,
    [string]$OutputDir,
    [switch]$NoPublish
)
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot '..\tools\BuildTools.psm1') -Force

# USB 供电接法（对齐 H7B0 工程）：
#   External = VDD33USB 由板上 3.3V 供给 → 只开电压检测器 USB33DEN，内部稳压器关。
#   Internal = 用片内 USB 稳压器 → HAL_PWREx_EnableUSBReg()。
$h7UsbReg = [int]($UsbSupply -eq 'Internal')

$h7Root = $PSScriptRoot
if (-not $Gcc) { $Gcc = (Get-Command arm-none-eabi-gcc).Source }
$h7Bin = Split-Path -Parent $Gcc
$h7Sdk = if ($SdkRoot) { [IO.Path]::GetFullPath($SdkRoot) } else { Join-Path $h7Root 'sdk' }
if (-not (Test-Path -LiteralPath (Join-Path $h7Sdk 'Drivers/CMSIS/Device/ST/STM32H7xx/Include/stm32h743xx.h'))) {
    throw "缺少 STM32H743 SDK；请准备 sdk 目录，或用 -SdkRoot 指定包含 Drivers/Core/RTT_TRACE_LOG 的目录：$h7Sdk"
}
$h7Build = if ($OutputDir) { [IO.Path]::GetFullPath($OutputDir) } else { Join-Path $h7Root 'build' }
New-Item -ItemType Directory -Path $h7Build -Force | Out-Null

# ---------------------------------------------------------------- 时钟档位
# 参考频率 25MHz / M=5 = 5MHz。
#   480 档：VCO=960MHz，P=2 → SYSCLK 480MHz；TRACECLK = VCO/R/2（R 由 -TraceR 选）。
#   同频档（-CoreMHz 35..90）：VCO = 8f，P=8，R=4，D1CPRE=/1 → CPU = TRACECLK = f。
$h7M = 5
if ($CoreMHz -in @('480', '240')) {
    $h7N = if ($CoreMHz -eq '480') { 192 } else { 96 }; $h7P = 2
    $h7R = if ($TraceR -ne 0) { $TraceR } else { 2 }
    $h7D1CPRE = 'RCC_SYSCLK_DIV1'; $h7HPRE = 'RCC_HCLK_DIV2'; $h7Lat = 'FLASH_LATENCY_4'
} else {
    $f = [int]$CoreMHz
    $vco = 8 * $f
    $h7N = $vco / 5
    $h7P = 8
    # 不给 -TraceR 就是同频档（CPU == TRACECLK）；给了就用它当 R，得到 trace = 4f/R
    $h7R = if ($TraceR -ne 0) { $TraceR } else { 4 }
    $h7D1CPRE = 'RCC_SYSCLK_DIV1'; $h7HPRE = 'RCC_HCLK_DIV1'; $h7Lat = 'FLASH_LATENCY_2'
    if ($h7N -ne [math]::Floor($h7N)) { throw "同频档 $CoreMHz MHz 的 N=$h7N 不是整数" }
    if ($vco -lt 192 -or $vco -gt 960) { throw "同频档 $CoreMHz MHz 的 VCO=$vco MHz 超出 192..960" }
}
# 物理 TRACECLK = VCO / R / 2
$h7TraceMHz = (25000000 / $h7M) * $h7N / $h7R / 2 / 1000000
# AHB 分频覆盖（默认取档位自带的 HPRE）
if ($AhbDiv -gt 0) { $h7HPRE = "RCC_HCLK_DIV$AhbDiv" }

$h7Flags = @(
    '-mcpu=cortex-m7', '-mthumb', '-mfpu=fpv5-d16', '-mfloat-abi=hard',
    '-Og', '-g3', '-gdwarf-4',
    '-fno-inline', '-fno-optimize-sibling-calls',
    '-fno-if-conversion', '-fno-if-conversion2',
    '-ffunction-sections', '-fdata-sections', '-fno-common',
    '-Wall', '-Wextra', '-Wno-unused-parameter', '-Wno-unused-function',
    '-DUSE_HAL_DRIVER', '-DSTM32H743xx', '-DHSE_VALUE=25000000',
    "-DH7_CDC_PLL_M=$h7M", "-DH7_CDC_PLL_N=$h7N", "-DH7_CDC_PLL_P=$h7P", "-DH7_CDC_PLL_R=$h7R",
    "-DH7_CDC_D1CPRE=$h7D1CPRE", "-DH7_CDC_HPRE=$h7HPRE", "-DH7_CDC_FLASH_LATENCY=$h7Lat",
    "-DH7_CDC_USB_INTERNAL_REGULATOR=$h7UsbReg",
    "-DH7_DWC2_TRDT_FROM_CORE=$([int]($Trdt -eq 'Core'))",
    "-DH7_CDC_BANNER_INTERVAL_MS=$BannerEveryMs",
    # SEGGER RTT 控制块固定到 .rtt_cb（链接脚本把它放在 DTCM 0x20002000）
    '-DRTT_CB_ATTR=__attribute__((section(".rtt_cb"),aligned(4)))',
    "-I$h7Root\src",
    "-I$h7Sdk\Core\Inc",
    "-I$h7Sdk\RTT_TRACE_LOG",
    "-I$h7Root\cherryusb\config",
    "-I$h7Root\cherryusb\core",
    "-I$h7Root\cherryusb\class\cdc",
    "-I$h7Root\cherryusb\common",
    "-I$h7Root\cherryusb\port\dwc2",
    "-I$h7Sdk\Drivers\STM32H7xx_HAL_Driver\Inc",
    "-I$h7Sdk\Drivers\STM32H7xx_HAL_Driver\Inc\Legacy",
    "-I$h7Sdk\Drivers\CMSIS\Device\ST\STM32H7xx\Include",
    "-I$h7Sdk\Drivers\CMSIS\Include"
)

# ---------------- 源文件清单 ----------------
$h7Sources = @(
    "$h7Root\src\main.c",
    "$h7Root\src\runtime.c",
    "$h7Root\src\trace_hooks.c",
    "$h7Root\src\boot_probe.c",
    "$h7Root\src\usb_dc_low_level.c",
    "$h7Sdk\Core\Src\system_stm32h7xx.c",
    "$h7Sdk\RTT_TRACE_LOG\SEGGER_RTT.c",
    "$h7Sdk\RTT_TRACE_LOG\SEGGER_RTT_printf.c",
    "$h7Root\cherryusb\core\usbd_core.c",
    "$h7Root\cherryusb\class\cdc\usbd_cdc_acm.c",
    "$h7Root\cherryusb\port\dwc2\usb_dc_dwc2.c",
    "$h7Root\cherryusb\port\dwc2\usb_glue_st_h7b0.c"
)

# HAL：只挑这个 demo 用得上的（PCD 是必需项 —— CherryUSB 的 glue 走 HAL_PCD_MspInit）
foreach ($h7Hal in @('hal', 'hal_cortex', 'hal_rcc', 'hal_rcc_ex', 'hal_pwr', 'hal_pwr_ex',
                     'hal_flash', 'hal_flash_ex', 'hal_gpio', 'hal_dma', 'hal_dma_ex',
                     'hal_exti', 'hal_pcd', 'hal_pcd_ex', 'hal_hsem', 'hal_mdma')) {
    $h7Sources += Join-Path $h7Sdk "Drivers\STM32H7xx_HAL_Driver\Src\stm32h7xx_$h7Hal.c"
}
# ll_usb.c 提供 USB_ReadInterrupts/USB_SetRxFiFo 等 HAL PCD 依赖的底层函数
$h7Sources += Join-Path $h7Sdk 'Drivers\STM32H7xx_HAL_Driver\Src\stm32h7xx_ll_usb.c'

$h7Startup = Join-Path $h7Sdk 'Drivers\CMSIS\Device\ST\STM32H7xx\Source\Templates\gcc\startup_stm32h743xx.s'

# ---------------- 编译 ----------------
$h7Objects = @()
foreach ($h7Src in $h7Sources) {
    if (-not (Test-Path $h7Src)) { throw "源文件不存在：$h7Src" }
    $h7Obj = Join-Path $h7Build ([IO.Path]::GetFileNameWithoutExtension($h7Src) + '.o')
    $r = Invoke-Tool -Exe $Gcc -Args (@($h7Flags) + @('-c', $h7Src, '-o', $h7Obj))
    if ($r.ExitCode -ne 0) { throw "编译失败：$h7Src" }
    $h7Objects += $h7Obj
}
$h7StartupObj = Join-Path $h7Build 'startup.o'
$r = Invoke-Tool -Exe $Gcc -Args (@($h7Flags) + @('-c', $h7Startup, '-o', $h7StartupObj))
if ($r.ExitCode -ne 0) { throw "编译失败：$h7Startup" }
$h7Objects += $h7StartupObj

# ---------------- 链接 ----------------
# 用 -nostartfiles：H7 的 ST 启动文件自己准备栈与向量，不需要 crt0；
# src/runtime.c 提供真实 _init/_fini，禁止将 _init 链接到零地址。
$h7Elf = Join-Path $h7Build 'fw.elf'
$r = Invoke-Tool -Exe $Gcc -Args (@($h7Flags) + @($h7Objects) + @("-T$h7Root\ld\stm32h743.ld", '-nostartfiles',
    '-specs=nano.specs', '-specs=nosys.specs', '-Wl,--gc-sections',
    "-Wl,-Map=$h7Build\fw.map", '-o', $h7Elf))
if ($r.ExitCode -ne 0) { throw '链接失败' }

# 先出稠密 .bin
$r = Invoke-Tool -Exe "$h7Bin\arm-none-eabi-objcopy.exe" -Args @('-O', 'binary', $h7Elf, "$h7Build\fw.bin")
if ($r.ExitCode -ne 0) { throw '转换 .bin 失败' }
# 🚨 .hex 必须由**稠密 .bin** 生成，不能直接 `-O ihex fw.elf`：
#    objcopy 2.36 的 ihex 输出会省掉整段 0x00（实测 0x08000298..0x29F 这 8 字节没有记录），
#    烧 hex 的人会得到"这 8 字节保持 0xFF"，网页逐字节比对 ELF 就报
#    「目标机器码与 ELF 不匹配：0x08000298（ELF 00 ≠ 目标 ff）」。见 VARIANTS.md。
$r = Invoke-Tool -Exe "$h7Bin\arm-none-eabi-objcopy.exe" -Args @('-I', 'binary', '-O', 'ihex',
    '--change-addresses', '0x08000000', "$h7Build\fw.bin", "$h7Build\fw.hex")
if ($r.ExitCode -ne 0) { throw '转换 .hex 失败' }

Write-Output (Invoke-Tool -Exe "$h7Bin\arm-none-eabi-size.exe" -Args @($h7Elf)).Stdout
(Invoke-Tool -Exe "$h7Bin\arm-none-eabi-nm.exe" -Args @('-n', '-S', $h7Elf)).Stdout | Set-Content -LiteralPath "$h7Build\fw.symbols.txt" -Encoding utf8
(Invoke-Tool -Exe "$h7Bin\arm-none-eabi-objdump.exe" -Args @('-d', '-S', $h7Elf)).Stdout | Set-Content -LiteralPath "$h7Build\fw.disasm.txt" -Encoding utf8
foreach ($h7Ext in $(if ($NoPublish) { @() } else { @('elf', 'bin', 'hex') })) {
    Copy-Item -LiteralPath "$h7Build\fw.$h7Ext" -Destination "$h7Root\fw.$h7Ext" -Force
}

Write-Output "构建完成：STM32H743 CPU ${CoreMHz}MHz / 物理 TRACECLK $h7TraceMHz MHz (M=$h7M N=$h7N P=$h7P R=$h7R) -> $h7Elf"
Write-Output "固件不开启 ETM；trace 由外部 SWD 工具配置。关键符号："
Select-String -Path "$h7Build\fw.symbols.txt" -Pattern ' g_trace$| _SEGGER_RTT$| g_boot_stage$| OTG_FS_IRQHandler' | ForEach-Object { $_.Line }
