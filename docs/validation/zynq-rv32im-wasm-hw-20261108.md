# Zynq RV32IM 的 OpenOCD Wasm 真机验收

2026-11-08 使用 `toolbox.py serve` 的本机源码工作台（8912）。源码基于 GitHub `main` 的 `11441e5`，含本地未提交改动。OpenOCD Wasm 为 `0.12.0+dev-g9f2d876-dirty`，akaLinkPro 固件为 2.1.2。8878 的 Windows 试用包未开放本次 CLI 所需的队列端点。

| 项目 | 真机结果 |
| --- | --- |
| 导入和连接 | `target import` 解析 `original_mcu.cpu`，Flash bank 为空。实板经 WebUSB、JTAG 1 MHz 识别 TAP `0x00000001`、XLEN 32 和 `misa=0x40001100`。 |
| ELF 写入及回读 | `debug_demo.elf` 写入 2,932 字节，`rtt.elf` 写入 2,165 字节。两份 ELF 的文件加载段均从 `0x80000000` 写入，逐字节回读一致。 |
| 调试 | 暂停、读 33 个寄存器、继续和复位并停均验证。载入 `debug_demo.elf` 解析出 43 个符号和 240 条 DWARF 行记录。两次指令单步使 PC 从 `0x80000000` 依次到 `0x80000004`、`0x80000008`。 |
| RTT Viewer | `target use rtt` 沿用相同 CFG。从 ELF 定位 `_SEGGER_RTT` 到 `0x80000878`。目标运行时导出 65,408 字节，包含完整启动横幅和序号 `00000000` 到 `000003fc` 的 1,021 条完整记录。采集状态无丢失和损坏计数。 |
| 恢复 | 回写原有 128 KiB BRAM，逐字节校验后从 `0x80000000` 重启并确认 CPU 运行。释放 WebUSB 后，探针模式从临时 JTAG 配置恢复到测试前的 SWD + VCOM，读回一致。 |

配置使用 `cmsis-dap`、`usb_bulk`、接口 0、`jtag`、1 MHz、`reset_config none`、IR 长度 5、预期 TAP ID `0x00000001`，并指定 RISC-V 的 `sysbus` 内存访问。初次连接探针处于 SWD + VCOM，返回 `JTAG not supported`。在当前浏览器仅有 WebUSB 授权时，通过 MCU 项目的探针 CLI 暂时切入 JTAG，再从工作台连接。

调试页在本次 CLI 会话中始终显示未选择镜像目录，`dump_image` 因此未执行。备用路径在主机解析 ELF 的 `PT_LOAD` 段，经 `toolbox.py page dbg invoke memWrite --target session` 写入，再用 `memRead` 校验。CPU 与内存操作均由浏览器里的 OpenOCD Wasm 执行。内置 `load_image` 和一键 RAM 下载未完成验收。专用烧录页拒绝沿用无 Flash bank 的 CFG，未执行 Flash 操作。

首次 `reset halt` 报 `CMD_DAP_JTAG_SEQ failed`，后续 RTT 下载和独立调试复验中的复位均成功，原因待复核。调试器的 `b main` 发出硬件断点请求，在本目标上返回 `can't add breakpoint`，当前实测通过的是指令单步。

分块回读在保留 200 个队列任务结果后遇到 HTTP 429。确认此前的写入任务均已完成，使用 `toolbox.py ack` 回收已保存的任务，再用 `toolbox.py batch` 完成整片回读。原 BRAM 镜像和实机 CLI 日志保存在 MCU 项目的本机测试目录中。RTT 原始导出 SHA-256 为 `d58437b6cf267a49ebf66570ac7e83384ec31c90f62695848c69d45c94c1155c`。

8878 的 Windows `web.7-rc.1` 打包版随后通过调试页内置 `dump_image` 两次读出 128 KiB BRAM，耗时 1.4481 秒和 1.4347 秒。名为 `debug_demo-8878-readback.bin` 的 2,932 字节回读与下载前备份的同一段逐字节相同，与 `debug_demo.elf` 的文件加载段不同。此次缺少对应的 `load_image` 执行日志，内置下载仍待复验。验收时应在同一标签页依次保留写入完成日志、读回和 ELF 逐字节比较结果。

同版页面已解析 `nano.elf` 中的 `rt_schedule` 到 `0x80003cc8`，随后硬件断点请求被拒绝。RV32 会话默认显示 8 个硬件断点槽，未按目标能力显示。原生 OpenOCD + GDB 已在同一软核上命中 `debug_demo` 的两个 BRAM 软件断点。Web 验收应覆盖软件断点命中、继续跨过断点、清除及断开时恢复原指令。通用 CFG 的一键 RAM 调试入口也需要使用明确的可写地址范围，并在读回校验后停在入口或 `main`。
