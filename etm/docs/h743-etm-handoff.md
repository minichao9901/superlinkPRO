> **2026-10-17 后续审查更正**：请先读 [H743 网页复核](h743-web-review-2026-10-17.md)。下文是 DeepSeek 当时的实验纪要；“帧内完整FSYNC是正常空闲”“字节对账为零等于无丢失”“全链路O(1)”等结论已被后续复核否定，不能作为当前验收结论。原始证据保留未改。

# H743 CDC + ETMv4 交接文档

> 写给接手的 AI/人。看完这份就能知道**当前状态、东西在哪、结论是什么、接下来做什么**。
> 配套详细报告：**`docs/h743-cdc-etm-bringup.md`**（§1–§9，含每条结论的证据）。
> 最后更新：2026-10-17 · 分支 `h743-dsh-etm` · HEAD `d021083`

---

## 0. 一句话状态

STM32H743（阿波罗板）上：**CDC ACM 已修通并枚举**（COM43），**ETMv4 + CSTF + ETF + TPIU 4 位并行 + Kingst LA + OpenCSD 解码全链路已打通**，
能抓"复位 → 初始化 → 枚举 → `hello world!` 回显"的连续记录并导出「简化 C」。
**解码器已去掉 200 万条上限，新增流式解码（内存 O(1)）**。
**未完成**：网页端打开超长记录仍是 O(全量)（三处待改，见 §6 P0）。

板子当前运行：**CPU=60MHz / TRACECLK=30MHz / USB 时钟 PLL3Q / TRDT 按 HCLK**（`g_trace.sysclk=60000000`、`ready=1`）。

---

## 1. 环境与硬件（物理事实，别再问）

| 项 | 值 |
|---|---|
| 板子 | 正点原子阿波罗 STM32H743IIT6，HSE **25MHz**，2MB flash |
| 探针 | akaLinkPro `0D28:0204`，序列号 `8F538633E6AEC4CA8ABB6A36FEBB1AC2`，CMSIS-DAP 2.1.2，探针自带 CDC = **COM34** |
| 目标 CDC | **COM43**（`VID_F055 / PID_7431`，USB 串行设备） |
| LA | Kingst（KingstVIS），socket **23367**；最高 **500MSa/s**；深度上限 **2.5e9 样本**（500MSa/s → 5s） |
| LA 接线 | `CH0=PE2=TRACECLK`, `CH1=PE3=D0`, `CH2=PE4=D1`, `CH3=PE5=D2`, `CH4=PE6=D3`，阈值 1.65V |
| 工具链 | `E:/Share/env-windows/tools/gnu_gcc/arm_gcc/mingw/bin/arm-none-eabi-gcc.exe` |
| 调试 | pyOCD 0.45.1（**主用**）、OpenOCD（备用）；探针在 pyOCD 里报 `Board ID 8F53 is not recognized`（无害） |
| 本机内存 | 总 14.8GB，**空闲只有 ~3GB** ← 决定了解码必须流式，别再写"整段读进内存"的代码 |
| 仓库 | `E:\web-etm-trace`，分支 **`h743-dsh-etm`** |
| 硬件锁 | `E:\web-etm-trace-coordination\hardware.lock\owner.json`，owner `dsh-session-4d688a58`（本轮一直持有；交班时按协议释放或转交） |
| 网页工具 | `python server.py --port 8766`；测试浏览器 CDP `9340`；页面记录在 `records/h743-clk30-trace/` |

---

## 2. 提交历史（本分支，只列本轮新增）

