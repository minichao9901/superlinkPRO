# ETM 离线处理性能基准说明（口径与复现）

版本：1.1｜2026-10-20｜分支 `ds/etm-1s-p0p2`｜基线 `7f6c042`（§8 为 P0-C 规范基准）

本文件说明 ETM 记录在主机侧离线处理的**计时口径、固定输入、复现命令与历史数字边界**。
所有数字都是特定机器、特定运行的结果，**不是固定加速承诺**；新增数字必须用
`tools/perf/bench_pipeline.py` 实测后再写入，未实测项不得写成结论。

## 1. 计时口径

与《ETM 1 s 记录与原生快速链路实施方案》§3.1 对齐，分项记录、不混算：

| 指标 | 起止边界 | 包含 | 不包含 |
|---|---|---|---|
| 完整处理（首屏数据就绪） | KVDAT/CSV 完整可读 → 完整二进制记录 + 统计 + 必要索引就绪 | 取样（含原始文件读取与 SHA256）、TPIU 解复用、OpenCSD 原生解码、事件展开、浏览索引、`decoded.json`/JSONL 落盘 | LA 上传、Kingst `save-data`、浏览器绘制、可选全量文本导出、验证扫描 |
| 首次打开已处理记录 | 请求开始 → overview/首页/默认轨迹图响应就绪 | SQLite 查询、payload 序列化 | 浏览器渲染、网络传输（本机回环） |
| 整段窗口 | `store.window()` 冷请求 | 区间统计与 JSON 序列化 | 第二次缓存请求（单独列为 `windowCached`） |
| 局部/半区间窗口 | `store.window(from,to)` 冷请求 | 同上 | 重复请求 |

其他约定：

- **首次与后续分列**：首次包含冷 OS 文件缓存、Python/扩展首次导入等一次性成本；
  后续完整重跑是独立进程冷启动，不把“第二次更快”当作算法提速，也不承诺固定加速。
- **缓存状态分列**：整段窗口的冷请求与缓存命中分别记录；复用已有索引只改善重新打开/相同输入重解，
  不改善“新输入第一次处理”。
- **不关闭安全软件**：不修改 Windows Defender、电源方案或任何系统设置，也不以关闭安全软件
  作为用户正常性能前提；Defender 状态只做只读查询并随运行记录。
- **验证扫描不计入计时**：等价性/产物哈希核对在计时结束后单独执行；计时内不做额外全量重读。
- **进程树资源**：100 ms 采样线程记录本进程 + 子孙进程（OpenCSD 解码器、原生索引器）的
  峰值 RSS 与 CPU 时间；`childCpuSecondsApprox` 由采样差分估算，精度受 100 ms 采样限制。
- **计数诊断不算完整链路**：只做 OpenCSD 解码计数、空回调或缓存命中的数字不能替代本口径。

## 2. 固定输入

`tools/perf/inputs.json`（由 `tools/perf/build_inputs.py` 生成，可 `--check` 复核）固定方案 §9.1 的五份只读记录：

| 键 | 记录 | 负载 | 协议/目标 |
|---|---|---|---|
| `steady` | `docs/validation/20261018-ds-review-bb2f8ee-6ff02745/data/records/capture-be1e…` | 200 ms、约 368 万条路径指令 | ETMv4 / STM32H743 |
| `boundary` | `docs/validation/20261018-ds-recheck-0cd4229-a7b4f912/live-data/records/capture-3643…` | 200 ms、约 386 万条指令（含未知/边界） | ETMv4 / STM32H743 |
| `ds-rerun` | `etm/records/capture-de9b…` | 200 ms、3,675,477 条指令 | ETMv4 / STM32H743 |
| `usb-init-10ms` | `etm/records/capture-4395…` | 10 ms、210,706 条指令（USB 初始化） | ETMv4 / STM32H743 |
| `etm-v3-10ms` | `etm/records/capture-2d6d…` | 10 ms、172,596 条指令 | ETMv3.4 / STM32F103ZE |

清单对每份记录给出 `capture.csv` / `capture.kvdat` / `etm.bin` / `firmware.elf` /
`native-config.txt` / `session.json` / `sources.json` / `tpiu.bin` / `capture.csv.la.json`
的字节数与 SHA256，另有目录清单摘要（文件名+大小）。记录始终只读：基准把原始输入
复制/硬链接到本次 run 目录并只改写副本的 `session.input`。

## 3. 复现命令

