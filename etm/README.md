# STM32H7B0 并行 ETM / Kingst LA 首次验证

**AI/命令行入口：**`python -X utf8 etm/trace.py --help`（在合并仓库根目录运行）。硬件复用网页的调试命令、烧录和 WebUSB 驱动，不依赖 pyOCD；支持断点起点联动采集和持久调试会话。
配置、真机采集、原生解码、查询与导出的完整流程见
[给AI使用的Trace CLI](../docs/trace-cli-ai.md)。CLI当前真机验收范围为H743，历史网页/固件说明保留在下文。

**网页初版已完成：**打开 **http://127.0.0.1:8766**；下次双击 `start-web.bat` 启动。

**当前稳定基准：CPU 70 MHz / TRACECLK 35 MHz v1。** 已重新烧录并完成 1 ms 复验与 10 ms 全地址业务窗口分析，恢复 484,570 条路径记录、132 次完整的 100 字节 SPI 发送；未发现 overflow 或包覆盖遗漏。固件、原始记录、完整解码与源码执行流已封存，见 [稳定基准说明](baselines/spi-70-35-v1/README.md)。保留 397 条未知条件执行状态和捕获边界，启动全过程尚未验收。频率优化已按用户要求暂停。

下次继续前先阅读 [交接摘要、待办及建议做法](docs/交接摘要_2026-10-11.md)，其中包含基准标识、关键文件、已知限制和可直接使用的接手指令。

**第三版已实现：**侧栏按调试/采集/时钟/文件分组；增加函数断点起点采集、历史导航与调用栈、长记录索引/分页和可信区段定位。真实验证从 `HAL_SPI_Transmit` 入口采集，首条恢复指令与断点地址一致；见 [第三版验收与使用说明](docs/执行流与界面第三版验收.md)。

当前界面启动时不预置或自动载入固件：在调试页“浏览载入 ELF”，手动选择函数起点后操作。已进一步压缩顶部工具区与底部调用栈，见 [界面调整说明](docs/第三版界面压缩与ELF流程调整.md)。历史记录可从记录列表打开，不会自动绑定到当前调试目标。

界面已对齐主工具的深色配色、系统字体、等宽代码字体、侧栏与控件样式，见 [主工具样式对齐](docs/主工具样式对齐.md)，为后续集成保持视觉一致。

已新增函数调用统计、可放大的计数火焰图、源码热点和完整性报告，见 [执行流分析第二版](docs/执行流分析第二版.md)。
已支持采集/停止、SWD 复位与 ETM 配置、OpenCSD 解码、指令与源码浏览，以及源码执行流导出。
网页不默认绑定固件或载入记录。调试目标需要浏览选择 ELF；已有 1/10 ms 记录可从记录列表打开。下方 8/4 MHz 示例与早期操作说明作为历史参考，旧 ELF 与当前板上固件不同。
当前使用方式与完整验收结果见 [网页初版](docs/网页初版.md)；以下内容保留早期固件与物理层验证说明。

目标：让 H7B0 自行配置并输出真实 ETMv4 指令 trace，用 Kingst LA 采集原始波形，再离线分析。
现已交付低频测试固件、采集工具、物理层分析、TPIU 解复用，以及网页中的 ETMv4 指令路径恢复和源码浏览。

## 1. 当前固件

- HSI 64 MHz / 8，CPU、HCLK、APB 均为标称 **8 MHz**，不使用外部晶振或 PLL。
- RCC 选择 HSI 同时作为 TRACEPORTCK 来源；TPIU 的物理 **TRACECLK 约 4 MHz**，数据双边沿传输，半周期约 125 ns。
- 四位并行 trace：`ETM → CSTF(S0) → ETF(硬件 FIFO) → TPIU → PE2~PE6`。
- ETMv4.0，trace ID = 2，配置寄存器 = 0，关闭返回栈压缩、周期计数和时间戳。
- 主循环 `trace_workload` 连续执行 8 次交替分支，分别调用 `trace_even` / `trace_odd`，并包含 1 kHz SysTick 中断。
- `-Og -g3 -gdwarf-4 -fno-inline -fno-optimize-sibling-calls`，保留机器码、函数边界和源码行映射。
- 代码位于内部 128 KiB Flash，诊断变量位于 AXI SRAM，栈位于 DTCM。

