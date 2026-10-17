# STM32H7B0 · SEGGER RTT 吞吐（饱和速度）测试固件

> 📌 本目录在**工具仓库**（`web-serial-rtt-tools`）与**探针仓库**（`akaLinkPro/script_test/`）里
> **逐字节一致**（65 个文件，含 `sdk/`；两仓的 `fw.elf` 也相同）。文中出现的 `make fw-h7-*`、
> `tools\selftest\*`、`tools\target-firmware\...` 这些路径属于**工具仓库**；在探针仓库那边，
> 对应的跑法用 `script_test\*.py`（例如 `rtt_h743_bridge.py`）或网页。
> 改这边就同步那边，别让两边漂开。
> **只有一种构建：HAL/SDK 版**（280MHz，时钟配置 = 板子 demo 的 `SystemClock_Config()` 原文）。
> ⚠️ 2026-10 用户定调：早先那个"寄存器版（`-Minimal` / `-SlowClock`）"**已删除** ——
> 它与 HAL 版双轨、两边各自漂过（DIVM1 写成"值-1"→ 实际 350MHz 却自报 280MHz；`PWR_CR3`
> 偏移写成 0x08 → 读到 CR2），维护成本大于收益。需要"只跑 HSI 64MHz 的保命档"时，
> 改 `sdk\Core\Src\main.c` 的 `SystemClock_Config()` 即可，不再单独维护寄存器级实现。

拿这块板子量 **RTT 端到端能跑多快**：目标死循环灌数据，主机（网页 WebUSB / 桥+OpenOCD）拼命取，
页面上「读取 xxx KB/s」就是这块板子 + 这条主机通路的**饱和吞吐**。
套路与 [`../stm32f103_rtt_speed`](../stm32f103_rtt_speed) 完全一致，方便两块板子横向对比。

## 它到底在测什么

```
while(1) SEGGER_RTT_Write(0, "hello world!\n", 13);   // 不延时、不碰串口
```
RTT 用 **BLOCK_IF_FIFO_FULL**（`segger_rtt/SEGGER_RTT_Conf.h`）：缓冲满就阻塞 ⇒
**目标写多快完全由主机取多快决定**，主机读到的 B/s 就是这条通路的实际吞吐。
（所以"目标主频高"只保证它总能填满缓冲；瓶颈在主机侧 —— 这正是要测的。）

三个全局量留给主机读，交叉验证用：

| 符号 | 含义 |
|---|---|
| `g_bytes` | 目标实际写出的字节数（阻塞模式下 ≈ 主机读到的字节数） |
| `g_loops` | 循环次数（×13 = g_bytes） |
| `g_ms` | SysTick 毫秒数 —— **判目标死活**：阻塞在 RTT 写里它照样在走，不涨才是真卡死 |
| `g_sysclk_hz` | 实际切过去的系统时钟（确认 280MHz 有没有生效） |

## 快速开始

```powershell
# 1) 编译（一条命令，不需要 Keil/Make）
pwsh -File build.ps1              # HSE→PLL1 280MHz（VOS0）；产物复制到本目录 fw.elf

# 2) 烧录（首选：网页「烧录器」→ 后端 WebUSB → 芯片 stm32h7b0 → 选本目录的 fw.elf）
pwsh -File flash.ps1              # 兜底通道：OpenOCD（target/stm32h7x.cfg）

# 3) 测速：网页 RTT Viewer → 后端 WebUSB → 连接探针
#          芯片选「STM32H7B0/H7A3/H7B3」→ 它会扫 DTCM + AXI SRAM 找到控制块
#          看左下角「读取 xxx KB/s」；SWD 时钟先用「自动」，再手工比 8/12/20MHz
```

命令行测速（不起浏览器）：

```powershell
node tools\selftest\rtt-speed.mjs                       # 用 pyOCD? 不需要——走网页同款 WebUSB 通路时用下面的
CLOCKS=4000,8000,12000,20000 SECS=4 node tools\selftest\rtt-speed-webusb-sweep.mjs
```
（`rtt-speed-webusb-sweep.mjs` 需要先开着带调试端口的浏览器：`make browser`）

## 这块板子上要注意的（都是 H7 特有的坑）

