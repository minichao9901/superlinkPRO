# 原作者速度数字的复现与解读（含 F103ZET6 狂发例程）

日期：2026-09-28 ｜ 分支 `main` ｜ 相关提交：`adab10e`（双口径测速）、本文件同批（ZE 构建支持）

## 1. 那张截图是什么

原作者（akkako）的测速截图：

```
Adapter Request Speed: 20000 kHz
Passed Rounds        : 10 / 10
Write Avg Speed      : 3645.959 KiB/s
Read  Avg Speed      : 3045.310 KiB/s
```

截图对应的脚本**就在本仓库里**，不是别处的工具：

| 痕迹 | 位置 |
| --- | --- |
| 测速脚本 | **`script_test/swd/benchmark_readback.tcl`**（akako 提交 `82ec7dd`/`b3c4bb6`） |
| 包装脚本 | `script_test/swd/run_benchmark.py <speed_khz> <iterations>` |
| JTAG 版 | `script_test/swd/benchmark_readback_jtag.tcl` |
| 测试数据 | `script_test/swd/test1.bin`、`test2.bin`（各 **65536 B**） |

方法：`target/stm32f1x.cfg`（STM32F103）、SRAM `0x20000000`、**64 KB × 10 轮**，
每轮 `load_image` → `dump_image` → **host 侧逐字节比对**；**速度值取 OpenOCD 自己
打印的 `(N KiB/s)`**（即"纯传输"口径，不含 telnet 往返），不是脚本掐表。

## 2. 物理核对：20 MHz 跑不出 3.6 MB/s

SWD 是单比特线。一次 32 位**写** = 8 bit 请求 + 4 bit ACK + 32 bit 数据 + 校验
≈ **46 bit** ⇒ 20 MHz 的写上限只有 ≈ **1.74 MB/s**（读还要加 turnaround，更低）。
截图是 3.73 MB/s，**超上限 2.1 倍**。

原因：**`adapter speed` 只是档位选择器，不是频率生成器。**

```c
// DAP.c:57  Set_Clock_Delay()：映射到 6 个固定速率引擎
if      (clock >= 60000000) SWD_DynamicLoad_60M();
else if (clock >= 45000000) SWD_DynamicLoad_45M();
else if (clock >= 36000000) SWD_DynamicLoad_36M();
else if (clock >= 30000000) SWD_DynamicLoad_30M();
else if (clock >= 20000000) SWD_DynamicLoad_20M();
else                        SWD_DynamicLoad_Slow();   // swd_speed_calc() 延时环

// DAP.c:468  ×10 加速（HID CMD_SET_CONFIG byte[5]，默认 0）
if (g_param.clock_accel_mode != 0U) clock *= 10U;     // 20000 → 200000 → 60M 引擎
```

⇒ 截图里"请求 20000 kHz"配 3.6 MB/s，只可能是 **`clock_accel_mode` 开着**
（20000×10 顶到 60M 引擎），或当时请求的就是更高档。

## 3. 复现：F103ZET6 + 他本人的脚本，64 KB

本机高速探针（HPM5301EVKLite 移植版）跑同一份脚本：

* 目标换成 **F103ZET6**（512 KB flash / **64 KB SRAM**，`DBGMCU_IDCODE=0x10036414`），
  `file_size = 65536` 正好铺满 SRAM。
* **踩坑**：HPM SDK 自带的 OpenOCD 的 tcl 树**没有 `target/` 目录**，原作者脚本里的
  `[find target/stm32f1x.cfg]` 会报 `Can't find target/stm32f1x.cfg`。补一条搜索路径即可
  （用本地 OpenOCD 源码树，v0.12.0）：

```powershell
openocd.exe -s E:\sdk_env_v1.11.0\tools\openocd\tcl `
            -s E:\web-serial-rtt-tools\tmp\openocd-src\tcl `
            -f .bench_60000.tcl
```

（把 `benchmark_readback.tcl` 里的 `adapter speed N` 换掉就是各档位的临时脚本，
`run_benchmark.py` 也是这么做的。）

| `adapter speed` | 实际引擎 | 写 (KiB/s) | 读 (KiB/s) | 通过 |
| --- | --- | --- | --- | --- |
| 20000 | **20M** | 1487.4 | 1399.8 | 10/10 |
| 60000 | **60M** | 3471.4 / 3475.1 | 2958.1 / 2967.0 | 10/10 |
| 截图 "20000" | （只能是 60M） | 3645.959 | 3045.310 | 10/10 |

**结论：**