已完成编译与 ELF 静态检查，以及 **2026-10-11 真实采集、ETMv4 解码和源码路径验收**：500/100 MSa/s 均抓到约 4.008 MHz 时钟，TPIU 解帧得到 trace ID 2 数据流，随后恢复实际指令路径。物理采集证据见 [首次采集报告](docs/validation/2026-10-11-first-capture.md)，完整结果见 [网页初版](docs/网页初版.md)。

## 2. 接线

| LA 通道 | MCU 引脚 | 信号 |
|---|---|---|
| CH0 | PE2 | TRACECLK |
| CH1 | PE3 | TRACED0 |
| CH2 | PE4 | TRACED1 |
| CH3 | PE5 | TRACED2 |
| CH4 | PE6 | TRACED3 |
| GND | GND | 共地 |

引脚为 AF0、推挽、无上下拉。使用短线，PE2 尤其应避免长支线。板子上与 PE2~PE6 相连的其他输出不能同时驱动。
LA 阈值示例为 1.65 V（3.3 V IO）；若板子 IO 电压不同，请按实际电压调整。
SWD 的 PA13/PA14 与这些 trace 引脚分开，可以继续用于烧录。

## 3. 烧录

本次直接使用 [fw.elf](firmware/stm32h7b0_etm/fw.elf)，它包含后续分析需要的 DWARF。
另有 [fw.hex](firmware/stm32h7b0_etm/fw.hex) 和 [fw.bin](firmware/stm32h7b0_etm/fw.bin)。BIN 烧录地址为 `0x08000000`。

如需重新构建，在本目录运行：

```powershell
pwsh -NoProfile -File .\firmware\stm32h7b0_etm\build.ps1
python -X utf8 .\tools\check_firmware.py
```

你可以用现有烧录器写 ELF/HEX；也可使用随例程拷贝的 OpenOCD + CMSIS-DAP 脚本：

```powershell
pwsh -NoProfile -File .\firmware\stm32h7b0_etm\flash.ps1
```

固件烧录成功后应处于运行状态，BOOT0 为 0。trace 初始化前约有 100 ms 等待；正常运行无需调试器先配置 trace。
首次验证建议烧录后给目标板断电重启一次，清除先前调试会话可能留下的 CoreSight 状态。调试器应关闭自己的 trace 自动配置，以免覆盖本固件设置。

## 4. 首次采集

建议先使用 **100 MSa/s、五通道、10 ms、无触发条件**。4 MHz 时钟的半周期有约 12~13 个采样点，适合先检查稳定区；也可提升到 500 MSa/s。

在 KingstVIS 中可以手动采集，随后保存 `.kvdat` 并导出五通道跳变 CSV。不要使用 SWD 解码器解释这组信号。
若使用脚本，先打开 KingstVIS；Socket API 需在 `%LOCALAPPDATA%\kingst\vis.config` 中开启，修改后重启 KingstVIS：

```xml
<enaSocket>1</enaSocket>
<listenPort>23367</listenPort>
```

在 `E:\web-etm-trace` 中运行：

```powershell
python -X utf8 .\tools\la\kingst_la.py info
pwsh -NoProfile -File .\capture.ps1
# 提高采样率：
pwsh -NoProfile -File .\capture.ps1 -Rate 500000000 -Seconds 0.01
```

如果上次采集仍在等待触发，先结束它：

```powershell
python -X utf8 .\tools\la\kingst_la.py stop
```

