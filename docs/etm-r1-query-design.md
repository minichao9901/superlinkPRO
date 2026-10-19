# ETM R1 查询索引与导航设计（预研）

- 日期：2026-10-20
- 分支：`ds/etm-1s-r0r3`（`1891916`；任务 t3）
- 作者：p2-native（R1 预研/导航与原生辅助负责人）
- 状态：**设计 + 可行性原型**（不改产品代码；原型与证据在 `docs/validation/20261020-ds-etm-r1-design-1891916/`）
- 上游依据：`docs/etm-1s-next-steps-20261020.md` §2/§4/§5；Codex 复核
  `docs/validation/20261020-codex-ds-next-1685c01-6e93c42a/`
- 关联任务：t3（本文件）、t4（p1 实现 window/timeline 快路径 + 索引构建）、t5（p2 导航/原生辅助）
- 一句话结论：现有 store 的 `window/function_timeline/navigate` 成本与逻辑事件数同量级，必须替换算法；
  按本文的**持久化查询索引（新块类型 12）+ 单元格聚合 + 路径/函数区间 + 稀疏状态表**，
  19,861,216 条指令的合成 1 s store 上全段 window/timeline 可做到 **首次冷查询 ≤1 s**
  （原型实测见 §7），字段与参考实现逐字段 0 差异；**R1 不需要 C++ 查询 helper，
  但 R2 的索引构建必须由原生 builder 一并完成**（Python 构建 19.86M 需分钟级，见 §5）。

---

## 1. 现有 store 与读路径成本核实

### 1.1 块布局（trace-store v1）

`etm/tools/trace_store.py` 常量见 L47–L132；块类型 L67–L83；序列记录类型 L86–L89；
NAV 模式 L91–L97。单文件 `store.bin` = 56 B 头 + 24 B/块索引 + 块负载，
块可单独压缩（zlib，`flags&1`）；`manifest.json` 是唯一发布点（`_publish_manifest`，L1587），
`generationTag` 防止混读两代（`_Container.__init__` L325–L331）。

| 块 | 类型 | 内容 | 3× store（607,757 事件） | 19.86M store（19,863,518 事件） |
|---|---|---:|---:|---:|
| `strings` | 1 | 字符串池 | 1 块 / 16,247 B / 1,181 项 | 1 块 / 16,268 B / 1,183 项 |
| `static` | 2 | 静态指令字典（PC/机器码/助记符/函数/文件/行/flow/谓词） | 1 / 29,442 B / 1,719 条 | 1 / 29,208 B / 1,719 条 |
| `sequence` | 3 | 展开段/事件/检查点（唯一的事件级数据） | 2 / 1,946,899 B / 96,698 记录 | 65 / 67,931,849 B / 3,194,439 记录 |
| `packets` | 4 | ETM 包（offset/hex/说明） | 1 / 278,250 B / 52,806 | 9 / 9,275,000 B / 1,760,200 |
| `nav` | 5 | 稀疏导航 seq 列表（call/return/exception/issue/event） | 5 / 8,591 B / 7,720 | 8 / 272,066 B / 244,594 |
| `agg` | 6 | **仅块级计数**（14 个 varint/块） | 1 / 56 B / 2 块 | 1 / 2,000 B / 65 块 |
| `meta` | 7 | 格式/计数/字段存在性/校验信息 | 1,084 B（压缩） | 1,101 B（压缩） |
| `summary` | 8 | decoded.json 摘要 | 14,440 B（压缩） | 14,927 B（压缩） |
| `sources` | 9 | 源码文本快照 | 284 B | 284 B |
| `issues` | 10 | 谓词未知/边界/包问题区间 | 16,568 B（压缩） | 525,686 B（压缩） |
| `checkpoints` | 11 | 调用状态检查点索引 (seq, block, offset) | 886 B / 149 个 | 29,085 B / 4,881 个 |
| **合计** | | | **2,312,747 B** | **78,097,474 B** |

关键事实：

1. **事件级数据只有 `sequence` 一块**。展开段（`REC_RUN`，L86）用 delta varint 存静态 ID 列表，
   每条约 `10 + 1.2×count` 字节（19.86M 指令摊到 3.42 B/指令，含块头/检查点/事件）。
2. `agg` 块只有 `AGG_KEYS`（L130）14 个整数：`instructions/executed/unknown/notTaken/cycles/rareEvents/calls/returns/exceptions/boundaries`。
   实测写出器只累加了 `instructions/executed/unknown/notTaken/cycles/rareEvents`（`trace_store.py` L627–L631、L647）
   以及事件侧的 `exceptions/boundaries`（L1225/L1229）；**`calls`、`returns` 从未累加，恒为 0**
   （`grep "stats\['calls'\]" trace_store.py` 无命中）。即：现有 AGG **不能**回答函数/源码热点、
   周期贡献、路径/调用贡献、事件位置、周期桶中的任何一项——不是“聚合不够细”，而是这些信息没有落盘。
3. 检查点每 4,096 条指令一个（`DEFAULT_CHECKPOINT_INSTRUCTIONS` L60），`checkpoints` 索引
   只给出 (seq, sequence 块号, 块内字节偏移)；`_checkpoint_at_or_before`（L1991）用二分定位。
4. `nav` 已有真正的稀疏索引：`NAV_CALL/NAV_RETURN/NAV_EXCEPTION/NAV_ISSUE/NAV_EVENT`
   （写入见 L1210–L1234，读取见 `_load_nav` L1822），但 `navigate()` **只用前 4 类**；
   `NAV_EVENT` 虽已存在，却从未被查询路径使用（见 §4.1）。

### 1.2 查询读路径与“为什么必须逐事件展开”的具体代码点

所有查询都收敛到唯一原语 `TraceStore._expand()`（L2001）：

- `_expand` 从 `start` 之前最近的检查点开始，`tracker = _decode_tracker(...)`（L1878）重建
  `CallTracker`（L1967 构造，L1974 `_sync`），然后逐条解码 `REC_RUN`/`REC_EVENT`：
  - **每条指令**调用 `tracker.feed_instruction(...)`（L2080）→ `flow_analysis.CallTracker.feed_instruction`
    （`flow_analysis.py` L187–L287）：维护调用栈、逐函数统计字典、火焰树、热点、周期归因、
    并在栈变化时 `_sync()` 做 `json.dumps(path)`（L154–L161）；
  - **每条指令**构造完整事件字典 `_instruction_event(...)`（L2122）：`bytes.hex()`、`callPath=list(tracker.path)`
    （L2131 复制列表）、字段裁剪（L2132）；
  - 每条事件构造事件字典并 `tracker.feed_event(...)`（L2110–L2113）。
- `events()`（L2135）只是 `_expand` 的薄包装；`event(seq)`（L2151）展开 `seq..seq` 前仍要从检查点
  重放最多 4,096 条指令 + 事件。
- `_window()`（L2311）对 `self.events(lo, hi)` 做**单遍**逐事件处理：
  - 每条指令：桶 LCP（L2349–L2358）、火焰树逐节点累加（L2363–L2382）、函数统计（L2383–L2401）、
    热点（L2402–L2410）；周期路径 inclusive（L2392–L2395）。成本 ∝ 事件数。
- `function_timeline()`（L2465）对 `self.events(lo, hi)` 做**两遍**完整展开
  （第一遍 L2483–L2509，第二遍 L2525–L2544）→ `_expand` 调用次数恰为 2×事件数。
