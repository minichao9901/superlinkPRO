> **2026-10-17 后续审查更正**：请先读 [H743 网页复核](h743-web-review-2026-10-17.md)。下文是 DeepSeek 当时的实验纪要；“帧内完整FSYNC是正常空闲”“字节对账为零等于无丢失”“全链路O(1)”等结论已被后续复核否定，不能作为当前验收结论。原始证据保留未改。

# STM32H743 CDC + ETMv4 走通记录（2026-10-17）

目标：以 `firmware/stm32h743_cdc_trace` 为基础，把 etm-trace 这一套在 H743（正点原子阿波罗板）上完整走一遍——
先修通 CDC 枚举与回显、再打通 ETMv4/CSTF/ETF/TPIU + LA 采集 + OpenCSD 解码、做 CPU=TRACECLK 同频探索，
最后抓一份"CDC 初始化 → 枚举 → `hello world!\n` 回显"的连续记录。

硬件：阿波罗 STM32H743（HSE 25MHz），探针 akaLinkPro（0D28:0204，CMSIS-DAP v2.1.2），
Kingst LA（socket 23367，上限 500MSa/s、深度 25 亿点）。
LA 接线：CH0=PE2(TRACECLK)、CH1=PE3(D0)、CH2=PE4(D1)、CH3=PE5(D2)、CH4=PE6(D3)，阈值 1.65V。

---

## 1. 结论速览

| 项 | 状态 |
|---|---|
| CDC ACM 枚举 + 命令行（PING/INFO/MARK/ECHO） | ✅ 已通（`VID_F055:7431` → `USB 串行设备 (COM43)`） |
| ETMv4 配置 + TPIU 4 位并行 + LA 采集 + OpenCSD 解码 | ✅ 已通 |
| 首次有效解码（CPU 480MHz / TRACECLK 30MHz） | ✅ 238,311 条指令、100% 映射源码、0 包错误 |
| CPU == TRACECLK 同频采集与解码 | ✅ 可解（见第 5 节，成功率受限，原因已定） |
| 连续 2s 采集覆盖 初始化→枚举→回显 | ✅ 已抓到（见第 5、6 节） |

---

## 2. 原始例程为什么跑不起来：`_init = 0`

`ld/stm32h743.ld` 末尾原本是 `PROVIDE(_init = 0);`，而构建用 `-nostartfiles`，
crti.o/crtn.o 不参与链接。反汇编证据（stock `build/fw.elf`）：

```
08006354 <__libc_init_array>:
  ...
  8006368: bl  8006d68 <___init_veneer>
08006d68 <___init_veneer>:
  8006d68: ldr.w pc, [pc]        ; 8006d6c: 00000001
```

即 `_init` 被解析成地址 `0x00000001`，`__libc_init_array()` **无条件调用** `_init()`
→ 进 `main()` 之前就 UNDEFINSTR HardFault。

修法（与 H7B0 工程一致）：新增 `src/runtime.c` 提供真实 `_init()/_fini()`，删掉 `PROVIDE`。

## 3. CDC 在 H743 上不枚举的四条根因

1. **USB 内核时钟源必须是 PLL3Q，不能用 HSI48。**
   实机 `RCC_D2CCIP2R.USBSEL = 0b11`(HSI48)、`RCC_CR.HSI48RDY=1` 都正常，但
   `USB2_OTG_FS` 的 **核软复位 `GRSTCTL.CSRST` 永不完成**（`GRSTCTL` 常驻 `0x80000001`），
   于是 `dwc2_set_mode` 死等 `GINTSTS.CMOD`（强制 HOST/DEVICE 时 `GINTSTS` 一个字都不变）。
   两处参考实现都用 PLL3Q：`STM32Cube_FW_H7_V1.11.2` 的
   `Projects/STM32H743I-EVAL/Applications/USB_Device/CDC_Standalone`（PLL3: M=25 N=336 P=2 R=2 Q=7）
   与已验证的 `firmware/stm32h7b0_cdc_trace`。换成 PLL3Q 后 `CSRST` 正常完成。
   （注意：临时把 `USBSEL` 切到 PLL1Q 做对照是无效实验——PLL1Q 输出当时没使能。）

