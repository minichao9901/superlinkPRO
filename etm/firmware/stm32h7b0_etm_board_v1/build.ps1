# 构建 8 MHz H7B0 / 4 MHz 四位并行 ETM 固件，保留 DWARF 4。
param([string]$Gcc)
$ErrorActionPreference = 'Stop'
$traceFwRoot = $PSScriptRoot
$traceBuild = Join-Path $traceFwRoot 'build'
if (-not $Gcc) {
    $traceCompiler = Get-Command arm-none-eabi-gcc -ErrorAction SilentlyContinue
    if ($traceCompiler) { $Gcc = $traceCompiler.Source }
    else { $Gcc = 'E:\Share\env-windows\tools\gnu_gcc\arm_gcc\mingw\bin\arm-none-eabi-gcc.exe' }
}
if (-not (Test-Path -LiteralPath $Gcc)) { throw '找不到 arm-none-eabi-gcc，请用 -Gcc 指定。' }
$traceBin = Split-Path -Parent $Gcc
$traceSdk = Join-Path $traceFwRoot 'sdk'
$traceHal = Join-Path $traceSdk 'Drivers\STM32H7xx_HAL_Driver\Src'
New-Item -ItemType Directory -Path $traceBuild -Force | Out-Null
$traceElf = Join-Path $traceBuild 'fw.elf'
$traceSources = @(
    "$traceSdk\Core\Src\main.c", "$traceSdk\Core\Src\stm32h7xx_it.c",
    "$traceSdk\Core\Src\stm32h7xx_hal_msp.c", "$traceSdk\Core\Src\system_stm32h7xx.c",
    "$traceFwRoot\src\startup.c", "$traceFwRoot\src\trace.c"
)
foreach ($traceHalFile in @('stm32h7xx_hal.c','stm32h7xx_hal_cortex.c',
    'stm32h7xx_hal_rcc.c','stm32h7xx_hal_rcc_ex.c',
    'stm32h7xx_hal_pwr.c','stm32h7xx_hal_pwr_ex.c',
    'stm32h7xx_hal_flash.c','stm32h7xx_hal_flash_ex.c',
    'stm32h7xx_hal_gpio.c','stm32h7xx_hal_exti.c')) {
    $traceSources += Join-Path $traceHal $traceHalFile
}
$traceFlags = @(
    '-mcpu=cortex-m7','-mthumb','-mfpu=fpv5-d16','-mfloat-abi=hard',
    '-Og','-g3','-gdwarf-4','-fno-inline','-fno-optimize-sibling-calls',
    "-fmacro-prefix-map=$traceFwRoot=E:\web-etm-trace\firmware\stm32h7b0_etm",
    '-ffunction-sections','-fdata-sections','-fno-common',
    '-Wall','-Wextra','-Wno-unused-parameter',
    '-DUSE_HAL_DRIVER','-DSTM32H7B0xx',
    "-I$traceFwRoot\src", "-I$traceSdk\Core\Inc",
    "-I$traceSdk\Drivers\STM32H7xx_HAL_Driver\Inc",
    "-I$traceSdk\Drivers\STM32H7xx_HAL_Driver\Inc\Legacy",
    "-I$traceSdk\Drivers\CMSIS\Device\ST\STM32H7xx\Include",
    "-I$traceSdk\Drivers\CMSIS\Include",
    "-T$traceFwRoot\ld\stm32h7b0.ld", '-nostartfiles',
    '-specs=nano.specs','-specs=nosys.specs','-Wl,--gc-sections',
    "-Wl,-Map=$traceBuild\fw.map"
)
& $Gcc @traceFlags @traceSources -o $traceElf
if ($LASTEXITCODE -ne 0) { throw "编译失败：$LASTEXITCODE" }
foreach ($traceFormat in @(@('binary','bin'),@('ihex','hex'))) {
    & "$traceBin\arm-none-eabi-objcopy.exe" -O $traceFormat[0] $traceElf "$traceBuild\fw.$($traceFormat[1])"
    if ($LASTEXITCODE -ne 0) { throw '产物转换失败。' }
}
& "$traceBin\arm-none-eabi-size.exe" $traceElf
if ($LASTEXITCODE -ne 0) { throw '检查 ELF 大小失败。' }
& "$traceBin\arm-none-eabi-objdump.exe" -d -S $traceElf |
    Set-Content -LiteralPath "$traceBuild\fw.disasm.txt" -Encoding utf8
if ($LASTEXITCODE -ne 0) { throw '生成反汇编失败。' }
& "$traceBin\arm-none-eabi-nm.exe" -n -S $traceElf |
    Set-Content -LiteralPath "$traceBuild\fw.symbols.txt" -Encoding utf8
if ($LASTEXITCODE -ne 0) { throw '导出符号失败。' }
foreach ($traceExtension in @('elf','bin','hex')) {
    Copy-Item -LiteralPath "$traceBuild\fw.$traceExtension" -Destination "$traceFwRoot\fw.$traceExtension" -Force
}
Write-Output "已生成：$traceFwRoot\fw.elf / fw.bin / fw.hex"
Write-Output 'CPU = HSI/8 = 8 MHz；TRACECLK = 4 MHz；PE2 时钟，PE3~PE6 数据。'
Write-Output '本脚本只编译；烧录和实际波形验证需要目标板。'
