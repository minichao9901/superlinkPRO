# ETM 周期计数的用途（参考 SEGGER Ozone）——实现与真机验证（2026-10-18）

## 一句话

开了 ETMv4 的**周期计数**后，记录里终于能按"**周期 / 时间**"回答"谁慢、慢在哪"：
函数自身周期与占比、CPI、覆盖时长（µs）、热点行的周期；而不是只能看"指令条数"。

## 数据链路（关键认知：周期计数是**独立元素**）

OpenCSD 把周期计数作为**独立元素**抛出，不是挂在指令区间上：

```c
/* decoder/include/opencsd/trc_gen_elem_types.h */
OCSD_GEN_TRC_ELEM_CYCLE_COUNT,  /* = 12，语义：自上次周期计数以来、前一个指令区间的周期数 */
```

所以链路是：

```
ETMv4 TRCCONFIGR.CC(bit4)=1 + TRCCCCTLR(≥256)
   → 解码器输出 type=12 元素（每 256 周期一个）
   → decode_trace / decode_stream **回填到前一个区间的最后一条指令**
       · 列表路径（decode_trace）：直接改 events[-1]
       · 流式路径（decode_stream）：把区间末指令"挂起"（pending_tail），等周期元素到了再一起发出
   → 事件上就有 cycleCount（求和 = 总周期），归因到该指令所属函数
   → flow_analysis 聚合：selfCycles / totalCycles / cyclePercent / cpi / flame 的 valueCycles、selfCycles
   → 页面：指标行 + 函数表（自身周期 / 占比 / 指令 / CPI / 实际调用）+ 热点行周期
```

## 改了什么

| 文件 | 改动 |
|---|---|
| `etm/tools/decode_trace.py` | 处理 type=12 元素，**回填**给上一区间末指令（`cycleCount` 累加、`cycleSpan`=区间长度） |
| `etm/tools/decode_stream.py` | 同上，但用 `pending_tail` 挂起末指令（流式不能回头改已写出的行）；流结束补发 |
| `etm/tools/flow_analysis.py` | `analyze_calls` 增加周期聚合：每函数 `selfCycles/cyclePercent/cpi`、`timing` 段（totalCycles/cycleRanges/cpi/functions 排序）、火焰图节点 `selfCycles/valueCycles`、热点行 `cycles`；`enrich` 把 `coreHz` 与 `coveredSeconds` 写进 `timing`；无周期数据时 `timeFlameAvailable=false` 且给出原因 |
| `etm/web/app/history.js` | 指标行追加 `N 周期 · CPI x.xx · 覆盖 xx µs`；「函数」视图在周期可用时改为 `函数/自身周期/占比/指令/CPI/实际调用` 并**按周期降序**；「热点」视图加周期列 |
| `etm/tools/tests/test_flow_cycles.py` | **6 个单测**：自身/总周期与占比、CPI、按周期排序、热点周期、火焰图周期、**无周期数据必须降级** |

## 真机验证（H743 + akaLinkPro + KingstVIS，全程走网页 ETM 页）

```
采集：CC-only（只勾周期计数）→ decodeStatus=done
TRCCONFIGR 回读 = 0x11（bit0 RES1 + bit4 CC）✓
```

| 指标 | 实测 |
|---|---|
| 周期元素（type 12） | **2,126** 条，单区间最大 401 周期 |
| 总周期 | **569,211** |
| CPI | **3.12** |
| 覆盖时长（coreHz 60 MHz） | **9,486.9 µs** |
| 事件/包 | 210,706 路径 · 17,601 包 |

按自身周期排序（页面截图：`tmp/shot-cycles.png`）：

| 函数 | 自身周期 | 占比 | 指令 | CPI | 实际调用 |
|---|---|---|---|---|---|
| `main` | 498,043 | 87.5% | 160,588 | 3.10 | 0 |
| `_svfprintf_r` | 16,244 | 2.9% | 7,880 | 2.06 | 35 |
| `probe_stage` | 7,604 | 1.3% | 5,644 | 1.35 | 2 |
| `_printf_i` | 6,016 | 1.1% | 3,569 | 1.69 | 36 |
| `memmove` | 5,941 | 1.0% | 5,351 | 1.11 | 186 |
| `SEGGER_RTT_WriteNoLock` | 3,689 | 0.6% | 700 | **5.27** | 35 |
| `dwc2_low_level_init` | 276 | 0.0% | 16 | **17.25** | 1 |

> 这类"指令少但 CPI 高"的行就是 Ozone 里最有用的信号：`dwc2_low_level_init` 16 条指令花 276 周期（等硬件/延时），
> 而 `memmove` 5351 条只用 5941 周期（CPI 1.11，接近理想）。

## 怎么开