1. **本机在 60M 引擎下复现到 3471 / 2958，与他 3646 / 3045 只差 4.8% / 2.9%**
   （差异来自目标板型号与主频、测量噪声）。⇒ **我们的探针速度和原作者一样**，
   不存在"他有什么黑科技"。
2. 字面 20 MHz 只有 **1487 / 1400**，比他截图低 **2.45 倍** ⇒ 截图那个数**不可能**
   来自 20M 引擎，只能是 ×10 加速（或等价的更高档请求）。
3. 之前"我们比他差一大截"的印象，一半来自**口径**（见 §4），一半来自把"请求频率"
   当成了"实际频率"。

> 小坑：跑完 `rtt_probe_bridge.py` 之后立刻跑这套脚本，第一次可能报
> `CMSIS-DAP command mismatch. Sent 0x0 received 0x11`（探针 IN 端点里残留一条上
> 一手的响应），**重跑一次即可**，不是固件问题。

## 4. 口径：墙钟 vs OpenOCD 内部计时

我们原来的 `make sram-test` 是**自己掐表**：每条 `load_image`/`dump_image` 都要经一次
telnet 往返（实测 ~1 ms），在 20 KB 量级上吃掉 **~19%**。OpenOCD 自己打印的是
**纯传输**口径。`sram_speed_test.py` 现在两者都报（`ocd_speed()`）：

| SWD 请求 | 墙钟 写 / 读 | 纯传输 写 / 读 |
| --- | --- | --- |
| 10 MHz | 803 / 762 | 840 / 803 |
| 20 MHz | 1374 / 1267 | 1468 / 1392 |
| 36 MHz | 2134 / 1944 | 2376 / 2173 |
| 45 MHz | 2366 / 2191 | 2769 / 2504 |
| 60 MHz | 2859 / 2506 | **3386 / 2941** |

60 MHz 档纯传输读 **2941 KB/s = in-probe 链路天花板（3281）的 90%**；写 3386
甚至高于 in-probe 的**读**基准 —— 因为写没有 turnaround。⇒ 主机驱动这条路
**已经基本贴着链路天花板**，之前那个"还剩 24%"是量出来的假象。

## 5. F103ZET6 狂发（RTT flood）例程

`script_test/stm32f103_rtt_speed/` 现在一块源码支持三块板，**每块板一个独立输出
目录，互不覆盖**（两套固件可以同时躺在目录里，随时烧任意一块）：

```powershell
cd script_test\stm32f103_rtt_speed
pwsh -File build.ps1 -Board cb -Clean   # CB  : 128KB flash / 20KB RAM, RTT 上行 12KB -> build-cb\
pwsh -File build.ps1 -Board c8          # C8  :  64KB flash / 20KB RAM, RTT 上行 12KB -> build-c8\
pwsh -File build.ps1 -Board ze          # ZET6: 512KB flash / 64KB RAM, RTT 上行 32KB -> build-ze\

pwsh -File flash.ps1 -Board cb          # 烧录（OpenOCD + CMSIS-DAP 探针）
pwsh -File flash.ps1 -Board ze -Erase
```

* 新增 `ld/stm32f103cb.ld`（128KB flash）与 `ld/stm32f103ze.ld`（512KB flash / 64KB RAM）；
  `SEGGER_RTT_Conf.h` 的 `BUFFER_SIZE_UP` 加了 `#ifndef` 守卫，ZE 用
  `-DBUFFER_SIZE_UP=32768`（实测 `bss` 12652 → 33132）。
* 实测两套 `.bin` 均为 1052 B，内容不同（RTT 控制块里的缓冲长度常量不同），
  MD5 分别是 `17c92b3a…`（cb）与 `9f1bb36b…`（ze）。
* 烧录用本仓库的配置（`flash.ps1` 里写死的 ESP-IDF OpenOCD 已随 ESP-IDF 卸载，
  现在默认走 sdk_env 那份 + `script_test/openocd_stm32f1_swd.cfg`）：

```powershell
openocd.exe -s <sdk>\tools\openocd\tcl -f script_test\openocd_stm32f1_swd.cfg `
  -c "init" -c "adapter speed 2000" -c "reset halt" `
  -c "program {…/build-ze/fw.elf} verify reset" -c "exit"
```

* 实测（96 MHz HCLK，HSE 8 MHz ×12）：`RCC_CFGR=0x0029A50A` 确认 PLL×12；
  `g_ms` 1 s 涨 1005；`g_bytes` 灌满 32 KB 后阻塞不动（BLOCK_IF_FIFO_FULL，
  预期行为）。探针桥交付 **2486.2 KB/s 无损**（8.06 s，0 gap、0 丢字节、0 重复），
  与 C8 在 45 MHz 档的 2484.6 KB/s 持平 ⇒ **瓶颈在探针/USB 侧，不在目标**；
  缓冲从 12 KB 加到 32 KB 也没有变化。

