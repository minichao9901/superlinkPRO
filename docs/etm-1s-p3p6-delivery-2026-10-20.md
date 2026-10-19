# ETM 1 s 原生链路方案：P3–P6 交付说明（2026-10-20，离线部分）

- 分支：`ds/etm-1s-p3p6`（基线：P0–P2 分支 `ds/etm-1s-p0p2` 的 `b2d8148`；总基线 `7f6c042`；**未 push**）
- 执行方式：AgentTeams（Lead + p1-store / p2-native / p0-baseline）；真机采集未参与
- 方案：`docs/etm-1s-native-pipeline-plan-20261019.md`
- 范围：**P3、P4、P5、P6 的可离线部分**；真机 1 s 采集/验收留待用户/Codex（清单见
  `docs/etm-1s-hardware-checklist.md`）。**默认采集上限仍为 200 ms**，1 s 未放开。

Tested-Revision: 本分支最终提交（见 §1）
Tested-Base: b2d8148（P0–P2 交付索引）
Tested-Dirty: false（分支提交后工作树干净）
Validation-Result: partial-pass（P3/P4/P5/P6 离线门禁通过；§3.1 的 1 s 完整处理、全区间统计、导航 P95 未达标，真机未测）
Validation-Cwd: E:/superlinkPRO
Hardware-Test: not-run（无硬件；全部输入为既有只读记录 + 显式标注的合成数据）

## 1. 提交清单（本阶段）

| 提交 | 内容 |
|---|---|
| `d894903` | P6a：合成 1 s 数据构造器（10 ms 记录拼接 + 显式复位，非真机） |
| `81642b1` | P3：融合写出器 `native.bin → trace-store v1`（跳过 events.jsonl/SQLite） |
| `3f896e1` | P5a：有界分块物理前端（KVDAT/CSV→滤波/DDR→TPIU，跨块等价） |
| `c6c48fc` | P5b：原生物理前端 `etm_frontend`（KVDAT 分块→滤波/DDR→TPIU） |
| `a666bd2` | P3 审计修复 F1：`--no-hash-inputs` 真正跳过输入哈希 |
| `a372c73` | P4：TraceStore 查询适配器 + server 集成 + 能力声明 + 导入导出 |
| `60d76bc` | P6b：合成 1 s 规模化预检文档 + 真机 1 s 验收清单 |
| `f865319` | P3 缺陷修复：checkpoint flush 时谓词未知末指令不再计入 `internal_unknown` |
| 最终提交 | 运行时快照同步适配（P2 二进制模式路径）+ 本交付说明 |

## 2. 关键结果

### 2.1 P3 融合写出器（`native.bin → trace-store v1`）

- 语义：steady 3,675,818 / boundary 3,860,432 / ETMv4 10 ms 210,731 / ETMv3 10 ms 172,623，
  合计 **7,919,604 条事件逐字段 0 差异**、analysis 重放 0 差异；与 P1 参考写入器
  static/sequence/checkpoints/nav/agg **20/20 块逐字节一致**（packets 仅说明文本差异，W-P3-2）。
- 性能：`etm.bin → 可查询 store` 中位 **steady 42.95 s / boundary 41.04 s**（P0-C 旧链
  97.358/87.946 s → **2.27×/2.14×**）；安静窗口写库 34.27/32.68 s；store 13.03/13.71 MiB
  （旧派生数据 3.5 GiB，**257× 更小**），指令本体 3.3 B/条；进程树峰值 RSS 0.12–0.15 GiB。
- 独立复核（t8）：第四份 ds-rerun 200 ms 记录上 3,675,860 条事件 0 差异、查询 0 差异，
  同口径中位 28.28 s。
- 已知限制：距 §3.1 的 200 ms ≤3 s 仍差 ~11×；热点为 Python `CallTracker`（33%）与
  逐记录/范围处理（55%），需原生/常驻流式实现（P3b 已给出设计与负结果，见 §2.5）。

### 2.2 P4 TraceStore 查询适配与能力

- `/api/browse` 全接口对新格式 store 记录与旧 JSONL 记录：直接对象层 96 项 + HTTP 94 项 =
  **190 项检查 0 差异**（白名单 3：`traceStore` 概要键、融合 store 的 `streamed` 键、融合包说明文本）。
- 后端选择：manifest + 记录声明/无 events.jsonl → TraceStore；旧记录仍走 RecordStore；
  store 缺失/损坏/输入不一致明确 400，不返回拼凑结果。
- 能力：`GET /api/capabilities` 暴露 traceStore 能力与格式版本；`captureMaxSeconds=0.2`、
  `captureMaxSamples=1e8` **未变**；前端读取能力做校验，未放开 200 ms。