```
d021083 feat(decode): 去掉 200 万条上限，新增流式解码（内存 O(1)，可解任意长度）
de5da72 docs(h743): 补 5.4 同频 vs CPU/2 口径修正与实测
858aa97 fix(tpiu): 帧中间的 FSYNC 直接跳过，不再丢半帧
0e3e850 fix(h743): USB TRDT 必须按 HCLK 查表（原来按 SystemCoreClock/CPU 频率）
55caafa docs(h743): TRACECLK 钉 30MHz + 拉高 CPU 的更优口径（含 60/120MHz 实测对比）
3f1c0d0 docs(h743): 补网页工具解码与「简化 C」导出流程
119735d docs(h743): CDC+ETMv4 走通记录与同频扫频结论
b4ba64b fix(h743): 打通 CDC ACK 枚举与 ETMv4 trace 全链路   ← 固件四修 + 工具落地
```
基线：`6b2b98e`。

---

## 3. 脚本清单（路径 · 作用 · 怎么跑）

### 3.1 主入口

| 文件 | 作用 |
|---|---|
| **`tools/h743_etm.py`** | ★ 唯一主入口。子命令：`info` / `etm-on` / `etm-off` / `capture` / `span` / `decode` |
| `tools/decode_stream.py` | ★ 流式解码（`decode_stream` / `decode_capture_stream` / `export_simplified_stream`） |
| `tools/decode_trace.py` | 常规（一次性）解码；**200 万条上限已删**，改为可选 `max_events` |
| `tools/tpiu_demux.py` | TPIU 4 位流解复用（本轮改了两处，见 §5.6） |
| `tools/trace_port.py` | LA CSV → 采样相位 → nibble → 字节流 |
| `tools/source_export.py` | 简化 C / 简化 HTML 的行规则（网页与 CLI 共用） |
| `tools/record_store.py` | 网页的 SQLite 浏览索引（**O(全量) 加载，见 §6 P0**） |
| `server.py` | 网页后端（`/api/decode`、`/api/download/<sid>/simplified`、`/api/browse/<sid>/page` …） |

常用命令：

```powershell
# 读硬件事实（CPUID / DBGMCU / RCC / ETM IDR）
python -X utf8 tools\h743_etm.py info

# 短窗采集 + 解码（稳态）
python -X utf8 tools\h743_etm.py capture --record captures\h743\tXXX `
    --time 0.005 --rate 500000000 --overwrite --decode

# 完整流程：复位停在入口 → 配 ETM → 武装 LA（触发对齐）→ 放行 → 连续 echo → 收 CSV
python -X utf8 tools\h743_etm.py capture --record captures\h743\fullXXX `
    --at-entry trace_hook_entry --trigger "--reset --neg-edge 1" `
    --time 1.0 --rate 500000000 --echo --echo-repeat 300 --echo-interval 0.004 `
    --export-span 0 0.020 --overwrite

# 从同一次采集的 LA buffer 里再导一个时间窗并解码
python -X utf8 tools\h743_etm.py span --record captures\h743\fullXXX --start 0.60 --end 0.62

# 流式解码（任意长度，内存 O(1)），同时导出简化 C
python -X utf8 tools\h743_etm.py decode --record captures\h743\stream200 --stream
```

### 3.2 辅助脚本（本轮从 `tmp/` 收进仓库，原来被 gitignore）

| 文件 | 作用 |
|---|---|
| `tools/h743/h743-flash.ps1` | OpenOCD 烧录 + verify（AIRCR 软复位 + 屏蔽 NVIC） |
| `tools/h743/ocd.ps1` | 通用 OpenOCD 读寄存器/内存（多条命令用 `;;` 分隔） |
| `tools/h743/h743-sweep.py` | CPU=TRACECLK 同频扫频驱动（构建→烧录→采集→解码，写 `captures/h743-sweep/sweep.json`） |
| `tools/h743/h743-try-phases.py` | 对同一份采试试不同取样相位，看能否解出 |
| `tools/h743/h743-find-echo.py` | 在 LA 采集里扫时间窗找含 CDC 回显的窗（**注意 §7 第 6 条**） |
| `tools/h743/h743-demux-diag.py` | 解复用诊断（FSYNC 位置、失败偏移、各 id 字节数） |
| `tools/h743/h743-compare.py` | 多份记录的配置横向对比表 |
| `tools/h743/h743-phase-scan.py` | 扫 0.05–0.95 取样比例，报 TPIU 解复用是否有效 |
| `tools/h743/hclk-usb-scan.py` | 固定 CPU、扫 AHB 分频，验 CDC 能否枚举 + 回显（TRDT A/B） |
| `tools/h743/etm-web-export.mjs` | CDP 驱动**真网页**：选记录→打开→单独解码→选「简化源码 .c」→导出 |
| `tools/h743/etm-web-shot.mjs` | 给正在显示解码结果的页面截图（**不设 deviceMetricsOverride**） |
| `tools/h743/h743-entry-test.py` / `h743-reset-diag.py` | 入口断点/复位诊断（早期排查用，保留） |

