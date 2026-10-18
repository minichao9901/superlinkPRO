# 解码提速：200 ms 记录 837 s → 119.5 s（≈7×，2026-10-18）

> 对象：现有 200 ms 真机记录 `capture-de9b8e0ed24c41c68e5b374f3cb3c9c5`
> （capture.csv 500,410,009 B、glitchNs=4、config 0x1011）。
> 口径：`capture.csv → 解码完成 + browse 索引可查` 的**端到端**墙钟时间，同一台机器（Ryzen 7 PRO 5850U / 8C16T / 14.8 GB）。

## 结果

| 口径 | 秒 | 倍数 |
|---|---:|---:|
| 改动前（生产实测 2026-10-18） | 814.1 | — |
| 改动前（profile 复跑，计时包装开销 +2.8%） | 837.287 | — |
| **改动后（opt 实测）** | **119.6** | **7.00×** |
| **改动后（队长复核复跑）** | **119.5** | **7.01×** |
| 改动后（verify 独立复核，sidecar 未命中） | 157.0 | 4.65× |

> 便携包**随带 numpy/pandas**（用户要求），包内同样是全速（~120 s）；若删除 runtime 里的这两个目录，代码会自动回退纯 Python（约 270 s ≈ 2.7×，结果逐位相同）。

## 阶段对照（互斥口径）

| 阶段 | 改动前 | 改动后 | 倍数 |
|---|---:|---:|---:|
| 前端 `capture.csv → 字节流` | 135.5 | 29.9 | 4.5× |
| 展开 + 富化 + 字节核算 + summary 扫描 | 456.1 | 53.1 | 8.6× |
| browse 索引（v3 → v4） | 233.6 | 35.2 | 6.6× |
| TPIU 解复用 / native OpenCSD | 2.6 / 6.1 | 1.3 / 4.0 | — |
| **合计** | **837.3** | **119.5** | **7.0×** |

## 做法（只改 4 个文件，未碰 C++/采集控制）

| 文件 | 改动 |
|---|---|
| `etm/tools/decode_stream.py` | **单遍引擎**：一次读 `native.jsonl`，同遍完成展开/富化/字节核算/统计，去掉“序列化 2 次 + 反序列化 7 次 + 3 遍扫描”。旧的多次实现原样保留为 `_expand_legacy`；`ETM_DECODE_ENGINE=legacy` 可整体回退 |
| `etm/tools/record_store.py` | **索引 v4**：不再复制整条事件 JSON，改存字节偏移列；索引后建 + PRAGMA + 3 个索引；2.90 GB → 1.04 GB。sidecar（`browse-columns.tsv`）首行写 `events=<字节数> rows=<行数>` 指纹，按指纹判新鲜（不看 mtime） |
| `etm/tools/trace_port.py` | **前端向量化**（可选）：numpy/pandas 批量解析 CSV 与 4 ns 滤波；时间列用 `float_precision='round_trip'` 保证与 Python `float()` 逐位一致；缺 numpy/pandas 或 CSV 形状异常自动回退纯 Python |
| `etm/tools/flow_analysis.py` | 单遍 `CallTracker` 累加器，`analyze_calls` 变薄封装；fast/legacy 共用同一套规则与排序 |

## 正确性证据

- 7 个产物（`etm.bin`/`tpiu.bin`/`native.jsonl`/`events.jsonl`(1.68 GB)/`packets.jsonl`/`elements.jsonl`/`sources.json`）sha256 与记录自带文件**全等**
- `decoded.json` 的 `summary/quality/analysis/streamed/status/elf/config/physical/framing/capture` **逐字段相等**；语义红线：instructions 3,675,477、packets 300,074、executed 3,124,443、exceptions 190、gaps 1、packetIssues 0、totalCycles 11,399,244、events 3,675,860、elements 778,889、inputBytes 365,449
- 记录自带生产 v3 索引 vs 新 v4 索引：3,675,860 行 **0 差异**；overview/page/event/navigate/window/timeline/issues/source 抽样一致
- 跨记录 fast vs legacy：ETMv4 小窗与 ETMv3/F103 记录产物 sha256 全等
- 缺 numpy/pandas 的回退路径：physical 报告与相位字节流逐位相同
- 旧长窗记录（schema=3，13,121,380 事件）可建索引、可读

## 限制与回退

- 新记录首次解码要多写一份 `browse-columns.tsv`（784 MB，建索引复用；净磁盘仍比 v3 少约 1.1 GB）。
- 索引依赖 `events.jsonl`/`packets.jsonl` 不再就地改写（signature 含 size/mtime，改动会触发重建）。
- 任何怀疑：设 `ETM_DECODE_ENGINE=legacy` 回到旧多次实现（结果相同，只是慢）。
- 未做 10×：剩余成本是逐条 Python 展开 54 s + 索引 35 s + CSV 解析 26 s；再往下需要 C++ 重构热路径或 6-worker 并行，按决定不做。

## 证据目录（gitignored）

- `docs/validation/decode-profile-20261018/`（基线 profile、阶段拆分、热点、基线产物 sha256）
- `docs/validation/decode-10x-20261018/`（优化报告、8 个复现脚本、make-test 日志）
- `docs/validation/decode-verify-20261018/`（独立复核报告、跨记录 A/B、索引逐行对比）