- `page()`（L2197）在需要过滤（search/hide_skipped）时也是**两遍** `events()`（L2229、L2239）。
- `navigate()`（L2560）：
  - `instruction` 模式走 `_scan`（L2615）；**向后扫描时从当前块向前逐块调用 `self.events(lo,hi)`**
    （L2628–L2643），块内最多 1 MB/数十万事件 → 一步后退最坏扫整块；
  - `pc` 目标（L2568–L2578）用 `_scan` 逐事件谓词匹配，命中前要构造所有事件字典；
    未命中时先扫到记录尾、再从记录头 wrap 扫一遍（L2576）→ 最坏 **2×全部事件**；
  - `source` 模式（L2601–L2613）先 `self.event(seq)`（检查点重放）再 `_scan`。
- `_scan` 无索引，全部依赖 `_expand`；`NAV_*` 稀疏表只服务 call/return/exception/issue。

结论（与 Codex profile 一致）：`_expand` 生成器进入次数 ≈ 逻辑事件数 × 遍数（1 或 2），
window/timeline/instruction 导航的成本与 19.86M 同量级；把 `_expand` 内部循环从字典查找改成
局部变量只能降低常数，不能改变量级。

### 1.3 基线复测（本任务实测）

测量脚本：`baseline_queries.py`（`_expand` yields、`CallTracker.feed_instruction` 调用计数）、
`probe_navigate.py`（导航各模式）；原始 JSON/日志在本证据目录。**测量期间 p1-store 正在跑
R0-A 定向回归（重建 200 ms 夹具、哈希大量文件），机器有并发磁盘/CPU 负载，绝对秒数偏高**；
同一脚本在机器空闲时复跑用于对照（见 §1.3.2）。

#### 1.3.1 3× store（607,757 事件 / 607,686 指令；seq 0..607,756）

| 操作 | 首次冷查询 | 重复（同进程） | `_expand` yields | `feed_instruction` | 输出字节 |
|---|---:|---:|---:|---:|---:|
| `window(0,199999)` | 2.10 s | 2.45 / 2.28 s | 200,000 | 199,978 | 358,815 |
| `function_timeline(0,199999)` | 3.75 s | 3.71 / 3.51 s | **400,000**（两遍） | 399,956 | 50,492 |
| `navigate(400000,-1,'instruction')` | 0.59 s | 0.49 / 0.45 s | 64,323 | 64,315 | 33 |
| `navigate(0,1,'instruction',pc=首条PC)` | 0.0001 s | — | 2 | 1 | 28 |
| `navigate(0,1,'call'/'return'/'issue')` | ≤0.00002 s | — | 0 | 0 | 28–35 |
| `event(303878)` | 0.003 s | — | 1 | 742 | 610 |
| `window()` 全段 | 10.70 s* | — | 607,757 | 607,686 | 359,284 |
| `function_timeline()` 全段 | 16.80 s* | — | 1,215,514（两遍） | 1,215,372 | 53,896 |

\* 全段数字是在 p1 R0-A 回归并发负载下测得；空闲复跑见 §1.3.2。

#### 1.3.2 19.86M store（记录-1s；seq 0..19,863,517）

| 操作 | 首次冷查询 | 重复 | `_expand` yields | `feed_instruction` | 输出字节 |
|---|---:|---:|---:|---:|---:|
| 打开 store（`verify='sizes'`） | 0.220 s | — | 0 | 0 | — |
| `window(0,199999)` | 1.91 s | — | 200,000 | 199,978 | 358,817 |
| `function_timeline(0,199999)` | 3.59 s | — | 400,000（两遍） | 399,956 | 50,494 |
| `navigate(400000,-1,'instruction')` | 0.62 s | — | 64,323 | 64,315 | 33 |
| `event(9,931,758)` | 0.014 s | — | 1 | 2,031 | 607 |
| **`window()` 全段** | **295.52 s** | — | **19,863,518** | 19,861,216 | 558,799 |
| **`function_timeline()` 全段** | **528.56 s** | — | **39,727,036**（两遍） | 39,722,432 | 164,286 |
| `navigate(0,1,'call'/'return'/'issue/exception')` | ≤0.00002 s | — | 0 | 0 | 28–35 |

与 Codex profile 对照：3× 的 `window` 200,001 / `timeline` 400,002 / `navigate` 64,323 次
`_expand` 进入点在本实现中分别为 200,001 / 400,002 / 64,324（含最外层生成器一次），**数量级一致**；
19.86M 全段 = 19,863,518 / 39,727,036 次，恰为事件数 ×1 / ×2。

重复查询（同进程再查，无内部结果缓存）：`window(0,199999)` 3.45/2.81/3.52 s、
`function_timeline(0,199999)` 4.09/4.56/3.97 s、`navigate(400000,-1)` 0.52/0.50/0.64 s
（`baseline-1s-repeat.json`）——与首次同量级；`TraceStore` 不缓存结果，缓存只在
`trace_store_adapter` 层（命中约 1 ms）。

#### 1.3.3 导航基线（3× store，`probe_navigate.py`）

| 用例 | 首次耗时 | `_expand` yields | 说明 |
|---|---:|---:|---|
| instruction 前进一步（seq=1000） | 0.008 s | 1 | 目标记录恰为指令 |
| instruction 后退一步（seq=400000） | 0.98 s | 64,323 | 向后扫整块 |
| instruction 后退一步（seq=last/2） | 5.61 s | 303,878 | 扫整块（块更大） |
| PC 前向命中（记录 80% 处的 PC） | 0.91 s | 49,994 | 命中前构造全部事件 |
| PC 后向命中（同上 PC，从末尾） | 4.49 s | 272,079 | 同上 |
| **PC 无命中** | **19.53 s** | 1,215,513 | 先扫到尾部，再 wrap 扫到头部（2×） |
| source 前向 | 0.0002 s | 3 | 附近即变化 |
| source 后向 | 4.65 s | 272,080 | 同上 |
| call/return/exception/issue 前/后 | ≤0.00003 s | 0 | 走 `nav` 稀疏表 |

结论：`call/return/exception/issue` 已满足 P95 ≤200 ms（微秒级）；**instruction ±1、PC、source**
在现有实现下是 0.9–19.5 s，必须换算法（§4）。

### 1.4 规模画像（用于体量估算）

`probe_structure.py`（单遍解码 run，无事件字典、无 CallTracker）：

| 指标 | 3× store | 19.86M store |
|---|---:|---:|
| 事件 / 指令 / run | 607,757 / 607,686 / 96,478 | 19,863,518 / 19,861,216 / 3,187,256 |
| run 长度 mean / P50 / P95 / max | 6.30 / 5 / 11 / 168 | 6.23 / 5 / 11 / 168 |
| 静态 PC / 函数 / 文件 / (file,line) / 热点 | 1,719 / 54 / 16 / 407 / 416 | 1,719 / 54 / 16 / 407 / 416 |
| 谓词未知静态 ID | 30 | 30 |
| 周期锚点（带 cycleCount 的指令） | 6,284 | 207,947 |
| 周期总量 / 单条最大 | 1,683,183 / 401 | 55,710,825 / 401 |
| 函数变化次数 | 5,005 | 157,683 |
| **源码行变化次数（line 值 / (file,line) 区间）** | 106,552 / 106,740 | 3,535,114 / 3,540,831 |
| 热点变化次数 | 110,321 | 3,655,380 |
| 路径区间数（`PATH_SEG`，原型 builder 实测） | 5,006 | 157,684 |
| 路径树节点数（`NODE`） | 161 | 161 |

要点：源码行变化密度约 1/5.7 条指令（19.86M → 3.54M 次），**源码导航索引不能按指令存**，
必须存「行区间」且按 varint/位图压缩；函数/路径变化密度低一个量级（1/126 与 1/63 条指令），
函数区间/路径区间是低成本高收益的聚合骨架。

---

## 2. R1 目标与不可破坏语义