### 3.3 固件

`firmware/stm32h743_cdc_trace/`：

```powershell
pwsh -NoProfile -File .\build.ps1 -CoreMHz 60 -TraceR 8 -AhbDiv 1 -Trdt Hclk `
     -Gcc 'E:/Share/env-windows/tools/gnu_gcc/arm_gcc/mingw/bin/arm-none-eabi-gcc.exe'
cd E:\web-etm-trace; pwsh -NoProfile -File tools\h743\h743-flash.ps1
```

参数：
- `-CoreMHz`：480 / 240 / 120 / 100 / 90 / 80 / 70 / 60 / 50 / 45 / 40 / 35 / 30 / 25（走 480 档之外的都按 `VCO=8f, P=8, R=-TraceR`）
- `-TraceR`：PLL1R 分频（`TRACECLK = VCO/(2R)`）
- `-AhbDiv`：1/2/4/8/16 → HCLK = SYSCLK/div（测"AHB 太低 USB 不工作"用）
- `-Trdt`：`Hclk`（正确，默认）/ `Core`（旧行为，A/B 用）
- `-UsbSupply`：`External`（默认）/ `Internal`（片内 USB 稳压器）

---

## 4. 记录清单（`captures/`，**未提交**，都在本机）

`captures/h743/`（约 10GB）：

| 目录 | 配置 | 窗口 | 关键结果 / 用途 |
|---|---|---|---|
| `t2` | CPU480 / trace30 | 2ms | ★ 首次有效解码：238,311 条指令、100% 映射、0 包问题 |
| `sf30b` | CPU30=trace30 | 5ms | 同频：11,295 条，字节对账全 0（证明不丢数据） |
| `sf30-50ms` | CPU30=trace30 | 50ms | 同频长窗：82,109 条 |
| `c60t30-50ms` | CPU60 / trace30 | 50ms | ★ 326,109 条、字节零丢失 |
| `cpu60-trace30` / `cpu120-trace30` | CPU60/120 · trace30 | 5ms | 5ms 窗对比（33,848 / 68,403 条；120 档把 USB 栈全解出来） |
| `full60` | CPU60 / trace30 | 1.0s 采集，CSV 只导了 [0,20ms] | 初始化窗 109,848 条 + `etm-simplified.c`（873KB） |
| `full60b` | CPU60 / trace30 | 1.0s 采集，CSV 只导了 [0.60,0.62] | ★ **回显窗** 235,023 条 + `etm-simplified-echo.c`（2.3MB），含完整 bulk_out→handle_command→cdc_write |
| `full120` | CPU120 / trace30 | 1.0s 采集 | 初始化窗 130,599 条 / 15 函数；回显窗 255,408 条 |
| `stream200` | CPU60 / trace30 | **200ms 整段** | ★ **2,598,778 条**（破旧上限）、859MB decoded.json、简化 C 650,444 行 |
| `dense40` | CPU480 / trace40 | 2ms | 对照：密集流 40MHz 也解不出 → 证明 40MHz 是 **TRACECLK 物理上限** |
| `trdt-Core` / `trdt-Hclk` | CPU120 · HCLK30 | 5ms | TRDT A/B：39,723 vs 36,703 → 与 trace 质量无关 |
| `sf30-tr15` | CPU30 / trace15 | 50ms | 反方向实验（trace=CPU/2=15MHz）：**解不出**，异常半周期 1 万多 |
| `full30` / `full35` / `full35t` / `sf30` / `sf35` / `t1` / `full` | — | — | 过程记录，可删 |

`captures/h743-sweep/`：同频扫频 30/35/40/45/50/60/70/80/90 各一份 + `sweep.json`（结论见 §5.3）。
`captures/hclk-usb-scan.json`：AHB 分频 × TRDT 的 CDC 扫描结果。

> ⚠️ `captures/` 被 gitignore（体积太大）。要交接给别的机器必须**另外拷贝**，或重跑上面的命令重建。

---

## 5. 已确认的结论（都有数据，别再重复验证）

### 5.1 CDC 在 H743 上不枚举的四条根因（已修）

1. **`ld/stm32h743.ld` 的 `PROVIDE(_init = 0)`** 被 newlib 真调用：`__libc_init_array → ___init_veneer → ldr.w pc,[pc]` 装载 **0x00000001** → 进 main 前 UNDEFINSTR HardFault。修法：补 `src/runtime.c` 提供真 `_init/_fini`。
2. **USB 内核时钟必须 PLL3Q，不能 HSI48**。HSI48 下 `USBSEL=3`、`HSI48RDY=1` 都正常，但 `GRSTCTL.CSRST` **永不完成**，`dwc2_set_mode` 死等 `GINTSTS.CMOD`。
3. **USB 供电处理整段缺失**：补 `HAL_PWREx_EnableUSBVoltageDetector()`（+ 可选 `HAL_PWREx_EnableUSBReg()`）。
4. **`DCFG.DSPD` 选速判据错**：实机 `GHWCFG2=0x229FE190` 同时报 ULPI(2) 与 DEDICATED(1)，旧判据选成 0b01 把专用 FS PHY 当 HS PHY 驱 → 主机报"设备描述符请求失败"。改成 `fs_phy_type!=0 → SPEED_FULL(0b11, 48MHz)`。

否证（不必再查）：`USB2_OTG_FS` FIFO 就是 **952 words**；三个 OTG AF 宏取值都是 `0x0A`。

### 5.2 USB TRDT 必须按 HCLK（已修）

`cherryusb/port/dwc2/usb_dc_dwc2.c` 原来传 `usbd_dwc2_get_system_clock()`（= **SystemCoreClock = CPU 频率**），
TRDT 必须按 **AHB/HCLK** 查 ST 表（`stm32h7xx_ll_usb.c` 的 `USB_SetTurnaroundTime`，RM0433 Table 519）。

| HCLK | 旧（按 CPU） | CDC | 修好（按 HCLK） | CDC |
|---:|---|---|---|---|
| 120/60 MHz | 0x6 | ✅ | 0x6 | ✅ |
| 30 MHz | 0x6 | ✅ | **0x7** | ✅ |
| **15 MHz** | 0x6 | ❌ | **0xE** | ✅ |
| 7.5 MHz | 0x6 | ❌ | 0xF | ❌（**<14.2MHz 是硬下限**，ST 表最低档就是 14.2–15MHz） |

### 5.3 频率：TRACECLK 上限 30–35MHz，CPU 越高越好

| 配置 | 5ms 窗指令 | 50ms 窗指令 | 字节对账 |
|---|---:|---:|---|
| CPU=trace=30MHz | 11,295 | 82,109 | 全 0（零丢失） |
| **CPU=60 / trace=30MHz** | 33,848 | **326,109** | 全 0 |
| CPU=120 / trace=30MHz | 68,403 | — | 全 0 |
| CPU480 / trace30（2ms） | 238,311 | — | 全 0 |

- 同频扫频：30MHz ✅ / 35MHz ⚠️边缘（仅 f=0.15/0.20 解出） / **40–90MHz ❌**
- 对照 `dense40`（CPU480 + trace40，密集流）**也全相位失败** → 40MHz 是**这块板+接线+500MSa/s LA 的 TRACECLK 物理上限**，与是否同频无关
- **推荐口径：TRACECLK 钉 30MHz，CPU 尽量高（≥60，120 更好）**；只有必须同频时才用 30/30

### 5.4 "稀疏流"不是问题

CPU=TRACECLK 时端口利用率只有 ~1.7%，但**字节对账 `unprocessedBytes=0 / unaccountedBytes=0 / overlapBytes=0`** ——
ETM 字节一个没丢；同墙钟内指令少只是 CPU 执行得少。**要精确执行流就拉长窗口，不要怀疑解码**。

### 5.5 流式解码（破 200 万上限）

`tools/decode_stream.py`：原生输出直接落盘、events/packets/elements 边写 JSONL、反汇编缓存有上限。
实测 200ms：**2,598,778 条指令 / 字节零丢失 / decoded.json 859MB / events.jsonl 668MB / 简化 C 650,444 行**，全程在 3GB 空闲内存内完成。

### 5.6 `tpiu_demux.py` 两处真 bug（已修）

1. 帧内出现 `FF FF` 原来直接抛异常 → 改为"丢半帧 + 跳到下一个 FSYNC 重对齐"（格式化器会在半帧处转 idle）。
2. **帧内出现完整 FSYNC 原来是错误** → 现在**直接跳过 4 字节继续收集本帧**（数据字节恒为偶数，`FF FF FF 7F` 不可能是数据）。新增 `mid_frame_fsyncs` 计数。

---

## 6. 待解决的问题（按优先级，含具体做法）

### P0 · 网页打开超长记录仍是 O(全量)（三处，改完才叫"任意长度不卡"）

分页设施**已经存在**：`tools/record_store.py` 是 SQLite 浏览索引，页面已调 `browse/<sid>/page`（`web/app/history.js:24`）。
卡在三个地方：

1. **建索引时把整个 `decoded.json` 读进内存**
   `server.py:141-142` → `RecordStore(dir, lambda: result(sid))`；`tools/record_store.py:34` 的 `r=self.loader()`。
   **改法**：索引从 `events.jsonl` **流式**逐行 `INSERT`（`executemany` 分批），内存 O(1)。
2. **每个请求都重算文件哈希**
   `server.py:96 verify_record_inputs()` 每次请求重算 `capture.csv`(476MB)+`firmware.elf` 的 sha256 → 单次秒级。
   **改法**：按 `(mtime, size)` 缓存，文件没变不重算。
3. **events 重复落盘**
   `decoded.json` 里已经含 events，`events.jsonl` 又存一份（长记录多占 ~2/3 磁盘）。
   **改法**：流式模式下 `decoded.json` 只写 `summary/quality/sources/status`，events 只落 `events.jsonl`（`streamed.eventsFile` 已指向它）。

改完后的真实边界（**不是内存**）：解码线性（200ms≈2–3 分钟，1s≈10–15 分钟）、首次建索引线性（2.6M 条约 1–2 分钟）、磁盘 ~2GB/200ms。
页面每页从此 O(150 行)，与长度无关。

### P1 · 长记录的"整段 CSV"没留下

`full60`/`full60b`/`full120` 都只导了某个时间窗的 CSV（`--export-span`），LA buffer 已被后续采集覆盖，
只剩 `capture.kvdat`（完整 1s 波形，可在 KingstVIS 打开）。
**要整段解码必须重新采集**（`--export-span` 不给，CSV 会是 GB 级）。

### P2 · 回显窗定位靠"密集重复"而不是时间对齐

单次 echo 的 CPU 活动只有 **7–10µs**，用毫秒窗去扫命中率 ≈4%（本轮踩过）。
现在用 `--echo-repeat 300 --echo-interval 0.004` 解决。
**更好的做法**：`meta.json` 里已记 `echo.writes`（每次写入相对时刻），
但 LA 触发点与主机时间线之间有漂移（实测 39ms 级、每次不同），所以仍需一个共同锚点。
候选：用 `cdc_puts`（banner）或 `trace_hook_mark` 的出现位置反推偏移。

### P3 · H743 还没回灌网页

- `web/app/target.js` 里 H743 的 `traceValidated` 还是 false
- 主工具箱 `#tab-etm` 还没接这套 H743 支持
- `records/h743-clk30-trace/`（用 30MHz 同频记录做的页面记录，已导出 `etm-simplified.c` + 截图）在 `records/` 下，**未提交**

