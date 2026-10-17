# 构建 F103ZE（用 pwsh 7 运行；Windows PowerShell 5.1 按 GBK 读 UTF-8 脚本会解析失败）
$ErrorActionPreference = 'Stop'
$gcc = 'E:\Share\env-windows\tools\gnu_gcc\arm_gcc\mingw\bin\arm-none-eabi-gcc.exe'
Set-Location 'E:\web-etm-trace\firmware\stm32f103_cdc_trace'
& '.\build.ps1' -Gcc $gcc -Chip ze
exit $LASTEXITCODE