### 2.1 性能门槛（来自上游 §4.4）

- 现有规模 store：**首次冷**全段 `window` 与 `function_timeline` 各 ≤1 s；
- 至少 50 个固定种子局部/跨块窗口做**逐字段对照**并计时；
- 导航覆盖正反向/稀疏命中/无命中/回绕，**P95 ≤200 ms**（首次与重复分开记录）；
- 普通大窗口不得出现与逻辑指令数同量级的 Python 事件字典构造 / `CallTracker.feed_instruction()`；
- 索引构建时间与体积必须记录，并纳入新输入处理总预算（不能把分钟级成本藏到首个查询之后）。

### 2.2 必须保持的窗口语义（上游 §4.3）

精确区间首尾、周期只计归属指令、未知/未成立状态、首事件/首指令、call 进入窗口的前驱关系、
IRQ pending 状态、mixed/common-prefix 路径、marker 顺序、隐藏未成立与搜索口径。

本文的处理方式：**先写合并规则（§3.5），再用跨调用/异常/检查点的窗口做逐字段对照**；
原型已对 3× store 的 50 个固定种子窗口（200 条～600k 条指令，含跨块、跨检查点、含异常/IRQ 区间）
以及 19.86M store 若干区间做逐字段比对（§7）。

### 2.3 参考实现的“隐藏口径”清单（必须照抄，不能想当然）

1. **调用计数**：`_window` 只在「前一条**记录**（seq−1）是已成立的 call 指令」时给**目标函数** +1
   （L2399–L2401）；窗口起点之前是 call 时，窗口第一条指令**不计**（循环内 `previous=None`）。
2. **IRQ 入口**：`pending_irq` 由任一事件重置为 `type==9`（L2342）；指令消费后清零（L2396–L2398）。
   等价规则：指令 seq = 事件 seq+1 且事件 type=9 才计中断入口；事件本身必须在窗口内。
3. **桶的 `firstSeq`**：桶由**首条记录**创建（L2335–L2336），`firstSeq` 就是该记录 seq（= `startSeq`），
   **不是**桶内第一条指令 seq；区间级 `firstSeq` 才是首条指令 seq（L2345–L2346）。
4. **桶路径**：`path` 初始为桶内第一条指令的 `callPath`；之后每条指令做 LCP，任一不同即 `mixed=True`
   （L2349–L2358）；`mixed` 时末尾追加 `多段调用（放大查看）`（L2419）。
5. **周期**：`cycleCount` 只落在原生范围的末指令（`_window` L2362/L2392）；`selfCycles` 归函数，
   `inclusive` 沿 `callPath` 的**每个标签**（含递归重复）累加（L2394–L2395）；`totalCycles` = 各函数
   selfCycles 之和（L2420）。
6. **`cycleRanges`**（window 的 `analysis.timing`）是「**有 selfCycles 的函数个数**」（L2449），
   不是周期锚点数（与 `CallTracker.cycleRanges` 不同名不同义）。
7. **热点的 `pc`**：热点键 `(function,file,line)` 首次出现时的指令 PC（L2402–L2407）。
8. **函数/热点列表顺序**：`functions` 按 `instructions` 降序稳定排序（插入序=首次出现序，L2440–L2442）；
   `hotspots` 按 `(cycles, records)` 降序稳定排序（L2443–L2446）；火焰树子节点按创建序
   （= 该窗口内首次出现的路径前缀序）。
9. **timeline 的两处边界**：`records` 只计指令（事件单列 `events`，L2339–L2344）；
   marker 桶的 `startSeq/endSeq` 是桶内首个/末个事件 seq（L2493–L2501）；
   `filteredRecords` 在无筛选时等于区间记录数（L2489）。
10. **隐藏未成立**：`hide_skipped` 只过滤 `executionState=='not-taken'` 的指令（L2486、L2528），
    `unknown` 不被隐藏。
11. **搜索口径**：指令的搜索文本 = `function + file + mnemonic + operands + pc` 的 hex/十进制小写拼接
    （L2253–L2259），事件文本 = `'    '+text`；子串匹配、大小写不敏感。

---

## 3. 持久化查询索引 v1（R1 核心）

### 3.1 承载形式与版本

**结论：新增 `store.bin` 块类型 `BLK_QUERY = 12`（可选块），不用 sidecar 文件。**

理由：manifest 是唯一发布点，`generationTag` 能拒绝混读两代；把索引放进同一 `store.bin` 可以复用
块 CRC、块索引、原子发布（`_publish_manifest` L1587）与旧记录兼容路径；sidecar 需要额外的
原子改名/代绑定/删除策略，R1 不值得。

- `BLK_QUERY` **不进入 `NAV_BLOCK_TYPES` 的必需集合**（旧 reader 会拒绝未知块类型，见 `_Container` L352–L353）。
  兼容策略：**升级读取器**（R1 的 p1 改动）把 12 加入已知类型；旧 reader 读新 store 会明确报
  “块类型未知”而不是错读——这是预期行为（新 store 由新版本写出，旧服务端不读）。
  若要“旧 reader 也能读新 store”，可选方案是把索引写成**同一 store 的第二个文件**并在 manifest
  `files[]` 里声明（旧 reader 会因 `files` 长度 >1 校验失败？`TraceStore.__init__` L1646 循环校验所有文件，
  但 `_Container` 只打开 `store.bin`，多文件不会报错——实测需 p1 确认）。本设计的首选是**新块 12**，
  R1 的读取器版本号（`SCHEMA_VERSION`）**不升**：新块是可选的，旧记录没有它就走慢路径。
- 文件头 `optional_features`（`_Container` L328）增加位 `0x0001 = QUERY_INDEX`；
  `required_features` 必须为 0（不阻塞旧 reader，但旧 reader 会因块类型 12 明确报错，见上）。
- meta 增加 `queryIndex` 段：
  ```json
  {"version":1,"cellInsn":4096,"tables":["funcSeg","pathSeg","node","cellHot","cellFlame",
    "unknown","notTaken","callPair","cycleAnchor","events","pcCells","lineRuns",
    "defaultWindow","defaultTimeline"],"coverage":[0,19863517],
   "builtBy":"python-reference|native","builtAtUnix":...,"buildSeconds":...}
  ```
- **版本/兼容判定**：`TraceStore.__init__` 在加载完 agg/nav/checkpoints 后尝试加载 12 号块：
  - 块缺失 → `self.query_index = None`（旧记录，慢路径，行为与今天完全一致）；
  - 块存在但 `version`/`cellInsn` 不认识、或自洽校验失败 → 记 `issues`（category=`query-index`）
    并 `query_index=None`（**回退慢路径，不静默返回拼接结果**）；
  - 自洽校验：`cellCount×cellInsn ≥ instructions > (cellCount−1)×cellInsn`；函数区间首尾覆盖
    `[0,instructions)` 且 `count>0`；路径区间同样；稀疏表 seq 严格递增且 ≤lastSeq；
    `cell_hotspot_first` 的 cell/offset 在范围内；默认视图的 `range` 与 meta 一致。
  - 写入端在 `write()` 最后**先 assemble 索引块、再发布 manifest**；失败则整代删除（现有语义不变）。

### 3.2 两套坐标：指令序号与逻辑 seq

- **指令序号** `i ∈ [0,N)`：只数指令，用来切单元格、函数区间、路径区间；
- **逻辑 seq** `s`：`_expand` 的序号，包含事件；
- 换算：`s = i + #events(<s)`；`i = s − #events(<s)`。事件总数极少（19.86M 记录中 2,302 条），
  实现用排序事件 seq 数组 + `searchsorted`，O(log E)；批量查询可先向量化换算（`seq_of_insn`），
  原型已验证（§7）。