1. **内存是散的**：DTCM 0x20000000(128KB，内核直连、免时钟) / AXI SRAM 0x24000000(1MB，要开 D1 时钟) /
   AHB SRAM 0x30000000…。本工程把代码放 flash、**数据/栈/RTT 缓冲全放 DTCM**，
   所以链接脚本只有两段内存（`ld/stm32h7b0.ld`），也**不用开任何外设时钟**。
2. **PWR/SYSCFG 的时钟要先开**（`RCC_APB4ENR` 的 `PWREN`/`SYSCFGEN`）：复位后它们是**关**的，
   不打开的话写 `PWR_D3CR`/`SYSCFG_PWRCR` **会被直接忽略**（不报错、也不生效）——
   现象是"PLL 配了但频率没变 / 一跑就 HardFault"。ST 的 HAL 里
   `__HAL_RCC_PWR_CLK_ENABLE()` + `__HAL_RCC_SYSCFG_CLK_ENABLE()` 就是干这个的。
3. **280MHz 要 VOS0**：先 `SYSCFG_PWRCR.ODEN=1`，再把 `PWR_D3CR.VOS` 设成 `0b11`（并等 `VOSRDY`），
   否则 PLL 配得再对内核也上不去。
4. **Flash 等待周期**：升频**之前**就要把 `FLASH_ACR.LATENCY` 提上去。本工程取 4（宁可多等，
   给少了会读到错指令），`WRHIGHFREQ` 按 185~285MHz 档。手上是 H7B3/H7A3 跑更高频时照 RM0455 复核。
5. **HSI 就是 64MHz**：H7 不需要外部晶振也能到 280MHz —— 自定义板子常常没焊 HSE，这点很省事。
   板上有晶振、想要更准的时钟：把 `clock_280mhz_hsi()` 换成 HSE 版本（改 `RCC_PLLCKSELR.PLLSRC` 与 DIVM1）。
6. **烧录粒度 32 字节**：H7 的 flash 按 256 位（32B）flash word 编程 ——
   `app/flash/algos.js` 里 H7B0 条目带 `write_granularity: 32`，烧录器会照它补 0xFF。
7. **H7B0 的 flash 只有 128KB**（value line）。如果你手上是 H7B3/H7A3（2MB/1MB），
   把 `ld/stm32h7b0.ld` 的 `FLASH LENGTH` 和 `app/flash/algos.js` 里的 `flash_length` 一起放大。
8. **F7/M7 的 FPU**：编译开了 `-mfpu=fpv5-d16 -mfloat-abi=hard`，所以 `startup.c` 里必须打开
   CPACR 的 CP10/CP11（否则一用浮点指令就跑飞）。本工程其实不用浮点，留着是为了别踩这个坑。
9. **SWD 引脚**：默认 PA13(SWDIO)/PA14(SWCLK)，别在固件里复用它们；nRST 建议接到探针（不接也能烧，
   但"复位运行"要用 AIRCR 软复位，工具里已经这么做）。

## 板子到手后的 bring-up 检查清单（照着走，出问题能立刻定位）

| 步 | 做什么 | 期望看到 | 不对时先查 |
|---|---|---|---|
| 1 | 探针接 SWD、给板子上电，读 DP IDCODE | **0x6BA02477**（H7 的 SW-DP）<br>F103 是 0x1BA01477 | 接线/共地/上电；SWCLK-SWDIO 有没有接反 |
| 2 | OpenOCD 认芯片：`make fw-h7-flash`（或桥连一次） | 日志里 `RM0455 (id 0x480) M7` | 晶振/BOOT 引脚；RDP 等级（读保护会挡住调试口） |
| 3 | `make fw-h7-build` 编译 + 烧录 + 网页 RTT 连接 | 控制块在 `0x2000xxxx`（DTCM）；<br>`hello world!` 刷屏；**能出 KB/s 数字** | 接线/启动/DTCM 布局；控制块地址要选对 `stm32h7b0` 那颗芯片 |
| 4 | 再确认 `g_sysclk_hz` = 280000000、`g_clk_err` = 0 | 时钟真的切到 PLL1 | 若挂：看是不是 VOS0/PLL/latency（第 2~4 条坑）；要"完全不碰 PLL"的保命档就改 `sdk\Core\Src\main.c` |
| 5 | 网页上把 SWD 时钟从「自动」切到 8/12/20MHz 各测一遍 | 找到这块板子最快的档 | 高了会 NO ACK 或错位读（页面会提示） |

