# 合成 1 s 规模化验证说明（非真机，P6b）

状态：P6b 交付（2026-10-20）｜分支 `ds/etm-1s-p3p6`｜证据
`docs/validation/20261020-ds-etm-p6-scale-b2d8148/`｜性能数字汇总见
`docs/etm-performance-baseline.md` §9｜真机验收清单见 `docs/etm-1s-hardware-checklist.md`

> **这不是真机 1 s 验收。** 本文件说明用合成 1 s 输入对 P3 融合写出器（t1）+ P4 TraceStore
> 适配器（t4）做的规模化预检：构造方式、实测数字、缺陷闭环、未覆盖项。
> 合成数据不能冒充连续真机 trace，也不据此宣称 1 s 达标。

## 1. 构造（与 P6a 的关系）

| 项 | 值 |
|---|---|
| 合成输入 | 10 ms USB 记录的 `etm.bin`（22,342 B）**字节拼接 100 次** = 2,234,200 B |
| 为什么不用 P6a 的 9.18 GB events.jsonl | t1 融合写出器的输入是 `native.bin`（由 `etm_decode --binary` 从 `etm.bin` 产生），不是 events.jsonl；直接拼接 ETM 字节才能走真实 t1 链路并测到 decode 阶段 |
| 与 P6a 的关系 | P6a 的物理 1 s 合成（KVDAT 100 段）证明"物理输入逐段等价"；本 P6b 的 ETM 字节拼接是同一"重复 capture"思路在 ETM 层，用来评测 1 s 量级事件流的写出/查询 |
| 计数 | 19,861,216 条指令（期望 21,070,600，−5.74%）、19,863,518 事件、4,142,783 原生范围；拼接处不完整帧被解码器丢弃 |
| 声明 | `tmp/p6b-scale/record-1s/synthetic.json`：`synthetic=true`、`continuousRealTrace=false`、`notForHardwareAcceptance=true`，段间无真机会话复位语义 |

## 2. 实测（摘要；完整样本见基线 §9 与证据）

- **首次处理（t1）**：3 次墙钟 159.15 / 141.25 / 123.40 s（中位 **141.25 s**，写库 140.58 s）；
  阶段中位：decode 2.085、stream **136.538**、assemble 0.550、publish 0.349 s。
  store **78,099,689 B（74.5 MiB，3.93 B/指令）**；进程树峰值 RSS **392.6 MiB**。
- **查询（t4 适配器，100× store）**：open 0.376 s；overview 0.004 s；首页 0.126 s；中/末页
  0.005–0.013 s；event 0.001–0.023 s；navigate 中位 0.0008 s、**P95 0.988 s**；
  `window(0,200k)` 2.47 s、局部窗口 2.29 s、缓存 1 ms 级；`timeline(0,200k)` 4.08 s、缓存 1.2 ms；
  issues 0.001 s。**全段 window/timeline 被 P3 写出器缺陷阻塞**（见 §3）。
- **3× 对照 store（607,757 事件，可全量展开）**：全量逐事件对照旧解码器 text oracle
  **0 差异**（607,757 条 / 607,686 指令，analysis 0 差异；summary 仅多 3 个新增元数据键）；
  全段 window 10.81 s、timeline 17.37 s、缓存 1 ms 级。用于证明"100× 的读侧失败是写出器缺陷，
  不是合成数据语义问题"。
- **方案 §3.1 逐项**：RSS（392.6 MiB ≤2 GiB）、派生体积（74.5 MiB ≤512 MiB）、**对象层部分请求**
  首开（open+overview+首页 ≈0.51 s，`objectLayerPartial`）、缓存（1 ms 级 ≤100 ms）达标；
  1 s 首次处理（141.25 s vs ≤10 s）、全区间首次统计（阻塞/局部 2.5 s，外推全段 ≈250 s）、
  导航 P95（0.988 s vs ≤200 ms）**未达标**；**完整首屏 `firstScreenWithTimeline`（含默认轨迹图、
  按真实加载顺序）未通过**——R0 前默认 timeline 全段 497.13 s，旧页面在请求首页行前先 await 它。
- **首屏口径（R0 修正）**：0.51 s 只是对象层部分请求，**不能写成首屏达标**。字段定义见
  `etm-performance-baseline.md` §1/§10；R0 已把默认轨迹图与 overview/首页行解耦（图表独立加载、
  有进度、失败/超时不挡表格），真实浏览器 A/B 时序见
  `docs/validation/20261020-ds-etm-r0-firstscreen-1891916/`。完整首屏要等 R1 的快速默认 timeline
  后再按真实网页重新验收。