- **桶/时间轴坐标**：`step = ceil((hi−lo+1)/bins)`（window，L2319）或
  `bin = (seq−lo)×bins//(hi−lo+1)`（timeline，L2493/L2543）。两者都按 **seq** 划分，
  所以「桶内记录数 = 桶内 seq 数 − 桶内事件数」，不需要逐指令数据。

### 3.3 表结构（逐表字段/类型/编码）

所有 `int` 默认小端；`varint` 为 trace_store 的 `_wv/_dvar`（L147/L166）同款编码；
`delta` 为严格递增序列的差分。**原型为便于快速验证使用 numpy 定长数组（npz）**，
产品格式（p1 实现）按下面第 4 列编码写入 12 号块；两者语义字段一一对应，p1 的 Python 参考写出器
与 R2 原生 builder 必须产出**规范化语义相等**的内容（不要求字节相同，见上游 §5.3）。

| 表 | 字段 | 原型类型 | 产品编码 | 规模（19.86M 实测/估算） |
|---|---|---|---:|---:|
| `FUNC_SEG` | startInsn, count, funcID | int32×3 | startInsn delta varint + count varint + func uint16 | 157,684 段 / ~0.9 MB |
| `PATH_SEG` | startInsn, count, leafNode | int32×3 | startInsn delta varint + count varint + leaf varint | 157,684 段 / ≈1.0 MB（varint；原型 int32×3 = 1.9 MB） |
| `NODE` | parent, labelStringID | int32×2 | parent varint + label varint（label 是字符串池 ID；路径伪标签如 `中断 N` 也进字符串池） | 161 节点 / 1 KB 级 |
| `STATIC_ENRICH` | func, hotspot, pc, flow, predicated | int16/int16/uint32/uint8/1bit | 每静态项 4+2+2+1+1 B | 1,719 项 / 17 KB |
| `CELL_ROW` | firstSeq, insnCount, executed, unknown, notTaken, cycles, cycleRuns, anchor(block,offset,skip) | int64×1+int32×5+int16/uint32/uint16 | 定长行 48 B（便于随机访问） | 4,849 行 / 0.23 MB |
| `CELL_HOT` | count, unknown, first(id<<12\|offset) | uint16,uint16,uint32 | 稠密 C×H；或 CSR 稀疏（`H×C` 超阈值时） | C=4,849,H=416 → 8 B/单元格-热点 → 16.1 MB（原型；zlib 后约 4 MB） |
| `CELL_FLAME` | value, valueCycles | uint16,int64 | 稠密 C×N；N 大时改 CSR | C×N → 7.8 MB（N=161；N 大时改 CSR） |
| `SPARSE_UNKNOWN` | seq, func | int32,int16 | seq delta varint + func varint | 86,614 条 / ~0.5 MB |
| `SPARSE_NOTTAKEN` | seq, func | int32,int16 | 同上 | 2,708,068 条 / ~13 MB（产品可用「tail 位图+func」压缩） |
| `SPARSE_CALLPAIR` | callSeq, targetFunc | int32,int16 | seq delta varint + func varint | 77,147 条 / ~0.4 MB |
| `SPARSE_CYCLE` | seq, hotspot, cycles | int32,int16,int64 | seq delta + hotspot varint + cycles varint | 207,947 条 / ~1.2 MB |
| `SPARSE_HOT_CYCLE` | cell, hotspot, cycles | int32,int16,int64 | 由 CYCLE 派生，可省 | 同数量级 |
| `SPARSE_NODE_SELF` | cell, node, count, cycles | int32,int32,int32,int64 | 稀疏（只存叶节点） | 28,397 项 |
| `EVENTS` | seq, type, textStringID, flags, payload, nextFunc | int32,uint8,int32,int64,int64,int16 | seq delta + 字段 varint | 2,302 条 / ~50 KB |
| `NAV_INDEX` | 现有 5 类稀疏列表（保持块 5 不变）+ **`EVENT_SEQS`（= NAV_EVENT 已有）** | — | — | 244,594 seq / 0.27 MB |
| `PC_CELLS` | CSR：offset[staticCount+1] + (cell, id) 对 | int32[] + uint16 pair | 按 id 分组、cell 递增 delta | 624,713 对 / 1,256,306 B（≈1.2 MB） |
| `LINE_RUNS` | startInsn 列表 + flag（none-line / 退化行） | int32[] + uint8[] | startInsn delta varint + flag 2bit | 3,540,831 段（(file,line) 极大区间）/ ~14.2 MB（delta varint 后约 4.6 MB） |
| `DEFAULT_VIEWS` | 全段 window(1200 bins) + timeline(1600 bins) JSON | zlib | 两个压缩 JSON | 69,135 B（window 47,148 + timeline 21,987） |
| `SEARCH`（R1 可选/后续） | 3-gram → 静态 ID 倒排 + 每 ID 的 cell 计数（可由 `CELL_HOT`/`PC_CELLS` 派生） | — | — | 静态搜索文本共 190,300 B，3-gram 词表 ~10^4 级 |

**体量公式**（产品格式，按 19.86M 实测输入；字符/字节为估算）：

```
index_bytes ≈
  0.9 MB (FUNC_SEG) + 1.0 MB (PATH_SEG) + 0.0 MB (NODE)
+ 0.02 MB (STATIC_ENRICH)
+ 0.23 MB (CELL_ROW)
+ 4,849×416×(2+2+4) B = 16.1 MB (CELL_HOT；zlib 后 ~4 MB)
+ 4,849×N×(2+8) B  (CELL_FLAME；N=161 时 7.8 MB；N=2000 时 97 MB → 需改 CSR)
+ 0.5 MB (UNKNOWN) + 13 MB (NOTTAKEN) + 0.4 MB (CALLPAIR) + 1.2 MB (CYCLE)
+ 50 KB (EVENTS) + 0.27 MB (NAV) + 1.5 MB (PC_CELLS) + 4.6 MB (LINE_RUNS)
+ 0.7 MB (DEFAULT_VIEWS)
≈ 25–40 MB(zlib 前) / 10–18 MB(压缩后)
```

与现 store（78.1 MB）相比，索引约 +13%～+50%（原型实测见 §7），**R2 必须重测体积/RSS**；
若 CELL_FLAME 的 N 在真实 1 s 记录上变大，改用「每单元格节点直方图 CSR」并把 `valueCycles`
改为按 `(cell,node)` 稀疏（只存非零），可把该表压到 ~1 B/指令以下。

### 3.4 window / timeline 每个输出字段的来源