以下命令的工作目录都是仓库根；`<out>` 为新的证据输出根（每次运行自动新建 run 目录，不覆盖历史）。

```text
# 1) 固定基线源码快照（示例：7f6c042），并在快照内构建解码器
git worktree add tmp/perf-baseline 7f6c042
pwsh -NoProfile -File tmp/perf-baseline/etm/build-decoder.ps1

# 2) 单样本完整链路（记录可直接写目录，也可写 inputs.json 键名）
python -X utf8 tools/perf/bench_pipeline.py \
  --record usb-init-10ms --code tmp/perf-baseline/etm --kind csv \
  --variant sanity-10ms --repeat 1 --output <out>

# 3) 旧/新代码 A/B：--repeat N 与多 --code 按轮次交错
python -X utf8 tools/perf/bench_pipeline.py \
  --record steady --code <旧快照>/etm --code <新快照>/etm \
  --kind kvdat --variant 200ms-ab --repeat 3 --window --output <out>

# 4) 比较两次/多组运行（输入/代码指纹、产物哈希、阶段耗时中位数/范围）
python -X utf8 tools/perf/compare_runs.py \
  <out>/200ms-ab-c1 <out>/200ms-ab-c2 --label old --label new \
  --json <out>/compare-200ms-ab.json

# 5) 复核固定输入指纹
python -X utf8 tools/perf/build_inputs.py --check
```

输出结构：`<out>/<variant>/runs/<runId>/run.json` 与 `bench.log`（失败另有 `error.log`、
`process.log`），逐 run 记录：输入指纹、代码源文件哈希 + git revision/dirty 清单、
构建参数、环境、各阶段墙钟/CPU、进程树峰值 RSS、产物字节数与 SHA256、
overview/page/timeline/window 耗时；`<out>/invocations/<id>.json` 保存本次调用摘要
（各变体中位数/范围）。run 目录只增不改。

`compare_runs.py` 的三项检查：`inputFingerprintMatch`（输入指纹）、`codeSourceMatch`
（源码摘要）、产物 `compareSha256`（对含绝对路径的 decoded/physical/API JSON 做路径归一化，
`browse-v3.sqlite` 含 mtime 签名按易变项单列）；耗时给中位数与范围。默认只报告，
`--require-artifact-match` 时产物不一致返回 2。

## 4. 历史实测（特定运行，不承诺固定加速）

| 运行 | steady | boundary | 来源 |
|---|---:|---:|---|
| 原 CSV 链路：完整解码+索引后首屏数据就绪 | 111.179 s | 111.755 s | `docs/validation/20261019-kvdat-fastpath-bd482e70/final-report.md` |
| KVDAT 链路：历史单次完整处理 | 60.482 s | 62.165 s | 同上（每份每种配置一次） |
| 原首次整段执行顺序窗口 | 43.900 s | 46.938 s | 同上 |
| KVDAT 首次整段窗口 | 0.971 s | 1.017 s | 同上 |

后续独立复跑（`docs/etm-kvdat-fastpath-rerun-2026-10-19.md`，同一 200 ms 记录）：
便携 EXE 连续两次完整重解码 **107.3 / 107.7 s**，源码后端 **105.4 / 103.0 s**，
未复现早先“EXE 首次 105.4 s、第二次 67.2 s”的差异。

因此统一口径为：**历史单次 60～62 s，后续完整重跑 103～108 s，尚无稳定 60 s 保证**；
窗口约 1 s 是相对稳定的收益，与首屏波动分开表述。60～62 s、67 s、103～108 s 都只是特定运行。

上述历史数字由当时的临时脚本产生，缺逐 run 原始计时目录；后续 200 ms 规范数字由 P0-C
用本仓库工具补齐（交错 ≥3 次、全部样本、中位数/范围），本文件先不写未实测承诺。

## 5. 本次工具验证运行（10 ms，仅证明工具可用）

2026-10-19 用 `tmp/perf-baseline`（`7f6c042`，Ninja Release，OpenCSD 1.8.3，
`-O3 -DNDEBUG`）对 `usb-init-10ms` 跑通全链路。**10 ms 样本只用于工具 sanity，不与
200 ms 历史数字横向比较，也不构成性能结论。**

