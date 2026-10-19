# ETM 离线处理性能基准说明（口径与复现）

版本：1.2｜2026-10-20｜分支 `ds/etm-1s-r0r3`（R0 首屏口径与加载顺序）｜P0-C 基线 `7f6c042`（§8）

本文件说明 ETM 记录在主机侧离线处理的**计时口径、固定输入、复现命令与历史数字边界**。
所有数字都是特定机器、特定运行的结果，**不是固定加速承诺**；新增数字必须用
`tools/perf/bench_pipeline.py` 实测后再写入，未实测项不得写成结论。

## 1. 计时口径

与《ETM 1 s 记录与原生快速链路实施方案》§3.1 对齐，分项记录、不混算：

| 指标 | 起止边界 | 包含 | 不包含 |
|---|---|---|---|
| 完整处理（记录可浏览） | KVDAT/CSV 完整可读 → 完整二进制记录 + 统计 + 必要索引就绪 | 取样（含原始文件读取与 SHA256）、TPIU 解复用、OpenCSD 原生解码、事件展开、浏览索引、`decoded.json`/JSONL 落盘 | LA 上传、Kingst `save-data`、浏览器绘制、可选全量文本导出、验证扫描 |
| `objectLayerPartial`（对象层部分请求） | 打开记录 → **overview + 首页 page（首批执行流行）**响应就绪 | store 打开/校验、SQLite 查询、payload 序列化 | **默认轨迹图 timeline**、浏览器渲染、网络传输（本机回环） |
| `firstScreenWithTimeline`（完整首屏） | 打开记录 → **首批执行流行可用，且默认轨迹图已就绪（或明确失败/超时并有进度提示）** | 上行全部 + 默认轨迹图请求与页面绘制（真实加载顺序） | 用户后续交互（翻页/搜索/局部窗口/导航） |
| 整段窗口 | `store.window()` 冷请求 | 区间统计与 JSON 序列化 | 第二次缓存请求（单独列为 `windowCached`） |
| 局部/半区间窗口 | `store.window(from,to)` 冷请求 | 同上 | 重复请求 |

其他约定：

- **首次与后续分列**：首次包含冷 OS 文件缓存、Python/扩展首次导入等一次性成本；
  后续完整重跑是独立进程冷启动，不把“第二次更快”当作算法提速，也不承诺固定加速。
- **缓存状态分列**：整段窗口的冷请求与缓存命中分别记录；复用已有索引只改善重新打开/相同输入重解，
  不改善“新输入第一次处理”。
- **首屏分两级，不得混用**：`objectLayerPartial`（overview + 首页 page）只是**对象层部分请求**；
  `firstScreenWithTimeline`（完整首屏）必须按真实网页加载顺序测，且包含默认轨迹图。R0 修复前
  `history.js` 在请求首页行前 `await renderTimeline()`，大样本 timeline 会把它拖到分钟级/45 s 超时，
  因此任何只含 open+overview+page 的秒数都不能写成“首屏达标”。完整首屏在 R1 提供快速默认
  timeline 之前判为**未通过**；定义与 R0 实测见 §10。
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

---

## 9. 合成 1 s（非真机）：P6b 规模化预检（2026-10-20）

本节是 **P6b** 的离线规模化预检。**没有真实连续 1 s 记录**（真机采集/验收按约定由用户/Codex
执行，见 `docs/etm-1s-hardware-checklist.md`），因此用**合成 1 s 输入**观察 t1（P3 融合写出器）
与 t4（TraceStore 适配器）在 1 s 量级输入下的阶段结构、内存/磁盘与查询行为。
**本节数字不是真机 1 s 验收，也不能用来宣称 §3.1 达标。**

### 9.1 合成输入（明确非真机）