ETM 页 → 采集前勾选 **「周期计数」**（F103/ETMv3 上该勾选框是禁用的，硬件不支持）。

## 已知限制（诚实记录）

1. **当前建议只开周期计数**：三者（分支广播 + 返回栈 + 周期计数）**同时开**时，250 Mbit/s 左右的码流在
   **500 MSa/s（≈2.08 样本/位）**下边沿密度过高，TPIU 字节相位定不出来：
   ```
   不能唯一确定 TPIU 字节相位：0 个候选 … 非法源 ID 0x7f（物理偏移 16032）
   ```
   实测这条 LA 的 API 报的采样率上限就是 **500 MSa/s**（`get-supported-sample-rate` 原样返回
   `500000000 … 20000`），而 KingstVIS 界面里那个 1/2/5/10 GSa 下拉是**通用列表**，不代表本机可用。
   **只开 CC** 时同样的设置稳定解码（本文的数据就是这样拿到的）。
2. **归因粒度**：周期元素对应的是"一个指令区间"，回填到区间末指令。若某区间**跨越函数边界**
   （极少见），该区间周期会算到末指令所在函数 → 极端情况有偏移。函数级总量在常规代码上准确。
3. **不是逐指令精确周期**：区间内各指令共享这段周期，页面不假装能分摊到每条指令
   （这是 ETM cycle-count 的语义边界，Ozone 同样只在区间/累加层面使用）。

## 测试

```
python -m unittest tools.tests.test_flow_cycles     → 6/6 OK
python -m unittest discover -s tools/tests           → 125 项里 120 通过
   （5 项失败全部是 test_real_* 需要未迁移的样本记录/captures，属 D-2 裁剪的已知缺口）
python tools/dev/test-etm.py                        → 4/4 PASS
node tools/selftest/etm-page-copy.test.mjs          → 42/0
```

## 追加：周期计数的三个"用起来"（2026-10-18 上午）

### 1) 周期加权火焰图（Ozone 口径）

调用路径火焰图新增 **「切换到周期 / 切换到记录数」** 按钮：

* 宽度 = 该节点累计的 **周期数**（`valueCycles`），而不是指令记录数（`value`）；
* 节点 title 同时给出"包含 N 条记录、M 周期（自身 K 周期）"；
* 没有周期数据（F103 / 未启用）时按钮**禁用**并写明原因，绝不假装能按周期画。

实测（H743 CC-only 记录）切换前后同屏对比：

| 口径 | 标签 | 前 5 个方块宽度 |
|---|---|---|
| 周期 | `所选执行区间 · 569,211 周期` | `999 / 998.1 / 998.1 / **106.1** / **102**` |
| 记录数 | `所选执行区间 · 210,706 条` | `999 / 998.2 / 998.2 / **202.2** / **181.5**` |

差别正是有用的信息：叶子函数"指令多但周期少"（`memmove` 类）在周期口径下会明显收窄。

### 2) 所选区间的周期也参与聚合（窗口分析）

`record_store.window()` 原先只统计记录数，现在同样累加周期：
`analysis.timing = {totalCycles, cycleRanges, cpi, functions[], timeFlameAvailable, basis}`，
火焰图节点与函数表都带 `selfCycles/cyclePercent/totalCycles/cpi`，
**窗口口径与整记录口径对得上**（实测同一记录：窗口与记录级都是 **569,211 周期 / CPI 3.12**）。

> 实现细节：事件表 `events.data` 里本来就存了完整事件 JSON（含 `cycleCount`），
> 所以窗口聚合**不需要改表结构**，直接从 `data` 读。

### 3) 长窗口聚合（多切片）

`etm/tools/agg_cycles.py`：把多份 CC 记录按**切片逐行 + 跨切片合并**给出分布：

```bash
python -X utf8 etm/tools/agg_cycles.py etm/records            # 全部 CC 记录
python -X utf8 etm/tools/agg_cycles.py etm/records --limit 6  # 最近 6 份
```

输出三段：总周期/平均 CPI/总覆盖时长 → 每切片一行（时间轴）→ 按自身周期排序的函数表
（含"出现在几份切片里"）。**没有周期数据的记录单独列出、不混算**。

原理：LA 在 500 MSa/s 下单窗深度 5M 点 = 10 ms，所以"长窗口"靠**多切片顺序采集**再聚合，
而不是提高单窗时长（提高时长必须降采样率，240 Mbit/s 的码流就解不出来了，见 `docs/tpiu-utilization.md`）。

### 4) 本次新增/改动的测试

```
python -m unittest tools.tests.test_flow_cycles   → 6/6（周期聚合，含降级）
python tools/dev/test-etm.py                      → 4/4 PASS（页面副本 + ETM UI）
node tools/selftest/etm-page-copy.test.mjs        → 42/0
```