| 运行 | 完整处理 | 取样 | 原生解码 | 展开 | 索引 | 峰值树 RSS | 整段窗口（冷/缓存） |
|---|---:|---:|---:|---:|---:|---:|---:|
| CSV 单次 | 7.815 s | 1.971 s | 0.275 s | 3.450 s | 1.722 s | 195.7 MB | 0.157 s |
| CSV 重复 2 次（中位） | 7.526 s | 1.513 s | 0.296 s | 3.598 s | 1.825 s | 196.5 MB | 0.168 s |
| KVDAT 单次（`--window`） | 5.094 s | 0.350 s | 0.290 s | 2.719 s | 1.424 s | 113.8 MB | 0.150 s / 0.0021 s |

- **并发负载说明**：该组运行期间本机同时有其他成员的 Python 等价性测试（见证据目录
  `logs/concurrent-processes.txt`、`logs/cpu-load.txt`，CPU 负载约 32–39%）。因此这些绝对秒数只作
  工具可用性证明，离散也大于空闲窗口（空闲时同命令曾约 5.6–6.7 s，未保留为证据）。
  **200 ms A/B 必须同批交错、同负载条件比较并记录环境负载。**
- 同一 CSV 输入 3 次完整处理 7.323–7.815 s（中位数 7.526 s，范围 0.492 s，约 6.5%）；
  `compare_runs.py` 显示 18/18 可比较产物哈希一致。
- KVDAT 本次取样阶段 0.350 s vs CSV 1.971 s（约 1.6 s 差异，来自边沿存储省掉 CSV 解析）；
  展开/索引差异与并发负载噪声混在一起，不能据此得出“KVDAT 让解码更快”。
- 更早的**首次**运行（OS 缓存冷、扩展首次导入）明显更慢（本机曾观察到约 10 s，未保留为本目录
  证据）；首次与后续必须分列，不能拿一次运行当结论。

等价性：四份运行产出的 `etm.bin`、`tpiu.bin`、`native.jsonl`、`sources.json` 与源记录
逐字节一致；CSV 与 KVDAT 两组的 `etm.bin`/`tpiu.bin`/`native.*`/events/packets/elements
及 page/timeline/window JSON 一致（`decoded.json`/`physical.json`/`session.json`/overview
按输入格式不同含 `capture.csv` 与 `capture.kvdat` 的名称/哈希，属预期差异）。
证据见 `docs/validation/20261019-ds-etm-p0-tooling-7f6c042/`（report.md、result.json、
environment.json、sha256.json、sanity/、compare-*.json、logs/）。

## 6. 环境快照（本次运行）

| 项 | 值 |
|---|---|
| OS | Windows 11 10.0.26200（AMD64） |
| CPU | AMD Ryzen 7 PRO 5850U，8 物理核 / 16 逻辑核，标称 1901 MHz |
| 内存 | 15,919,939,584 B（约 14.8 GiB） |
| 磁盘 | E: 总 1,081,588,649,984 B，运行时可用约 117 GB（109 GiB） |
| Python | CPython 3.13.5（MSC v.1943，64 bit），`-X utf8` |
| 编译器 | gcc/g++ 16.1.0（MSYS2 mingw64）；CMake 4.0.3；Ninja 1.13.0 |
| Git | 2.49.0.windows.1 |
| 解码器 | `etm_decode.exe` SHA256 `9b236d43…e7eac1032d`（4,167,203 B）；`etm_index.exe` `e05c9f1c…10fc0a37`（4,989,700 B） |
| Defender | 实时防护开启（签名 1.461.106.0）；**未修改任何设置** |
| 电源 | 平衡方案 `381b4222-f694-41f0-9685-ff5bb260df2e`；**未修改** |

完整环境（含工具版本、构建参数、Defender/电源只读查询结果）见证据目录
`environment.json` 与各 `run.json` 的 `environment`/`build` 字段。

## 7. 未完成 / 不在本文件范围

- 200 ms 规范 A/B 已由 P0-C 完成，见 §8；后续 1 s 记录与真实浏览器/EXE 口径仍不在本文范围。
- 真实 1 s 连续记录、真实浏览器首屏绘制、独立 EXE 口径未纳入本次基准；EXE 与源码分别记录。
- 安全软件保持开启；不提供“关闭 Defender 后”的数字作为用户正常性能前提。
- 本轮未操作硬件、未采集新记录；全部输入为既有只读记录。

## 8. P0-C 规范基准（最终快照，2026-10-20）

本节数字全部由 `tools/perf/bench_pipeline.py` 实测，运行目录与原始日志见
`docs/validation/20261019-ds-etm-p0-canonical-7f6c042/`。**这些是共享机器上的特定运行，
不是固定加速承诺；未达标项不因改口径而变绿。**