| 项 | 值 |
|---|---|
| 构造 | 10 ms USB 记录（`etm/records/capture-4395612c313f47ce97af1e1c17e0a0d5`）的 `etm.bin`（22,342 B）**字节拼接 100 次** = **2,234,200 B**（1 s 量级 ETM 字节流） |
| 输入指纹 | `sha256=8433fff5dbbd18fa…`（完整值见证据 `build.json.record.etmSha256`） |
| 记录目录 | `tmp/p6b-scale/record-1s/`（gitignored）：`etm.bin` + `firmware.elf`/`native-config.txt`/`session.json`/`sources.json` 拷贝 + `synthetic.json` 声明 |
| 段语义 | 100 个独立 capture 的重复；段间没有真机会话复位标记，解码器在拼接处重新同步；**不是连续真机 trace** |
| 计数对照 | 期望 100×210,706 = 21,070,600 条指令；实测 **19,861,216**（−5.74%），原生范围 4,142,783（期望 4,616,300，−10.3%）——拼接处不完整帧被解码器丢弃，因此也不是"100 段等量复读" |

### 9.2 首次处理（t1 融合写出器：`etm.bin` → 可查询 store）

计时边界与 P3 一致：`etm.bin` 可读 → store + decoded.json 已发布、manifest 可打开；
3 次运行（共享机，同一轮连续；未调整 Defender/电源）：

| 轮次 | 墙钟（s） | 写库自报（s） |
|---:|---:|---:|
| 1 | 159.15 | 158.49 |
| 2 | 141.25 | 140.58 |
| 3 | 123.40 | 122.60 |
| **中位** | **141.25** | **140.58** |
| 范围 | 123.40–159.15 | 122.60–158.49 |

阶段中位（秒）：`decode 2.085`（`etm_decode --binary`）、`loadElf 0.185`、`openNative 0.444`、
`staticPrep 0.127`、**`stream 136.538`（约 97%：区间切分 / 调用栈 / 统计）**、
`summarize 0.004`、`blocks 0.128`、`assemble 0.550`、`publish 0.349`。

| 资源/体积 | 值 |
|---|---|
| 逻辑事件 / 指令 / 展开段 / 原生范围 / 包 | 19,863,518 / 19,861,216 / 3,187,256 / 4,142,783 / 1,760,200 |
| store 体积 | 78,099,689 B（**74.5 MiB**，约 **3.93 B/指令**） |
| 输入 ETM | 2,234,200 B（2.13 MiB） |
| 进程树峰值 RSS | **392.6 MiB**（含解码子进程） |
| 磁盘 | store 74.5 MiB + 记录 etm.bin 2.13 MiB（处理期 native.bin 在临时目录，运行后删除） |

**与 P3 的 200 ms 对照（不跨规模外推）**：P3 steady（3,675,477 指令）端到端中位 42.95 s、
写库 34.27 s；本合成 1 s 指令量 5.4×、写库中位 140.58 s（约 4.1×），`stream` 占比同为 90%+，
说明 P3 profile 的结论（Python CallTracker/逐范围处理主导）在更大输入上仍成立。
按事件摊销：旧 P0-C 200 ms 完整链路 97.358 s/3.68 M 事件 ≈ 26.5 µs/事件；本合成 1 s
141.25 s/19.86 M 事件 ≈ 7.1 µs/事件（不同负载/规模，仅作量级参考）。

### 9.3 查询（t4 适配器；服务端 `browse_store()` 使用同一对象）

`trace_store_adapter.adapter(record)`；store 打开含 manifest sha256 + 块 CRC + 输入指纹校验。
下表是 **100× store**（19.86 M 事件）实测；"缓存"指同进程第二次相同调用（适配器对
window/timeline 有有界缓存）。