| 输出字段 | 来源（快路径） | 边界处理 |
|---|---|---|
| `range` / `resolution` / `step` | 参数与 `first/lastSeq` 算术 | — |
| `timeline[k].startSeq/endSeq` | 桶边界算术（seq 连续记录覆盖整个区间） | 末桶截断到 `hi` |
| `timeline[k].firstSeq` | = `startSeq`（参考实现的桶创建口径，§2.3-3） | — |
| `timeline[k].records` | 桶内 seq 数 − 桶内事件数 | — |
| `timeline[k].unknown/notTaken` | `SPARSE_UNKNOWN/NOTTAKEN` 二分计数 | 桶边界二分 |
| `timeline[k].events` | `EVENTS` 表按 seq 切片 | — |
| `timeline[k].path/mixed` | `PATH_SEG` 与桶指令范围求交，逐段 LCP | 段可跨桶：只取交集部分 |
| `firstSeq`（区间级） | 首条指令 seq（跳过开头事件） | 无指令则 `null` |
| `analysis.flame` | `CELL_FLAME` 覆盖单元求和 + 边界单元逐指令 + `SPARSE_NODE_SELF` | 边界 ≤2×4,096 条指令有界解码 |
| `analysis.functions[]` | `FUNC_SEG`（instructions）+ `SPARSE_UNKNOWN/NOTTAKEN`（按函数）+ `SPARSE_CALLPAIR`（calls）+ `EVENTS(type=9,nextFunc)`（interruptEntries）+ `CELL_FLAME` 节点周期按标签归并（selfCycles/totalCycles） | 段与稀疏表在 seq/指令序号上闭合，**无边界误差** |
| `analysis.hotspots[]` | `CELL_HOT`（records/unknown/cycles/first）+ 边界单元逐指令 | 首次出现 id→`st_pc` 得 `pc` |
| `analysis.observedCalls` | `SPARSE_CALLPAIR` 在 `[lo,hi−1]` 的条数 | — |
| `analysis.timing.*` | 由上面函数表派生（totalCycles/executedInstructions/instructions/cpi/cycleRanges） | — |
| `function_timeline.lanes` | `FUNC_SEG` 按 (function,bin) 分配 − `SPARSE_UNKNOWN/NOTTAKEN` 按 (function,bin) 扣除；top/other/unknown/not-taken 组装 | 段-桶重叠按 seq 算术 + 事件扣除，精确到单条指令 |
| `function_timeline.markers` | `EVENTS` 表按 bin 聚合 | — |
| `function_timeline.summary.filteredRecords` | 无筛选时 = `hi−lo+1`（指令+事件） | — |

### 3.5 合并规则规范（先规范、后实现）

定义：`I=[lo,hi]` 为请求的 seq 闭区间；`I_insn=[i_lo,i_hi]` 为其中指令序号闭区间（可空）；
单元格 `cellSize=4096`；「覆盖单元格」= 完全落在 `I_insn` 内的单元格，「边界单元格」= 与 `I_insn`
相交但未完全覆盖的单元格（0～2 个）。

1. **首尾**：`i_lo = insn_of_seq(next_instruction(lo))`，`i_hi = insn_of_seq(prev_instruction(hi))`；
   若 `next_instruction(lo) > hi` 则区间无指令（`firstSeq=null`，函数/火焰为空，桶只有事件）。
   任何聚合都以**指令**为单位；事件只进 `events/markers/unknown/notTaken 边界语义`。
2. **桶边界**：桶 `k` 为 `[lo+k·step, min(hi, lo+(k+1)·step−1)]`；桶内记录数 =
   (`high−low+1`) − (`#events∈[low,high]`)；`unknown/notTaken` 用稀疏表在 `[low,high]` 上的计数；
   `PATH_SEG` 与桶求交时，路径 LCP 只对**落在桶内的指令**生效（段首尾裁剪）。
3. **调用进入**：对目标指令 `j>i_lo`，若 `seq(j−1)==seq(j)−1` 且 `j−1` 的静态 flow=`call` 且
   `executed=True`，则目标函数 +1；`j==i_lo` 不计（参考口径，§2.3-1）。
   跨单元格/跨块由 `SPARSE_CALLPAIR` 统一回答：只取 `callSeq ∈ [lo, hi−1]`（保证目标指令在窗口内）。
4. **IRQ pending**：只统计 `type=9` 事件 `e`，且 `e∈[lo,hi−1]`，且 `e+1` 是**指令**；
   目标 = `e+1` 指令所属函数（`EVENTS.nextFunc` 在构建时解析，-1 表示 e+1 不是指令）。
   窗口前的事件不计（参考口径，§2.3-2）；连续事件时只有最后一个可产生入口（因为 e+1 必须是指令）。
5. **marker 顺序**：事件按 seq 严格递增；同一 bin 的 `types` 计数与 `endSeq` 更新按 seq 顺序；
   `markerCount` = 事件条数；marker 列表顺序 = 首次出现的 bin 顺序（即 bin 升序）。
6. **周期归属**：`cycleCount` 只出现在 run 末指令（`_expand` L2071–L2075）；`selfCycles` 归该指令函数；
   `valueCycles`（火焰/inclusive）沿该指令 `callPath` 的每个节点累加；周期锚点表 `SPARSE_CYCLE`
   用于边界单元格；覆盖单元格用 `CELL_FLAME.valueCycles` 预聚合，两者不重不漏（边界/覆盖划分互斥）。
7. **未知/未成立**：`unknown` ⟺ 静态 `predicated`（IT 条件未证实）；`notTaken` ⟺ run 末指令且
   `tail=False` 且非谓词；二者都属指令状态。函数/热点/桶归属按指令；`knownExecuted = instructions −
   unknown − notTaken`（与参考 L2389–L2391 同义）。
8. **callPath / 跨调用**：指令的 `callPath` 由 `PATH_SEG`（leaf node）给出；call 指令自身的 path
   **含被调帧**（`CallTracker.feed_instruction` 在 call 分支先压栈再产出事件），路径区间在「sync 后
   第一条指令」处切换；窗口起点落在段中间时，路径仍取该段 leaf（与参考一致：参考也从检查点前推得到相同栈）。
   common-prefix/LCP：桶内所有指令 path 的 LCP；**任何一条 path 与当前前缀不同 → `mixed=True`**；
   LCP 结束后若 `mixed` 追加标记。递归（同一函数在 path 中出现多次）保留重复节点。
9. **跨调用/异常/检查点的窗口**：上述规则不引用检查点（检查点只是慢路径重放用的物理锚点）；
   因此窗口语义与 seq 块/检查点边界无关——原型对跨块窗口逐字段验证（§7）。
   例外：`event(seq)` / `page(search)` 的慢路径仍用检查点，语义不变。
10. **隐藏未成立**：`hide_skipped` 只影响「输出行过滤」与 timeline 的 `not-taken` lane 是否出现
    （参考 L2486/L2521–L2523），不改变 `records/unknown` 计数；快路径用 `SPARSE_NOTTAKEN` 实现。
11. **搜索**：静态指令的搜索文本只由静态字段决定，可把「匹配的静态 ID 集合」预先算出
    （扫描 `static` 表即可，1,719 项）；事件文本需事件表；分页 total 由「匹配 ID 的 (cell,count) 和」
    与事件表计算，行定位按 cell 顺序 + 有界解码。R1 第一批可先保留慢路径（语义不变），
    提速按 §3.3 的 3-gram 倒排实现。

### 3.6 默认全段视图的预计算与持久化

- **window(全段, bins=1200)** 与 **function_timeline(全段, bins=1600)** 在**构建索引阶段**用同一套
  查询算法生成，zlib 压缩后进 `DEFAULT_VIEWS` 表（各 0.6 MB / 0.2 MB 级，见 §7）。
- 命中条件：`start is None and end is None`（或等价于 `[firstSeq,lastSeq]`）、`bins` 与
  `search=''`、`hide_skipped=False`、`bins` 等于预计算值（1200/1600）。命中 → 解压即返回，
  实测 7–14 ms（含解压）。
- **不命中**（任意区间、任意 bins、search/hide_skipped）→ 走 §3.4 的算法路径，结果仍精确；
  局部/筛选路径的计时在 §7 报告。这样既满足「默认图持久化」，也不是“只缓存默认图”。
- `meta.queryIndex.defaultViews` 记录两个视图的 `{bins, range, sha256}`；读取端校验
  `range` 与 store 一致，否则忽略默认视图并走算法路径（防止用旧代/篡改视图冒充）。

### 3.7 构建成本与落盘策略

- 构建是**单遍顺序扫描** `sequence` 块：每指令 O(1)（路径状态机只在栈变化时做 O(depth)），
  每单元格一次 numpy 归约；**不构造事件字典、不调用 `CallTracker` 的统计/热点/火焰**（原型用
  语义等价的 `PathTracker`，见 §7.1）。