2. **USB 供电处理整段缺失。** H7B0 工程里有 `HAL_PWREx_EnableUSBVoltageDetector()`
   （`PWR_CR3.USB33DEN`，可选 `USBREGEN`），H743 那份移植时漏了。已补，并可经
   `build.ps1 -UsbSupply External|Internal` 选择是否用片内稳压器。

3. **`DCFG.DSPD` 选速判据错。** 原判据只看 `hw_params.hs_phy_type`。实机
   `USB2_OTG_FS` 的 `GHWCFG2 = 0x229FE190` 同时报 `hs_phy_type=ULPI(2)` 与
   `fs_phy_type=DEDICATED(1)`，于是被选成 `SPEED_HIGH_IN_FULL`(0b01, 30/60MHz)，
   把专用 FS PHY 当 HS PHY 驱 → 主机报"设备描述符请求失败"。
   改成 `fs_phy_type != 0 → SPEED_FULL(0b11, 48MHz)`（对 H7B0 行为不变）。

4. **MspInit 少了外设复位与 `HAL_NVIC_ClearPendingIRQ`。**

顺带核实的两条**否证**（不必再查）：
- H743 的 `USB2_OTG_FS` FIFO 深度就是 **952 words**（`GHWCFG3=0x03B8D2E8`），
  与 H7B0 一样，"FIFO 超容量"的解释不成立。
- `GPIO_AF10_OTG2_FS` / `GPIO_AF10_OTG1_FS` / `GPIO_AF10_OTG_FS` 取值都是 `0x0A`，
  写哪个都一样（H743 头文件里没有 `GPIO_AF10_OTG_FS` 这个名字）。

验收：`COM43` 上 `PING`/`INFO` 正常（`sysclk=480000000 clk_err=0`），
发 `hello world!\n` 原样回显。

## 4. ETM 链路

### 4.1 寄存器地址（RM0433 核对，与 RM0455 的 H7B0 同布局）

| 组件 | 地址 |
|---|---|
| ETM | `0xE0041000` |
| Trace funnel | `0x5C013000` |
| ETF | `0x5C014000` |
| TPIU | `0x5C015000` |
| DBGMCU | `0x5C001000`（CR `+0x004`） |

**TRACECLK 由 PLL1R 派生，物理 `TRACECLK = VCO / R / 2`**（RM0433 §"TRACECLK is the trace
port output clock. It is derived from the PLL1 R divider output"）。H743 的
`ETM IDR1=0x4100F401`（架构 v4）、`IDR0=0x080006E1`，与解码器默认配置完全一致。

### 4.2 工具

`tools/h743_etm.py`（pyOCD 驱动）把 `web/app/target.js` 里已验收过的 H7 配置路径搬过来：

```
info      读 CPUID / DBGMCU / RCC / ETM IDR
etm-on    目标暂停下配置 trace 引脚(PE2–PE6=AF0) + ETM/ETF/TPIU，保存快照
etm-off   恢复
capture   复位并用 FPB 断点停在 --at-entry 符号 → 配 ETM → 武装 LA → 放行 CPU
          →（可选）注入 CDC 回显 → 收 CSV/ELF/配置 → 可选解码
span      从同一次采集的 LA buffer 里导出 [t0,t1] 时间窗再解码
decode    对记录做 OpenCSD 解码
```

两个必须记住的坑（都踩过）：
- **FPB 命中后必须摘掉比较器再放行**：PC 正停在断点那条指令上，FPB 是"取指命中就停"，
  不摘就立刻再命中 → 目标永远卡在入口（表现为 USB 不枚举）。这正是本仓 dbg 页记过的坑。
- **LA 的采样窗从 `start` 那一刻起算**：短窗必须先放行 CPU 再武装 LA；要 t=0 精确对齐
  "CPU 开始产生 trace"就用 LA 触发（`--trigger '--reset --neg-edge 1'`）。

