# script_test

akaLinkPro (HPM5301) 固件测试脚本集合。这些脚本用于验证 **UART ↔ CDC 串口桥**、
**DAP 模式切换**（SWD/空闲 = UART，JTAG = FGPIO）以及设备枚举状态。

## 回环接线（两台板子不同！）

| 板子 | CDC VCOM 引脚 | 回环短接位置 |
| --- | --- | --- |
| akaLinkPro | UART2 PA08(TXD)/PA09(RXD) | PA08 ↔ PA09 |
| HPM5301EVKLite | UART2 PB08(TXD)/PB09(RXD) | **J3.5 ↔ J3.3**（丝印 I2C_SCL/I2C_SDA） |

> 接逻辑分析仪**不算**回环，必须把 TX 和 RX 两个脚真正短接。

## 依赖

- Python 3.11 + `pyserial`、`pyusb`（`pip install pyserial pyusb`）
- Windows 上需已安装 libusb/WinUSB 驱动（pyusb 后端）
- 设备运行 `application_5301` 固件，并在系统中枚举出 CDC 串口（COMx）

COM 端口号在不同机器上会变化，脚本支持通过参数传入，默认 `COM75`。
EVKLite 上 CDC 是复合设备的 `MI_01`，可用
`Get-PnpDevice -PresentOnly | Where-Object InstanceId -match 'VID_0D28&PID_0204&MI_01'` 确认。

## 脚本说明

| 文件 | 用途 |
| --- | --- |
| `uart_loopback.py` | 串口回环压力测试：逐字节回环 + 大块随机数据流，覆盖 115200~2Mbps |
| `uart_loopback_hs.py` | 高速回环测试：2Mbps~11.25Mbps，含吞吐量与线速效率统计 |
| `uart_loopback_common.py` | 常见速率回环详表：请求/实际波特率、误差、耗时、吞吐、线速效率 |
| `diag_loopback.py` | 单次大流量回环并定位首个错误位置/零区块，用于诊断 DMA 边界问题 |
| `test_modeswitch.py` | 通过 CMSIS-DAP 发 `DAP_Connect`(JTAG/SWD)/`DAP_Disconnect`，验证引脚复用切换 |
| `uart_stress_loop.py` | 定时长回环压测（发一块读一块），统计 errors/late/最大延迟，可区分丢失与延迟 |
| `uart_stress_stream.py` | 连续流回环压测（读线程 + 不丢包比对），用于与 SWD 并发时做满负荷验证 |
| `list_usb.ps1` | 列出 `VID_0D28` 相关 USB 设备与 CDC 端口（状态检查） |
| `hold_port.py` | 打开串口并保持若干秒，便于用 J-Link/GDB 在线检查运行状态 |
| `swd/run_benchmark.py` | 生成指定 `adapter speed`/`iterations` 的 OpenOCD SWD 读写校验并运行 |

> `stm32f103_rtt_speed/` 是 RTT 测速用的 STM32F103 测试固件（SEGGER RTT + 计数
> 全局变量），`rtt_*.py` 需要它编译出的 `fw.bin`：先跑
> `powershell -File stm32f103_rtt_speed\build.ps1 -Board cb|ze` 可重新生成（需要
> arm-none-eabi 工具链）；**`build-cb/fw.elf` 和 `build-ze/fw.elf` 与 `fw.bin` / `fw.hex`
> 均已入库**，可直接在调试器页面载入 ELF，或烧录对应板卡；`fw.map` 不入库。
> **每块板子一个独立输出目录，互不覆盖**：`build-cb/`（128KB flash / 20KB RAM）、
> `build-c8/`（64KB flash / 20KB RAM）、`build-ze/`（512KB flash / 64KB RAM，
> RTT 上行缓冲 32KB）；烧录用 `flash.ps1 -Board <名字>`。
> `sram_speed_test.py` / `rtt_*.py` 走 OpenOCD，路径由环境变量
> `OPENOCD_EXE` / `OPENOCD_SCRIPTS` / `HPM_SDK_ENV_DIR` 覆盖。

### EVKLite（HPM5301EVKLite）专用