> ⚠️ 跑 64 KB `load_image` 基准会把**整片 SRAM**（含 RTT 控制块与所有变量）覆盖掉，
> 跑完要重新烧固件；目标被留在 halted 状态。

## 6. 附：H743（正点原子阿波罗）实测与一个未解之谜

同一块探针、同一档（60000 kHz）、同样 64 KB、数据哈希校验通过：

| 目标 | 内存 | 写 (KiB/s) | 读 (KiB/s) |
| --- | --- | --- | --- |
| **H743** | AXI SRAM `0x24000000` | 3421.9 | 2685.9 |
| F103ZET6 | SRAM `0x20000000` | 3471.4 | 2958.1 |
| 原作者截图 | — | 3645.959 | 3045.310 |

⇒ **探针的 SWD 性能与目标无关**（H743 只低 1.5% / 9%），"AXI SRAM 经 AHB-AP 慢"
这个猜想可以排除。基准方法：`openocd -s <tcl> -f script_test/openocd_stm32h7_swd.cfg \
-c "init" -c "adapter speed 60000" -c "halt" -c "load_image … 0x24000000 bin" \
-c "dump_image … 0x24000000 65536"`，再比对哈希。

### 但 RTT 交付只有 ~720 KB/s（未解）

`rtt_h743_bridge.py`（**与 F103 同一个 RTT→CDC 桥**，同一块探针、同一段主机读法）
扫了四档，**完全不随 SWD 时钟变化**：

| SWD 档 | 20 MHz | 36 MHz | 45 MHz | 60 MHz |
| --- | --- | --- | --- | --- |
| H743 交付 | 676.9 | 723.4 | 717.1 | 717.5 KB/s |
| （对照）F103ZET6 | — | — | — | **2486 KB/s** |

已排除的解释（都有实测）：

1. **不是链路/SWD 侧**：四档曲线是平的，链路还有大量余量（同档 in-probe 一直能跑 3 MB/s）。
2. **不是 AXI SRAM 本身慢**：CPU 停住时同一块内存 64 KB 读 2686 KB/s（见上表）。
   注意该值由 `rtt_h743_bridge.py` 的 `fw_ram.elf` 全 RAM 版测的，**测试时 I/D cache 是关的**。
3. **不是取指争用**：全 RAM 版代码就在 AXI SRAM 上，I-cache 关着 → M7 取指与探针调试读
   抢同一块内存 —— 这个假设**看着很美，实测被否**：`H743_CCR=0x20000` 只开 I-cache 后，
   四档数字与关掉时**完全相同**（675.9/724.0/717.2/717.8 vs 676.9/723.4/717.1/717.5）。
4. **不是配置差异**：H743 的 `SEGGER_RTT_Conf.h` 与 F103 逐行相同（12 KB、BLOCK_IF_FIFO_FULL、
   空 LOCK/UNLOCK）。

数字本身指向**每次搬运的固定开销**：~1840 B/次、391 次/秒 ⇒ **2.56 ms/次**（F103 是
~0.8 ms/次），且与 SWD 时钟无关 ⇒ 瓶颈在"每次搬运"而不是"每字节"。下一步的判别实验
（还没做）：用桥的 **discard 模式**（`CMD_RTT` action 7 的 discard=1，只轮询不推 CDC）
读桥自己的 `drained` 计数器 —— 若能到 2.5 MB/s 就是 CDC/USB 那一段的问题；
若仍是 720 KB/s，则是探针轮询这个目标的 RTT 本身就慢（下一步看 CB 的 Flags/RdOff 语义
或 H7 的 cache-line 对齐缓冲是否让轮询失效）。

> 工具已就位：`rtt_h743_bridge.py` 现在支持 `python rtt_h743_bridge.py <COM> <秒>`、
> 环境变量 `H743_CCR`（CCR 值，默认 0=关 cache）、`H743_NO_LOAD=1`（跳过载入
> `fw_ram.elf`，用于板上已有 flash 版固件时）。
> 另注：**H743 的 flash 版烧不进去**（`flash write algorithm aborted by target`，
> sdk_env 的 OpenOCD + 最小 cfg 下 H7 flash 算法跑不起来）—— 这正是当初做"全 RAM 版"
> 的原因，所以"代码在 flash、缓冲在 AXI SRAM"那种布局目前无法在本机验证。