| 查询 | 首开 | 缓存 | 说明 |
|---|---:|---:|---|
| open（full verify） | **0.376 s** | — | 含 manifest/块 CRC/输入指纹 |
| `overview` | 0.004 s | 0.005 s | 摘要块 |
| `page('events',0,150)` 首页 | 0.126 s | 0.002 s | total=19,863,518 |
| 中页（offset 10,448,400） | 0.005 s | 0.005 s | 150 行 |
| 末页（offset 19,863,450） | 0.013 s | 0.012 s | 68 行（末页不足 150） |
| `event(seq)` ×5（0/123/10.4M/17.3M/末） | 0.001–0.023 s | — | 均成功 |
| `navigate` ×4 模式 ×±（80 次） | 中位 **0.0008 s**；**P95 0.988 s**；最大 1.93 s | — | instruction 模式有 0.6–1.9 s 尖峰；2 次失败（触到缺陷段） |
| `window(0,200000)` | **2.47 s** | 0.0013 s | 局部窗口 |
| `window(17,309,891,17,509,891)` | **2.29 s** | 0.0009 s | 局部窗口 |
| `window(0,last)` 全段 | **376.23 s**（修复前失败；修复后实测） | — | 远未达 ≤1 s；缺陷闭环见 9.4 |
| `function_timeline(0,200000)` | **4.08 s** | 0.0012 s | 1600 桶 |
| `function_timeline(0,last)` 全段 | **497.13 s**（修复前失败；修复后实测） | — | 同上 |
| `issues(0,100)` | 0.001 s | 0.001 s | total=52,673（每段约 527，合成复读） |

**3× 对照 store**（`record-3x`，607,757 事件，可全量展开；用于区分"合成数据语义"与"写出器缺陷"）：
open 0.041 s；首页 0.127 s；`window(0,200000)` 3.68 s、全段 `window(0,607756)` **10.81 s**；
`function_timeline(0,200000)` 5.61 s、全段 **17.37 s**；navigate P95 1.63 s（instruction 模式尖峰
0.25–4.7 s，其余模式约 1 ms）；缓存均 1 ms 级。
**3× 全量逐事件对照**（旧解码器 text 路径为 oracle）：607,757 条事件、607,686 条指令
**全部字段 0 差异**，analysis 0 差异；summary 仅多 3 个"新增元数据键"
（`captureScope`/`captureStart`/`laFilter`，写入器从 `session.json` 补齐，属表示差异，
见证据 `oracle-3x.json`）。

按 3× 的全段耗时线性外推（仅量级参考，不外推为事实）：1 s 全段 window ≈ 250 s、
timeline ≈ 570 s——即使没有缺陷，**全区间首次统计也远未达 §3.1 的 ≤1 s**（P4 的块级预聚合未做）。

### 9.4 缺陷闭环：P3 写出器 `internal_unknown` 多算（t11 修复）

100× store 读侧拒绝 **15 条展开段**：`TraceStoreError: 展开段内部谓词未知计数不符`
（seq 1,621,778 … 17,301,890；3 次构建一致；扫描脚本 `scan_defects.py`）。根因：写入器
`flush_run` 用增量计数器 `run_predicated` 计内部谓词未知，当 checkpoint 恰好把"最后一个
内部指令"当成段末（tail）时多算 1；读侧按 `identifiers[:-1]` 校验。
**这是 P3 写出器的真实缺陷，不是合成数据的语义问题**：3× 合成 store 与四份真机记录均可正常展开，
只有 100× 合成在 checkpoint × 谓词未知边界上触发。修复由 t11 负责（`flush_run` 改为按
`identifiers[:-1]` 的 `static.predicated` 求和，与 P1 参考写入器一致）。

<!-- 缺陷闭环：t11 完成后在此补"修复前失败/修复后通过"数字 -->

**闭环（修复候选 = 当前工作树写出器 `fcca9207…`，t11 修复中；正式结论以 t11 为准）**：
同一合成输入重建 store（190.42 s；78,099,690 B，+1 B 为计数编码纠正），
`scan_defects.py` 复扫 **15 → 0 条**；全段 `window(0,last)` **从失败变为通过（376.23 s）**、
`timeline(0,last)` **从失败变为通过（497.13 s）**；缺陷 seq 上的 `page`（0.042/0.034 s）、
`event` ×5（0.029–0.040 s）、`navigate` 16/16（P95 0.220 s）全部通过。
即：**缺陷消除，但性能结论不变**——全段统计仍是分钟级，未达 §3.1 ≤1 s（P4 块级预聚合未做）。
t11 的独立验证（100× 15→0、四份真机记录逐事件 0 差异、内容块 20/20）见
`docs/validation/20261020-ds-etm-p3-flushfix-b2d8148/`（其中 `unknownExecution` 修正为
86,614，修复前多算 15）。