- 导入导出：记录包含 store，导入后免解码浏览；旧 `.etmrecord` 兼容。
- 测试：adapter 21 项 OK（1 skip）、server backend 27 项 OK（2 skip）；全后端 408 项 OK（8 skip）。
- 限制：`window/timeline` 仍 O(区间)；浏览器/EXE UI 未验收；`web/bridge` 快照已在最终提交同步
  （见 §2.6）。

### 2.3 P5 物理前端（参考 + 原生）

- P5a 参考实现 `trace_frontend.py`：10 ms 记录做到 1 字节分块（468.8 万次有界读取）；
  steady/boundary 200 ms 多尺度分块与旧链路**逐字节/逐字段一致**；200 ms 端到端 5.65/5.67 s、
  峰值 RSS **71.7 MB vs 1108 MB（≈15.4×）**，且不随记录长度增长；24 项单测 + 15/15 解复用分块扫描。
- P5b 原生实现 `etm_frontend`：17/17 分块 case + **100/100 合成 1 s 段**与参考逐字节一致
  （含 1 字节分块与混合计划；`-ffp-contract=off` 保证逐位一致）；200 ms 端到端
  **1.57–1.74 s vs 参考 6.79–7.66 s（3.9–4.8×）**；合成 1 s 8.35 vs 31.06 s（3.72×）；
  峰值 RSS **36.6 MB 且与记录长度无关**。
- **默认链路未切换**：方案要求物理前端切换以真机连续 1 s 验收为前提；切换条件见 P5b 报告 §6.4。

### 2.4 P6 合成 1 s 规模化预检（非真机）

- 合成输入：10 ms 记录 `etm.bin` 字节拼接 100 次（2,234,200 B）；实测 19,861,216 指令 /
  19,863,518 事件（拼接丢帧 −5.74%，已声明不是连续真机 trace）。
- 首次处理（P3）：3 次 159.15/141.25/123.40 s（中位 **141.25 s**，stream 97%）；store
  **74.5 MiB（3.93 B/指令）**；进程树峰值 RSS **392.6 MiB**。