`capture.ps1` 会保存到 `captures\trace-时间.csv`，同时尝试保存同名 `.kvdat`，再运行物理层分析。
如果只需导出 KingstVIS 当前缓冲区，避免重新采集：

```powershell
python -X utf8 .\tools\la\kingst_la.py export --channels 0 1 2 3 4 --out E:\web-etm-trace\captures\trace.csv
python -X utf8 .\tools\trace_port.py .\captures\trace.csv
```

## 5. 预期看到什么

- CH0 连续方波，标称 4 MHz，周期约 250 ns，高/低各约 125 ns。
- CH1~CH4 应有数据跳变；它们与 CH0 的双边沿相关，不能只采上升沿。
- TPIU 格式化包含同步、源 ID 与填充；看到规律性序列不一定是测试图案，也不代表 CPU 停住。
- `trace_port.py` 默认在每个半周期的 **1/4 处**取样，按 D0 到 D3 拼出 nibble，输出两个字节相位候选：`.phase0.bin`、`.phase1.bin`，以及 `.trace.json` 报告。实测数据跳变接近半周期中点，不能直接在中点取样；可用 `--sample-fraction` 调整并比较结果。
- 报告给出时钟频率、异常半周期、数据跳变数与 `FF FF FF 7F` 同步序列候选数。同步命中只作为线索，需后续 TPIU/ETM 解码确认数据有效性。

保留并提供**同一次采集的 CSV 和 KVData**；分析执行流时还要配套该次烧录的 ELF。
物理工具输出为 TPIU 字节流候选，可进一步运行新增的解复用工具。选择有效候选后运行（`phase0` 仅为本次采集的有效相位，重新采集可能变化）：

```powershell
python -X utf8 .\tools\tpiu_demux.py .\captures\trace.phase0.bin
```

输出 `.id2.bin` 为去除 TPIU 同步/帧包装后的 ETM 字节流，`.tpiu.json` 为帧与数据源统计；这一步尚未恢复指令执行流。采集开头未同步片段、尚未识别源 ID 的数据与结尾不完整帧不作为完整 trace 输出。

## 6. 无数据时排查

先检查目标运行、接线、共地和采集通道。只有 TRACECLK 而没有变化的数据时，用调试器读取 `g_error` 和 `g_trace`；当前构建的地址见 `firmware\stm32h7b0_etm\build\firmware-info.json`。

| 变量 | 正常值 / 含义 |
|---|---|
| `g_error` | 0；1/2/3 为时钟初始化失败；`0x100 + n` 为 trace 错误 n |
| `g_sysclk_hz` | 8000000（基于 RCC 寄存器计算，不是实测） |
| `g_round` | 持续增加 |
| `g_systick_count` | 大约每秒增加 1000 |
| `g_trace.stage` | 4 = 初始化与配置回读通过；不代表波形已通过验证 |
| `g_trace.etm_idr[1]` | 架构主版本字段 bits[11:8] 为 4 |
| `g_trace.etm_traceid` | 2 |
| `g_trace.etm_victl` | 0x201（always true + started） |
| `g_trace.tpiu_port_size` | 8，one-hot 的 bit3 表示四位 |
| `g_trace.funnel_ctrl` | 0x301，ETM 开、ITM 关 |
| `g_trace.etf_mode` / `etf_ctrl` | 2 / 1 |

trace 错误：1 解锁失败；2 ETM 电源未就绪；3 ETM 未进入 idle；4 TPIU flush/停止超时；5 ETF 未 ready；6 ETM 架构不是 v4；7 ETM 未退出编程稳定状态；8 配置回读不匹配。
所有等待都有上限。不要仅凭时钟方波就判定 ETM 链路成功。

## 7. 来源与验证