### 9.5 方案 §3.1 逐项（合成 1 s，非真机；修复前快照）

| # | 指标 | 目标 | 合成 1 s 实测 | 判定 |
|---|---|---|---:|---|
| T1 | 200 ms 新输入完整处理 | ≤3 s | —（本任务不含 200 ms） | 不适用 |
| T2 | **1 s 新输入完整处理** | ≤10 s，争取 5 s | **141.25 s（中位）** | **未达标**（约 14×；stream 97%） |
| T3 | 首次打开已处理记录（`objectLayerPartial`） | ≤1 s | open 0.376 + overview 0.004 + 首页 0.126 ≈ **0.51 s** | **仅对象层部分请求达标**；`firstScreenWithTimeline`（完整首屏，含默认轨迹图）**未通过**——全段 timeline 497.13 s，R0 修复前会阻塞首页行；见 §10 |
| T4 | 1 s 全区间首次统计 | ≤1 s | 全段 window **376.23 s**、timeline **497.13 s**（修复后实测；修复前被缺陷阻塞）；局部 200k window 2.47 s | **未达标**（约 376×） |
| T5 | 已缓存相同区间 | ≤100 ms | window/timeline 缓存 **0.9–1.4 ms**、overview 5.4 ms | 达标 |
| T6 | 局部定位/翻页/导航 P95 | ≤200 ms | 翻页 0.005–0.013 s（达标）；**导航 P95 0.988 s**（重点：instruction 模式尖峰） | **未达标**（导航） |
| T7 | 1 s 全流程峰值 RSS | ≤2 GiB | **392.6 MiB** | 达标 |
| T8 | 新派生数据体积 | ≤512 MiB | **74.5 MiB**（3.93 B/指令） | 达标 |

结论：新链路在 1 s 量级输入上 **RSS/体积/首开/缓存达标**，**首次处理与全区间统计/导航未达标**；
其中全区间查询当前还被 P3 写出器缺陷阻塞（t11 修复后需复测）。**不宣称 1 s 达标，默认 200 ms 保持。**

### 9.6 限制（不得当成 1 s 验收）

1. **不是真机连续 1 s**：段间是重复 capture 的字节拼接，事件数比 100×10 ms 少 5.74%、
   原生范围少 10.3%；不能推断真机 1 s 的字节/语义行为。
2. **无真机采集/上传/保存环节**：LA 深度、USB 稳定性、save 完整性未验证（见 checklist §1/§2）。
3. **共享机负载**：3 次波动 123–159 s（约 29%），未做安静窗口复测；绝对秒数不是固定值。
4. **浏览器/完整首屏未验证**：本节是对象层（适配器）数据，非页面/HTTP 端到端。0.51 s 只是
   `objectLayerPartial`（open+overview+首页）；含默认轨迹图的 `firstScreenWithTimeline` 在 R1
   快速 timeline 前判为**未通过**（R0 前默认 timeline 全段 497.13 s，旧页面会先 await 它）。
   R0 的加载顺序修复与真实浏览器 A/B 时序见 §10。
5. **全段 window/timeline 仍 O(区间事件数)**：即使无缺陷也需分钟级（P4 遗留，块级预聚合未做）。
6. 原始证据：`docs/validation/20261020-ds-etm-p6-scale-b2d8148/`（report.md、build.json、
   queries-full.json、queries-small.json、oracle-3x.json、capabilities.json、defect-internal-unknown.json、
   result.json、sha256.json、logs/）；复现命令见该目录报告。

