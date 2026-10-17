# 构建 STM32H743 480MHz（用 pwsh 7 运行）
$ErrorActionPreference = 'Stop'
$gcc = 'E:\Share\env-windows\tools\gnu_gcc\arm_gcc\mingw\bin\arm-none-eabi-gcc.exe'
Set-Location 'E:\web-etm-trace\firmware\stm32h743_cdc_trace'
& '.\build.ps1' -Gcc $gcc
exit $LASTEXITCODE