- 19.86M 的 Python 参考构建：138.0 s（不含默认视图 3.8 s（window 2.34 + timeline 1.44））；
  这证明「一次性迁移」可行，但**不能**进入 R1 的 200 ms/1 s 处理预算 → R2 原生 builder 必须
  在解析记录的同一次遍历里直接产出索引块（§5）。
- 旧记录迁移：p1 提供 `--build-query-index` 或 `trace_store_write.py` 的一次性 CLI；
  迁移失败的旧记录仍可读（`queryIndex=None` → 慢路径）。

---

## 4. 导航设计

### 4.1 instruction 前/后一步（有界定位，不重放）

**结构**：无需新表——`NAV_EVENT`（块 5，所有事件 seq，严格递增，已在每个 store 中）+
「指令 seq 连续」不变量（`write()` 要求 seq 从 0 连续，L1102–L1105；`__init__` 校验 L1702）。

**算法**：前进：`s = seq+1`；若 `s` 是事件（`bisect_left(EVENT_SEQS,s)` 命中），跳过**连续**事件段
（事件很少、连续段更少，最坏 O(k)），返回第一个指令 seq；超过 `scoped_hi` → `None`（不 wrap，
与参考一致）。后退对称（`bisect_right−1`）。复杂度 O(log E + k)，k ≤ 连续事件数（实测 ≤2）。
**不再**从块首扫描，也**不再**恢复 CallTracker（参考实现为找到一条指令会重放最多 30 万条事件）。

### 4.2 call / return / exception / issue（沿用现有稀疏 NAV）

| 模式 | 索引 | 参考谓词（`record_store.py` L580） | 快路径 |
|---|---|---|---|
| call | `NAV_CALL` | `flow='call' AND state='executed'` | bisect O(log) |
| return | `NAV_RETURN` | `flow='return' AND state='executed'` | bisect |
| exception | `NAV_EXCEPTION` | `type IN (9,10)` | bisect |
| issue | `NAV_ISSUE` | `state='unknown' OR type IN (1,2,6,7,8)` | bisect |
| event | `NAV_EVENT` | 任意事件 | bisect（供 §4.1 使用） |

边界：`scoped_lo/scoped_hi` 过滤与方向由 bisect 后线性跳过越界项（每侧最多 1 项）；
无命中返回 `{"seq":null,"wrapped":false}`；不回绕（参考 call/return/issue 也不回绕）。

### 4.3 PC 导航（静态 ID + 出现位置索引）

**结构**：
- `PC→staticID`：现有 `static_by_pc`（`_load_static` L1785），O(1)；
- `PC_CELLS`：CSR 倒排 `offset[staticID+1] + cells[]`（每项 = 该静态 ID 出现的单元格号，升序）；
  构建时每单元格对 `unique(ids)` 生成 (id, cell)，按 id 分组。规模：19.86M 实测
  624,713 对（约 1,256,306 B（≈1.2 MB）），远小于「每指令一条」的 19.86M 条。
- 精确 seq：候选单元格用 `CELL_ROW.anchor` **有界解码**（≤ `cellSize` 条指令；
  原型实测 10.7 ms（P50 8.6） ms/单元格），命中静态 ID 的指令序号 → `seq_of_insn`。

**算法**（与参考同义）：
1. 主搜索：方向 forward → 从当前指令序号之后的第一个候选 cell 起，找最小 `seq > seq0` 的命中；
   backward → 找最大 `seq < seq0` 的命中；候选 cell 用 CSR 二分定位，必要时逐 cell 前进/后退
   （只在「该 cell 内没有满足位置的命中」时移动，通常 1 个 cell）。
2. 回绕：参考在未命中时**总是从记录头向前**再找一次（`trace_store.py` L2576；`record_store.py`
   L595–L596），返回第一个命中并 `wrapped=true`；快路径直接取该 PC 的**第一个**出现
   （CSR 第一项 + 有界解码第一个 cell）。
3. 无命中/PC 不存在：CSR 中该 ID 为空 → 直接 `{seq:null}`，**O(log S)**，不再扫 19.86M 事件。

**复杂度**：O(log S + K·(log + cellSize))，K = 需要的候选单元格数（通常 ≤2）。

### 4.4 源码导航（行区间索引）

**结构**：`LINE_RUNS`：把指令序列切成「(file,line) 相同的极大区间」，只存每个区间的
`startInsn`（严格递增）与 `flag`（`line is None` / 退化 `(file='',line=0)`）。
规模：19.86M 实测 3,540,831 段（按 (file,line) 极大区间；其中仅 line 值变化为 3,535,114 次）；产品编码 startInsn delta varint + 2 bit flag ≈ 4.6 MB
（原型按 int32 存 ≈14.2 MB，仅用于验证）。

**算法**（参考语义：找「line 非空且 (file,line) 与当前不同」的下一条/上一条指令）：
1. 当前指令：`i0 = insn_of_seq(seq0)`；`current = (file,line)` 由静态表给出（事件/指令同一字段）。
2. 前进：`k = 包含 i0 的行区间`；候选 = 区间 `k+1` 的 `startInsn`；若候选 `line is None` → 取
   `k+2`（连续 None 行已合并，最多跳一次）；若候选与 current 相同（退化 `('',0)` 情形）→ 再 +1；
   返回 `seq_of_insn(candidate)`。后退：候选 = 区间 `k−1` 的**最后一条指令** `start_insn(k)−1`；
   同样跳过 None/退化。
3. 无命中（已到边界）→ `{seq:null}`；不 wrap（参考 source 模式不 wrap）。

**复杂度**：O(log L) 二分 + O(1)（无需解码，因为 flag 已区分 None/退化；file/line 比较只在
退化情形需要，最多 2 次静态查表）。

### 4.5 导航索引体量与 P95 预算

| 索引 | 19.86M 规模 | 查询复杂度 | 预期 P95 |
|---|---:|---|---:|
| `NAV_EVENT`（已有） | 2,302 | O(log E+k) | <0.1 ms |
| call/return/exception/issue（已有） | 244,592 | O(log) | <0.05 ms |
| `PC_CELLS`（新增） | 624,713 对 / 1,256,306 B（≈1.2 MB） | O(log S + cell decode) | 8.7 ms（19.86M 单元格倒排 + 有界解码） |
| `LINE_RUNS`（新增） | 3,540,831 段 / ~4.6 MB（varint） | O(log L) | <0.1 ms（设计） |
| instruction ±1 | 0 新增（复用 NAV_EVENT） | O(log E+k) | <0.1 ms（实测 0.011 ms） |

R1 验收要求「导航覆盖正反向/稀疏命中/无命中/回绕，P95 ≤200 ms」：按上表，最重的 PC 导航由
「1～2 次单元格有界解码（10.7 ms，P50 8.6 ms）+ 二分」构成，**不依赖 19.86M 级扫描**；
原型在 3× store 上给出实测（§7.4），19.86M 上给出解析量/复杂度与单 cell 解码实测。

---

## 5. 原生 helper 判定

### 5.1 判定：R1 查询 **不做** C++ helper；R2 索引构建 **必须** 原生

依据（全部为本任务实测，见 §7）：

1. **全段 window/timeline**：持久化默认视图命中时 7–14 ms；非默认参数下，
   19.86M 全段算法路径 window 0.77 s / timeline 0.77 s
   （局部/常规区间 0.01–0.4 s：12 个 1k–200k 区间 window P50 72 ms / max 111 ms）。R1 门槛只要求「首次冷全段 ≤1 s」，默认视图 + 单元格批量
   求和已满足，无需 C++。