## 10. R0 首屏计时口径与加载顺序（2026-10-20）

本节把“首屏”拆成两级，并给出 R0 在真实无头浏览器里的 A/B 时序。背景：§9.5 的 **0.51 s**
只是 `objectLayerPartial`（open+overview+首页 page）；R0 前 `history.js` 在请求首页行前
`await renderTimeline()`，大样本默认轨迹图（全段 497.13 s）会把首屏拖到 45 s 超时，
所以 **0.51 s 不能写成“首屏达标”**。**`firstScreenWithTimeline`（完整首屏）在 R1 提供快速
默认 timeline 之前判为未通过。**

### 10.1 字段定义

| 字段 | 起止边界 | 包含 | 不包含 |
|---|---|---|---|
| `objectLayerPartial` | 打开记录 → overview + 首页 page（首批执行流行）就绪 | store 打开/校验、查询、payload 序列化 | 默认轨迹图 timeline、浏览器渲染、HTTP 传输 |
| `firstScreenWithTimeline.objectLayer` | 打开记录 → 首批执行流行可用 **且** 默认轨迹图响应就绪（或明确失败/超时） | 上行 + 默认轨迹图请求（对象层，不含 DOM 绘制） | 浏览器绘制、后续交互 |
| `firstScreenWithTimeline.browser` | 点击“打开”→ 首批执行流行出现在 DOM **且** 默认轨迹图数据就绪（或明确显示失败） | 上行 + 真实浏览器加载/解析/渲染 | 用户后续交互（翻页/搜索/局部窗口/导航） |
| `progressFirstVisible` | 点击“打开”→ 轨迹图摘要行出现“正在加载默认轨迹图…Ns” | R0 新增的图表进度提示（不挡表格） | 图表数据就绪 |
| `timelineFailureVisible` | 点击“打开”→ 轨迹图摘要行出现失败提示，且首批行仍在 | R0 新增的失败隔离 | 图表可用性 |

约定：任何只含 open+overview+page 的秒数只能标 **`objectLayerPartial`**；`firstScreenWithTimeline`
必须按真实网页加载顺序（overview → 首批行 → 默认轨迹图）测，并接受“图表失败/超时但首批行可用”
是**失败隔离**而不是**完整首屏达标**。`progressFirstVisible` 只描述可用性，不参与 ≤1 s 判定。

### 10.2 R0 修复内容（可用性修复，不是性能达标）

- `etm/web/app/history.js`：`render()` 不再 `await renderTimeline()`；默认轨迹图改为独立任务
  （`startTimelineLoad`），overview/首页行/表格先渲染；同一 key 的请求合并，翻页/重绘不重复触发；
  进度提示只更新轨迹图摘要行；timeline 失败/超时只把轨迹图标为失败（`timelineKey` 不写入缓存，
  可重试），不影响表格、分页、源码与其它视图；折叠/展开与视图切换行为保持。
- `etm/web/app/view.js`：**超时值保持不变**（overview 300 s / window 180 s / 其它含 timeline 45 s），
  仅补注释说明“不延长超时冒充性能修复”。
- `etm/web/index.html`：轨迹图摘要行加 `role="status" aria-live="polite"`，说明文字标注
  “慢图/超时不影响执行流与表格”。
- 未重打实验包、未改采集上限（`CAPTURE_MAX_SECONDS=0.2`、`CAPTURE_MAX_SAMPLES=1e8`）、未操作硬件。

### 10.3 真实浏览器实测（无头 Edge + 真实 Flask 服务）

输入：`tmp/p6b-scale/` 合成 store 的临时副本（3× = 607,757 事件；1 s 量级 = 19,863,518 事件，
全段 timeline 此前实测 497.13 s）。每个场景独立 record id（独立 trace-store 适配器缓存，
timeline 均为该 id 的**首次请求**）。页面：`after` = R0 工作树；`before` = HEAD `18919168`
的 `index.html`/`view.js`/`history.js` 静态副本，经 `?api=` 访问同一 Flask。

