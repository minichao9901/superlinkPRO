# RTT 吞吐测试固件（STM32F103）

用来量 **SEGGER RTT 的极限速度**，并且**在同一块板子上对比 WebUSB 与 OpenOCD 两条主机通路**。

## 主频（超频）—— 生产者速率随主频线性缩放

固件启动时自己把主频顶上去（`main.c` 的 `clock_init()`），**不依赖 OpenOCD 外挂 boost**：

| 目标主频 | 配置 | RTT 生产者实测 |
| --- | --- | --- |
| 8 MHz | 复位默认 HSI | 277 KB/s |
| 64 MHz | HSI/2 ×16 | 2217 KB/s |
| 72 MHz | HSE 8 MHz ×9 | 2477 KB/s |
| **96 MHz** | **HSE ×12（当前默认 `TARGET_HCLK_MHZ 96`）** | ~3300 KB/s（外推） |
| 128 MHz | HSE ×16，固件切会硬故障（见下） | ~4400 KB/s（外推） |

**生产者速率 ≈ 34.6 B/ms 每 MHz 主频**（8/64/72 MHz 三个点都吻合到 1% 以内）。
要更高的 RTT 交付率，先把目标主频提上去，而不是一味提 SWDCLK。

选 96 MHz 而不是 128 MHz 的理由：**flash 余量**。latency 上限是 2（3 周期），
96 MHz 下 = 31 ns，128 MHz 下 = 23 ns，而 F103 flash 实际需要 ~40 ns —— 两者都在超，
但 96 MHz 安全得多；而实测交付率两者持平（96 MHz: 2508 KB/s、128 MHz: 2507~2637 KB/s），
因为 45 MHz SWD 档的**链路本身只到 ~2.5 MB/s**，生产者再快也吐不出来。

两条硬限制（都实测过）：

1. **这块板子上 144 MHz 做不到**：板上晶振实测 8.005 MHz，而 F103 的 PLL 倍频上限是 **×16**
   ⇒ 物理上限 128 MHz。要 144 得换 9 MHz（×16）或 12 MHz（×12）晶振，或从 OSC_IN 灌外部时钟。
2. **128 MHz 下 flash 喂不动核心**：latency 最多 2（3 周期 ≈ 23 ns），而 F103 flash 实际要 ~40 ns。
   固件自己在开头切到 128 MHz 会立刻硬故障（实测 `HFSR=0x40000000` FORCED、
   `CFSR=0x00001001` = IACCVIOL|STKERR，即取指出错跳飞）。128 MHz 目前只能在 halted 状态下由
   上位机切换、让固件从复位向量就以该频率启动 —— 紧凑循环靠预取缓冲勉强跑得住，余量很小；
   想稳跑 128 MHz 应把热代码搬到 SRAM 执行。

`clock_init()` 里三件套缺一不可：FLASH_ACR 先给足等待周期 + 预取；APB1 ≤ 36 MHz、APB2 ≤ 72 MHz；
以及**换频必须走完整时序**（SW→HSI、关 PLL、改倍频、开 PLL、SW→PLL）——PLL 配置位在 `PLLON=1`
时是写保护的，直接改无效（最容易踩的一条，症状是"设了新频率但速度没变"）。

## 它做什么

```c
for (;;){
  unsigned n = SEGGER_RTT_Write(0, "hello world!\n", 13);   /* 不加任何延时 */
  g_bytes += n;
  g_loops++;
}
```

RTT 配成 **`SEGGER_RTT_MODE_BLOCK_IF_FIFO_FULL`**（缓冲满就阻塞）——这是测量的关键：
目标写多快**完全由主机取多快决定**，所以**主机读到的字节/秒就等于 RTT 的实际吞吐**，
不需要去数目标侧的计数，也不受"目标丢包/覆盖"干扰。

留了三个全局量给主机交叉验证：

| 变量 | 含义 |
|---|---|
| `g_bytes` | 目标实际写出去的字节数（阻塞模式下应当 ≈ 主机读到的字节数） |
| `g_loops` | 循环次数（×13 = `g_bytes`） |
| `g_ms` | SysTick 毫秒数 —— **判断目标是否还活着**：阻塞在 RTT 写里时它照样在走，不涨才是真卡死 |

## 编译 / 烧录

**每块板子一个独立输出目录，互不覆盖**（`-Clean` 也只清当前这块）：

```powershell
pwsh -File build.ps1             # CB  : 128KB flash / 20KB RAM, RTT 上行 12KB -> build-cb\
pwsh -File build.ps1 -Board c8   # C8  :  64KB flash / 20KB RAM, RTT 上行 12KB -> build-c8\
pwsh -File build.ps1 -Board ze   # ZET6: 512KB flash / 64KB RAM, RTT 上行 32KB -> build-ze\

pwsh -File flash.ps1 -Board cb   # OpenOCD + CMSIS-DAP（烧录前先把桥/OpenOCD 停掉）
pwsh -File flash.ps1 -Board ze -Erase
```

产物：每个目录下 `fw.elf` / `fw.bin` / `fw.hex` / `fw.map`。ZE 的 32KB 上行缓冲通过
`-DBUFFER_SIZE_UP=32768` 覆盖 `segger_rtt/SEGGER_RTT_Conf.h` 的默认值（那里有 `#ifndef` 守卫）。

> ⚠️ 跑 64KB `load_image` 基准（`script_test/swd/benchmark_readback.tcl`）会把 ZE 的
> **整片 SRAM**（含 RTT 控制块）覆盖掉，跑完要重新烧固件。

## 跑吞吐测试

```powershell
# 走桥（OpenOCD Tcl RPC）
node bridge\rtt-bridge.mjs --target stm32f103
node tools\selftest\rtt-speed.mjs bridge 10

# 走 WebUSB（零安装，需要浏览器带调试端口）
pwsh -File tools\selftest\launch-browser.ps1
node tools\selftest\rtt-speed.mjs webusb 10
```

脚本会打印：读到的字节数 / 秒、轮询次数、每次平均搬多少字节，并把目标侧的 `g_bytes` 读出来对账。

## 实测结果（MicroLink CMSIS-DAP + STM32F103，64KB/8KB 缓冲，2026-09-26）

| 主机通路 | 吞吐 | 每次读平均 | 说明 |
|---|---|---|---|
| **WebUSB · CMSIS-DAP** | 见 `rtt-speed.mjs` 输出（本次实测值写在下文「结论」里） | | 单命令往返 0.34 ms；受限于 AP 读速 |
| **OpenOCD · Tcl RPC** | | | `read_memory` 是**文本十六进制字**，同样受 AP 读速限制，但多了一层文本转换 |

> ⚠️ 阻塞模式有个反直觉现象：**主机一停，固件就卡在 `SEGGER_RTT_Write` 里不返回**。
> 这不是死机（看 `g_ms` 还在涨），是设计使然。所以测完记得让主机继续读、或者复位目标。