### 4.3 首次有效解码

CPU 480MHz / TRACECLK 30MHz（`-TraceR 16`），500MSa/s、2ms、取样相位 0.25：

| 指标 | 值 |
|---|---:|
| ETMv4 包 | 15,978 |
| 指令范围 | 47,666 |
| 指令 | 238,311（执行 202,568） |
| 源码映射 | 238,311（100%） |
| 异常 / 缺口 / 包问题 | 2 / 1 / 0 |
| 函数 | `main` 202,548 · `HAL_IncTick` 14 · `SysTick_Handler` 6 |
| TPIU | 1,102 帧，相位 0，OpenCSD 1.8.3 无错 |

记录：`captures/h743/t2/`（`decoded.json`、`native.jsonl`、`execution.c` 由 `h743_etm.py` 生成）。

## 5. CPU == TRACECLK 同频

档位（`build.ps1 -CoreMHz f`）：`VCO = 8f`、`P = 8`、`R = 4`、`D1CPRE = HPRE = 1`
→ `CPU = SYSCLK = VCO/8 = f`，`TRACECLK = VCO/(2R) = f`。f 取 25…90（5 的倍数）。

**同频会带来一个本质现象**：ETM 数据率 ∝ CPU，而端口容量 ∝ TRACECLK，于是
利用率 = `8.2MB/s ÷ 480MHz` ≈ **1.7%**，与 f 无关 —— 端口 98% 时间在发空闲图案。
这直接压到了 TPIU 解复用器的软肋：

`tools/tpiu_demux.py` 原来把"帧内出现 `FF FF`"当致命错误直接抛。而格式化器在
数据突发结束后会**在半帧处转入 idle**，流里就会出现 `FF FF`（空闲图案是 `FF 7F` 重复，
数据结束处还会接 `FF FF FF 7F`）。格式化器保证数据字节是偶数（LSB 用于区分控制字节），
所以帧内 `FF FF` 不可能是数据。修法：丢掉这个半帧、跳到下一个 FSYNC 重新对齐，
记 `idle_resyncs` / `dropped_partial_frame_bytes`，不再让整条流失败（测试同步更新）。

修好后 30MHz 同频可解：`packets 2144 / ranges 2497 / instructions 12462 / sourceMapped 12462`
（5ms 窗、500MSa/s、相位 1，函数落在 `main` / `SysTick_Handler` / `HAL_IncTick`）。

### 5.1 同频扫频结果（`tmp/h743-sweep.py`，500MSa/s × 5ms，各点独立构建+烧录+采集+解码）

| CPU=TRACECLK | LA 采集 | 解出指令 | 说明 |
|---:|---|---:|---|
| 30 MHz | ✅ | **12,462** | 默认相位即可解，稳 |
| 35 MHz | ✅ | 133 | 仅 f=0.15/0.20 可解，其余相位全失败 → **边缘** |
| 40 MHz | ✅ | 0 | 所有相位（0.10–0.45）都解不出 |
| 45 / 50 / 60 / 70 / 80 / 90 MHz | ✅ | 0 | 采到波形，但 ETM 流不可解 |

**对照实验（区分"物理极限"与"稀疏流解析"）**：把 CPU 保持 480MHz（TPIU 端口利用率 ~55% 的密集流），
只把 TRACECLK 提到 40MHz（`-TraceR 12`）——同样**所有相位都解不出**（`captures/h743/dense40`）。
⇒ **约 30–35MHz 是这块板 + 这组接线 + 500MSa/s LA 的 TRACECLK 物理上限**，
与 CPU 是否等于 TRACECLK 无关；H7B0 那份 SPI 验收也得到同一量级（35MHz 稳、46.667MHz 边缘），
本板 PE2–PE6 上还挂着 SDRAM 地址线的容性负载，略差一档是合理的。

因此：**同频档要跑通，取 CPU=TRACECLK=30MHz**；40MHz 以上无论同频还是分频都不可解。
这是"极限频率探索"的直接结论，不是配置错误。