| 文件 | 用途 |
| --- | --- |
| `evk_echo.py` | **最常用**：打开 CDC 写 27 字节并比对回环，退出码 0=PASS |
| `evk_flash.py` | HID `CMD_ENTER_DFU` → DFU 虚拟盘拖入 `_pack.bin` → 等 APP 回来 |
| `evk_dfu_enter.py` | 只发 HID 进 DFU 命令（不烧录） |
| `evk_jtag_mode_test.py` | 验证切到 SWD+JTAG 模式后 COM 口仍可用（引脚复用回归） |
| `evk_probe.py` | 快速探测：CDC 有无自发数据 + HID 应答格式 |
| `evk_ident.py` | 读运行中固件的版本/编译时间/参数（确认板上到底是哪一版） |
| `evk_pyocd_sram.py` | 同一 SRAM 测速改用 pyOCD（换一套主机栈交叉验证瓶颈在哪一侧） |
| `evk_swd_probe.py` | 固定 SWD 时钟做传输并打印 OpenOCD 的完整日志（找重试/ACK 异常） |
| `evk_target_clock_test.py` | 读 STM32F103 的 RCC，对比「复位默认 8MHz」与「提到 64MHz」下的吞吐 |
| `evk_verify.py` | 带 HID 状态读数的回环验证（需 TEMP-DIAG 诊断固件） |
| `evk_diag.py` | 全量诊断（需诊断固件）：引脚直连自测 / UART 内部回环 / 计数器 / force-start |
| `evk_watch_jumper.py` | 打开 TX 心跳并实时轮询 RX 计数，用于边插跳线边观察（需诊断固件） |
| `rtt_probe_bridge.py` | **探针侧 RTT 桥测速**：boost 目标 → HID `CMD_RTT` 启动桥 → 只读串口测吞吐，并做全流零丢包校验 |
| `rtt_bridge_sweep.py` | **调优扫描**：时钟 × clock_delay × 块大小，并排给出「纯 SWD 基准 / 丢弃模式搬运 / 端到端」三个数，直接指出天花板在哪一侧（`--clk=` 指定桥的请求档位） |
| `rtt_rate_matrix.py` | **逐档对照表**：同一目标主频下逐档量「纯 SWD 读速」与「RTT 交付率」，给出占比与主导方（按每次搬运字节数实测判定），可直接贴进文档 |
| `rtt_peek.py` | 读探针自身内存（HID `CMD_RTT` action 5），bring-up 期查 `DAP_Data`/trace 用 |
| `rtt_rawdap.py` | 经探针主循环透传原始 CMSIS-DAP 请求（action 4/6），bring-up 期用 |

### HPM6800EVK（HPM6880，RISC-V / JTAG-only）专用

第一次调 RISC-V 目标，脚本统一带 `hpm6800_` 前缀。完整背景、接线与结论见
[`../docs/hpm6800evk-jtag.md`](../docs/hpm6800evk-jtag.md) 与 README 的
「HPM6800EVK（HPM6880，RISC-V）目标调试」一章。