2. **导航**：instruction ±1 与 call/return/... 是 O(log)；PC/源码是 O(log + 有界 cell 解码)，
   单次毫秒级，低于 200 ms P95 门槛 2 个数量级 → 无需 C++。
3. **索引构建**：19.86M 的 Python 参考构建 138.0 s（含默认视图）——**不能**进入新输入处理
   预算（200 ms 目标 ≤1.5 s / 1 s 目标 ≤10 s），且 R2 的原生 builder 本来就要做「解析→区间→
   调用状态→聚合」的同一次遍历，顺带写 `BLK_QUERY` 的边际成本远小于再来一遍 Python 扫描。
   因此：**R1 用 Python 一次性迁移旧 store（成本记录在案，不进验收计时）；R2 由原生 builder
   同遍产出索引，Python 实现保留为语义参考与对照。**
4. 反例（为什么不是 R1 就上 C++）：R1 的 hot loop 已被 numpy 批量化（每单元格 O(1) 次归约），
   Python 层只剩「段遍历」（19.86M 全段 157,684 段）与字符串/事件组装；把这部分搬到 C++
   需要新增进程/IPC/版本握手，收益 <0.2 s，反而增加集成风险。上游 §4.1 要求「必要时加原生查询
   helper；中间结构按批次跨 Python 边界」——满足于**构建侧**（R2）而不是 R1 查询侧。

### 5.2 若后续需要原生查询 helper（预留接口）

不在 R1 实现，但格式/接口先定，避免 R2 返工：

```
C ABI（DLL）：
int etm_query_open(const char* store_path, uint64_t generation_tag, etm_query_handle** out);
int etm_query_nav(handle, const etm_nav_request* requests, size_t count,
                  etm_nav_result* results);      // 批量：一次调用处理 N 个导航请求
int etm_query_window(handle, uint32_t start, uint32_t end, uint16_t bins,
                     etm_window_buffer* buffer); // 结果按「列式批次」返回
void etm_query_close(handle);
版本握手：etm_query_version() -> {abiVersion, indexVersion, buildSha}
失败回退：索引不匹配/版本不符 → 返回明确错误码，Python 侧回退慢路径并记录 issue（不静默）
```

约束（上游 §4.1/§5.2）：中间结构按批次跨边界；**不得**为每条动态指令做函数调用/IPC/JSON；
结果最后在 Python 侧组装页面 JSON。R1 不引入该 DLL；R2/R3 若 profile 显示 Python 段遍历成为
瓶颈（>0.3 s），再实现 `etm_query_nav` 的子集（PC/source/instruction），window/timeline 仍走
numpy 聚合或由原生 builder 的索引直接回答。

### 5.3 与 R2 原生 builder 的分工

- R2 `etm_storebuild`（原生）在写 store 的同一次遍历中产出：`FUNC_SEG`、`PATH_SEG`、`NODE`、
  `CELL_*`、`SPARSE_*`、`EVENTS`、`PC_CELLS`、`LINE_RUNS` 与 `DEFAULT_VIEWS`
  （默认视图可在原生侧聚合、Python 只做 JSON 序列化/压缩，或原生直接写 zlib）。
- 验收：`docs/etm-r1-query-design.md` 的语义字段 + 原型 50 窗口对照 + R2 的逐事件对照；
  **不要求**索引块与 Python 参考逐字节一致（上游 §5.3），但要求规范化后逐字段相等。

---

## 6. p1 / p2 接口与文件职责约定（避免 R1 互相改同一文件）

### 6.1 文件职责

| 文件 | 负责人 | R1 改动 |
|---|---|---|
| `etm/tools/trace_store.py` | p1 | 新增 `BLK_QUERY=12` 常量/块校验/加载；`TraceStore.query_index`；`window/function_timeline` 快路径分派；**不改** `_expand`/事件语义 |
| `etm/tools/trace_store_write.py` | p1 | 索引构建器（Python 参考）与 `--build-query-index` CLI；在 `write()` 末尾追加索引块；`manifest.counts.queryIndex` |
| `etm/tools/trace_store_adapter.py` | p1 | 缓存键绑定 generation（R0-A 已做）；默认视图/索引版本进入 `signature()`；查询计时打点 |
| `etm/tools/query_index.py`（新） | p1（格式/编码）＋ p2（只读接口） | 索引编解码共享库：`encode/decode`、`QueryIndex.load(container)`、`window()/function_timeline()` 的纯计算；**只 p1 修改** |
| `etm/tools/trace_store_nav.py`（新） | **p2** | instruction ±1 / PC / source 快路径；只 import `query_index` 的只读 API，不改格式 |
| `etm/tools/trace_store.py` 的 `navigate()` | p1 只加**一行分派** | `navigate()` 检测 `query_index` 可用 → 调 `trace_store_nav.navigate_fast(...)`；失败回退现有 `_scan` |
| `etm/native/*`、`etm/CMakeLists.txt` | p2（R2 起） | `etm_storebuild` 原生 builder；R1 不改 |
| 测试 | p1：索引/窗口/timeline 对照；p2：导航对照（新文件 `test_trace_store_nav.py`） | 互不覆盖 |

**冲突规避**：`trace_store.py` 的 `window/function_timeline/navigate` 三个入口由 p1 统一改
（分支判断 + 回退）；p2 的导航实现全部在新文件 `trace_store_nav.py`，只暴露
`navigate_fast(store, seq, direction, mode, pc, start, end)`；如 p1 需要改导航分派，只允许
「一行调用 + 异常回退」，不内联导航逻辑。

### 6.2 Python API（p1 提供，p2 消费）

```python
# etm/tools/query_index.py （p1）
class QueryIndex:
    @classmethod
    def load(cls, container, meta) -> "QueryIndex | None"   # 块缺失/校验失败 → None
    def window(self, start=None, end=None, bins=1200) -> dict          # 与 TraceStore._window 同构
    def function_timeline(self, start=None, end=None, search='', hide_skipped=False,
                          bins=1600, max_functions=6) -> dict           # 与现状同构
    def default_window(self) -> dict | None      # 持久化默认视图（未命中返回 None）
    def default_timeline(self) -> dict | None
    # 导航只读访问器（p2 使用；不修改索引）
    def event_seqs(self) -> np.ndarray
    def next_instruction_seq(self, seq) -> int
    def prev_instruction_seq(self, seq) -> int
    def insn_of_seq(self, seq); def seq_of_insn(self, index)
    def pc_cells(self, static_id) -> np.ndarray          # CSR slice
    def line_runs(self) -> tuple[np.ndarray, np.ndarray] # startInsn, flag
    def decode_cell_instructions(self, cell) -> tuple    # ≤cellSize 条，边界/导航用
    def close(self)

# etm/tools/trace_store_nav.py （p2）
def navigate_fast(store, seq=0, direction=1, mode='instruction', pc=None,
                  start=None, end=None) -> dict | None
# 返回 {"seq": int|None, "wrapped": bool}；索引不可用/异常 → None（p1 回退慢路径并记 issue）
```

### 6.3 格式契约（写入方 p1 / 原生 R2；读取方 p1/p2 共用）

- 块类型 `12`；块负载：`header(varint: version, cellInsn, tableCount)` + 表目录
  `(tableId, offset, length, encoding)` + 表数据 + `defaultViews`；
- `meta.queryIndex` 字段（§3.1）；`required_features` 不含新位；
- 规范化判定：语义字段（§3.4 的每个输出字段）逐字段相等；**不要求**字节相同、不要求
  字符串 ID/表顺序相同（上游 §5.3）；R2 原生实现的对照脚本用「规范化 JSON 比较」
  （`compare_results.py` 同等逻辑，忽略列表顺序时按业务键排序）。

---