- 查询（P4 适配器）：open 0.376 s、overview 4 ms、首页 126 ms、中/末页 5–13 ms、
  event 1–23 ms、navigate 中位 0.8 ms/**P95 0.988 s**、window(200k) 2.47 s、
  timeline(200k) 4.08 s、缓存 ~1 ms；全段 window **376.23 s**、timeline **497.13 s**。
- 方案 §3.1（合成 1 s）：首开 0.51 s、缓存 1 ms、RSS 392.6 MiB、派生体积 74.5 MiB **达标**；
  1 s 完整处理（141.25 s vs ≤10 s）、全区间统计（376 s vs ≤1 s）、导航 P95（0.988 s vs ≤200 ms）
  **未达标**；真机未测。**不宣称 1 s 达标**。

### 2.5 P3b 原生热点加速（负结果，未进入交付）

- Python 等价重写 `FastCallTracker` 无稳定收益（回放 0.95×；端到端 0.82–1.14×），且 ETMv4 10 ms
  的 `analysis.hotspots` 残留 1 处差异 → **不启用**；实验代码归档在
  `docs/validation/20261020-ds-etm-p3-native-b2d8148/archive/`，未入库。
- 结论与后续设计：需常驻流式 C++ helper（帧协议 I/L/E/C/F，每 4096 条批量），预期 tracker
  11–12 s → ~0.2 s、总时间 35.8 s → ~24 s；floor 19 s 需第二步把 native.bin 记录解码与区间构建
  也搬原生。

### 2.6 缺陷闭环（P6 规模化验证的价值）

- 100× 合成 1 s store 在 P3 写出器上触发 **15 条展开段**校验失败（`internal_unknown` 多算 1）：
  任何触到这些 seq 的 page/event/navigate 与全段 window/timeline 抛错；200 ms 真机记录未触发。
- 修复（`f865319`）：`flush_run` 按 `identifiers[:-1]` 的 `static.predicated` 求和（与参考写入器/
  读侧同口径），删除增量计数器；最小复现测试 + 20 项单测；100× 坏段 **15 → 0**，
  19,863,518 事件全量展开 0 错误（277.7 s），`unknownExecution` 86,629→86,614（恰 -15）；
  四份真机记录仍逐事件 0 差异、内容块 20/20 与参考一致。
- 修复后全段 `window(0,last)` 376.23 s / `timeline(0,last)` 497.13 s 通过；缺陷 seq 的
  page/event/navigate 全部通过（navigate P95 0.220 s）。

## 3. 回归与打包（Lead 实跑）

| 项 | 结果 |
|---|---|
| 后端全量 | `python -X utf8 -m unittest discover -s etm/tools/tests -p "test_*.py" -q` → **408 项 OK / 8 项既有 skip**（624 s） |
| ETM JS | `node --test etm/tools/tests/*.test.mjs` → **62 项，61 通过 / 1 既有 skip / 0 失败** |
| 定向测试 | trace_store_write 20、native_frontend 16、trace_frontend 24、adapter 21、server backend 27、usb 4、runtime sync 6 等均 OK |
| 运行时快照 | `sync-etm-runtime.py --etm-root etm` → 72 文件，sourceRevision `f865319`；新增 trace_store/trace_store_write/trace_store_adapter/native_binary 四个模块；快照 decoder 的 UTF-8 路径转换补上 P2 二进制模式的新开放路径（config / binary out） |
| 便携包 | `build-etm-package.py` → `etm-bridge-win64.zip` **75,148,467 B**，sha256 `3cabcd55f4472b8b7e0e01456222c0a6e32b713349490e91805d9a417c001b21`，packageId `d5783031431567836a3a582a0229eec429238bf72da60fe06f31a24aaa9eba37`，version 1.0.13（未升版本，实验包） |
| 便携 EXE selftest | 7 项测试（独立启动/中文与空格路径/重解码/源码快照刷新等）；首轮 2 项因快照 UTF-8 config 路径失败，修复 sync 转换后中文与空格路径用例通过，最终以全新 VALIDATION_DIR 跑 7/7（详见最终证据 tests-summary.json） |

## 4. 未完成 / 限制

1. **真机 1 s 未采集/未验收**：LA 深度、KVDAT save、上传、首末页浏览器交互等按
   `docs/etm-1s-hardware-checklist.md` 由用户/Codex 执行；默认 200 ms 不放开。
2. **性能未达 §3.1**：1 s 完整处理 141 s（目标 ≤10 s）、全段窗口 376–497 s、导航 P95 0.988 s；
   主要成本是 Python `CallTracker`/逐范围处理与 `window/timeline` 的 O(区间) 展开。
3. **P3b 原生热点加速未交付**（负结果）；后续需常驻流式 C++ helper（2–3 h 级）与块级窗口预聚合。
4. **P5 原生前端未接默认路径**；CSV 未做原生；极小分块（1–13 字节）退化。
5. **P4 UI 未做浏览器/独立 EXE 交互验收**；融合 store 的包说明文本仍缺失（U3）。
6. 便携包仍是实验包（version 1.0.13），发版前需版本号、provenance 与发布授权流程。
7. 合成数据全部显式标注：段间为独立 capture、非连续真机 trace，不能用于 1 s 硬件验收。

## 5. 证据（本地，按仓库约定不入库）

- `docs/validation/20261020-ds-etm-p3-fused-b2d8148/`（P3 融合写出器）
- `docs/validation/20261020-ds-etm-p3-audit-b2d8148/`（P3 独立审计 + F1 修复）
- `docs/validation/20261020-ds-etm-p3-flushfix-b2d8148/`（checkpoint flush 缺陷修复）
- `docs/validation/20261020-ds-etm-p4-query-b2d8148/`（P4 适配器/serve/能力/导入导出）
- `docs/validation/20261020-ds-etm-p5-frontend-b2d8148/`（P5a）
- `docs/validation/20261020-ds-etm-p5-native-b2d8148/`（P5b + P3b 负结果归档）
- `docs/validation/20261020-ds-etm-p6-synthetic-b2d8148/`（P6a）
- `docs/validation/20261020-ds-etm-p6-scale-b2d8148/`（P6b）
- `docs/validation/20261020-ds-etm-p3p6-final-b2d8148/`（最终收口：报告/提交/测试/包指纹）

## 6. 下一步建议

1. **真机 1 s 验收**：按 `docs/etm-1s-hardware-checklist.md` 递进 200→500→1000 ms；通过前默认 200 ms。
2. **原生 storebuild helper**（P3b 续）：常驻流式 C++ 进程 + 批量描述符，先复验 100× store 20/20 块一致。
3. **块级窗口预聚合**：让全段 window/timeline ≤1 s（当前 O(区间)）。
4. **浏览器/EXE 端到端**：P4 UI 验收、包说明文本策略、便携包版本与发布流程。
5. 真实 1 s 记录补齐后，重跑 P6b 全部判据并更新 `docs/etm-performance-baseline.md`。