| 文件 | 用途 |
| --- | --- |
| `hpm6800_probe.py` | 探针控制：`set-mode 1` 切 SWD+JTAG（**只存 RAM，每次上电都要重设**）、读状态/电压 |
| `hpm6800_flash_probe.py` | 探针固件 DFU 升级（编译 → 等虚拟盘 → 写入 → 等 APP 回来） |
| `hpm6800_flash_target.py` | **用探针烧 HPM6800EVK**：默认就是狂发固件 ELF，并打印启动头 + 复位后 PC 自检 |
| `hpm6800_selfcheck.py` | **完整性门禁**：探针写已知图案再读回比对校验和；**动任何时序都必须先跑它** |
| `hpm6800_riscv.py` | 探针侧 RISC-V 引擎：`open` / `status` / `rbench` / `wbench` / `sbench` / `rcheck` / `delay` |
| `hpm6800_rtt_delivery.py` | **RTT 交付率主尺子**：先开串口并起读线程、再启动桥，主机只读串口不计往返 |
| `hpm6800_rtt_loss.py` | **字节级丢包校验**：狂发固件写固定 13 字节记录，模式错位即丢包（比计数硬） |
| `hpm6800_rtt_diag.py` | 出事时用 HID PEEK 读**探针自身 RAM** 定位：`g_uartrx` in/out、rtt_bridge 计数器、`s_dbg` 原始 41 位 DMI 响应 |
| `hpm6800_cdc_check.py` | 拆 CDC 那一跳：先做串口回环，再每 200 ms 采样「环 in/out vs 主机收到的字节数」 |
| `hpm6800_jtag_raw.py` | 不经探针固件、主机直接 bit-bang JTAG 的原始扫描（bring-up 期验 TAP/IR/DR） |
| `hpm6800_jtag_bench.tcl` | OpenOCD 侧三个后端（`progbuf` / `sba` / `abstract`）的对照基准 |
| `hpm6800_timing_sweep.ps1` | DMI 时序旋钮扫描（`DMI_CAP_HIGH_NOP` / `DMI_NAV_*_NOP` × 自检 × 读写基准），每点约 2 分钟 |
| `hpm6800evk_rtt_flood/` | 目标侧狂发固件（`flash_xip`，RTT 上行 32 KB，`BLOCK_IF_FIFO_FULL`，死循环发 `hello world!\n`） |
| `sram_speed_hpm6800.py` | HPM6800 的 SRAM **主机侧**口径（OpenOCD load/dump，与探针侧引擎对照） |
| `probe_usb_recover.ps1` | 探针从 USB 上消失时，给它上游的 USB Hub 断电重枚举（实测救回两次） |
| `openocd_hpm6800evk_dap.cfg` | OpenOCD 配置：`transport select jtag` + **`reset_config none`**（排线第 15 脚是探针自己的 RESET_N，必须） |

> ⚠️ **`hpm6800_riscv.py wbench 0x1200000` 会覆盖目标正在用的 RAM** —— `0x1200000`
> 就是狂发固件 `.bss` 的起点，目标随后不再产数据（RTT 交付塌到 3 KB/s，但**读回
> 校验和仍然是对的**，只有速率会暴露）。做完写基准确认要重烧一次目标，或换空闲
> scratch 地址。
>
> ⚠️ `hpm6800_rtt_diag.py` 里的符号地址是从**当前构建**的
> `build_dfu_evklite/output/akaLinkPro_App.asm` 的 `# <sym>` 注释里取的；改固件后
> 要重新取（`s_drained` 之类是 `static`，`.map` 里不一定有）。

### RTT 测速为什么慢（实测结论，2026-09-27）

`make rtt-test` 早期只有 ~0.4 MB/s，比 SRAM 读写的 2.5 MB/s 差 6 倍。逐项量下来是
**三个原因叠加**，与探针固件无关：

**① RTT 的「生产者」跑在目标机上（主因）**：STM32F103 上电默认 HSI 8 MHz，此时
固件的 `SEGGER_RTT_Write` 死循环只能产出 **277 KB/s**，把 SWD 从 10 MHz 提到
20 MHz **一点不变**（276.8 → 277.1 KB/s）——瓶颈在目标，不在链路。

**② RTT 是「轮询」模型，不是流式**：每次取数要 3 个 host↔探针来回（读
WrOff/RdOff → 读环形缓冲 → 写回 RdOff），每次轮询约 3 ms 固定开销。SRAM 的
`load_image/dump_image` 是**一次大块流水传输**，所以能跑满 2.5 MB/s；轮询做不到。

**③ 目标运行中，长块读会失败**：运行的内核会与 AHB-AP 抢总线，一次 12 KB 的
块读会成百次失败（吞吐掉到 76 KB/s），拆成 1 KB 分块后 **0 失败**。所以轮询读
必须限长 + 重试。

实测阶梯（目标均已归一化到 64 MHz）：

| 配置 | 吞吐 |
| --- | --- |
| 目标停在复位默认 8 MHz（任何 SWD 时钟） | 277 KB/s ← 原来的 0.4 MB/s 就是它 |
| `make rtt-test`（OpenOCD rtt server），目标 8 MHz | 403 KB/s |
| `make rtt-test`，目标 64 MHz | **919 KB/s**（20 MHz 档） |
| `make rtt-max`（轮询跑在 OpenOCD 内、32 位分块读、12 KB 环） | **1140 KB/s**（60 MHz 档，0 失败） |