### 8.1 冻结快照与运行条件

- 最终快照 `tmp/perf-final/etm`：从工作树 `etm/` 复制（排除 `records/`、`build-native/`、
  `__pycache__/`、`*.pyc`），625 个源文件、44,514,239 B，source manifest SHA256
  `078ff1ba6539d734…`；快照内用 `build-decoder.ps1` 构建（Ninja/Release，OpenCSD 1.8.3，
  `-O3 -DNDEBUG`，g++ 16.1.0），`etm_decode.exe` SHA256 `ae0347def15e2f99…`。
- 基线与最终对照用 `tmp/perf-baseline/etm`（7f6c042，`etm_decode.exe` `9b236d4396ba131c…`）。
- 输入：`inputs.json` 的 `steady` 与 `boundary`（200 ms、500 MSa/s、H743，`capture.kvdat`），
  记录只读、run 目录内为硬链接副本。
- 每条记录 legacy(text) 与 binary(`ETM_NATIVE_FORMAT=binary`) 各 3 次，按轮次交错；
  基线各 1 次（紧跟第 3 轮，尽量共享负载窗口）。
- **主机负载**：运行窗口内 20 s 采样 65 点，CPU 负载 min 1% / 中位 18% / max 49%
  （其他项目的 Python/Playwright/构建进程并存，不是空闲机）。因此本节的绝对秒数高于
  历史空闲窗口的 60～62 s；逐轮交错保证 legacy/binary 在同一负载附近比较。

### 8.2 交错性能（全部样本）

完整处理（KVDAT → 记录+统计+索引就绪），秒：

| 轮次 | steady legacy | steady binary | boundary legacy | boundary binary |
|---|---:|---:|---:|---:|
| 1 | 116.321 | 93.954 | 87.069 | 67.093 |
| 2 | 91.161 | 68.052 | 87.946 | 74.705 |
| 3 | 97.358 | 97.882 | 121.924 | 97.695 |
| **中位数** | **97.358** | **93.954** | **87.946** | **74.705** |
| 范围 | 91.161–116.321 | 68.052–97.882 | 87.069–121.924 | 67.093–97.695 |

逐轮配对差（binary − legacy）：steady −19.3% / −25.3% / +0.5%，boundary −22.9% / −15.1% / −19.9%。
中位数差：steady −3.5%，boundary −15.1%。第 3 轮 binary steady 遇到负载尖峰（97.9 s），
与 legacy 同轮 97.4 s 持平；**不要只用中位数宣称整链路加速**。

分阶段中位数（秒）：

| 阶段 | steady legacy | steady binary | boundary legacy | boundary binary |
|---|---:|---:|---:|---:|
| 取样 sample | 6.606 | 6.842 | 6.171 | 6.441 |
| 解复用 demux | 2.222 | 1.600 | 1.871 | 1.913 |
| 原生解码 nativeDecode | 6.478 | **0.514** | 4.739 | **0.706** |
| 展开 expand | 51.369 | 48.564 | 46.163 | 39.777 |
| 索引 index | 37.800 | 31.742 | 29.590 | 25.494 |
| ELF 加载 | 0.404 | 0.281 | 0.329 | 0.348 |

- 原生解码阶段 steady **6.478 → 0.514 s（12.6×）**、boundary **4.739 → 0.706 s（6.7×）**；
  原生输出 steady 284.1 MB `native.jsonl` → 54.5 MB `native.bin`（5.21×），
  boundary 298.4 MB → 57.2 MB（5.21×）。这与 P2 交付的 10× 级解码器收益一致。
- 整链路仍由 **expand + index** 主导（合计约 80–89 s / 65–70 s），所以 P2 之后
  “200 ms ≤3 s”仍不达标；这是 P3/P4 的工作。
- 进程树峰值 RSS：legacy/binary 均约 **1.055 GiB**（无显著差异）；
  tree CPU：legacy 中位 87.7 s（steady）/78.4 s（boundary），binary 82.0 s/70.0 s。
- 产物字节（`artifactBytes`，含 events/native/索引/列存）：steady legacy **3.133 GiB** →
  binary **2.613 GiB**（−16.6%）；boundary 3.291 GiB → 2.745 GiB（−16.6%）。
  其中 events.jsonl 仍各 1.57/1.65 GiB，是“派生数据 ≤512 MiB”不达标的主因。

### 8.3 legacy vs binary 语义对照