### J-Link 交叉验证（含 ⚠️ 一次被推翻的结论，2026-09-28）

> **修正记录**：本节初版结论是"J-Link 也 ~700 KB/s ⇒ 目标侧天花板"。**该推论已作废。**
> 随后用 J-Link 自己的 **CLI**（`JLink.exe` 的 `loadbin`/`savebin`，同样 CPU halt 条件）
> 实测：读 4×512 KB @50 MHz = **691.7 KB/s**、@4 MHz = **293.8 KB/s**，写也是 ~697 KB/s
> —— 读写同值、随链路速率变化且明显饱和 ⇒ **~700 KB/s 是 J-Link 工具侧（CLI 每块一次
> USB 往返）的天花板，不是目标的**。所以 J-Link 的 RTT 数字（692.9）同样被它自己的
> ~700 卡住，**不能当作"目标侧天花板"的证据**。

换上 **SEGGER J-Link**（自家参考实现）+ `JLinkRTTLogger.exe`，同一块 H743、同一个
狂发固件（此时已用 J-Link 把 **flash 版**烧好，代码在 flash、CB+栈在 AXI SRAM）：

| 取数工具 | SWD 速率 | RTT 交付 |
| --- | --- | --- |
| J-Link + JLinkRTTLogger | 4 MHz | 290.5 KB/s |
| J-Link + JLinkRTTLogger | **50 MHz** | **692.9 KB/s**（logger 自报 686~709） |
| **本探针的 RTT→CDC 桥** | 45 MHz | 676.9 ~ 724.4 KB/s |
| （对照）本探针桥测 **F103ZET6** | 45 MHz | **2486 KB/s** |

**修正后的结论：**

1. **同一块 H743、同样 CPU halt，本探针读 AXI SRAM 是 2685.9 KB/s，J-Link CLI 只有
   691.7 KB/s —— 探针快 3.9 倍。** ⇒ 目标侧没有 700 KB/s 的墙；J-Link 的 ~700 是它
   自己的工具侧上限（4 MHz→293.8、50 MHz→691.7，往返受限且饱和）。
2. 因此 J-Link 的 RTT 数字（692.9 KB/s）**不能**用来证明目标侧天花板，上一版推论作废。
3. 仍然成立的：**本探针在等效路径上不输 J-Link** —— OpenOCD 路径 2685.9 vs 691.7；
   RTT 路径 45 MHz 下 676.9~724.4 vs J-Link 50 MHz 下 692.9。
4. **已解（2026-09-28 晚）：本探针的 RTT 桥在 H743 上只有 ~700 KB/s，是因为目标当时
   跑在复位默认的 HSI 64 MHz。** 用厂商 `Stm32_Clock_Init` 序列把 H743 顶到
   **480 MHz** 后（`script_test/stm32h743_rtt_speed`，见源码注释里的两个坑），
   探针桥交付变成 **1379.6 / 2162.3 / 2486.6 / 2931.2 KB/s**（20/36/45/60 MHz），
   **与 F103@96 MHz 完全一致** ⇒ 瓶颈换成了探针自己，目标侧不再是。
   同时 J-Link 在同目标同主频下只有 **1473 KB/s**（400 与 480 MHz 都是 1473，说明
   那是 J-Link 自己的上限）⇒ 探针是它的 **2 倍**。

> 顺带修好了板子：sdk_env 的 OpenOCD 烧不进去的 H743 flash，**用 J-Link 一次成功**
> （`loadbin fw.bin, 0x08000000` + `verifybin` ⇒ `O.K.` / `Verify successful`），
> 比全 RAM 版更适合反复测试（**复位-proof**：全 RAM 版被任何一次 `connect`/复位冲掉）。
> J-Link 用法备忘：
> ```
> device STM32H743VI / si SWD / speed 4000 / connect
> loadbin "<...>/fw.bin", 0x08000000
> verifybin "<...>/fw.bin", 0x08000000
> r / go / exit
> ```
> RTT 测速：
> ```
> JLinkRTTLogger.exe -Device STM32H743VI -If SWD -Speed 50000 \
>   -RTTSearchRanges "0x24000000 0x80000" -RTTChannel 0 rtt.txt
> ```
> （它在控制台周期性打印 `Transfer rate: … KB/s Data written: … MB`，直接读那一行即可；
> **别先用 `JLink.exe` 的 `connect` 探测** —— `connect` 默认"复位+halt"，会把全 RAM 版冲掉。）