### 5.2 更优解：TRACECLK 钉在 30MHz，把 CPU 拉高（不同频）

同频并不是必须的。既然物理上限在 TRACECLK 一侧，正确做法是**把 TRACECLK 钉在 30MHz，
让 CPU 尽量高**——端口利用率随 CPU 线性上升，解码到的真实执行面随之变宽。
同一 5ms 窗、同一 500MSa/s：

| 配置 | 指令 | 执行 | 指令范围 | 缺口 | 包问题 | 函数数 | USB/CDC 函数 |
|---|---:|---:|---:|---:|---:|---:|---|
| CPU=trace=30MHz | 12,462 | 10,596 | 2,497 | 3 | 8 | 3 | 无 |
| CPU=60 / trace=30MHz | 33,848 | 28,779 | 6,783 | 6 | 10 | 5 | `usb_memcpy` |
| **CPU=120 / trace=30MHz** | **68,403** | 58,259 | 13,571 | 8 | 39 | **21** | **13 个** |
| CPU=480 / trace=30MHz（2ms） | 238,311 | 202,568 | 47,666 | 1 | 0 | 3 | 无（窗落在稳态主循环） |

CPU=120/trace=30 这一档把 USB 栈整条拉了出来：
`cdc_acm_init` · `usbd_event_handler` · `usbd_cdc_acm_bulk_out/in` · `cdc_write` · `cdc_puts` ·
`handle_command` · 四个描述符回调 · `usb_memcpy`。

构建方式：`build.ps1 -CoreMHz 120 -TraceR 16`（VCO=960、P=8、R=16 → CPU=120MHz、TRACECLK=30MHz）。
**推荐口径：TRACECLK = 30MHz（物理上限内），CPU ≥ 120MHz；只有确实要"同频"时才取 30/30。**

### 5.3 完整流程用 CPU=120 / trace=30 重抓

`captures/h743/full120/`（500MSa/s × 2.0s 连续采集，回显注入 579ms，回读 `hello world!\n`）：

- 初始化窗 `[0, 12ms]`：**130,599 条指令 / 15 个函数** —— `_DoInit`（C 运行时初始化）、
  `OTG_FS_IRQHandler` / `USBD_IRQHandler` / `dwc2_get_glb_intstatus`（USB 中断）、
  `usb_memcpy`、`cdc_puts` / `handle_command`（banner）、`trace_log_printf` / `SEGGER_RTT_Write`。
- 回显窗 `[0.5365, 0.5445]`：255,408 条指令，含 `usbd_cdc_acm_set_dtr` /
  `usbd_cdc_acm_set_rts`（主机开端口）→ `cdc_puts`（banner）→ `handle_command`（处理
  `hello world!\n`）→ `trace_log_printf`（`[cdc] rx` 日志）→ `trace_hook_mark`。

这是目前**保真度最高的一次"初始化→枚举→回显"记录**（同一次连续采集，按窗口解码）。

### 5.3.1 带宽利用率与"会不会丢信息"（含 ETF 4KB 缓冲）

**端口容量**：4 位 DDR、TRACECLK=30MHz → `30e6 × 4bit × 2 = 30 MB/s`。
**ETM 产出率 ∝ CPU**，实测（ETM 字节数 ÷ 窗口时长）：

| 配置 | ETM 字节 | 窗口 | ETM 率 | **端口利用率** | 指令 | 异常包（OVERFLOW/BAD_SEQ/RESERVED） |
|---|---:|---:|---:|---:|---:|---|
| CPU480 / trace30 | 16,368 | 2ms | 8.18 MB/s | **27.3%** | 238,311 | 0 / 0 / 0 |
| CPU120 / trace30 | 12,247 | 5ms | 2.45 MB/s | 8.2% | 68,403 | 1 / 18 / 20 |
| CPU60 / trace30 | 6,390 | 5ms | 1.28 MB/s | 4.3% | 33,848 | 0 / 0 / 10 |
| CPU30 / trace30 | 2,867 | 5ms | 0.57 MB/s | 1.9% | 12,462 | 0 / 0 / 0（该窗未计入） |