- 固件底座来自 `E:\web-serial-rtt-tools\tools\target-firmware\stm32h7b0_rtt_speed`，其原始 README/实测记录保存在 `docs\provenance`，它们不是本次 ETM 验收结果。
- `tools\la` 原样拷贝自 `E:\web-serial-rtt-tools\tools\la`；其中 `decode` 是 SWD 解码，ETM 物理分析入口为新增的 `tools\trace_port.py`。
- 底座中的 `segger_rtt` 源码保留作来源参考，本固件不编译也不调用 RTT。
- 时钟、寄存器与路由核对依据：[ST RM0455](https://www.st.com/resource/en/reference_manual/dm00463927.pdf)，Rev 13，RCC_CFGR 与第 64 章；[ST AN5307](https://www.st.com/resource/en/application_note/an5307-getting-started-with-stm32h7a37b3-line-and-stm32h7b0-value-line-microcontroller-hardware-development-stmicroelectronics.pdf) 的 trace 引脚表。
- Cortex-M7 的 OS Lock 不实现，trace 同步周期固定为 1024 B：[Arm ETM-M7 TRM](https://documentation-service.arm.com/static/5e906dfe8259fe2368e2ac54?token=)。
- 静态验证：启动向量、Flash/RAM 范围、DWARF 4、测试函数源码行映射及 BL 调用指令。
- 物理分析测试：已知字节流合成波形，覆盖初始时钟高/低、两种字节相位、数据延迟、恒高数据与时钟缺口。
- 中文 KingstVIS 的 GBK CSV、实测半周期中点附近的数据跳变、TPIU 辅助位恢复与旧/新数据源切换已加入离线测试。

运行离线分析测试：

```powershell
python -X utf8 -m unittest discover -s .\tools\tests -v
```

ELF 静态检查需要 `pyelftools`；采集与物理波形分析只使用 Python 标准库。

## SPI 复杂例程实测

原 App_SPI_H7B0 工程已复制到 `firmware/stm32h7b0_spi_trace/reference/`，工作版本适配 25 MHz HSE、LDO 和 H7B0 128 KiB Flash。

当前推荐 `fw-280-r8.elf`：**CPU 280 MHz / 物理 trace 35 MHz**，LA 500 MSa/s、取样位置 0.25。页面点击“查看 SPI 实测”会载入真实记录和匹配 ELF；1 ms 样本恢复 190,653 条路径记录，核对了 51 次完整的 100 字节发送。

IT 条件指令有 554 条状态未知，启动入口暂停尚未通过本板验收；页面明确显示这些限制。火焰图按路径记录数加权，不是耗时图。

- [SPI 固件说明](firmware/stm32h7b0_spi_trace/README.md)
- [实测、频率与配置分工](docs/SPI例程Trace验收与频率边界.md)
- [自包含实测记录](docs/validation/spi-280-35.etmrecord)
- [源码执行流](docs/validation/spi-280-35.execution.c)

最新：见 [断点流程与芯片预置第四版](docs/断点流程与芯片预置第四版.md)。调试→时钟→采集→文件；函数＋与源码行硬件断点；F1/F4/H7 型号组和 RCC 识别。完整 trace 保持 H7B0 70/35 MHz 实测范围。

WebUSB 故障修复与 2 ms 原始 USB 生产路径验收：见 [WebUSB控制写与FAULT修复](docs/WebUSB控制写与FAULT修复.md)。

源码目录 413 修复：见 [源码目录413修复](docs/源码目录413修复.md)。只读取 ELF 引用的配套源码，分批保存本地快照，采集请求使用快照编号。

源码处理已复用主工具 SourceStore：见 [复用SourceStore](docs/复用SourceStore.md)。目录授权、索引、匹配、读取和缓存统一，ETM 保留本地快照保存层。

局部执行区间、精简 C、离线双栏 HTML 与单一源码目录入口：见 [局部执行区间与简化导出](docs/局部执行区间与简化导出.md)。当前横轴为执行序号，尚非逐指令微秒时间。

界面细节：见 [界面细节收紧](docs/界面细节收紧.md)。记录与质量合并顶栏，区间控件保持整体，默认仅一个 Demo 与用户记录。