### P4 · 小问题

- 固件对未知行会回一条 `ERR UNKNOWN`（`hello world!` 回显后紧跟），演示时想去掉
- LA 触发点与"CPU 开始产生 trace"的对齐偏晚（`full30` 的 t=0 已落在 main 主循环）

---

## 7. 硬约束与踩过的坑（别重新踩）

1. **FPB 命中断点后必须摘掉比较器再放行**：PC 停在断点上，FPB 是取指命中就停，不摘立刻再命中 → 目标永远卡在入口（现象 = USB 不枚举）。
2. **LA 的采样窗从 `start` 那一刻起算**：短窗必须先放行 CPU 再武装；要 t=0 对齐"开始产出 trace"就用 `--trigger '--reset --neg-edge 1'`。
3. **LA 子进程要加 `-u`**，否则 stdout 块缓冲，看不到"开始采集"会误判卡死。
4. **`export-data` 用绝对路径**（相对路径会被 LA 服务端 NAK）。
5. **`h743_etm.py` 的 echo 必须走 `--echo-repeat`**（见 §6 P2）。
6. **`events.jsonl` 必须是真 JSONL（一行一个对象）**：我第一次写成逗号分隔整段，导致 `export_simplified_stream` 读出 0 行。
7. **PowerShell 里别在命令行拼含中文/引号的多行字符串**（会被 sh 吃掉，本轮坏过两次提交和一次文档插入）→ 用文件传（`git commit -F`、`Set-Content` + python 脚本）。
8. **本机只有 ~3GB 空闲内存**：任何"整段读进内存"的写法都会炸。
9. pyOCD 打印 `Board ID 8F53 is not recognized` 是**正常**的；`0x20003000` 处是 `g_trace` 魔数 `'TRC4'` + sysclk + ready。
10. 板子状态自检：`pwsh tools/h743/ocd.ps1 -Cmds 'mdw 0x20003000 3'` → 期望 `34524354 <sysclk> 00000001`。

---

## 8. 当前板子状态 / 一键盘点

```powershell
# 板子还在跑什么
pwsh -NoProfile -File tools\h743\ocd.ps1 -Cmds 'mdw 0x20003000 3'
#   34524354 03938700 00000001  → CPU=60MHz, ready=1

# CDC 在不在
python -c "from serial.tools import list_ports; print([(p.device,p.vid,p.pid) for p in list_ports.comports()])"
#   期望含 ('COM43', 0xF055, 0x7431)

# ETM 寄存器是否可读
python -X utf8 tools\h743_etm.py info
```

恢复/重抓一份干净记录（约 3 分钟）：

```powershell
cd E:\web-etm-trace\firmware\stm32h743_cdc_trace
pwsh -NoProfile -File .\build.ps1 -CoreMHz 60 -TraceR 8 -Gcc 'E:/Share/env-windows/tools/gnu_gcc/arm_gcc/mingw/bin/arm-none-eabi-gcc.exe'
cd E:\web-etm-trace; pwsh -NoProfile -File tools\h743\h743-flash.ps1
python -X utf8 tools\h743_etm.py capture --record captures\h743\handoff --time 0.05 `
    --rate 500000000 --overwrite --decode
```