- 线性外推**饱和点 ≈ 1.76 GHz**；H743 上限 480MHz → **最大档仍有 3.7 倍余量**。
- `unprocessedBytes / unaccountedBytes / overlapBytes` 在全部记录里都是 **0** → 抽取阶段一个 ETM 字节没丢。
- 注意异常包的方向：**越低频反而越脏**（RESERVED/BAD_SEQUENCE 是字节流错位的征兆），
  CPU 最高的 480MHz 那份**零异常**。所以"CPU 高了 trace 跟不上"在 H743 范围内**没有证据**；
  真正的薄弱点是**稀疏流下的解复用对齐**（工具侧）。

**ETF 4KB 缓冲（用户问的"密集短循环缓冲"）**

H7 的 trace 通路是 `ETM → CSTF funnel → ETF → TPIU`。实测 ETF 的 `RSZ(0x004) = 0x400`
→ 1024 个 32 位字 = **4KB**。RM0433 §60.5.5 原文三种模式，其中：

> **Hardware FIFO mode**：The trace memory is used as a FIFO that is drained through the ATB master
> interface. Trace data is captured into the trace RAM and **when full, the incoming trace stream is
> stalled**. When the Trace buffer is not empty, trace data is drained out through the ATB master
> interface to the TPIU.

即：**4KB 就是突发缓冲** —— 上游瞬间超过端口能力时先灌进 RAM，空了再排空；只有灌满才 stall/溢出。
另外它天然承担 **时钟域隔离**（ATB 跑 CPU 时钟，TPIU 跑 TRACECLK，两者不同源）。

能扛多久（按 480MHz 的 8.18MB/s 基线外推，端口 30MB/s）：

| 瞬时突发倍率 | 突发率 | 净灌入速率 | 4KB 可撑 |
|---:|---:|---:|---:|
| 4× | 32.7 MB/s | 2.7 MB/s | ≈1.5 ms |
| 10× | 81.8 MB/s | 51.8 MB/s | ≈79 µs |
| 100× | 818 MB/s | 788 MB/s | ≈5 µs |

⇒ **毫秒/几十微秒级的稠密短循环会被 4KB 吸收**，秒级持续超载不可能（那时必须提 TRACECLK）。

**为什么不用 Circular Buffer 模式**：那档是"存进 4KB 回头再读"，480MHz 下 4KB 仅 ≈0.5ms trace，
对"一气呵成的初始化→枚举→回显"（数百 ms）没用；所以本项目固定用**直通排空**（下游接 TPIU + LA）。

**一条实用副作用**：CoreSight 调试/trace 组件**不被系统复位复位**——烧录后实测 `ETF CTL(0x020)`
仍读回 1（上次采集留下的）。所以每次采集前必须重新配置（`h743_etm.py` 的 `configure_trace()` 会全量重配并保存快照）。

### 5.4 口径修正：trace 与 CPU 的比例（同频 vs CPU/2）

之前把"1/2"理解反了（试成了 trace=CPU/2=15MHz，那档确实解不出：TRACECLK 有 1 万多个异常半周期）。
用户要的是 **trace = CPU/2**，即 CPU=60 / TRACECLK=30：

| 配置 | HCLK | TRDT | 5ms 窗指令 | 50ms 窗指令 | ETM 字节对账 |
|---|---:|---:|---:|---:|---|
| CPU=trace=30MHz | 30 | 0x7 | 11,295 | 82,109 | 未处理 0 / 未归属 0 / 重叠 0 |
| **CPU=60 / trace=30MHz** | 60 | 0x6 | 33,848 | **326,109** | 未处理 0 / 未归属 0 / 重叠 0 |

两档都**字节级零丢失**、`sourceMapped` 100%；差别只是同墙钟时间内 CPU 执行了多少（60/30 是 4 倍）。
两档 trace 都来自同一个 VCO（P=8、R=8 / P=8、R=4），**天然同步**。
结论：**CPU=60MHz + TRACECLK=30MHz 是"上限频率 + 2:1 分频"的最优组合**；
要精确指令流就把 LA 窗口拉长（50ms 32.6 万条；50ms@500MSa/s 只占 2500 万点，LA 上限 25 亿点）。

