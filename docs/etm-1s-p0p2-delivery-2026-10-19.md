# ETM 1 s 原生链路方案：P0–P2 交付说明（2026-10-19）

- 分支：`ds/etm-1s-p0p2`（基线 `7f6c042`，方案提交 `b18dc10`；**未 push**）
- 执行方式：AgentTeams（Lead + p0-baseline / p1-store / p2-native），真机采集未参与
- 方案：`docs/etm-1s-native-pipeline-plan-20261019.md`（由 Codex 编写并转交）
- 范围：**P0、P1、P2**（按方案 §13「先把 P0～P2 的结果交付审查」）；P3–P6 未开始

Tested-Revision: 6d69bb8（P0-C 提交；P1/P2 内容在其之前的 d314fbb/648c17e）
Tested-Base: 7f6c0428f15a565f6f280236d299f513f1e6ee1d
Tested-Dirty: false（分支 HEAD 提交后工作树干净）
Validation-Result: partial-pass（P0/P1/P2 交付与语义门禁通过；方案 §3.1 的 200 ms ≤3 s、派生体积 ≤512 MiB 未达标，1 s 未测）
Validation-Cwd: E:/superlinkPRO
Hardware-Test: not-run（未操作硬件；全部输入为既有只读记录）

## 1. 提交清单

| 提交 | 内容 | 关键验证 |
|---|---|---|
| `2b045ae` | P0-A：`tools/perf/` 基准与对照入口、五份固定输入清单、环境记录、性能文档口径收口 | 10 ms sanity 4 份 run；18/18 产物一致 |
| `60bbd87` | P0-B(a)：运行时同步脚本只在明确缺 SQLite3/目标不存在时降级，降级删除旧 `etm_index.exe`，真实错误失败且不写 provenance | 6 项离线测试；真实缓存分类检查 |
| `ec9770b` | P0-B(b)：USB 扫描按 `executionState` 过滤，not-taken/unknown 单独计数；CSV 重生成 | 4 项测试；10 ms 复跑 76 行/52 confirmed，CSV 可复现 |
| `648c17e` | P2：`etm_decode --binary`（trace-bin native v1）+ Python 读取器 + `ETM_NATIVE_FORMAT=binary` 实验开关 | 文本模式逐字节不变；两 200 ms 记录元素/包/字节核算零差异；events.jsonl 逐字节一致；解码 10.4× |
| `d314fbb` | P1：trace-store v1 规范 + Python 参考读写器 + 逐事件/逐接口对照器 + 损坏测试 | 四份记录 7,921,602 项检查 0 差异；107 项测试 |
| `6d69bb8` | P0-C：基准工具增强（`--queries`/`--artifact-hash`/`--no-reference`）+ 基线说明 §8 | 14 次完整运行；语义/基线产物全一致 |
| `b18dc10` | 方案文档入库（cherry-pick 自 `codex/etm-1s-plan`） | — |

## 2. 关键结果

### 2.1 P0-C 规范基准（steady/boundary，200 ms 真机离线输入）

| 项 | steady | boundary |
|---|---|---|
| legacy 完整处理中位 | 97.358 s（91.161–116.321） | 87.946 s（87.069–121.924） |
| binary 完整处理中位 | 93.954 s（68.052–97.882） | 74.705 s（67.093–97.695） |
| `7f6c042` 基线（默认路径） | 114.542 s | 106.694 s |
| 原生解码阶段 | 6.478 → **0.514 s（12.6×）** | 4.739 → **0.706 s（6.7×）** |
| 原生输出 | 284.1 MB → 54.5 MB（5.21×） | 298.4 MB → 57.2 MB（5.21×） |
| 进程树峰值 RSS | 1.055 GiB | 1.055 GiB |

- 语义：`events.jsonl` 逐字节一致；`decoded.json` 的 summary/quality/analysis/status 全等；与基线的七项产物逐字节一致，无回退。
- 整链路仍由 **expand（约 40–51 s）+ index（约 25–38 s）** 主导，binary 模式中位仅 −3.5%/−15.1%（逐轮配对差 −19.3%…+0.5%，受共享机负载影响），**不宣称整链路加速**。
- 环境：共享机 CPU 负载中位 18%、峰 49%，非空闲窗口；绝对秒数高于历史空闲窗口。

### 2.2 P1 trace-store v1

- 四份记录（steady 3,675,818 / boundary 3,860,432 / ETMv3 10 ms 172,623 / ETMv4 10 ms 210,731 事件）逐事件全字段 + 逐接口对照：**0 差异**，合计 7,921,602 项检查；白名单仅 3 条（新增 `traceStore` 概要键、ETMv3 旧记录缺失的 93 个周期键、window 固定 JSONL 回退）。
- store 体积合计 **50.5 MiB**（旧 events+sqlite 5.75 GiB）；指令本体 **3.3 B/条**（旧 458 B/条，约 139×）；静态字典按唯一 PC 只存一份，无逐指令 JSON 兜底块。
- 读取：open 38–56 ms、page 1.2–25 ms、event 3–9 ms；读取峰值 RSS 31–37 MiB。
- Lead 独立复核：ETMv3 10 ms 173,049 项检查 0 差异；steady 3,676,312 项检查 0 差异（221.6 s，RSS 45 MB）。

### 2.3 P2 原生二进制输出

