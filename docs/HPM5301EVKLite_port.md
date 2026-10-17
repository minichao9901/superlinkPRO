# akaLinkPro 固件移植：HPM5301EVKLite

本文档说明如何把 akaLinkPro（基于 HPM5301 的高性能 CMSIS-DAP 调试器）固件
运行在 **HPM5301EVKLite** 官方开发板上。DAP 的目标侧输出全部走板上
**J5（20 针标准 JTAG 座）**，同一个 J5 座在固件未运行（DFU/ISP 模式）时
仍然是芯片自身的 JTAG 调试口——即这块板子**既能被调试，又能当调试器**。

- 原 akaLinkPro 板级（`boards/akaLinkPro/`）完整保留，随时可切回。
- 新增板级：`firmware/application_5301/boards/hpm5301evklite/`、
  `firmware/bootloader_dfu/boards/hpm5301evklite/`。

---

## 1. 引脚映射

### 1.1 DAP 目标侧（J5 20 针 JTAG 座）

| DAP 信号 | HPM5301 引脚 | J5 位置 | 说明 |
| --- | --- | --- | --- |
| SWCLK / TCK | **PA06** | J5.9 | FGPIO 位带输出 |
| SWDIO / TMS | **PA07** | J5.7 | 单脚双向（FGPIO 方向切换） |
| TDI | **PA05** | J5.5 | JTAG 模式输出 |
| TDO | **PA04** | J5.13 | JTAG 模式输入 |
| nRESET（目标复位） | **PA08** | J5.3（nTRST 位） | 直出低有效复位 |
| GND | — | J5.4/6/8/… | 逻辑地 |
| VTref | — | J5.1 | 板上 3.3V |
| nSRST | — | J5.15 | **接芯片自身 RESET_N，勿接目标复位**（见 §5） |

> 调试 SWD 目标最少接 3 根线：`J5.9 → SWCLK`、`J5.7 → SWDIO`、`GND`；
> 建议再加 `J5.3 → 目标 nRESET`。JTAG 模式四线全可用（TCK/TMS/TDI/TDO）。

### 1.2 板载资源

| 功能 | 引脚 | 板上位置 | 说明 |
| --- | --- | --- | --- |
| CDC 虚拟串口（UART2） | PB08 (TXD) / PB09 (RXD) | J3.5 / J3.3 | 板上丝印 I2C_SCL/I2C_SDA，只有 10 k 上拉；2026-09-30 从 UART3/PB15+PB14 迁来（那两根给了 SPI2 的 IO3/IO2） |
| USER KEY | PA03 | 板载按键 | 按下 = 高；**上电时按住进 ROM ISP** |
| 运行时长按 USER KEY | PA03 | — | 长按 1s → 复位进 DFU 升级模式 |
| 状态 LED | PA10 | 板载 LED2 | 低电平点亮；LED1/LED2 模式驱动同一颗灯 |
| USB0 OTG（DAP 上行口） | PA24/PA25 | J1 Type-C | USB 2.0 高速设备 |
| 调试串口 UART0 | PA00 / PA01 | J3.36 / J3.38 | console，与原板相同 |

### 1.3 与原 akaLinkPro 硬件的差异

| 项目 | akaLinkPro | HPM5301EVKLite |
| --- | --- | --- |
| SWDIO | 单脚双向（PA07） | PA07 单脚双向，无方向脚 |
| nRESET | PA26 经三极管反相驱动（固件极性取反） | PA08 直出（真低有效） |
| VCOM 串口 | PA08/PA09 与 TDI/TDO 复用（JTAG 模式下串口断开） | PB15/PB14（UART3）独立，JTAG 模式下串口照常 |
| 5V 电平转换 / VREF ADC 检测 / SEL 硬件识别 | 有（PB13/PB10/PB08/09） | 无（功能自动裁剪） |
| LED | PB11+PB12 两颗，高电平点亮 | PA10 一颗，低电平点亮 |
| DFU 按键 | 专用 DFU 键 | USER KEY 长按（运行时） |
| DAP 输出座 | 板载排针 | J5 JTAG 座 |