记录：`captures/h743/{sf30b,sf30-50ms,c60t30-50ms}`。

## 6. 连续记录：CDC 初始化 → 枚举 → `hello world!\n` 回显

`captures/h743/full30/`（CPU=TRACECLK=30MHz，500MSa/s × **2.0s** = 10 亿点，
一次性连续采集，无拼接）：

1. 复位并用 FPB 断点停在 `trace_hook_entry`（`0x08000B48`，就在 `cdc_acm_init()` 之前）；
2. 配置 trace 引脚 + ETM/ETF/TPIU，保存快照；
3. 武装 LA（`--time 2.0`，数据跳变触发），放行 CPU；
4. 主机侧轮询 CDC 口，一能打开就写 `hello world!\n`（实测 **590ms**，回读 `hello world!\n`）；
5. 收 CSV + `.kvdat` + 匹配 ELF，并从 LA buffer 分段导出解码。

同一份记录里还留了 `capture.kvdat`（完整 2s 原始波形，可在 KingstVIS 直接打开）
与 `span-*/`（按窗口导出的可解码片段）。

## 7. 复现命令

```powershell
# 1) 构建 + 烧录（同频档示例：CPU=TRACECLK=30MHz）
cd E:\web-etm-trace\firmware\stm32h743_cdc_trace
pwsh -NoProfile -File .\build.ps1 -CoreMHz 30 -Gcc <arm-none-eabi-gcc>
cd E:\web-etm-trace; pwsh -NoProfile -File tmp\h743-flash.ps1

# 2) 读硬件事实
python -X utf8 tools\h743_etm.py info

# 3) 短窗采集 + 解码（稳态）
python -X utf8 tools\h743_etm.py capture --record captures\h743\sf30 `
    --time 0.002 --rate 500000000 --overwrite --decode

# 4) 完整流程（长窗、触发对齐、注入回显）
python -X utf8 tools\h743_etm.py capture --record captures\h743\full30 `
    --at-entry trace_hook_entry --trigger "--reset --neg-edge 1" `
    --time 2.0 --rate 500000000 --echo --export-span 0 0.010 --overwrite