## 7. 可行性原型（本任务实测）

### 7.1 原型组成与正确性前提

证据目录：`docs/validation/20261020-ds-etm-r1-design-1891916/`

| 文件 | 作用 |
|---|---|
| `r1_build_index.py` | 从现有 store 构建 `query.idx`（numpy 原型；`CELL=4096`）；单遍流式 |
| `r1_query.py` | `QueryIndex`：window/function_timeline 快路径 + 边界有界解码 + 导航算法（原型） |
| `path_tracker.py` | 与 `CallTracker` 栈/路径语义逐条等价的快速跟踪器（不做统计/JSON） |
| `validate_path_tracker.py` | PathTracker vs CallTracker 逐条比对（3×：607,686 指令 + 71 事件，0 差异） |
| `probe_structure.py` | 规模画像（§1.4） |
| `baseline_queries.py` / `probe_navigate.py` | 现状基线（§1.3） |
| `compare_results.py` | 逐字段比较（window/timeline；列表按业务键） |
| `build-1s.log` / `build-3x.log` / `*.json` | 原始日志与结果 |

正确性前提：**原型不重放 `CallTracker` 的统计/火焰/热点**，路径状态机用 `PathTracker`
（只复制栈逻辑，去掉 `statistics/root/hotspots/json.dumps`）；`validate_path_tracker.py` 在
3× store 上逐条比对 `callPath` 与 `unknown_restarts`：**607,686 条指令 + 71 事件，0 差异**。

### 7.2 3× store 结果（先验证语义）

| 项目 | 参考实现 | 索引快路径 | 差异 |
|---|---:|---:|---|
| 全段 `window()` | 10.70 s* | **84 ms**（含索引加载 38–50 ms） | **0 字段** |
| 全段 `function_timeline()` | 16.80 s* | **24 ms** | **0 字段** |
| 50 个固定种子 window（200～600k 条指令，含跨块/跨检查点） | 2.1–10.7 s/次 | 中位 79.7 ms，最大 139.8 ms | **0 / 50 失败** |
| 50 个固定种子 timeline（500～300k 条指令） | — | 中位 5.0 ms，最大 20.3 ms | **0 / 50 失败** |
| 索引构建（单遍） | — | 2.53 s | — |
| 索引体积 | — | 1.52 MiB（压缩等价 0.18 MiB） | — |

\* 并发负载下的数字（§1.3），仅用于量级对照；语义对照不受影响。

### 7.3 19.86M store 结果（规模证明）

| 项目 | 参考实现 | 索引快路径 | 差异 |
|---|---:|---:|---|
| 全段 `window()` | 295.52 s（19,863,518 次 `_expand`） | **14 ms（进程内）/ 0.135–0.187 s（冷进程）**（默认视图命中） | 0 字段 |
| 全段 `function_timeline()` | 528.56 s（39,727,036 次） | **7 ms（进程内）/ 0.137–0.156 s（冷进程）**（默认视图命中） | 0 字段 |
| 全段 `window()`（算法路径，非默认 bins） | — | 0.77 s | 与参考对照 0 字段 |
| 索引构建（Python 参考） | — | 138.0 s | — |
| 索引体积 | — | 47.7 MiB（压缩等价 5.3 MiB） | — |
| 索引加载（含 numpy 读盘/解压） | — | 260 ms | — |
| 默认视图解压+返回 | — | 7–14 ms | — |

### 7.4 导航原型（3× store；19.86M 的解析量/单 cell 解码）

| 模式 | 3× store 实测（`nav-3x.json`） | 19.86M 实测（`extra-1s.json`） |
|---|---:|---:|
| instruction ±1 | P95 0.010 ms，60 次对照 0 不符 | P95 0.011 ms，105 次对照 0 不符 |
| PC 导航（含 wrap） | P95 0.136 ms，40 次对照 0 不符（13 wrap） | P95 8.68 ms，300 次与全量出现位置索引 0 不符（39 wrap） |
| PC 无命中 | O(log)，≈0.0001 ms（CSR 空切片） | 同（设计同构） |
| source 导航 | P95 0.186 ms，40 次对照 0 不符 | P95 0.175 ms，30 次对照 0 不符 |
| 单元格有界解码（PC/边界用） | — | P50 8.57 ms / P95 10.68 ms（200 次） |


### 7.5 剩余瓶颈（诚实清单）

1. **索引构建仍是 Python**：19.86M 138.0 s → 必须由 R2 原生 builder 同遍产出（§5.1）；
   R1 只接受「已有 store 的一次性迁移」，并把它计入新输入总预算的独立行。
2. **默认视图之外的 window 全段**：算法路径 0.77 s（段遍历/桶 LCP 是 Python 循环，
   19.86M 全段 157,684 个路径段）；R1 门槛只要求默认全段 ≤1 s，因此可接受；
   若产品要求「任意 bins 的全段 window ≤1 s」，需要把「段→桶 LCP」改为 numpy 向量化
   （设计已给出：节点 min/max DFS 序 + 桶级二分）或放进原生 helper。
3. **search / hide_skipped 快路径**未在原型实现（R1 第一批保持慢路径，语义不变）；
   设计（3-gram 静态倒排 + cell 倒排）已给出体积/复杂度，p1 实现时补齐。
4. **CELL_FLAME 稠密矩阵**在真实 1 s 记录（路径节点数 N 可能远大于合成的 161）会膨胀，
   需按 §3.3 的 CSR/稀疏回退；R2 必须用真实记录重测体积/RSS。
5. **测量环境**：本轮所有秒数在与 p1 R0-A 回归并发时测得，绝对秒数偏高；语义对照结论不受影响。

---

## 8. 验收映射（t3 合同 → 本文件）

| 合同项 | 章节 |
|---|---|
| 块布局与读路径成本（引用 Codex/自测） | §1.1–1.3 |
| 持久化索引结构（块/feature/版本/旧记录兼容） | §3.1–3.3 |
| window/timeline 字段与跨边界合并规则 | §3.4–3.5 |
| 默认 1600 桶 timeline 预计算与持久化 | §3.6 |
| 导航索引（instruction/PC/源码/稀疏 NAV） | §4 |
| 原生 helper 判定（做/不做、接口、预期） | §5 |
| p1/p2 接口与文件职责 | §6 |
| 基线测量（200 ms 与 19.86M） | §1.3–1.4 |
| 可行性原型与实测、剩余瓶颈 | §7 |
| 证据目录 | 目录内 `report.md`/`result.json`/脚本/日志 |

---

## 附录 A：证据文件索引

见 `docs/validation/20261020-ds-etm-r1-design-1891916/report.md` 与 `result.json`；
原始测量：`baseline-3x*.json`、`baseline-1s.json`、`navigate-3x.json`、`structure-*.json`、
`build-*.log`、`out-*/`。**所有证据只读使用既有记录与合成数据，未操作硬件、未改产品代码。**

## 附录 B：测量命令

```pwsh
# 现状基线
python -X utf8 baseline_queries.py tmp/p6b-scale/record-3x/store --out baseline-3x.json --save-dir out-3x
python -X utf8 baseline_queries.py tmp/p6b-scale/record-1s/store --out baseline-1s.json --save-dir out-1s --scale full --sample 1
python -X utf8 probe_navigate.py tmp/p6b-scale/record-3x/store navigate-3x.json
# 规模画像
python -X utf8 probe_structure.py tmp/p6b-scale/record-3x/store --out structure-3x.json
# 原型
python -X utf8 r1_build_index.py tmp/p6b-scale/record-3x/store out-3x-index
python -X utf8 r1_build_index.py tmp/p6b-scale/record-1s/store out-1s-index
python -X utf8 r1_bench.py   # 见证据目录
```