- **默认能力未放开**：`CAPTURE_MAX_SECONDS=0.2`、`CAPTURE_MAX_SAMPLES=100_000_000`；
  Flask test client `GET /api/capabilities` 返回 200 且同上；前端 `view.js` 从能力读上限
  （缺失回退 0.2/1e8）；实验开关 `ETM_STORE_WRITE` 默认关闭、仅显式 `=1` 启用。

## 3. 缺陷闭环（P3 写出器 `internal_unknown` 多算）

100× store 读侧拒绝 15 条展开段（`展开段内部谓词未知计数不符`，seq 1,621,778…17,301,890，
3 次构建一致；`scan_defects.py` 可复现）。根因：`flush_run` 用增量计数器计内部谓词未知，
checkpoint 恰好把最后一个内部指令当段末时多算 1；读侧按 `identifiers[:-1]` 校验。
**这是 P3 写出器真实缺陷，不是合成数据语义问题**（3× 与四份真机记录均不触发）。
修复由 t11 完成：`flush_run` 改为按 `identifiers[:-1]` 的 `static.predicated` 求和。

- 修复前：全段 window/timeline 失败；触到缺陷 seq 的 page/event/navigate 失败。
- 修复后（工作树修复版写出器重建 100× store，sha256 `fcca9207…`）：缺陷扫描 **15 → 0 条**；
  全段 `window(0,last)` **376.23 s 通过**、`timeline(0,last)` **497.13 s 通过**；
  缺陷 seq 上的 page（0.042/0.034 s）、event ×5（0.029–0.040 s）、navigate 16/16（P95 0.220 s）
  全部通过 → **缺陷消除，性能结论不变**（全段统计仍分钟级，未达 ≤1 s）。
- 正式结论以 t11 修复报告为准；若最终字节与 `fcca9207…` 不同，需重跑 `closure_check.py verify`。
  t11 独立验证（100× 缺陷 15→0、四份真机记录逐事件 0 差异、内容块 20/20）见
  `docs/validation/20261020-ds-etm-p3-flushfix-b2d8148/`。

## 4. 未覆盖项

1. 真机 1 s 采集/保存/上传链（LA 深度、USB、save 完整性）未验证 → 见 `docs/etm-1s-hardware-checklist.md`。
2. 浏览器/独立 EXE 端到端未验证（对象层适配器数据）；HTTP `/api/browse` 全接口 JSON 差异见 P4 证据。
   R0 已补真实浏览器加载顺序 A/B（`docs/validation/20261020-ds-etm-r0-firstscreen-1891916/`），
   但**完整首屏性能未通过**：R1 快速默认 timeline 之前不能宣称首屏达标。
3. 共享机负载使 3 次波动约 29%，未做安静窗口复测；绝对秒数不固定。
4. 全段 window/timeline 的块级预聚合未做（P4 遗留 U6），即使无缺陷也需分钟级。
5. `schema=3` 长窗记录、Unicode/空格路径、并发/取消/磁盘满未覆盖。
6. 合成输入的事件数比严格 100 段少 5.74%（拼接丢帧），不能据此外推真机 1 s 的事件量/字节行为。

## 5. 复现命令（仓库根，Python 3.13.5）

```powershell
# 1) 合成记录 + 100× store（3 次）
python -X utf8 docs/validation/20261020-ds-etm-p6-scale-b2d8148/run_scale.py build --repeat 3
# 2) 缺陷扫描（读侧拒绝的展开段）
python -X utf8 docs/validation/20261020-ds-etm-p6-scale-b2d8148/scan_defects.py
# 3) 查询（缺陷感知；全段 window/timeline 会如实记录失败）
python -X utf8 docs/validation/20261020-ds-etm-p6-scale-b2d8148/run_scale.py queries --heavy
# 4) 3× 对照（构建 + oracle 全量对照 + 查询）
python -X utf8 tmp/p6b-build3x.py
python -X utf8 docs/validation/20261020-ds-etm-p6-scale-b2d8148/compare_small.py
python -X utf8 docs/validation/20261020-ds-etm-p6-scale-b2d8148/run_scale.py queries --record tmp/p6b-scale/record-3x --heavy
# 5) 能力核对
python -X utf8 docs/validation/20261020-ds-etm-p6-scale-b2d8148/run_scale.py capabilities
```

大产物在 `tmp/p6b-scale/`（gitignored）；证据目录只放报告/JSON/日志/指纹。