- 文本模式（默认）逐字节不变：steady/boundary/两份 10 ms 的 `native.jsonl`、`packets.jsonl`、`elements.jsonl`、`events.jsonl` 与历史产物 SHA256 全等。
- 二进制模式：元素 778,878/817,992 条 14 字段 + 稀有文字 0 差异；包 300,066/315,221 条 offset/size/hex + 故障分类 0 差异；`decoded.json` 分析口径一致；`events.jsonl` 逐字节一致。
- 速度（解码器完整输出）：3.492 s → **0.337 s（10.36×）**，繁忙窗口 7.904 → 0.792 s（9.98×）。

### 2.4 方案 §3.1 目标逐项（200 ms 实测）

| 目标 | 结果 | 判定 |
|---|---|---|
| 200 ms 新输入完整处理 ≤3 s | 74.705–97.358 s | **未达标**（expand+index 主导，属 P3） |
| 1 s 新输入完整处理 ≤10 s | 未测（无真实 1 s 记录，P6） | **未测** |
| 首次打开已处理记录 ≤1 s | 0.052–0.069 s | 通过 |
| 1 s 全区间首次统计 ≤1 s | 1.195–1.584 s（本负载） | **未达标**（历史空闲约 0.97–1.02 s） |
| 已缓存相同区间 ≤100 ms | 0.0018–0.0029 s | 通过 |
| 局部定位/翻页/导航 P95 ≤200 ms | steady 0.200–0.244 s；boundary 0.162–0.164 s | steady binary 压线，legacy 未达；boundary 通过 |
| 进程树峰值 RSS ≤2 GiB | 1.055 GiB | 通过 |
| 新派生数据 ≤512 MiB | 旧格式产物 2.613–3.291 GiB | **未达标**（新 trace-store 200 ms 为 50.5 MiB，1 s 外推约 250 MiB，但当前链路仍写旧 JSONL/SQLite） |

## 3. 回归与测试

- 后端全量：`python -X utf8 -m unittest discover -s etm/tools/tests -p "test_*.py" -q` → **300 项 OK / 5 项既有 skip**（含新增：native_binary 33、decode_stream_binary 7、trace_store 35、trace_store_corruption 72、usb_init_reg_access 4）。
- ETM JS：`node --test etm/tools/tests/*.test.mjs` → **62 项，61 通过 / 1 既有 skip / 0 失败**。
- P0-B 定向测试：USB 4 例、runtime sync 6 例，均 OK。
- 构建：`pwsh -NoProfile -File etm/build-decoder.ps1` 通过（Ninja/Release，OpenCSD 1.8.3）。

## 4. 未完成 / 限制

1. **整链路性能未达标**：P2 只缩短原生解码阶段；expand + index 仍是 75–97 s 量级，需 P3 的原生区间存储/增量统计。
2. **1 s 未测**：无真实连续 1 s 记录；真机采集按方案由用户/Codex 执行（P6），不线性外推。
3. **二进制模式默认关闭**：不生成 `packets.jsonl`/`elements.jsonl`/`native.jsonl`，网页包视图/elements 视图兼容留 P3/P4；`trace_store.py` 的 `BINDING_INPUTS` 应加入 `native.bin`。
4. **trace-store 参考实现限制**：`window/timeline` 为 O(区间)；`navigate(source/pc)` 极端单行循环可能退化；`streamed.schema=3` 无样本；Python 写入器约 110–205 s/200 ms（生产需原生写入器）。
5. **运行时快照未同步**：`web/bridge/etm/backend` 与 `provenance.json` 未改（工作树含多任务改动，留到 P4/发版在干净树执行 `sync-etm-runtime.py` 并做便携包验证）。
6. **主机非空闲**：P0-C 样本受共享机并发影响，逐轮交错保证同负载附近比较；绝对秒数不代表空闲上限。
7. **未做**：浏览器/HTTP/独立 EXE 验收、真机采集、P3+ 内容。

## 5. 证据（本地，按仓库约定不入库）

- `docs/validation/20261019-ds-etm-p0-tooling-7f6c042/`
- `docs/validation/20261019-ds-etm-p0-fixes-7f6c042/`
- `docs/validation/20261019-ds-etm-p1-tracestore-7f6c042/`
- `docs/validation/20261019-ds-etm-p2-binary-7f6c042/`
- `docs/validation/20261019-ds-etm-p0-canonical-7f6c042/`（472 文件、约 43 GB，含 14 个 run 目录）
- 汇总：`docs/validation/20261019-ds-etm-p0p2-final-7f6c042/`

## 6. 建议下一步（P3 起）

1. 生产用原生 trace-store 写入器：把 `CallTracker`/谓词/周期归因移入原生路径，直接落 `trace-store v1`，消除 `native.jsonl → events.jsonl → SQLite` 的重复中转。
2. 把「展开段」升级为「基本块 + 区间前缀汇总」，让整段窗口达到 ≤1 s；为 `navigate(source/pc)` 加变化点/PC 索引。
3. 查询适配层（`TraceStore` 后端 + 能力声明）与网页端到端；`BINDING_INPUTS` 加入 `native.bin`；包/元素视图兼容。
4. 原生物理前端（KVDAT 分块、相位/滤波/TPIU）与真实 1 s 验收（由用户/Codex 按硬件租约执行）。
5. P1/P3 完成后按方案 §12 交 Codex 审查（格式/语义、完整查询、跨块状态）。