同输入同快照，取每组一次完整运行做全量对照（`semantic-comparison.json`）：

- 七项产物：`etm.bin`、`tpiu.bin`、`events.jsonl`（1.68/1.77 GB）、`sources.json` 逐字节一致；
  `native.jsonl`、`packets.jsonl`、`elements.jsonl` 在 binary 模式按设计不生成
  （native 改为 `native.bin`，包/元素兼容层留 P3/P4）。
- `decoded.json` 的 **summary / quality / analysis / status 全等**；顶层差异仅
  `physical`（只差输入绝对路径，去掉后逐字段相同）与 `streamed`（仅 `nativeJsonl`
  从 `native.jsonl` 变为 `native.bin`，事件/元素/包计数相同）。
- 白名单逐项证明：`native-config.txt` 仅镜像绝对路径不同（归一化后一致）；
  `browse-columns.tsv` 仅首行 mtime/行数签名不同（忽略首行逐字节一致）；
  `browse-v3.sqlite` 因 binary 不建包表而略小（steady 293.5→275.4 MB、boundary 308.4→289.4 MB），
  属预期差异，不影响事件查询。

### 8.4 7f6c042 基线对照（默认 text 路径）

| 记录 | 七项产物 | decoded 核心字段 | 基线完整处理 | 最终 legacy 中位 | 变化 |
|---|---|---|---:|---:|---:|
| steady | 全部 identical | summary/quality/analysis/status 全等 | 114.542 s | 97.358 s | −15.0% |
| boundary | 全部 identical | 同上 | 106.694 s | 87.946 s | −17.6% |

七项哈希、`decoded.json` 核心字段、`physical`（除输入路径）均一致，证明 P2 默认 text 路径
没有改变产物语义；最终链路未回退（中位更快，最终样本范围也覆盖基线值）。

### 8.5 方案 §3.1 目标逐项（200 ms 实测）

| 目标 | steady legacy | steady binary | boundary legacy | boundary binary | 判定 |
|---|---:|---:|---:|---:|---|
| 200 ms 新输入完整处理 ≤3 s | 97.358 s | 93.954 s | 87.946 s | 74.705 s | **未达标（展开+索引主导）** |
| 1 s 新输入完整处理 ≤10 s | — | — | — | — | **未测**（无真实 1 s 记录，P6；不线性外推） |
| 首次打开已处理记录 ≤1 s | 0.065 s | 0.069 s | 0.052 s | 0.054 s | 通过（服务端代理，不含浏览器渲染/HTTP） |
| 1 s 全区间首次统计 ≤1 s | 1.584 s | 1.491 s | 1.256 s | 1.195 s | **未达标**（本负载下；历史空闲约 0.97–1.02 s） |
| 已缓存相同区间 ≤100 ms | 0.0024 s | 0.0024 s | 0.0029 s | 0.0018 s | 通过 |
| 局部定位/翻页/导航 P95 ≤200 ms | 0.244 s | 0.200 s | 0.162 s | 0.164 s | steady 未达标/binary 压线，boundary 通过 |
| 进程树峰值 RSS ≤2 GiB | 1.055 GiB | 1.055 GiB | 1.055 GiB | 1.055 GiB | 通过 |
| 新派生数据 ≤512 MiB | 3.133 GiB | 2.613 GiB | 3.291 GiB | 2.745 GiB | **未达标**（events/索引/列存） |

补充：3 次关键词搜索（LIKE）P95 约 2.5 s / 2.4 s（steady）与 2.1 s / 2.1 s（boundary），
单列，不混入“翻页/导航”目标；P2 二进制模式不生成 packets/elements 兼容层，网页包视图不在本轮。

### 8.6 结论

1. P2 二进制输出把**原生解码阶段**缩短 6.7–12.6×、原生输出缩小 5.2×，并在默认 text 路径
   保持七项产物与核心分析字段完全一致——可作为实验能力保留，默认仍走 text。
2. 整链路仍被 Python 展开与 SQLite 索引主导（占比约 85–90%），200 ms ≤3 s、派生数据 ≤512 MiB
   均未达标；P3/P4 需要改变区间存储与索引，而不是继续换原生输出格式。
3. 首开与缓存窗口达标；全区间首次统计在本负载下 1.2–1.6 s，未达 ≤1 s；
   1 s 目标未测，不宣称达到。
4. 下次做 1 s 验收前，应先降低共享机器负载或固定交错顺序，并把浏览器/HTTP 与 EXE 口径分开报告。
