# HPM6800EVK 调试、RTT 转发和 J-Scope 探针交接

2026-11-07（北京时间）。在基线 `73e4b82` 上，用本地 8911 工作台和重新构建的 Web bundle 验收。硬件为 HPM6800EVK、akaLinkPro，调试使用 HPM SDK 1.11.0 原版 `hpm6800_all_in_one.cfg` 和 OpenOCD Wasm / WebUSB，JTAG 时钟 8 MHz。目标运行先前已校验的 `hpm6800evk_dbgstress/fw.elf`。 [机器证据及指纹](hpm6800-wasm-handoff-20261107.json) · [验收脚本](../../web/tools/selftest/dbg-hpm6800-handoff-hw.py)

| 流程 | 真机结果 |
|---|---|
| Wasm 调试 → RTT 转发 → Wasm 调试 | 调试保持连接时沿用会话目标，启动时自动交接。探针从目标 RAM 的合成 RTT 环搬运，COM33 收到并逐字节校验 396 B，探针读错为 0，目标读游标推进 396 B。重新连接 Wasm 后，确认探针端转发停止，HID 句柄已释放。 |
| Wasm 调试 → J-Scope → Wasm 调试 | 调试保持连接时沿用会话目标并复用 ELF。J-Scope 以 1 kHz 采样 `g_ticks`、`g_stage`，包序缺口与读取错误均为 0。再次连接 Wasm 后，确认采样停止且 WebUSB 数据端已释放。两轮停采分别收到 2,418 / 2,419 和 1,922 / 1,923 个样本。 |
| 空闲探针 → RTT 转发、空闲探针 → J-Scope | Wasm 已断开，两页分别从空闲探针独立启动。RTT 再次逐字节校验 396 B，J-Scope 仍以 1 kHz 实采，停采收到 1,922 / 1,923 个样本。测试后 Wasm 可再次连接。 |

第一轮轮询只读取缓存的转发状态，等不到搬运计数。验收脚本改为主动向探针刷新状态后，两次完整的来回交接均通过。RTT 载荷由脚本预填在目标 ILM，验收覆盖实际 JTAG 取数、游标回写和 USB CDC 输出。持续 RTT 产流及吞吐另行验证。临时 RAM 恢复后逐字节核对，目标继续运行，探针和硬件协调锁已释放。测试期间未复位或烧录。

网页的会话配置和探针占用由工作台统一管理。目标控制和低速目标访问以 OpenOCD Wasm 为迁移方向，高速搬运由探针执行。RTT 转发和 J-Scope 共用板型和 ELF 的配置入口，启动时各自使用探针固件的私有 HID 指令。RTT 的目标 RAM 结构使用 SEGGER RTT，探针到主机使用 USB CDC。J-Scope 使用 WebUSB Bulk `0x83` 和探针私有采样帧。两项功能均需探针固件支持板卡的调试链路与运行态 RAM 读取。RTT 需要目标固件提供 RTT 控制块，J-Scope 需要有效的变量地址。 [SEGGER RTT](https://kb.segger.com/RTT) · [USB CDC](https://www.usb.org/defined-class-codes) · [探针命令](../../firmware/application_5301/src/api/api_param.c) · [J-Scope 传输](../../web/app/scope/transport.js)

ISSUE-043 继续跟踪 HPM 停采计数差。ISSUE-058 记录本次配置与探针交接覆盖范围，其他板卡仍需验证。ISSUE-065 继续跟踪 HPM 的 Wasm 运行态物理内存访问及 RTT Viewer 后端迁移。 [问题跟踪列表](../项目问题跟踪列表.md)
