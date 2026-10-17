# STM32H7B0 低频 ETM 测试固件

直接烧录本目录的 `fw.elf` 或 `fw.hex`；`fw.bin` 的 Flash 起始地址为 `0x08000000`。
CPU 为 HSI/8 = 8 MHz，四位并行 TRACECLK 标称 4 MHz。PE2 为时钟、PE3~PE6 为 D0~D3。

本固件由现有板级 HAL/SDK 例程改造，运行固定函数调用、交替分支和 SysTick 中断，完整配置 Cortex-M7 ETMv4、funnel、ETF 和 TPIU。
诊断变量 `g_trace`、`g_error` 位于 AXI SRAM。保留 ELF 的 DWARF 4，可用于后续源码映射。

详细接线、采集命令、排障和验证范围见 [项目说明](../../README.md)。

```powershell
pwsh -NoProfile -File .\build.ps1
# 自行选择烧录工具；若使用 OpenOCD + CMSIS-DAP：
pwsh -NoProfile -File .\flash.ps1
```

已完成编译、ELF 静态检查及 2026-10-11 的真实 LA 采集：约 4.008 MHz 时钟，TPIU 解帧得到 ID 2 字节流。尚未进行完整 ETMv4 指令路径解码。
