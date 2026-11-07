# OpenOCD 兼容范围与 CFG 目录验收

2026-11-07（北京时间）。工作台基于 `ee00aa3cce6ba02cdba4e12a360eba2dc0d34b3c` 加本地改动。参考脚本为 OpenOCD v0.12.0 的 `9ea7f3d647c8ecf6b0f1424002dfc3f4504a162c`。扫描了 886 个 CFG/Tcl 文件，对其中 685 个 board/target CFG 逐个尝试预检。 [上游版本](https://github.com/openocd-org/openocd/tree/v0.12.0/tcl)

| 上游入口 | 文件数 | 单文件解析通过 | 提供脚本目录后解析通过 |
|---|---:|---:|---:|
| board | 379 | 11 | 29 |
| target | 306 | 21 | 38 |

新增 35 个通过入口。目录模式通过的 67 个入口中，64 个解析为 ARM，3 个解析为 RISC-V。表中计数包含不同板卡指向同一芯片的情况，也包含超出 ARM/RISC-V 范围的入口。这个测试没有运行 Tcl 事件、接探针或写入 RAM，故只描述固定脚本树的静态预检结果。 [逐类失败及样例](openocd-coverage-20261107.json)

Chrome 从完整脚本目录实际导入 SAM4E 的 `board/atmel_sam4e_ek.cfg`，依次解析 `target/at91sam4sXX.cfg` 和 `target/at91sam4XXX.cfg`，得到 `SAM4E16E.cpu` 和 500 kHz SWD。合并包也验证了 SAM4E 和 STM3210E 的目录导入。RV32 用合成 board 和 target 文件，经目录导入、模拟 CMSIS-DAP/JTAG 连接、SBA 下载 RAM、设置入口和 F10，`a0` 从 0 变成 1、2，源码高亮 7 → 9 → 11。本轮仅进行软件与浏览器协议模拟验收。 [浏览器回归脚本](../../web/tools/selftest/dbg-cfg-page.test.mjs)

## 距离完整目标

| 必要能力 | 当前证据 | 尚需完成 |
|---|---|---|
| 用原版 CFG 还原目标会话 | 能解析有限条件、变量和本地静态依赖。语料中最多的失败类型为不支持的指令，另有条件、值和事件差异 | 支持 Jim Tcl 语义、配置与运行阶段、动态引用、过程及目标事件。需要对比上游行为，同时隔离宿主命令及 Flash 操作 |
| 通过实际探针访问目标 | 已有 CMSIS-DAP 的 Cortex-M/SWD 和单 TAP RV32/JTAG。通用 RV32 依赖 Debug Module 0.13 和 32 位 SBA | 覆盖 ARM/JTAG、多 TAP、多 AP/SWD multidrop、其它 ARM 内核、其它 RISC-V 调试模块及 progbuf/abstract 内存访问，并验证相应探针能力 |
| 把 ELF 安全放入可执行 RAM 后调试 | 现有 F103xE/nRF52832 一键入口使用芯片校验，通用 RV32 使用 `load_image` 和手动入口。F103ZE 与 HPM6800EVK 已有完整实板证据 | 为新目标确认内存窗口、初始化、入口/栈、指令缓存一致性、断点和步进。覆盖不同的 ELF 布局与入口约定 |

OpenOCD 支持 ARM7/ARM9、Cortex-A/R/M 和多种接口，脚本可以要求驱动、接线和板级初始化。部分接口依赖浏览器无法直接提供的宿主资源，例如并口或原始 TCP/Unix socket。完整验收应把目标兼容和探针通路分开，同一入口须同时具备可执行的配置、可访问的硬件、可验证的 RAM 下载和调试证据。 [OpenOCD 内核类型](https://openocd.org/doc/html/CPU-Configuration.html) · [适配器与传输](https://openocd.org/doc/html/Debug-Adapter-Configuration.html) · [配置与复位事件](https://openocd.org/doc/html/Config-File-Guidelines.html)

后续先验证复用上游配置和目标核心的浏览器原型。脚本和 ELF 可放入受控虚拟文件系统，探针适配层需要把同步调试调用映射到异步 WebUSB。先对照固定的复杂 CFG 和模拟探针，再验收新增目标实板。Wasm 移植的设备覆盖和 OpenOCD 行为一致性仍需验证。 [Emscripten 异步调用](https://emscripten.org/docs/porting/asyncify.html) · [虚拟文件系统](https://emscripten.org/docs/api_reference/Filesystem-API.html)

## 复现静态语料

```powershell
git clone --depth 1 --branch v0.12.0 https://github.com/openocd-org/openocd.git tmp/openocd-v012
node web/tools/selftest/dbg-cfg-corpus.mjs tmp/openocd-v012/tcl
```

浏览器模拟输入按 [前次复现步骤](dbg-cfg-e2e-20261107.md) 准备，再给 `dbg-cfg-page.test.mjs` 加第二个参数 `tmp/openocd-v012/tcl`。