固件通过 `board.h` 中的特性宏适配（`BOARD_HAS_SWDIO_DIR`、
`BOARD_NRESET_ACTIVE_LOW`、`BOARD_UART2_SHARES_JTAG_PINS`、
`BOARD_HAS_VREF_ADC`、`BOARD_LED_ACTIVE_LOW`、`BOARD_SWD_BLOB_EVKLITE` 等），
两个板级共用同一套应用源码。

---

## 2. 构建

本机脚本默认使用 `E:\sdk_env_v1.11.0`（SDK 1.11.0 + rv32imac 工具链），
可用环境变量 `HPM_SDK_ENV_DIR` 覆盖：

```bat
:: Bootloader (flash_xip @0x80000000) -> build_xip_evklite\
firmware\bootloader_dfu\build_xip_evklite.bat

:: APP (DFU 布局 @0x80020000) -> build_dfu_evklite\   (生成 _pack.hex/_pack.bin)
firmware\application_5301\build_dfu_evklite.bat
```

> **SDK 1.11+ 兼容说明**：`flash_dfu` 构建类型已被移除。evklite 脚本改用
> `HPM_BUILD_TYPE=flash_xip` + 自定义链接脚本 `linker/flash_dfu_app.ld`，
> 并由 CMake 追加 `-DFLASH_XIP=0 -DFLASH_DFU=1`，得到与原 `flash_dfu`
> 完全一致的镜像布局（APP 头在 0x80020000，入口 0x80020100）。
> bootloader 的 DFU 闪存移植层同时实现了新旧两代 CherryUSB 钩子
> （`dfu_write_flash`/`dfu_leave` 与 `usbd_dfu_write`/`usbd_dfu_reset`），
> 新旧 SDK 均可编译。

SWD 位带引擎是预汇编机器码 blob（内嵌 GPIO 位号）。EVKLite 引脚
（SWCLK=PA06/SWDIO=PA07）的 blob 已生成于
`src/dap/SW_DP/swd_blob_evklite.h`；如需再生成：

```
cd firmware\application_5301\src\dap\SW_DP\swd_blob
python build.py --variant 60M --toolchain <riscv-gcc-bin> ^
    --define SWD_PIN_SWCLK_SHIFT=6 --define SWD_PIN_SWDIO_SHIFT=7
:: 其余变体：45M 36M 30M 20M SLOW
```

JTAG 位带汇编直接编译自 `JTAG_DP_GPIO_ASM_45M.S`，引脚由 CMake 传入
`-DJTAG_PIN_JTCK_SHIFT=6 -DJTAG_PIN_JTMS_SHIFT=7 -DJTAG_PIN_JTDI_SHIFT=5
-DJTAG_PIN_JTDO_SHIFT=4`（默认值为 akaLinkPro 映射）。

---

## 3. 烧录

### 3.1 首次烧录（空片）：J-Link → J5

用 J-Link 的 20 针 JTAG 排线接 EVKLite 的 J5（板载 FT2232 不参与）：

```bat
firmware\application_5301\flash_jlink_evklite_full.bat
```

一次会话内烧写 bootloader + APP 并复位运行。脚本内定位于
`JLink_V882`，其它版本可用 `set JLINK_EXE=<路径>` 覆盖。

### 3.2 日常升级

- **DFU**：`build_dfu_evklite.bat` → `program_evklite.bat`（dfu-util，
  需自行安装并加入 PATH），或 bootloader 模式下把 `akaLinkPro_App_pack.bin`
  拖入虚拟 U 盘。
