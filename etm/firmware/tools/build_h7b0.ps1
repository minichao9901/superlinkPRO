# 构建 STM32H7B0 -CoreMHz 70（用 pwsh 7 运行）
$ErrorActionPreference = 'Stop'
$gcc = 'E:\Share\env-windows\tools\gnu_gcc\arm_gcc\mingw\bin\arm-none-eabi-gcc.exe'
Set-Location 'E:\web-etm-trace\firmware\stm32h7b0_cdc_trace'
& '.\build.ps1' -Gcc $gcc -CoreMHz 70
exit $LASTEXITCODE
