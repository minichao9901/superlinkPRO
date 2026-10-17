# STM32H743 · SEGGER RTT 吞吐（饱和速度）测试固件

与 `stm32f103_rtt_speed` / `stm32h7b0_rtt_speed` **完全同一套量法**（死循环发 `"hello world!\n"`、
RTT 用 `BLOCK_IF_FIFO_FULL`），用来回答「换更快的目标，RTT 交付率会不会更高、更稳」。

板子：正点原子阿波罗 H743（Cortex-M7，SW-DP IDCODE `0x6BA02477`，与 H7B0 同族）。

> 📦 本目录里的 **`fw.elf`（flash 版）是编好的产物** —— 直接在网页里载入/烧录即可，不必自己装
> 工具链。改了源码就重跑 `build.ps1`（它会把新产物覆盖回这一份）。

## 实测结论（2026-09-28）

| SWD 档 | H743 交付率 | F103(96MHz) 交付率 | H743 错误计数 | F103 错误计数 |
| --- | --- | --- | --- | --- |
| 20 MHz | 1379.6 KB/s | 1391.9 KB/s | 全 0 | 全 0 |
| 36 MHz | 2162.3 KB/s | 2178.3 KB/s | 全 0 | 全 0（偶有 wr_err=1） |
| 45 MHz | 2483.5 KB/s | 2526.7 KB/s | 全 0 | 偶有 wr_err=1 |
| 60 MHz | **2933.6 KB/s** | **2953.7 KB/s** | **全 0** | 约 1/5 概率抖动 |

- **速度不会更快**：每个档位两边几乎一样 —— 瓶颈在**探针侧**（SWD 链路 + USB/CDC），不在目标。
  H7 的 RTT 生产者能力远超这条链路，目标再快也吐不出来。
- **但明显更稳**：H743 在 20/36/45/60 MHz **全档 `rd_err=0 / wr_err=0 / rescan=0`**；
  F103 在 60 MHz 会偶发 `wr_err` / 重扫 / 抖动。
- **60 MHz 的偶发"流间隙"两台一样**（H743 长跑里也会偶尔出现 1 处）⇒ 该现象在探针侧 / RTT
  协议本身，**换目标治不好**，也印证了默认档取 45 MHz 的决定。
- 推论：**想提交付率只能改探针侧**；只有当目标自身是瓶颈时（例如 F103 跑 64/72 MHz），
  换更快的目标才有用。

## 两个 H7 特有的坑

1. **RTT 缓冲必须放 AXI SRAM(0x24000000)**：H7 的 ITCM/DTCM(0x20000000) 是内核私有总线，
   **外部调试器走 AHB-AP 读不到** —— 探针扫默认区间会什么都找不到（H7B0 那份固件也踩过，
   见 `../stm32h7b0_rtt_speed/RESULTS.md`）。本固件的链接脚本就是这么放的，`nm` 可验：
   `_SEGGER_RTT = 0x24000014`。
2. **只交 flash 版**（2026-10 用户定调：H743 走 flash，不做纯 RAM 运行版 —— 原来的 `-Ram` 分支
   与 `ld/stm32h743_ram.ld` 已删除，需要时从 git 历史里取）。早期在本机跑 `flash.ps1` 报过
   `timed out while waiting for target halted`（halt 通、AHB-AP 读写正常、flash 全 0xFF、
   `FLASH_OPTR` 的 RDP 无保护，但 SRST 没接到探针）—— 那是**当时那条连接/复位方式**的问题，
   不是固件的问题。写不进 flash 时按这几条查：SWD 的 nRESET 有没有接、烧录器用的复位方式
   （connect-under-reset）、读保护 RDP，或者改用板子自带的下载方式（BOOT 跳线 + 串口/USB DFU）。

## 编译 / 使用

```powershell
pwsh -File build.ps1          # → build/fw.elf（并覆盖目录根那份 fw.elf，给用户下载的就是它）
pwsh -File build.ps1 -Clean
```

零安装做法（本仓库的正路）：网页「RTT Viewer」→ 后端选探针 → 载入本目录的 `fw.elf`
（页面从 ELF 里取 `_SEGGER_RTT` 的地址，不用手填）；量速率用
`node tools\selftest\rtt-speed.mjs`（换固件用 `ELF=<路径>` 覆盖）。

> 复位后 H743 默认就跑 HSI 64 MHz，不配 PLL 也能测。要试更高主频（H7 可到 480 MHz），
> 可以在 halted 状态下改 RCC（`RCC_PLLCKSELR` / `PLL1DIVR` / `CFGR` + VOS0 + `FLASH_ACR`
> 等待周期），与 F103 版「上位机改频」的做法一致 —— 但按上面的结论，**在探针成为瓶颈的前提下
> 提目标主频不会改交付率**。