- **J-Link**：`flash_jlink_evklite.bat`（只烧 APP，保留 bootloader）。
- **USB ISP（救砖）**：按住 USER KEY 复位（或上电）→ ROM ISP 模式 →
  用 [hpm_manufacturing_tool](https://github.com/hpmicro/hpm_manufacturing_tool)
  经 USB/UART0 下载。

### 3.3 DFU/升级模式的进入方式

| 方式 | 操作 |
| --- | --- |
| 运行时长按 USER KEY | 1s 后自动复位进 DFU（推荐） |
| dfu-util | 自动发 DFU_DETACH |
| HID 命令 | `CMD_ENTER_DFU (0xFF)` |
| 镜像校验失败 | APP 头 CRC 错误时自动停留 DFU |

> 注意：EVKLite 上**复位/上电时按住 USER KEY 会先进 ROM ISP**（芯片 BOOT
> 功能），轮不到 bootloader 的按键检测；因此按键进 DFU 只在固件运行时有效。

---

## 4. 自调试（调试 EVKLite 板子本身）

DAP 目标脚（PA04–PA08）就是芯片自身的 JTAG 引脚：

- **APP 运行时**：这些脚被固件重配为 GPIO（DAP 输出），芯片自身 JTAG 不可用。
- **DFU / bootloader / ISP 模式**：固件完全不碰 PA04–PA08，J5 保持芯片
  JTAG 功能——此时用 J-Link（JTAG）即可调试/恢复固件。
  这也是"固件写挂了"的标准恢复路径（配合 USB ISP 双保险）。

> EVKLite **没有板载 FT2232 调试器**（见 UG §3.5 注），调试只走 J5 的
> J-Link/外部调试器；板上的 "USB to UART" 器件默认不贴片。

---

## 5. 探针侧 RTT→CDC 桥（已合入 `main`）

普通 RTT 是**主机轮询**模型：每次取数要 3 个 host↔探针来回（读 WrOff/RdOff → 读环形
缓冲 → 写回 RdOff），上限 ~1.1 MB/s。这个分支把轮询下沉进探针固件（J-Link 式），
固件自己在主循环里搜控制块、搬环形缓冲、回写 RdOff，数据直接进 CDC 串口环。

| 路径 | 吞吐（目标 64 MHz，12 KB 环） |
| --- | --- |
| 目标停在复位默认 8 MHz（生产者在目标侧，此时封顶） | 277 KB/s |
| `make rtt-test`（OpenOCD rtt server） | 919 KB/s |
| `make rtt-max`（轮询在 OpenOCD 内） | 1140 KB/s |
| **探针侧桥 @20 MHz SWD** | 1420 KB/s |
| **探针侧桥 @36 MHz SWD** | **2190 KB/s（2.14 MB/s），全流校验零丢包零重包** |

### 5.1 SWD 访问用 ARM DAPLink 官方 `swd_host.c`

`firmware/application_5301/src/swd_host/`（`swd_host.c/.h`、`debug_cm.h`）来自
DAPLink 官方代码（经 [MicroLink](https://github.com/minichao9901) 同款 5301 工程转录），
**时序逻辑一字未改**，只裁掉与 SWD 访问无关的部分：

- 去掉 `target_config.h`（连带 `flash_blob.h`/`util.h` 依赖链）；
- 裁掉 `swd_flash_syscall_exec/verify_exec`（flash 算法调用）；
- 裁掉 `swd_set_target_state_hw/sw` 与 `swd_wait_until_halted`（目标状态机）。

`swd_host_port.c` 提供它需要的两个平台符号：`SWD_Transfer()` 按 RnW 分派到本工程
`SW_DP.c` 的 `SWD_Read()/SWD_Write()`（与 DAP 主机通路同一套按速度预编译的 bit-bang
blob），`swd_set_target_reset()` 用 `DAP_config.h` 的 `PIN_nRESET_OUT()`。

### 5.2 四个必须注意的坑（都踩过）

1. **握手必须在默认低速档、提速必须在 `swd_init_debug()` 之后**：
   `swd_init_debug()` 内部会再调一次 `swd_init()` → `DAP_Setup()`，把时钟档重置回默认
   并加载 Slow blob。顺序写反（先提速再握手）会在读 DP IDCODE 时直接失败（返回 -2）。
2. **RdOff 回写要幂等重试**：`swd_write_data()` 收尾那个 dummy RDBUFF 读失败时**也会
   返回 0**，但此时写其实已经落到目标上了。若据此判定"没写成功"就跳过数据交付 → 丢一
   段；若判定"写成功"后重读重发 → 重一段。做法：先把数据交付进 CDC 环，再写 RdOff；
   失败就记下目标值，下轮**先补写同一个绝对值**（RdOff 是绝对值，重写永远安全），
   补上再继续搬。实测 `wr_err=1` 时流依然完整。
3. **高频档不能从慢档直跳，要斜着爬**：默认档是 4 MHz 的 Slow blob，直接切到 60/80 MHz
   会在换挡后的头几次访问上失败；失败会在 AHB-AP 上留下 sticky 错误，**之后连降到
   20 MHz 都读不动**（看着像"这一档不能用"，其实是链路被闩死了）。做法：先切 20 MHz
   装好 20M blob、清一次 sticky 错误、再切目标档，然后在目标档读一次 DP IDCODE 热身；
   失败就清错重试，仍失败才认输交给降档阶梯。详见 §5.9。
4. **长块读在目标运行中会失败**：单次块读别超过 2 KB（配合官方 `swd_read_memory()`
   内部的 1 KB 页切分）；实测 64 B/128 B/…/4 KB 的吞吐曲线在 2 KB 处到顶（3476 KB/s），
   再大无收益。

### 5.3 与 DAP 主机通路的互斥

桥只在 **DAP 空闲 ≥20 ms** 时才轮询（`rtt_bridge_note_dap_activity()` 记录最近一次
DAP 命令），每次轮询有界（≤2048 B），所以正常调试会话最多多约 1 ms 抖动，不会撕裂
传输。桥运行时 `uartx_set_cdc_source(1)` 让 RTT 成为 CDC 环的**唯一生产者**（UART 侧
退出，遵守 `chry_ringbuffer` 的单生产者约定）。

启动/测速：

```powershell
python script_test\rtt_probe_bridge.py COM52 6 36000
python script_test\rtt_bridge_sweep.py COM52 --sec=5 --clk=45   # 扫时钟/块大小
```

### 5.4 调优实测：天花板在哪一侧（2026-09-27）

桥的 SWD 参数做成了运行时可配（HID `CMD_RTT` action 7），配一个纯 SWD 基准
（action 8/9：按桥同一条 swd_host 路径反复读目标 SRAM，只报字节数 + MCHTMR tick），
就能把「SWD 侧」和「USB/CDC 侧」分开量。

**纯 SWD 基准**（目标 SRAM、块 512 B、全程 0 错误）：

| SWD 设定 | 20 MHz | 30 MHz | 36 MHz | 45 MHz | 60 MHz | 80 MHz | 100 MHz |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 吞吐 | 1491 | 2081 | 2412 | 2844 | **3431** | 3431 | 3431 KB/s |

60 MHz 起 plateau —— 最快那个 blob 的循环到顶了，再高的设定值没有意义。
`DAP_Data.clock_delay` 覆盖（0 与档位原生值）实测**没有差别**：≥20 MHz 的 blob
本来就跑在循环极限上。

**块大小**（60 MHz）：64 B 2922 / 128 B 3231 / 256 B 3366 / 512 B 3434 /
1024 B 3473 / 2048 B 3476 / 4096 B 3476 KB/s —— 512 B 之后只剩 1.2%，
再大没有收益（每块的固定开销本来就只有 CSW/TAR/prime/RDBUFF 共 5 次传输）。

**端到端 vs 丢弃模式**（桥只搬不送 CDC）：36 MHz 下 2217 / 2207 KB/s，
两者相等 ⇒ **瓶颈在 SWD 侧而不是 USB/CDC**；45 MHz 成功那一次端到端到
**2218 KB/s**（零丢包），说明 2.2 MB/s 附近是这条链路当下的实际交付上限。

**`__inline__` 没有收益空间**：工程本来就是 `-O3`，`swd_read_block` /
`swd_transfer_retry` / `swd_read_word` 在符号表里已经**不存在**（全被 GCC 内联），
每个 32 位字只剩一次进 blob 的调用 —— 在一个字约 450 个 CPU 周期里占 4~8 周期。
想再快只能压线上时间（时钟/块），压不动指令数。

### 5.5 目标主频才是交付率的上限（2026-09-28 实测）

把测试固件从 8 MHz 一路顶上去，同一块探针、同一个桥：

| 目标 HCLK | RTT 生产者 | 桥端到端 | 谁在限制 |
| --- | --- | --- | --- |
| 8 MHz（复位默认 HSI） | 277 KB/s | — | 目标 |
| 64 MHz（HSI/2 ×16） | 2217 KB/s | 2218 KB/s | 目标（每次只搬 505 B，环不满） |
| 72 MHz（HSE ×9） | 2477 KB/s | 2477 KB/s | 目标（丢弃模式 = 端到端） |
| **96 MHz（HSE ×12，固件固化）** | ~3300 KB/s（外推） | **2954 KB/s** | **USB/CDC**（丢弃模式 3232 vs 端到端 2954，见 §5.8/§5.9） |
| 128 MHz（HSE ×16，需上位机切） | ~4400 KB/s（外推） | 2507~2637 KB/s | **链路**（每次 2048 B，环满） |

**生产者速率 ≈ 34.6 B/ms 每 MHz 主频**，三个点吻合到 1% 以内。所以：
**要更高的 RTT 交付率，先把目标主频提上去，而不是一味提 SWDCLK** ——
SWD 链路在 60 MHz 档就有 3431 KB/s，而 64/72 MHz 的目标只能吐出 2.2/2.5 MB/s。

两条实测硬限制：

- **144 MHz 在本板上做不到**：板上晶振实测 8.005 MHz，F103 的 PLL 倍频上限是 ×16
  ⇒ 物理上限 128 MHz。要 144 得换 9 MHz（×16）或 12 MHz（×12）晶振，或从 OSC_IN 灌外部时钟。
- **128 MHz 下 flash 喂不动核心**：(latency 最多 2) ≈ 23 ns < F103 flash 实测 ~40 ns。
  固件自己切到 128 MHz 会立刻硬故障（`HFSR=FORCED`、`CFSR=IACCVIOL|STKERR`）；
  128 MHz 只能由上位机在 halted 下切换，让固件从复位向量就以该频率启动。

### 5.6 四处链路健壮性修复（都是为了高频档能用）

1. **自愈降档误触发**：把「降一档」放在*每次重扫*上是错的 —— 目标运行中任何一次瞬态
   读失败都会永久降档，几次突发就从 45 MHz 掉到 20 MHz（表现为「同一档位逐轮随机失败」）。
   改成**只有重扫也失败**才降档。修完 36/45 MHz 立刻稳定。
2. **sticky 错误闩锁**：AHB-AP 的一次瞬态错误会在 CTRL/STAT 留下 STICKYERR，
   之后每次访问都直接 FAULT；重扫前用 `swd_clear_errors()`（DAPLink 的 DP_ABORT 写）清掉。
3. **控制块扫描遇到未映射内存就停**：搜索区间 64 KB 而 F103C8 只有 20 KB，
   继续扫只是每次启动白记几十次读错误。
4. **每次 SWD 传输加临界区**（`swd_host_port.c`）：bit-bang blob 是纯时序循环，
   被 ISR 打断会把采样点推后、读回错误 ACK。只在「目标运行 + CDC 持续搬运」（USB ISR
   频繁）时暴露：20 MHz（位周期 50 ns）扛得住，36 MHz（28 ns）开始随机失败。
   一个字约 1.1 µs，两个 CSR 操作开销 ~2%。


### 5.7 高频档「间歇性」的归因与自愈

同一条链路、同一轮测试里会出现「36 MHz 基准 2412 KB/s 全对，而启动扫描失败」，
45 MHz 反而成功 —— 不是单调的频率阈值，是短读/字节读的时序余量问题
（逻辑分析仪挂在 SWD 线上会明显恶化，见 §6 注意事项）。因此：

1. **启动时降档重试**：从请求档位往下走（36 → 30 → 20 → 10），用第一个能扫到
   控制块的档位。用户直接要 60 MHz 也能自动落到当下可用的最快档。
2. **运行中自愈**：连续读失败就降一档并重新初始化（只降不升，避免抖动）。
   请求档位与当前档位分开保存，每次启动从请求档重新开始。

配套把**控制块扫描**从 4 字节步进的小读改成 **512 B 重叠窗块读**（步进 503 B、
重叠 9 B）：小读每步要 11 次传输（2 字块读 + 2 次字节读），扫 64 KB 就是
18 万次传输，36 MHz 以上必然踩到临界；块读后传输数少一个数量级，扫描本身也快得多。

实测自愈效果（36 MHz 请求、链路当时偏差）：自动降档后 **1352.6 KB/s、6 秒
零丢包零重包、delta +0 字节**。

> 判定链路是否被外部负载影响：先拔掉逻辑分析仪再复测同一档位。

---

### 5.8 SWD 读速 vs RTT 交付率（逐档对照表）

同一目标主频下（固件固化的 **96 MHz**，生产者上限 ≈ 3322 KB/s）逐档实测，
一条命令复现：`python script_test/rtt_rate_matrix.py COM52 --sec=5`。

| SWD 档位 | SWD 读（纯 SRAM 基准） | RTT 交付 | 交付/读 | 每次搬运 | 谁主导 |
| --- | --- | --- | --- | --- | --- |
| 20 MHz | 1473 KB/s | 1389 KB/s | **94%** | 2048 B | SWD 链路 |
| 30 MHz | 2047 KB/s | 1900 KB/s | **93%** | 2048 B | SWD 链路 |
| 36 MHz | 2359 KB/s | 2178 KB/s | **92%** | 2048 B | SWD 链路 |
| **45 MHz（当前默认）** | **2788 KB/s** | **2527 KB/s** | **91%** | 2048 B | SWD 链路 |
| 60 MHz（可选，见下） | 3312 KB/s | **2954 KB/s** | **89%** | 2048 B | **USB/CDC** |
| 80 MHz | 3312 KB/s | 2957 KB/s | **89%** | 2048 B | 同上（≥60 用同一个 blob） |

怎么读这张表：

- **交付/读 = 89~94%**：桥把 SWD 链路的可用带宽吃掉了九成上下，剩下 10% 左右是每轮的
  描述符读（24 B）、RdOff 回写（4 B）和 USB/CDC 那一侧的固定开销。**再优化桥的 SWD 搬运
  最多只能多拿这 10%。**
- **「谁主导」是按实测判的，不是推算**：每次搬运都顶到 2048 B 上限（`RTT_MAX_DRAIN`）
  且环从不空 ⇒ 目标的 RTT 环形缓冲**一直是满的**，即目标想给更多、链路搬不过来 ⇒
  在目标 96 MHz 下是**链路/CDC 侧主导**。反过来，如果每次只搬几百字节且环既不空也不满
  ⇒ 目标生产者主导（目标 64/72 MHz 时就是这个状态：每次 505 B、交付 2218/2477 KB/s）。
- 因此在这块板子上：**目标 ≤72 MHz 时交付率 = 生产者速率（34.6 B/ms/MHz）**；
  **目标 ≥96 MHz 时生产者不再限制**，交付率由链路与 USB/CDC 两者中更低的一方决定 ——
  60 MHz 档两者分别是 3312 与 ~2960，所以**现在的瓶颈是 USB/CDC（~2.96 MB/s）**，
  继续提 SWD 时钟（60→80）交付不再变化，正好印证这一点。

> ⚠️ **量交付率时主机侧的读法会直接影响结果**：Windows 上 pyserial 的
> `ser.read(n)` 每次都要新分配缓冲，实测把主机侧压到 **2169 KB/s**；改用
> `ser.readinto(大缓冲)` 能到 **2956 KB/s（差 36%）**。所有脚本已改用 readinto ——
> 用 `read()` 测会把「主机读得慢」误判成「设备只能跑这么快」。

※ 60/80 MHz 档原先**无法做完整初始化**（换挡后读直接 `err=-2`），现已修好，见 §5.9 #1。

（生产者的 34.6 B/ms/MHz 由 8/64/72 MHz 三个点实测拟合，见 §5.5。）

### 5.9 提速实践与后续方向

**已解决（2026-09-28）：60/80 MHz 档的「换挡瞬态」** —— 60 MHz 现在能跑，交付
**2954 KB/s（比 45 MHz 的 2527 高 17%）**；但**长跑不够稳**，所以默认仍用 45 MHz。

- 现象：低档初始化后切上去，60/80 MHz 稳态能跑 3312 KB/s；但做一次**完整初始化**
  （`swd_init()` + `swd_init_debug()` + 直接切到 60/80）后读直接 `err=-2`，
  而且**之后连降到 20 MHz 都读不动** ⇒ 说明失败的那次访问在 AHB-AP 上留下了 sticky
  错误（`STICKYERR` 等）把链路闩死了，并不是时钟本身不行。
- 修法（三处，都在 `rtt_bridge.c` / `rtt_swd_init()`）：
  1. **斜坡换挡**：默认档是 4 MHz 的 Slow blob，直跳 60/80 MHz 会在换挡后的头几次访问
     上失败；改成先切 20 MHz（装上 20M blob）、清一次 sticky 错误、再切目标档 ——
     即显式复现那条已验证能工作的「低档初始化 → 切上去」路径。
  2. **换挡后热身**：切完档先读一次 DP IDCODE 把瞬态吃掉；失败就清 sticky 错误再试一次，
     仍失败返回 **-4**（= 该档不可用），与握手失败的 -1/-2 区分开，让降档阶梯接手。
  3. **失败先清错再判死**：控制块扫描与纯 SWD 基准在读失败时，先 `swd_clear_errors()`
     重试同一段/同一轮，只有重试也失败才认定走出目标 SRAM / 该档不可用。
- 修完之后 60 MHz 的**稳态**是干净的（3 次 x10 秒实测 2930~2952 KB/s、`rd_err=0`），
  但更长窗口里约每 5~10 次 10 秒会出现一次链路抖动（`rd_err≈10 / wr_err≈4 / 2 次重扫`，
  并伴随约 1400 字节的流异常）。**既然要求"全程零丢包"，默认档保持 45 MHz**
  （同条件 3/3 次 0 错误）；60 MHz 可以用 HID `CMD_RTT` action 7 切过去做极限实验，
  此时下面的自动降档会兜底。
- **自动降档兜底**：累计 2 次「读失败触发的重扫」就自动降一档（`RTT_RESCANS_PER_STEP_DOWN`），
  所以强行用 60 MHz 时链路一抖就会自己退回稳的档，而不是一直丢数据。
- 降档阶梯扩到 `60→45→36→30→20→10`。
- 另外去掉 CDC IN 在「长度是 512 整数倍」时补发的 **ZLP**：CDC 是字节流不需要它，
  而桥按 2048 B 写环、线性窗经常正好是 512 倍数 ⇒ 几乎每次传输白多一个事务（+1.3%）。

**下一步方向（按性价比排序）**：

| # | 方向 | 预期收益 | 依据 / 做法 |
| --- | --- | --- | --- |
| **1** | **USB/CDC 侧（现在是瓶颈）** | 交付 2954 → **~3200 KB/s（+8%）** | 丢弃模式（不送 CDC）已能跑 3232 KB/s，而端到端 2954 ⇒ 差的 9% 全在 CDC/USB。可试：①去掉主循环 kick 依赖，让 IN 链路完全由完成回调驱动（环里有数据就续传，不必等主循环）；②一次 kick 连发多包/双缓冲；③把 32 KB 环加大到 64 KB 吸收突发 |
| **2** | **桥自身砍掉 ~10% 固定开销** | +6~10%（2950 → ~3200 KB/s） | 每轮固定成本 = 24 B 描述符读（约 9 次传输）+ RdOff 4 B 写（4 次）+ 主循环调度。做法：①只读 `{WrOff, RdOff}` 8 B（`buf`/`size` 不变，可缓存）；②RdOff 每 4 KB 批量回写一次，短访问减半；③把描述符读并进数据块读 |
| 3 | 为更高档重标定专用 blob | 链路 3312 → 3476 KB/s（+5%） | 60/80 MHz 现在都用同一个 60M blob（3312），而低档初始化后切到 80 MHz 曾测得 3476。重标定 turnaround/采样点能吃满这 5%，但要先解决 1（否则交付不变） |
| 4 | 目标侧：热代码搬进 SRAM | 生产者 3.3 → 4.4 MB/s（128 MHz） | 128 MHz 下 flash 喂不动核心（§5.5）。20 KB SRAM 装得下 12.6 KB RTT 缓冲 + 1 KB 代码。对**真实应用**而言，这一项决定的是「应用自己吐得多快」的上限 |
| 5 | 目标侧：减小 `SEGGER_RTT_Write` 调用开销 | 视应用 | 测试固件是 13 字节/次的死循环，每次调用固定开销占比很高；真实应用应按块写、少调用 |

**建议顺序**：**1**（+8%，已定位到具体代码）→ **2**（+6~10%，与 1 部分重叠，都在同一条链路上）
→ **3**（+5%，要改 blob 源码）→ **4/5** 取决于测的是「探针的上限」还是「真实应用的 RTT 上限」。

> 理论天花板（不改 blob、不换传输方式）：链路 3312 KB/s × 桥效率 89% ≈
> **2.96 MB/s（已经达到）**；把 USB/CDC 那 8% 与桥的固定开销补上可到 ~3.2 MB/s；
> 再往上必须动 blob（3）或换传输形态。

---

## 6. 注意事项

1. **J5.15（nSRST）是 EVKLite 自己的复位输入**（芯片 RESET_N），固件无法
   驱动它。用 20 针排线直连目标板的 JTAG 座时，不要让目标板的复位网络
   反灌 J5.15；SWD 目标建议直接杜邦线取 §1.1 的信号。
2. J5 的 PA04–PA08 与 DAP 固件共享：DAP 固件运行时不要同时接 J-Link
   连接，否则会争用引脚。
3. DAP 的 nRESET（PA08/J5.3）在 DFU 模式下呈现为芯片 nTRST 输入（高阻带上
   拉），外接目标复位线不受影响。
4. 无电平转换：DAP I/O 为 3.3V，目标板电平需匹配；无 VREF 检测，HID 的
   电压/LED2(VREF) 模式在此板上无意义（LED2 默认关闭）。
5. **CDC 回环必须短接 J3.8 ↔ J3.10**（UART_TXD ↔ UART_RXD）：只接逻辑分析仪
   不构成回环。固件侧全链路（复用/时钟/DMA/桥）已在 2026-09-27 实测通过，
   无回显时优先查这两个脚。

---

## 7. 验证清单

- [x] `build_xip_evklite.bat` / `build_dfu_evklite.bat` 编译通过，输出
      `[pack boot]` / `[pack app]`。
- [x] `flash_jlink_evklite_full.bat` 首刷后，USB0 口枚举
      `VID_0D28 PID_0204`（CMSIS-DAP 复合设备：DAP + CDC + HID + WebUSB + DFU）。
- [x] OpenOCD/pyOCD 等 SWD 主机可连外部目标（SWCLK=J5.9, SWDIO=J5.7）。
- [x] 长按 USER KEY 1s → 设备重枚举为 `PID_0207`（DFU+MSC 虚拟 U 盘）。
- [x] CDC 串口回环：**短接 J3.8 ↔ J3.10**，`script_test/evk_echo.py` 与
      `uart_loopback_common.py` 全速率通过（9600~10 Mbps，10M→974 KB/s）。
- [x] 探针侧 RTT 桥：`script_test/rtt_probe_bridge.py COM52 6 36000` →
      **2190 KB/s**，全流 13.5 MB 零丢包零重包（两连跑复现）；跑完桥后
      CMSIS-DAP 主机通路仍正常（`script_test/evk_swd_probe.py`）。

> 2026-09-27 实测记录（`script_test/evk_diag.py` 诊断固件）：
> UART3 内部回环自测 8/8、主机 27 字节经 CDC→UART TX 全部发出（TX DMA 完成
> 中断触发）、引脚直连自测确认 J3.8/J3.10 已短接；全速率回环 PASS。


---

## ⚠️ 2026-09-30 更新：CDC VCOM 从 UART3 换到 UART2

- **CDC VCOM = UART2：PB08(TXD, J3.5) / PB09(RXD, J3.3)**（板上丝印 I2C_SCL/SDA，
  只有 R21/R22 两颗 10 k 上拉，对 UART 无害）；
- **log_printf / console 仍是 UART0：PA00/PA01**（J3.36/J3.38，丝印 UART_LOG_TX/RX）；
- 原因：PB15/PB14（原 UART3 的 TXD/RXD）要让给 **SPI2 的 IO3/IO2**（USB→SPI/QSPI 桥
  从 SPI1 搬到 SPI2，见 `docs/spi-bridge-wiring.md` §6）。UART3 在 QFN48 上没有第二组
  可引出脚（只有未键合的 PA14/PA15），所以只能换实例。
