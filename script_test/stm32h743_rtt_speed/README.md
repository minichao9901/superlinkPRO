# RTT 吞吐测试固件（STM32H743 · 正点原子阿波罗 H743）

与 `stm32f103_rtt_speed` 完全同一套量法（死循环发 `"hello world!\n"`、RTT 用
`BLOCK_IF_FIFO_FULL`），用来回答「换更快的目标，RTT 交付率会不会更高、更稳」。

## 结论（2026-09-28 实测）

| SWD 档 | H743 交付率 | F103(96MHz) 交付率 | H743 错误计数 | F103 错误计数 |
| --- | --- | --- | --- | --- |
| 20 MHz | 1379.6 KB/s | 1391.9 KB/s | 全 0 | 全 0 |
| 36 MHz | 2162.3 KB/s | 2178.3 KB/s | 全 0 | 全 0（偶有 wr_err=1） |
| 45 MHz | 2483.5 KB/s | 2526.7 KB/s | 全 0 | 偶有 wr_err=1 |
| 60 MHz | **2933.6 KB/s** | **2953.7 KB/s** | **全 0** | 约 1/5 概率抖动 |

- **速度不会更快**：每个档位两边几乎一样，因为瓶颈在**探针侧**（SWD 链路 + USB/CDC），
  不在目标。H7 的 RTT 生产者能力远超这条链路，所以目标再快也吐不出来。
- **但明显更稳**：H743 在 20/36/45/60 MHz **全档 `rd_err=0 / wr_err=0 / rescan=0`**；
  F103 在 60 MHz 会偶发 `wr_err` / 重扫 / 抖动。
- **60 MHz 的偶发"流间隙"两台都一样**：H743 也会在长跑里偶尔出现 1 处间隙
  （实测 45 MHz 干净、60 MHz 约 1/几次出现）。**这说明该现象在探针侧 / RTT 协议本身，
  换目标治不好**，也印证了默认档取 45 MHz 的决定。
- 推论：**想提交付率只能改探针侧**（见 `docs/HPM5301EVKLite_port.md` §5.9）；
  只有当目标本身是瓶颈时（例如 F103 在 64/72 MHz），换更快的目标才有用。

## 两个 H7 特有的坑

1. **RTT 缓冲必须放 AXI SRAM(0x24000000)**：H7 的 ITCM/DTCM(0x20000000) 是内核私有
   总线，**外部调试器走 AHB-AP 读不到** —— 探针扫默认区间会什么都找不到。所以测速脚本
   用 `CMD_RTT` 的 START 显式指定 `0x24000000`（`rtt_h743_bridge.py` 已内置）。
2. **flash 算法在这块板子上跑不起来**（halt 通、AHB-AP 读写正常、flash 全 0xFF、
   `FLASH_OPTR` 的 RDP=0xAA 无保护，但写 flash 时 `timed out while waiting for target
   halted`；SRST 没接，connect-under-reset 也无效）。所以走 **RAM 运行**：
   `pwsh -File build.ps1 -Ram` 生成 `build/fw_ram.elf`，由 `rtt_h743_bridge.py` 用
   OpenOCD 的 AHB-AP 直接写进 AXI SRAM，再把 SP/PC 指过去 resume（不需要 flash 算法）。

## 用法

```powershell
pwsh -File build.ps1 -Ram                       # 编译 RAM 版
python ..\rtt_h743_bridge.py COM52 --sec 6      # 载入 + 逐档测速
```

> 复位后 H743 默认就跑 HSI 64 MHz，不配 PLL 也能测。要试更高主频（H7 可到 480 MHz），
> 可以用 OpenOCD 在 halted 状态下改 RCC（RCC_PLLCKSELR / PLL1DIVR / CFGR + VOS0 +
> FLASH_ACR 等待周期），与 F103 版「上位机改频」的做法一致 —— 但按上面的结论，
> **在探针成为瓶颈的前提下提目标主频不会改交付率**。