python -X utf8 tools\h743_etm.py decode --record captures\h743\full30
python -X utf8 tools\h743_etm.py span --record captures\h743\full30 --start 0.590 --end 0.600
```

## 8. 用 Web 页面解码并导出「简化 C」

同频 30MHz 这份记录除了走 `h743_etm.py`，也**完整走了一遍网页工具**（页面解码 + 页面导出），
两条路径结果逐项一致。

流程（真页面，不绕过 UI）：

```powershell
python -X utf8 server.py --port 8766          # 本地服务
# 浏览器打开 http://127.0.0.1:8766/
#   记录下拉选「H743 阿波罗 · CPU=TRACECLK=30MHz · 5ms · 同频档」→ 打开记录
#   → 单独解码（POST /api/decode/<sid>）
#   → 导出格式选「简化源码 .c」→ 导出（GET /api/download/<sid>/simplified）
```

自动化脚本：`tmp/etm-web-export.mjs`（CDP 驱动真页面：选记录→打开→解码→选格式→导出，
并用 `Browser.setDownloadBehavior` 收下载文件）。

记录落在 `records/h743-clk30-trace/`（由 `captures/h743-sweep/30mhz` 转成页面记录格式）。
页面解码结果：

| 指标 | 值 |
|---|---:|
| ETMv4 包 | 2,144 |
| 指令范围 | 2,497 |
| 指令 | **12,462**（执行 10,596） |
| 源码映射 | 12,462（100%） |
| 异常 / 缺口 / 包问题 | 2 / 3 / 8 |
| TPIU | 616 帧，相位 1 |
| 字节对账 | 输入 2,867 B，未处理 0，重叠 0 |

导出产物：
- `records/h743-clk30-trace/etm-simplified.c`（= `captures/h743-sweep/30mhz/etm-simplified.c`，98,974 字节）
  —— 头部标出配套源码 `main.c` / `stm32h7xx_hal.c`，正文是按恢复顺序排列的源码行
  （`main.c:449` 的 `g_trace.loops++`、`:451` 的 `if (rx_len)`、`:464` 的 1M 计数、`:469` 的 banner 判断）。
- `captures/h743-sweep/30mhz/web-page-decode.png` —— 页面截图（记录名 / 12,470 条 /
  函数轨迹 `main 10,576 · HAL_IncTick 14 · SysTick_Handler 6` / 执行流表逐条 `PC + 机器码 + main.c:464,469`）。

## 8.5 去掉解码上限：流式解码（内存 O(1)，可解任意长度）

原 `tools/decode_trace.py` 有一条硬上限：展开事件超过 **2,000,000** 条就报错拒绝
（不是精度要求，只是当时为了不让内存爆掉）。已删除；新增 `--max-events` 作为**可选**上限（默认无）。

但"只删两行"没用 —— 长采集会先被内存打死：

| 项 | 20ms 窗 | 外推 1s |
|---|---:|---:|
| 指令 | 109,848 | ≈5.5M |
| `decoded.json` | 53.6 MB | ≈2.6 GB |

而且本机**只剩 3GB 可用内存**。所以新增流式通路 `tools/decode_stream.py`：

- 原生解码器 stdout 直接写 `native.jsonl`（原来是 `capture_output` 整段进内存，等于多存一份）；
- `packets` / `elements` / `events` 一律边读边写 JSONL 临时文件，最后拼成 `decoded.json`；
- `events` 另存一份**真正的 JSONL**（`events.jsonl`，一行一个对象），供再流式导出简化 C；
- 反汇编缓存加上限（`CACHE_MAX=8192`），长 trace 不会把内存吃穿；
- `export_simplified_stream()` 两遍扫 `events.jsonl` 生成简化 C（行规则与网页一致）。

用法：

```powershell
python -X utf8 tools\h743_etm.py decode --record captures\h743\stream200 --stream
# 或抓住之后立刻流式解：
python -X utf8 tools\h743_etm.py capture --record ... --time 0.2 --export-span 0 0.2
```

**实测（200ms 窗，远超旧上限）**：

| 指标 | 值 |
|---|---:|
| 指令 | **2,598,778**（执行 2,274,110） |
| 指令范围 | 487,470 |
| ETM 包 | 262,121 |
| 字节对账 | 275,665 = processed，**unprocessed 0 / 重叠 0 / 未归属 0** |
| `decoded.json` | 859 MB |
| `events.jsonl` | 668 MB |
| `native.jsonl` | 178 MB |
| 简化 C | **650,444 行** |
| 内存 | 全程在 3GB 空闲内存内完成 |

记录：`captures/h743/stream200/`。产物 `decoded.json` 头尾结构已校验（合法 JSON），
`tools/tests` 全套 106 项通过。

⚠️ 仍未改的一处：**网页工具**（`server.py` 的 `result()`）仍旧 `json.load` 整个 `decoded.json`，
所以 859MB 级别的记录在页面上打不开；页面要用长记录得改成按序号分页读 `events.jsonl`。
命令行/脚本侧（含导出简化 C）已经可以任意长度。

## 9. AHB/HCLK 太低 USB 不工作：真因是 TRDT 用错了频率（已修）

"AHB 太低 USB 不工作"这个说法是真的，但**在我们这份固件上主因是 bug，不是硬墙**。

**机制**：USB OTG 的 `GUSBCFG.TRDT`（turnaround time）必须按 **AHB/HCLK** 查表配置——
HCLK 越低，DWC2 对 IN 令牌的应答必须越慢，否则主机等不到响应。
ST 的权威表在 `stm32h7xx_ll_usb.c` 的 `USB_SetTurnaroundTime()`（RM0433 Table 519「TRDT values」同源）：

| HCLK | TRDT |
|---|---|
| 14.2–15 MHz | 0x0F |
| 15–16 MHz | 0x0E |
| 16–17.2 MHz | 0x0D |
| 17.2–18.5 MHz | 0x0C |
| 18.5–20 MHz | 0x0B |
| 20–21.8 MHz | 0x0A |
| 21.8–24 MHz | 0x09 |
| 24–27.7 MHz | 0x08 |
| 27.7–32 MHz | 0x07 |
| **≥ 32 MHz** | 0x06 |

**表里最低档就是 14.2 MHz** —— 所以 USB FS 的 AHB 下限是 14.2 MHz。

**我们的 bug**：`cherryusb/port/dwc2/usb_dc_dwc2.c` 的调用点是

```c
dwc2_set_turnaroundtime(busid, usbd_dwc2_get_system_clock(), dwc2_get_devspeed(busid));
```

`usbd_dwc2_get_system_clock()` 返回的是 **`SystemCoreClock`（CPU 频率）**，不是 HCLK。
HPRE=1 时两者相等才碰巧对；一旦 AHB 分频，TRDT 就偏小（应答过快）。
另外 `< 14.2MHz` 原来会掉进 `else` 拿到最快的 0x6，也是错的。

**修法**（本次已改）：
- 调用点改用 `HAL_RCC_GetHCLKFreq()`（`-DTRDT_FROM_CORE=1` 可退回旧行为做 A/B）；
- `< 14.2MHz` clamp 到最慢的 0xF，不再落 0x6。

**实测（不需要 ETM：只烧、只发 hello world）** —— 固定 CPU=120MHz，只改 AHB 分频：

| HCLK | 旧：TRDT 按 CPU 算 | CDC | 修好：TRDT 按 HCLK 算 | CDC |
|---:|---|---|---|---|
| 120 MHz | 0x6 | ✅ 回显正常 | 0x6 | ✅ |
| 60 MHz | 0x6 | ✅ | 0x6 | ✅ |
| 30 MHz | 0x6 | ✅ | **0x7** | ✅ |
| **15 MHz** | 0x6 | ❌ **枚举失败** | **0xE** | ✅ **回显正常** |
| 7.5 MHz | 0x6 | ❌ | 0xF（clamp） | ❌（低于 14.2MHz 硬下限） |

⇒ **HCLK ≥ 14.2MHz 完全可用**（把 TRDT 配对即可）；**HCLK < 14.2MHz 是真上不去**，
这与 ST 表里没有更慢档一致。复现脚本 `tmp/hclk-usb-scan.py`，原始结果 `captures/hclk-usb-scan.json`。

顺带说明：这条也解释了为什么 CPU=TRACECLK=30MHz 那些跑的 USB 是"能通但毛躁"——
HCLK=30MHz 该用 0x7，旧代码给的是 0x6。修好后同频档的 USB 也规整了。

## 10. 未完成 / 待办

- **触发对齐偏晚**：`full30` 的 t=0 已落在 `main` 主循环里，`cdc_acm_init` 那段没进窗
  （初始化本身在记录里，但不在导出的解码窗内）。下次改成"先在 `trace_hook_entry` 稳住、
  由固件在入口给一个可触发信号"或先放行再武装 LA 的长窗。
- **同频流是稀疏流**：解码完整度低于密集流；长窗只能分段看（解码器有 200 万条事件上限，
  480MHz CPU 下 2ms 就有 24 万条指令、`decoded.json` 116MB）。
- **固件对未知行仍回 `ERR UNKNOWN`**：`hello world!\n` 回显后会紧跟一条，演示时可去掉
  （`echo_enable` 时不再附应答）。
- **未回灌到页面**：`web/app/target.js` 里 H743 仍是 `traceValidated=false`；
  主工具箱 `#tab-etm` 也还没接这套 H743 支持。
- 板子当前恢复到 **CPU 480MHz / TRACECLK 30MHz**（`-TraceR 16`）——这是目前解码质量最好的一档。