配套改动：RTT 环形缓冲 4 KB → **12 KB**（4 KB 时每次取数都撞满 `maxdrain=4095`，
轮询次数被白白吃掉）；`rtt-test` 会先 halt → 提频 → resume 并打印实测主频。

> 想再往上只有一条路：**把轮询下沉到探针固件里**（探针自己用 SWD 轮询目标的 RTT
> 控制块，再经 USB 推给主机），这样就没有主机往返开销，理论上限回到 SWD 读带宽
> （~2 MB/s）。J-Link 就是这么做的，属于固件新功能。

**这条路已经做完了**（已合入 `main`），实测：

| 配置 | 吞吐 |
| --- | --- |
| 探针侧桥，20 MHz SWD | 1420 KB/s |
| 探针侧桥，36 MHz SWD | **2190 KB/s（2.14 MB/s）**，全流 13.5 MB 零丢包零重包 |

`python rtt_probe_bridge.py COM52 6 36000` 一把跑完（自动 boost 目标 → 启动桥 →
只读串口测速 → 校验流完整性 → 读回状态字）。实现要点、三个坑（握手/提速顺序、
RdOff 幂等重试、512 B 分块）见
[`docs/HPM5301EVKLite_port.md` §5](../docs/HPM5301EVKLite_port.md#5-探针侧-rttcdc-桥分支-featprobe-rtt-bridge)。

`rtt_speed_test.py` 走的是 OpenOCD 自带的 rtt server，它的 `rtt polling_interval`
**默认 100 ms**，实测（20 MHz SWD、目标 64 MHz）：

| polling_interval | 吞吐 | 轮询/秒 |
| --- | --- | --- |
| 不设置（默认 100 ms） | 9.2 KB/s | 9 |
| 10 ms | 64.1 KB/s | 64 |
| 5 ms | 64.3 KB/s | 64 |
| **1 ms**（脚本采用） | **903.9 KB/s** | 904 |
| 0 ms（不限速） | 906.1 KB/s | 906 |

- **必须设成 1 ms**：默认值只有 9 KB/s（每次轮询只搬约 1 KB，100 ms 一次 = 9 KB/s）。
- 1 ms 再往下没有收益（0 ms 与 1 ms 等价），所以脚本保持 1 ms。
- 5~10 ms 反而异常差（64 KB/s）——OpenOCD 的定时器在这个区间量化得很糟，别在这个
  范围里"调优"。
- 注意这个参数**只影响 `make rtt-test`**；`make rtt-max` 的轮询跑在 OpenOCD 内部，
  与它无关。
- 低 SWD 时钟下这个参数也救不了：1 MHz 时 1 ms 间隔也只有 ~79 KB/s（受 SWD 读出
  速度限制）。

### SRAM 测速为什么必须固定目标主频 ⚠️

SWD 的每次 AHB-AP 事务要花掉目标机好几个 HCLK，所以吞吐上限是
`min(SWD 时钟, 目标 HCLK)`。STM32F103 **上电后 RCC 默认是 HSI 8MHz**，此时无论把
SWD 时钟拉到 45/60MHz，吞吐都会卡在 ~1.4MB/s；而 STM32F1 的 `reset halt` 是
**核心级复位、不会清 RCC**，所以只要之前有固件把 PLL 配起来过，测速又会变成
~2.8MB/s —— 同一个探针、同一份固件，数字却能差一倍。

`sram_speed_test.py` 因此默认在每轮 `reset halt` 之后把目标提到
**64MHz（HSI/2 ×16，无需外部晶振）**，并打印实测的 SYSCLK/HCLK：

```bat
python script_test\sram_speed_test.py              :: 归一化目标主频（推荐，数字可复现）
python script_test\sram_speed_test.py --no-boost   :: 保留目标复位后的原始状态
```

### 运行示例

```bat
python script_test\uart_loopback.py COM75
python script_test\uart_loopback_hs.py COM75 262144
python script_test\uart_loopback_common.py COM75
python script_test\diag_loopback.py COM75 115200 32768
python script_test\test_modeswitch.py COM75
powershell -ExecutionPolicy Bypass -File script_test\list_usb.ps1
python script_test\hold_port.py COM75 8
```

### 串口速率上限与就近取整（HPM5301）

- 驱动器公式：`baud = uart_clk / (div * osc)`，`osc` 为 8~30 的偶数。
- **默认（符合手册）**：UART2 时钟 = `PLL0CLK0(720MHz)/9 = 80MHz`（UART 输入时钟手册上限），
  硬件/软件上限 = `uart_clk/8 = 10 Mbps`（`UART2_CLK_DIV=9`、`UART2_MAX_BAUDRATE=10000000`）。
- **超频选项**：`CMakeLists.txt` 里取消注释 `sdk_compile_definitions(-DUART2_OVERCLOCK=1)`，
  则 `720/4 = 180MHz`、上限 22.5 Mbps，且 11.25/15/18 Mbps 可精确生成。
  **注意：180MHz 超出手册限制，不保证所有芯片稳定，仅供测试。**
- 无法精确生成的按**就近取整**（`uart2_round_baudrate()`）。
- 实际写入的波特率存于 `g_uart2_applied_baud`（可用 J-Link/GDB 读取核对）。
- 实测吞吐（默认 80MHz）：10M→~972KB/s（线速效率 99.6%）；>10M 请求被钳到 10M。
  超频 180MHz：22.5M→~2108KB/s。

## gdb/ — 在线调试检查脚本（配合 JLink GDB Server）

先启动 GDB Server（或用 `firmware/application_5301/gdb_server.bat`）：

```
JLinkGDBServerCL.exe -device HPM5301xEGx -if JTAG -speed 4000 -port 2331 -nogui -singlerun
```

然后：

```bat
riscv32-unknown-elf-gdb -batch -x script_test\gdb\<script>.gdb ^
  firmware\application_5301\build_dfu\output\akaLinkPro_App.elf
```

| 文件 | 用途 |
| --- | --- |
| `dbg_app.gdb` | 连接/复位/load/命中断点 main（与 VSCode launch 流程等价） |
| `dbg_test.gdb` | 连接后 reset/load/break main/查看寄存器 |
| `inspect*.gdb` | 读取 UART2 寄存器、DMA 通道、`dma_resource_pools`、引脚 FUNC_CTL 等 |

> 注意：`inspect*.gdb` 中的寄存器地址/符号对应 HPM5301 + 本工程，改动后需同步。

## jlink/ — J-Link 命令行测试片段

用法：`JLink.exe -NoGui 1 -ExitOnError 1 -CommanderScript script_test\jlink\<script>.jlink`

| 文件 | 用途 |
| --- | --- |
| `jl_probe.jlink` | `ShowEmuList`，列出已连接的 J-Link |
| `jl_min.jlink` | 最小 JTAG 连接测试 |
| `jl_test3.jlink` | JTAG 自动探测 + 连接 + 读内存 |
| `jl_read.jlink` | 读 flash（APP 签名 `HPM!`） |
| `jl_pins.jlink` | 读 PA08/PA09/PB13 的 IOC FUNC_CTL 与 GPIO 输出状态 |
| `jl_load.jlink` | `erase` + `loadfile` 示例（路径需按需修改） |
| `jl_flashinfo.jlink` / `jl_help.jlink` / `jl_test.jlink` / `jl_test2.jlink` | 早期调试片段 |

## CDC + SWD 同时满载

SWD 压测（`swd/run_benchmark.py`，目标可为 STM32F1，`adapter speed` 20/36/45/60MHz）
与 CDC 连续流回环（`uart_stress_stream.py COM75 9000000 <秒>`）同时运行。

实测（SWD 1000 轮 + CDC 9Mbps 连续流）：

| SWD 速度 | CDC 速率 | SWD 校验 | CDC 连续流 |
| --- | --- | --- | --- |
| 20 MHz | 9 Mbps | 1000/1000 PASS | 8/8 OK（每轮 ~7MB） |
| 20 MHz | 10 Mbps | 1000/1000 PASS | 8/8 OK |
| 20 MHz | 22.5 Mbps | 1000/1000 PASS | 16/16 OK（2 轮，~230MB） |
| 36 MHz | 9 Mbps | 1000/1000 PASS | 全 OK |
| 45 MHz | 9 Mbps | 1000/1000 PASS | 全 OK |
| 60 MHz | 9 Mbps | 1000/1000 PASS | 全 OK |
| 60 MHz | 22.5 Mbps | 1000/1000 PASS | 4/4 OK |

说明：SWD 20MHz 的临界区抖动最大；即使 `20MHz SWD + 22.5Mbps CDC`（双向极限）
两轮 16/16 也全部干净，无掉/重数据。偶发失败需先排查硬件接触。

结论：SWD 20~60MHz 与 CDC 9Mbps **同时满载无掉数据/无重复**。
（关键修复：给 `g_uartrx` 的 DMA-TC 生产者补齐临界区；并用 GPTMR 定时器驱动 RX flush，
不依赖 IDLE/满缓冲中断。）

RX flush 定时器周期**按波特率动态调整**（目标每次约 512 字节，clamp 到 200us~10ms）：
低波特率时降低中断频率。实测 GPTMR RLD：`9600 → 10ms`、`9M → 568us`。

## 关键结论（回归基线）

- 回环压力测试：115200 / 460800 / 921600 / 1M / 2Mbps **全部通过**。
- 常见速率（`uart_loopback_common.py`）：9600~9M **全部通过**；
  6M→5.625M、8M→7.5M、10M→9M、11.25M→9M（9M 软件上限 + 就近取整）；高速线速效率 ~99.7%。
- 高速回环（1MB×多次，稳定）：2M~9M **全部通过**，线速效率 ~100%。
- 模式切换：空闲回环 OK → JTAG 下 COM 无回显（正常）→ SWD 恢复 OK → Disconnect 恢复 OK。
- 引脚状态：PB13 `FUNC_CTL=0` 且输出高（5V_EN 开）；空闲/SWD 下 PA08/PA09 `FUNC_CTL=2`(UART2)。
- RX 采用 **DMAV2 infinite-loop 圆形缓冲**（无 disable/restart），消除了重启边界上的重复字节。

### HPM6800EVK（RISC-V / JTAG）回归基线

探针固件或 DMI 时序有任何改动，这几条都要重新对上（前两条是**门禁**，不过就别往下走）：

| 项 | 基线值 | 复现命令 |
| --- | --- | --- |
| **探针写图案读回校验和** | `got 0x08ECD0B4 want 0x08ECD0B4` → **PASS** | `hpm6800_selfcheck.py` |
| **字节流模式精确** | `pattern exact` + `lost=0 dup=0` + `probe-host delta: 0` | `hpm6800_rtt_loss.py COM5 20` |
| 探针侧 SBA 块读 | **1527 KB/s**（1024 B × 50；SBA 配置缓存之前是 1504.5） | `hpm6800_riscv.py rbench 0x1200000 1024 50` |
| 探针侧 SBA 块写 | **1551 KB/s**（前 1511.8） | `hpm6800_riscv.py wbench 0x1200000 1024 50` |
| 单字 SBA 读 | **10.3 µs**（前 25.6；配置缓存后只剩 4 次 DMI 扫描/字） | `hpm6800_riscv.py sbench 0x1200000 200` |
| RTT 交付率 | **1419.8 KB/s**（前 1389；20 s / 28.5 MB），`rderr=0 wderr=0` | `hpm6800_rtt_delivery.py COM5 5` |
| J-Scope over JTAG（HID 0x32 `flags bit6`） | 单字 **2.937 µs / 340.5 kHz**；8 通道（32 B span）**30.81 µs / 32.5 kHz** | `scope_hss_test.py bench --riscv --set one --addr 0x1240000` |
| 主机驱动 OpenOCD（`progbuf`） | 写 154.0 / 读 95.9 KB/s | `hpm6800_jtag_bench.tcl` |
| TAP 身份 | IDCODE `0x1000563D`、DTMCS `0x00007071`、DMSTATUS `0x00400CA2` | `hpm6800_riscv.py status` |
| 目标启动自检 | `0x80001000`=`009010BF`、复位后 PC=`0x80003000`、`0x1240000`=`"SEGGER RTT"` | `hpm6800_flash_target.py` |
| 有效 TCK 频率 | **~21 MHz**（54 TCK/字 × 1527 KB/s ÷ 4），规格上限 25 MHz | 由 rbench 反推 |

> ⚠️ `hpm6800_selfcheck.py` 收尾会发 `ACT_STOP`（关掉 TAP），所以后面要跑 `rbench`/`sbench`
> 得先 `hpm6800_riscv.py open`，否则状态里是 `open=0` 而基准动作直接判 `iters=0`。
> 块读/块写/单字读那几行数字在 2026-09-29 的"SBA 配置缓存"改动后**整体上浮**：
> 每次块读不再白付 6 次扫描（重配 SBCS 4 次 + 收尾读回清错 2 次）。

固件侧时序常量（**四个都已验证是最小值，别再减**）：

| 常量 | 现值 | 减一档会怎样 |
| --- | --- | --- |
| `DMI_NAV_LOW_NOP` | 6 | **4 → 自检 FAIL，读回全 0**（TMS 建立不够） |
| `DMI_NAV_HIGH_NOP` | 4 | 4 通过（配合 `NAV_LOW=8` 时的 4/4 曾 FAIL ⇒ 卡的是 LOW 不是 HIGH） |
| `DMI_TCK_LOW_NOP` | 4 | **2 → 自检 FAIL，校验和 `0x11D9A168`**（TCK 低电平太窄） |
| `DMI_CAP_HIGH_NOP` | 1 | **0 → 自检 FAIL**（采样点不够；TDI 预置后已垫了 8 条指令，补 1 拍正好） |
| `idle`（运行时） | 8 | **6 → 只跑得动 6/50 轮就死**；≤4 → `moved=0`（DMI 完全不应答） |

> ⚠️ 这套时序改起来最坑的一条：45M 汇编的 `andi t4, a4, JTDI_OFFSET` 看着能把
> "取位 + 移位"合成一条，但那只在 **TDI 流已预先左移过 `JTDI_SHIFT`** 的代码里成立。
> 套到 DMI 那边（`\in` 是原始请求寄存器、bit0 才是当前位）会去取 `bit[JTDI_SHIFT]`，
> 于是整串请求被移成 0（`op=NOP`）—— **基准照跑、rderr/wderr 全 0、idcode/dtmcs 也对，
> 只有自检能抓到读回全 0**。这就是自检必须当门禁的原因。


### J-Scope 波形（探针侧 HSS 采样）脚本

| 文件 | 用途 |
| --- | --- |
| `scope_hss_test.py` | **命令行端到端验收**：HID 0x32 配变量表 / 设 SWD 时钟 / 标定，再用 pyusb 读 bulk IN `0x83` 解包核对（`bench` = M0 标定，`run` = 实采）。网页走 WebUSB，同一根管子 |
| `stm32f103_scope/` | **靶子固件**（STM32F103C8，**HSE ×12 = 96 MHz 超频**，10 kHz 契约波形）。`build.ps1` / `flash.ps1` / `check.py`（客观验收：halt → dump RAM → 逐变量核对 + 反测时基）。源本在 web-serial-rtt-tools，本仓库这份**已提频到 96 MHz** —— 原因见下 |

> ⚠️ **靶子必须跑 96 MHz 才能量到探针的上限**：AHB-AP 每次读都要花目标侧几个 HCLK，
> 8 MHz 时块读封顶 1.47 MB/s 且 45/30 MHz 读数一样（目标饱和），96 MHz 下同一路径
> 3.37 MB/s。用 8 MHz 的靶子量出来的是靶子的上限（≈46 kHz），不是探针的。
> 原目录 `tools/target-firmware/stm32f103_scope` 保持 8 MHz 不动，本仓库这份是提频版。