> 真机步骤与预期数字见 `docs/backends.md` 的「五点八」。

## ⚠️ 首次接入记录（2026-09-27 实测，**板子当前连不上**）

用户接上自制的 H7B0 板后，探针侧现象如下（`tools` 里的裸客户端脚本可复现）：

| 项目 | 结果 |
|---|---|
| 读 DP IDCODE | **0x6BA02477** ✅（正是 H7 家族的 SW-DP，说明 SWD 接线/供电至少有基本通路） |
| 读 DP CTRL/STAT | **0x00000000**（两个电源 ACK 都是 0、无 sticky） |
| 写 DP SELECT / CTRL/STAT（任何值） | **FAULT(4)** ❌ |
| 读 AP CSW | FAULT ❌ |
| IDCODE 连读 5 次 | 稳定 ✔（不是接触不良抖动） |
| nRESET 引脚 | 高 ✔（不在复位态；拉低再放开也一样） |

软件侧已把能试的都试了（**全部无效**）：写序调换、延迟、SWD 时钟 250k/500k/1M/2M、
turnaround 1~4、线复位 64/256 位、重新发激活序列、DAP WriteABORT 清 sticky、
**复位下连接**（按住 nRESET 激活再放开）、把"读 IDCODE + 写寄存器"塞进同一条 DAP_Transfer。

**判断**：SWJ-DP 本体活着（否则读不到 IDCODE），但**它后面的调试/系统电源域起不来、
或者器件处于禁止调试写入的状态**。按可能性排：

1. **VCORE 没起来**（自制板最常见）：H7 要求 **VCAP1/VCAP2 各接 2.2µF** 电容，
   还有 VDD/VDDA/VREF+/VDD33USB 都要正常供电。VCORE 不工作时，AP 与"电源请求"逻辑
   都无法应答 —— 正好是"读得到 IDCODE、写一律 FAULT"。
2. **器件状态/选项字节**：RDP ≠ 0、TZEN=1（如果这颗带 TrustZone）、或调试被选项字节关掉。
   这类通常需要用别的工具/复位下连接去读选项字节确认。
3. 供电电流/焊接异常（QFP 电源脚虚焊、VDDA 缺失）。

**建议的下一步（都在硬件侧）**：
- 万用表量 **VCAP1/VCAP2 ≈ 1.2V**、**VDD ≈ 3.3V**、**VDDA**、GND 与探针共地；
- 量 **BOOT0 = 0**、nRST = 高（已确认高）；
- 上电电流是否正常（VCORE 没起来时电流通常明显偏小/偏大）；
- 若手上还有别的探针（ST-Link 等），换一个试 —— 以排除探针侧（注意同一探针在 F103 上是好的）。



## 预期数字（供对照）

| 板子 | SWD 时钟 | 饱和吞吐（网页 WebUSB，实测） |
|---|---|---|
| STM32F103C8（72MHz） | 8MHz | ~250–260 KB/s |
| STM32H7B0（280MHz） | 待你实测 | ? |
| 任一板 + 桥/OpenOCD | 4MHz | ~17 KB/s（Tcl RPC 是瓶颈） |

> 吞吐主要取决于**主机侧每轮往返次数**与 SWD 时钟，跟目标主频关系不大（目标只要灌得满缓冲）。
> 所以 H7B0 的数字应该和 F103 接近 —— 如果明显更高/更低，值得看看是不是掉进了"错位读自愈"或缓冲高位。

## 目录

```
sdk/Core/Src/main.c     死循环发 RTT + 时钟初始化（`SystemClock_Config()` = 板子 demo 原样，280MHz）
sdk/                    HAL + CMSIS + 板级 Core（51 个文件，本工程只编其中 14 个 .c）
src/startup.c           向量表 + .data/.bss + FPU 使能（M7）
ld/stm32h7b0.ld         FLASH 128KB + DTCM 128KB
segger_rtt/             SEGGER RTT 源码（与 F103 那份同版本，Conf 里 BUFFER_SIZE_UP=4096）
build.ps1 / flash.ps1   编译 / 烧录
```

## 许可

自有代码 Apache-2.0；`segger_rtt/` 是 SEGGER 的实现，随上游分发（见其目录内 README-SEGGER.md）。