| 场景 | 页面 | store | 点击→首批行 | 点击→默认轨迹图 | 点击→图表失败 | 说明 |
|---|---|---|---:|---:|---:|---|
| `after-3x` | R0 工作树 | 607,757 事件 | **192 ms** | 16,650 ms | — | 进度提示 155 ms 出现；150 行先可用 |
| `before-3x` | HEAD 页面 | 607,757 事件 | 19,916 ms | 19,876 ms | — | timeline 完成后才请求 page（page 起点≈timeline 终点） |
| `after-1s` | R0 工作树 | 19,863,518 事件 | **954 ms** | —（497 s 量级，未完成） | 45,922 ms | 客户端 45 s 超时后图表标失败，表格仍有 150 行 |
| `before-1s` | HEAD 页面 | 19,863,518 事件 | **60 s 内无行** | — | — | 摘要一直“等待记录”；timeline 45 s 超时后仍无行 |

HTTP 资源时序（页面内 `performance.getEntriesByType('resource')`，毫秒，相对导航开始）：
`after-3x` timeline 16,512 ms、page 26 ms（两者几乎同时发起）；`before-3x` timeline 19,774 ms、
page 15 ms 且 page 起点 = timeline 终点；`after-1s` timeline 45,023 ms 后中止（responseStatus 0）、
page 29 ms；`before-1s` timeline 45,012 ms 后中止、**没有 page 请求**。
同一次 run 内同一 3× store 的两次冷 timeline 为 16.5 s（after）与 19.8 s（before），
共享机负载会让绝对秒数波动，但“首批行是否等待轨迹图”的顺序结论不变（R0 前必等、R0 后不等）。

口径与环境：单次样本；`before` 页面为跨源静态副本（localhost CORS 允许），跨源资源
`transferSize=0`（无 `Timing-Allow-Origin`），主指标是页面内 DOM 时间戳；OS 文件缓存已被
`prepare_records.py` 的复制预热，本次“冷”指 store/适配器首次请求，不宣称磁盘冷缓存。
环境：Edge 155.0.4283.62 headless、Node 24.13.1、Python 3.13.5、HEAD `18919168`。
测量时共享工作树里另有其他成员的未提交 `trace_store_adapter.py` 改动（不在本任务范围；
before/after 两场景服务端完全相同）。

原始证据：`docs/validation/20261020-ds-etm-r0-firstscreen-1891916/`
（`measurements.json`、`browser_first_screen.mjs`、`serve_records.py`、`prepare_records.py`、
`logs/browser-measure.log`、报告与 JS 测试日志）。

### 10.4 结论

- **`objectLayerPartial` ≈0.51 s 仍只是对象层部分请求，不是首屏达标。**
- **`firstScreenWithTimeline` 未通过**：3× store 的默认轨迹图约 16.5–19.8 s（同一次 run 的两次
  冷请求）；1 s 量级 store 触发 45 s 客户端超时（服务端全段 timeline 497.13 s 量级）。R0 只修
  “基础页/行不被慢图挡住 + 有进度 + 失败隔离”，不改变轨迹图本身的耗时。
- R1 提供快速默认 timeline 后，必须按本节 `firstScreenWithTimeline.browser` 重新实测；
  在那之前不得写成完整首屏达标。

### 10.5 未覆盖项

1. 真机 1 s 记录（本测量用合成 store）、不同浏览器/机器、重复样本与安静窗口未覆盖。
2. `before` 页面经静态服务器跨源 + `?api=` 访问 Flask（线上桥同路径），不是同源部署；
   跨源资源无 `Timing-Allow-Origin`，只采用 DOM 时间戳。
3. 1 s 量级 store 的默认轨迹图未等到完成（已知 497.13 s 量级），只验证 45 s 超时后的
   失败隔离与表格可用性。
4. 极端条件（并发刷新、浏览器后台节流、网络桥断开、磁盘满）未覆盖。

