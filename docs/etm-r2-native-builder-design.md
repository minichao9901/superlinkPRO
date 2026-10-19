# ETM R2 原生 store builder 设计（`etm_storebuild`）

- 日期：2026-10-20
- 分支：`ds/etm-1s-r0r3`（任务 t8；上游 R0/R1 已交付：`7feff7d`、`41172fd`）
- 作者：p2-native（R2 预研；本任务**只写设计**，不改 `etm/` 产品代码）
- 上游依据：`docs/etm-1s-next-steps-20261020.md` §5（R2 原生边界、解除字节布局约束、验收门槛）；
  `docs/etm-r1-query-design.md` §3（查询索引格式）、§5（原生 builder 分工）、§6.3（格式契约）
- 证据目录：`docs/validation/20261020-ds-etm-r2-design-1891916/`（report.md/result.json/原型/日志）
- 结论摘要：
  1. **R2 最小版本** = `etm_storebuild` 三阶段（`prescan` → Python 静态字典 → `build`），
     输入 `native.bin`（+等价 ETM 配置/ELF 元数据），一次 `build` 调用完成**组装 + R1 查询索引
     （块 12）+ 默认视图 + store.bin + manifest 元数据**；manifest 原子发布仍由 Python 完成
     （复用 `trace_store._publish_manifest`），C++ 只写未发布的 generation 目录并返回 JSON 摘要。
  2. **解除字节布局约束（Codex §5.3）**：检查点内嵌状态、字符串 ID、块内顺序允许与 Python
     参考不同；等价用**规范化逐事件/接口对照**证明，不再要求逐字节一致（P3b 的“必须字节一致”
     路线作废，正是它把原生 helper 限制成 Python 流内服务）。
  3. **唯一 PC 预扫描很便宜**：实测 200 ms 类记录 0.16–0.20 s（735,337 个范围 → 22 个唯一范围）、
     19.86M 合成 0.60–0.86 s（4,142,783 → 341 唯一）、ETMv3 10 ms 0.03–0.06 s（172,596 → 122 唯一）；
     静态字典与参考 store **逐字段 0 差异**（59/1,719 条）。建议把预扫描**合并进 `etm_decode`
     的解码回调**（同一次读入），省掉第二遍。
  4. **build 热路径实测下界**（原生原型：范围批量追加 + 展开段/检查点/块模拟 + 单元格直方图）：
     200 ms 0.20 s、19.86M 1.16 s（不含 CallTracker/真实 store 写入）。据此给出
     200 ms ≤1.5 s（争取 ≤1.2 s）、合成规模 ≤4 s 的**分阶段预算**（§11），并标注估算项与
     R2 必须实测的判据；1 s 合成预算偏紧，必须做「预扫描并入解码 + ELF 静态缓存 + 热路径
     批量化」三项优化，否则第一版可能 4.2–4.8 s。
  5. 原生查询 helper 仍不做（R1 已判定）；本文件只定义 `etm_storebuild` 与 R3 集成接口。

---

## 1. 输入 / 输出边界

### 1.1 最小可验证版本（v1，读 `native.bin` 文件）

```text
输入（Python 编排准备）：
  record_dir/etm.bin            （源字节，仅用于绑定/字节核算，不读入 builder）
  record_dir/native.bin         （P2 二进制记录流；主输入）
  record_dir/native-config.txt  （协议/寄存器/内存镜像；决定 OpenCSD 配置，已在 native.bin 时使用）
  record_dir/firmware.elf       （ELF/DWARF；Python 侧静态字典来源）
  record_dir/session.json / sources.json（非派生元数据与源码快照；可选）
  record_dir/decoded.json       （仅取非派生元数据白名单；可选）
  参数：generation、generationTag、checkpointInstructions、cellInsn、bins（默认视图）、
        hashInputs、verifyImage、maxEvents（诊断）
  static-dict.bin               （Python 准备；§3）
  ranges.bin                    （native prescan 输出；§4）

输出（C++ 写在 store_dir/generations/<generation>/）：
  store.bin                     （trace-store v1：块 1–11 + **块 12 查询索引**，含默认视图）
  build-result.json             （计数/阶段计时/文件 sha256/块表/版本握手信息）
  （可选）native.log / 诊断日志
  **不写 manifest.json**——manifest 由 Python 在校验通过后原子发布（§7）
```

C++ 一次 `build` 调用必须完成（上游 §5.2 的清单，逐项在 §6 给出数据来源与验证）：

1. 原生记录解析（元素/包/状态；`native_binary` 同款校验）；
2. 范围 → 末指令/谓词/周期（末指令挂起 + type-12 周期折叠）；
3. 调用/IRQ/返回状态（CallTracker 等价状态机 + 检查点编码）；
4. 动态展开段、检查点、导航（NAV）、块级聚合（AGG）、统计/质量/字节核算；
5. 静态字典/字符串池落块、包块、meta/summary/sources/issues JSON 块；
6. **同遍产出 R1 查询索引（块 12）**：函数区间、路径区间、路径树、单元格热点/火焰聚合、
   稀疏未知/未成立/调用配对/周期锚点、事件表、PC/行区间导航表、默认 window/timeline 视图；
7. `store.bin` 的块索引/CRC/头/`sha256` 与 `build-result.json`。

### 1.2 R2b（可选优化，与 OpenCSD 回调融合）

v1 的输入是 `native.bin`；R2b 允许把 `element()/packet()` 回调直接接到 builder
（不再先写 306 MB 中间文件再读第二遍）。此时：

- 仍需 **native 侧预扫描/唯一范围**：可在解码回调里边走边收集唯一范围（R2b 语义等价）；
- 静态字典必须在解码前可用 → 使用 §3.4 的「全 ELF 字典 + 有界批次补齐」模式
  （实测全 ELF 字典覆盖 10,269 个 PC，参考记录用到的 1,719 个 PC 中 1,706 个字段 0 差异、
  13 个为函数符号之外的代码地址，需要预扫描/惰性补齐）；
- R2b **不是** R2 最小版本的前提；Codex §5.2 明确第一版允许读 `native.bin`。

### 1.3 不变量（不随实现改变）

- **格式规范**：块类型/编码/CRC/头/manifest 校验与 `trace_store.py` 读取端一致；
- **逻辑事件/查询结果**：逐事件字段、`analysis`、`window/timeline/event/page/navigate` 语义与参考一致；
- **未知/周期/质量语义**：谓词未知、周期归属、字节核算、issue/NAV 口径一致；
- **旧记录兼容**：旧 store（无块 12）照旧可读；新 store 的块 12 对旧读取器明确报“未知块类型”，
  不做静默降级；
- **原子发布**：manifest 是唯一发布点；失败 generation 必须删除，读取端不会看到半成品。

---

## 2. 进程形态与 CLI

### 2.1 可执行文件与模式

`etm_storebuild`（与 `etm_decode`/`etm_frontend` 同目录，`etm/build-native/etm_storebuild.exe`）：

```text
etm_storebuild --version
    → stdout 单行 JSON：{protocol:"etm-storebuild/1", schema:1, abi:1, compiler:"gcc 16.1.0",
       buildSha:"<git sha or 0>", formats:{native:"trace-bin native v1", store:1, queryIndex:1},
       capabilities:["build","prescan","query-index","default-views"]}
    → 退出码 0；缺失/不支持的能力必须明示，不允许“猜”。

etm_storebuild prescan --native <native.bin> --out <ranges.bin> [--max-unique-ranges N]
etm_storebuild build --native <native.bin> --static <static-dict.bin> --ranges <ranges.bin>
                     --out-dir <generation_dir> --generation <g-...> --generation-tag <u64>
                     [--checkpoint-instructions 4096] [--cell-insn 4096]
                     [--window-bins 1200] [--timeline-bins 1600]
                     [--verify-image 0|1] [--hash-inputs 0|1] [--result <build-result.json>]
```

- `prescan`：见 §4；`build`：见 §3–§6。
- `--verify-image`：C++ 用静态字典的 `pc/size/bytes` 与 range 走查结果交叉核对（默认 1，
  与 Python `verify_image` 同义）；ELF 机器码/源码位置已由 Python 准备阶段校验（§3.3）。
- `--hash-inputs 0` 仅用于诊断，**不得**出现在验收/最终用户路径（R0 §2.4 要求完整性；
  正常路径必须哈希“决定事件流的输入”，见 §7.3）。
- 所有数值参数写入 `build-result.json` 与 meta.layout，读取端可复核。

### 2.2 退出码与失败语义

| 退出码 | 含义 | Python 编排动作 |
|---|---|---|
| 0 | 成功，`build-result.json` 可解析且自洽 | 继续发布 manifest |
| 2 | 参数/用法错误 | 报错，删 generation |
| 3 | 输入不可读/格式错误（native 头、记录长度、static 字典 magic/版本） | 报错，删 generation |
| 4 | 静态字典缺失 PC / 范围核对失败（payload/end 不符） | **明确失败**，列出 pc/range（不静默跳过） |
| 5 | 内部一致性失败（seq 不连续、检查点/块结构、计数交叉校验） | 报错，删 generation |
| 6 | 版本/能力不符（Python 要求的 schema/能力不在 `--version` 中） | 报错（不静默回退） |
| 7 | 磁盘/IO/内存不足 | 报错，删 generation |
| 1 | 其它未分类异常（含崩溃） | 报错并保留日志；永不发布 |

**stderr** 只写诊断（人类可读）；**stdout** 只写 `--version`/最终 JSON；`build-result.json`
是机器接口（字段见 §7.2）。

### 2.3 与 Python 编排的职责分工

| 步骤 | Python | C++ `etm_storebuild` |
|---|---|---|
| 输入发现/白名单/路径 | ✔ | — |
| OpenCSD 解码到 `native.bin` | 调用 `etm_decode --binary`（现有） | （R2b 融合：C++ 内） |
| 唯一范围预扫描 | 调用 `prescan`（或并入 decode） | ✔ |
| ELF/DWARF/capstone 静态字典 | ✔（`static-dict.bin`，§3） | 消费 |
| 记录解析/组装/统计/analysis | — | ✔ |
| 检查点状态编码/块编码/CRC | — | ✔ |
| R1 查询索引（块 12）/默认视图 | 提供格式与校验规则（R1 设计） | ✔（同遍） |
| `store.bin` sha256 / 块表 | 复核 | ✔（边写边算） |
| manifest 构造与**原子发布** | ✔（`_publish_manifest`） | — |
| `decoded.json/sources.json` 重写 | ✔（可选） | — |
| 失败清理（删除未发布 generation） | ✔ | 负责自身临时文件；不发布 |

理由：原子发布、输入绑定策略、旧记录兼容都在 Python 侧已有实现与测试；C++ 只做“纯产出”，
进程崩溃时不会留下已发布 manifest。

---

## 3. 静态字典准备（Python 侧）

### 3.1 语义目标：与 `trace_store_write._Assembler._static_entry` 完全一致

每个唯一 PC 一份条目，字段与参考相同：
`pc, size, bytes(hex), mnemonic, operands, flow, conditional, function, file, line, predicated`，
**按首次出现顺序分配静态 ID**（ID 只在本 store 内有效；Codex §5.3 允许与 Python 参考不同，
但原型证明可以完全一致，便于对照）。

参考实现的关键口径（`decode_stream._InstructionTable.info`，L216–L286）：

- `mnemonic/operands` 来自**范围上下文**的 capstone 反汇编（IT 块内可能带条件后缀），
  同一 PC 取**首次出现**上下文；
- `flow/conditional` 用该指令字节**单独**反汇编（不继承 IT 状态）得到（L255–L258）；
- `predicated` = `flow=='linear' and predicate_addresses(image)` 判定（IT 保护位）；
- `function/file/line` 来自 ELF 符号表 + DWARF 行表（`Image.location`）；
- 首见 PC 的机器码/源码位置要与 ELF 交叉校验（`Image.read` / `Image.location`）。

### 3.2 静态字典文件格式（`static-dict.bin`，C++ 消费）

```text
header（61 B）:
  0   9  magic "R2STATIC1"
  9   4  version u32 = 1
  13 32  elfSha256（小端字节序原样）
  45  4  entryCount u32
  49  4  stringCount u32
  53  8  tableOffset u64（= 61 + 字符串池字节数）
string pool:
  每项 u32 字节长度 + UTF-8（索引 = 字符串 ID；0 号空串；含 function/file/mnemonic/operands）
entry table（按 PC 升序；16 B/项，`pc u32, size u8, id u32, predicated u8, flow u8, pad 3`）
```

- 条目 `id` 即 store `static` 块的顺序索引（首次出现顺序），C++ 直接用来填 RUN 记录；
- `flow` 用 `trace_store.FLOW_CODES`（linear 0/call 1/return 2/branch 3/unknown 4）；
- C++ 构建时把 `entryCount` 作为哈希表容量；PC 查找失败 → 退出码 4（明确失败）；
- 字符串池与 store `strings` 块的分配由 C++ 按首次使用顺序重建（**不要求**与 Python 的
  ID 编号一致）；静态字典里的字符串只作为“文本值”交付，避免跨语言 ID 绑定。

### 3.3 Python 准备流程与成本（原型实测）

`prepare_static.py`（证据目录）按 `ranges.bin`（首次出现顺序）执行：

1. `Image(elf)`：读 ELF 段/符号/DWARF 行表；
2. `predicate_addresses(image)`：全函数反汇编找 IT 保护位；
3. `_known_addresses(image)`：全函数反汇编收集已知指令地址（用于谓词回退判定）；
4. 对每个唯一范围 `_range_instructions(start,end,decoder,image,span_cache)`：
   线性反汇编并与 payload/end 核对；
5. 每个新 PC：`table.info(ins)` + `Image.read`/`Image.location` 交叉校验 → 追加条目；
6. 写 `static-dict.bin` + `static-entries.json`（对照用）。

实测（同一固件 `firmware.elf` 2,178,084 B，函数 319 个 / 28,362 B 代码，`known` 10,269 条）：

| 记录 | 唯一范围 | 唯一 PC | 与参考 store 静态字典 |
|---|---:|---:|---|
| 200 ms 真实（735,337 范围） | 22 | 59 | **0 差异**（59/59） |
| 19.86M 合成（4,142,783 范围） | 341 | 1,719 | **0 差异**（1,719/1,719） |
| ETMv3 10 ms（172,596 范围） | 122 | 待测（R2 补充） | — |

阶段耗时（Python，含旧版事件模板开销）：

| 阶段 | 200 ms | 19.86M |
|---|---:|---:|
| `Image(elf)`（DWARF 全量） | 0.59 s（冷）/ 0.28 s（热） | 0.27 s（热） |
| `predicate_addresses` | 0.11–0.20 s | 0.09–0.17 s |
| `_known_addresses` | 0.04–0.06 s | 0.04–0.07 s |
| 唯一范围反汇编 | 0.002 s | 0.043 s |
| `table.info`（含旧 events 模板） | 0.009 s | 0.29 s |
| 写字典 + 校验 | 0.01–0.02 s | 0.02–0.03 s |

**优化计划（R2 实现）**：
1. **ELF 静态准备缓存**（按 `elfSha256 + 工具版本`）：首次 0.3–0.6 s，命中 0.03–0.08 s；
   同一固件的多次采集（R2 验收的 2 份 200 ms 记录）只需付一次；
2. `predicate_addresses` 与 `_known_addresses` 合并成一次全函数反汇编（省 ~0.05–0.1 s）；
3. 静态条目不再生成旧 `events.jsonl` 模板/搜索串（`_InstructionTable.info` 的 body/columns），
   只保留 store 需要的字段 → `table.info` 成本可降到 ~10–20 µs/PC；
4. 与 `etm_decode` 并行执行（两者只共享 ELF 与 native-config，互不依赖；见 §11）。

### 3.4 是否需要预扫描：两条路线与判定

| 路线 | 静态字典来源 | 优点 | 风险/成本 |
|---|---|---|---|
| **A（推荐 v1）** | `prescan` 唯一范围 → first-occurrence 反汇编 | 与参考**逐字段 0 差异**（已实测 22/341 范围）；只算用到的 PC | 多一遍 native 读（0.16/0.60 s；可并入 decode 回调抵消） |
| B（R2b 融合） | 全 ELF 函数/段反汇编（10,269 PC）+ 惰性补齐 | 无预扫描、可与前端/解码并行 | 与参考“首次出现上下文”可能不同；19.86M 合成实测 13 个参考 PC 在函数符号之外（`0x80002d0…0x8000346`）→ 必须有补齐路径 |

**判定**：v1 用 A（正确性优先，Codex §5.2 允许第一版读文件）；R2b 若要融合，先实现 B 的
「全 ELF 字典 + 有界批次补齐」并用 A 的结果做等价回归；在等价证据覆盖所有验收记录之前，
R2b 不作为默认路径。

---

## 4. 唯一 PC 预扫描（native 侧）

### 4.1 算法

`prescan` 顺序读 `native.bin` 的元素记录（与 `native_binary.NativeFile.records` 同格式）：

- 只看 `etype=5 (OCSD_GEN_TRC_ELEM_INSTR_RANGE)`，取 `start/end/payload`；
- 以 `(start,end)` 为键去重，**保留首次出现顺序**；payload 只做一致性校验（不同 → 失败）；
- 其他元素/包/状态只计数（用于与 `native.bin` 头部计数交叉核对）；
- 输出 `ranges.bin`：magic `"R2RANGES1"` + `uniqueCount u64` + `rangeRecords u64` +
  `uniqueCount × (start u32, end u32, payload u32)`；同时输出 JSON 统计。

内存：唯一范围线性数组 + 哈希表；若唯一范围超过上限（建议 1,000,000）→ 退出码 7 并在 JSON
给出建议（分批/合并区间），**不**无界增长。稀缺/异常场景的**有界批次**：Python 静态准备可以按

anges.bin 的分块（如 ≤65,536 唯一范围/批）产出多段 static-dict 增量，C++ 逐批加载；
正常记录（本任务实测 22/341/122 个唯一范围）不需要分批。

**扫描遍数（v1，不融合）**：tm_decode 读 etm.bin 1 遍并写 native.bin → prescan 读 native.bin
1 遍 → uild 读 native.bin 1 遍；若按 §4.3 把 prescan 并入 decode，则 native.bin 只被 build
读 1 遍（总 I/O 从 3 遍降到 2 遍）。R2b 融合为 1 遍（etm.bin → 直接 build）。

### 4.2 实测成本（本任务原型 `prescan.cpp`，-O2）

| 输入 | 字节 | 记录数 | 范围元素 | 唯一范围 | 预扫描耗时 | 吞吐 |
|---|---:|---:|---:|---:|---:|---:|
| 200 ms 真实（steady） | 54,499,303 | 1,078,963 | 735,337 | **22** | **0.16–0.20 s** | 271–337 MB/s |
| 19.86M 合成 1 s | 306,874,671 | 6,117,534 | 4,142,783 | **341** | **0.60–0.86 s** | 358–509 MB/s |
| ETMv3 10 ms | 11,345,225 | 207,899 | 172,596 | **122** | 0.03–0.06 s | 198–351 MB/s |

（唯一范围很少是因为同一代码路径被反复执行；真实长 trace 的唯一范围数量受**代码体积**约束——
范围是代码区间，最多与 text 中基本块数同量级，远小于动态范围数。）

### 4.3 与解码合并（推荐）

`etm_decode --binary` 的元素回调已经逐条看到 range 元素；在同一进程里维护同样的
`(start,end)` 去重表并把 `ranges.bin` 写出，**不增加第二遍 I/O**：

- 预计增量：哈希插入 735,337 / 4,142,783 次 ≈ 0.02 s / 0.1 s（与 §4.2 的预扫描成本同源）；
- 代价：`etm_decode` 的职责从“纯解码”扩大到“解码+范围表”（仍只写文件，不依赖 ELF）；
- 若保持职责分离，v1 就保留独立 `prescan`（0.16/0.60 s），预算按 §11 的保守列计算。

---

## 5. 同遍产出 R1 查询索引（块 12）

R1 设计（`docs/etm-r1-query-design.md` §3）定义的索引表在 `build` 的**同一次元素遍历**中生成；
不得二次扫描 native.bin，也不得为每条指令跨语言调用。逐表生成方式：

| R1 表 | C++ 生成方式（同遍） | 规模（19.86M 实测/估算） |
|---|---|---|
| `FUNC_SEG` | 逐指令比较 function 变化，闭合段（R1 原型实测 157,684 段） | ~0.9 MB |
| `PATH_SEG` | CallTracker 状态同步时闭合段（R1 原型实测 157,684） | ~1.0 MB |
| `NODE` | 路径节点树（161 节点；字符串池内 label） | KB 级 |
| `CELL_ROW` | 每 `cellInsn=4096` 指令闭合一行（4,849 行） | 0.23 MB |
| `CELL_HOT` | 单元格×热点 (function,file,line) 计数/未知/首次出现；热点 ID 由静态字典的 (func,file,line) 映射 | 16.1 MB（原型，zlib 后 ~4 MB；自适应 CSR） |
| `CELL_FLAME` | 路径段闭合时按单元格累加节点 value/valueCycles + 叶节点 self/selfCycles | 7.8 MB（N=161；N 大时 CSR） |
| `SPARSE_UNKNOWN/NOTTAKEN` | 谓词未知/未成立指令的 (seq,func) | 0.5 / 13 MB |
| `SPARSE_CALLPAIR` | 已成立 call 指令 + 目标函数 | 0.4 MB |
| `SPARSE_CYCLE` | 周期锚点 (seq,hotspot,cycles) | 1.2 MB |
| `SPARSE_NODE_SELF` | 叶节点 self (cell,node,count,cycles) | 28,397 项 |
| `EVENTS` | 事件 seq/type/text/nextFunc | 2,302 条 / 50 KB |
| `PC_CELLS` | 每单元格 `unique(staticID)` → CSR（id → cells） | **624,713 对 / 1.26 MB**（原型实测与 R1 一致） |
| `LINE_RUNS` | (file,line) 极大区间 startInsn + flag | 3,540,831 段 / ~4.6 MB |
| `DEFAULT_VIEWS` | 同遍累加 1600 桶 timeline / 1200 桶 window 的默认视图，构建末尾序列化+zlib | 69,135 B |
| `NAV_INDEX` | 现有 5 类稀疏列表 + EVENT_SEQS（NAV_EVENT） | 0.27 MB |

**成本实测/估算**：
- 原生热路径原型（范围批处理 + 展开段/检查点 + 单元格直方图）19.86M 合成 **1.16 s**
  （无 CallTracker、无真实 store 写入）；其中单元格直方图（密集 id 计数）约 **0.28 s**
  （`hotloop --batch` 无聚合 0.88 s vs `--batch --dense-agg` 1.16 s）；
- CallTracker/路径段/火焰归并：P3b 实测 Python 2.57M 次 tracker 调用 ≈11–12 s（4.4 µs/次）；
  C++ 等价状态机按 50–100 ns/次估算 ≈0.13–0.26 s（该数字是**估算**，R2 必须实测）；
- 其余索引表写入 ~0.1–0.2 s（稀疏增量 + 67.7 MB 序列块/9.3 MB 包块/78 MB 文件 sha256）。

**与 R1 的一致性验证**：R2 生成的块 12 必须通过 R1 的读取器与默认视图/50 窗口对照
（`docs/etm-r1-query-design.md` §3.6/§7；R2 验收复用同一比较脚本），且 `PC_CELLS` 对
624,713、默认视图 69,135 B 等规模指标在合成记录上应与 R1 原型一致（±容差由格式差异解释）。

---

## 6. 产出项 → 数据来源 → 验证方式

| 产出 | 数据来源（C++ 同遍） | 参考实现位置 | 验证方式（规范化等价） |
|---|---|---|---|
| 区间/末指令/谓词/周期 | range 元素：`start/end/payload/lastExecuted/cycleCount`；末指令挂起，type-12 折叠；`predicated` 来自静态字典 | `trace_store_write._Assembler.add_range/hold_tail/fold_cycles`（L446–L475、L561–L567） | 逐事件对照（含 cycleCount/cycleSpan）；100× 缺陷用例 |
| 调用/IRQ/返回状态 | 同遍 CallTracker 等价状态机（栈/上下文/expected_resume/unknown_restarts）；检查点编码内嵌 | `flow_analysis.CallTracker`（L122–L309） | `callPath` 逐事件对照；检查点回放 `event(seq)`/窗口路径对照 |
| seq/事件计数/展开段 | 元素顺序驱动 `expected_seq`；事件前结束展开段；检查点每 4096 指令/块 1 MiB | `_Assembler._apply_instruction/flush_run/checkpoint/add_event` | `counts`/`seq` 范围逐字段；RUN 解码后逐事件 |
| 质量/字节核算 | 包记录的 offset/size/classify；status；`quality_from_counters` 同口径 | `native_binary.classify_packet`；`decode_stream.quality_from_counters` | `quality`/`summary`/`issues` 逐字段；包故障最小用例 |
| 统计/analysis | 同遍统计字典/火焰树/热点/edges（首次出现顺序）；`finish()` 排序 | `CallTracker.finish`（L309–L345） | `analysis` 深比较（函数/hotspots/edges/flame/timing/weight） |
| NAV/检查点 | `NAV_CALL/RETURN/EXCEPTION/ISSUE/EVENT` 递增列表；检查点 (seq,block,offset) | `_Assembler._apply_instruction/add_event`；`_SequenceBuffer.emit_checkpoint` | `navigate` 全模式对照；检查点回放结果对照 |
| 静态/字符串块 | Python `static-dict.bin` + 同遍字符串池（首次使用顺序） | `trace_store.write` 静态/字符串块编码 | 读取端 `static`/`strings` 解析后逐字段（ID 可不同，按 PC 对齐） |
| 包块 | 包记录 offset/size/raw；无说明文本（沿用 P3 W-P3-2） | `_write_fused` L1017–L1029 | 包视图/字节账逐项 |
| meta/summary/sources/issues | 同遍计数 + decoded 非派生元数据 + sources 快照 | `_write_fused` L1210–L1297 | JSON 块逐字段（白名单差异见 P4 W-P4-1/W-P3-2） |
| R1 查询索引（块 12） | §5 | `docs/etm-r1-query-design.md` §3 | R1 读取器 + 默认视图/窗口/timeline 对照 |
| manifest | Python 用 `build-result.json` + 输入绑定构造 | `trace_store._publish_manifest` | `TraceStore(verify='full')` + `_stale_inputs` |

---

## 7. 与 Python 编排的接口

### 7.1 文件接口与原子发布

```text
Python 编排（新增 native_storebuild.py 适配层）：
  1. 输入校验/白名单：见 §7.3；查 --version，协议/能力不符 → 明确失败
  2. prescan（或 decode 合并模式）→ ranges.bin
  3. prepare_static（缓存）→ static-dict.bin
  4. 创建 store_dir/generations/<generation>/（未发布）
  5. 调用 etm_storebuild build → build-result.json（+ store.bin）
  6. 校验：build-result 计数交叉核对、store.bin sha256/块表、可用 TraceStore 只读打开
  7. decoded.json/sources.json（可选）写入
  8. **最后**构造 manifest（bindings/checksums/processing.engine=etm-storebuild/协议/版本）
     并 `trace_store._publish_manifest()` 原子发布
  9. 失败/异常：删除该 generation 目录（复用 `_remove_tree`），不触碰旧 manifest
```

`build-result.json`（C++ → Python，最小契约）：

```json
{
  "protocol": "etm-storebuild/1", "schema": 1, "abi": 1,
  "engine": {"name": "etm_storebuild", "buildSha": "...", "compiler": "gcc 16.1.0",
             "nativeFormat": "trace-bin native v1", "storeSchema": 1, "queryIndex": 1},
  "generation": "g-...", "generationTag": "0x...",
  "files": [{"name":"store.bin","bytes":N,"sha256":"...","blocks":N,"payloadBytes":N}],
  "counts": {"events":N,"instructions":N,"ranges":N,"runs":N,"rareEvents":N,
             "staticPcs":N,"strings":N,"packets":N,"sequenceBlocks":N,"checkpoints":N,
             "executed":N,"unknownExecution":N,"notTaken":N,"exceptions":N,"gaps":N},
  "queryIndex": {"version":1,"cellInsn":4096,"tables":[...],"defaultWindowBins":1200,
                  "defaultTimelineBins":1600,"range":[0,lastSeq]},
  "phases": {"readNative":0.0,"assemble":0.0,"queryIndex":0.0,"defaultViews":0.0,
              "writeBlocks":0.0,"assembleFile":0.0,"hashFile":0.0},
  "peakRssBytes": N,
  "inputs": {"nativeSha256":"...","nativeBytes":N,"elfSha256":"...","rangesSha256":"...",
              "staticDictSha256":"..."}
}
```

### 7.2 版本握手

- `etm_storebuild --version` 输出 `protocol="etm-storebuild/1"`、`schema=1`、`abi=1`、
  `formats.store/queryIndex`、`capabilities`；Python 适配层在**动任何文件前**完成握手，
  缺失/版本不符/能力不足 → `NativeStorebuildUnavailable|Mismatch`（与
  `native_frontend.py` 的 `NativeFrontendUnavailable/Mismatch` 同构）。
- store 的 `manifest.bindings.processing` 增加 `engine="etm-storebuild/1"`、`buildSha`、
  `staticDictSha256`；`meta.processing` 同义，便于证据追溯。

### 7.3 输入绑定与完整性（R0 §2.4 的延续）

- **必须完整哈希（决定事件流的输入）**：`etm.bin`、`native-config.txt`、`firmware.elf`、
  `native.bin`（若存在/生成）、`session.json`/`sources.json`（存在时，小文件）。
  实测 sha256 吞吐 650–736 MB/s：200 ms native.bin 54.5 MB ≈0.084 s、elf ≈0.003 s、
  etm.bin ≈0.001 s；19.86M native.bin 306.9 MB ≈0.417 s。`native.bin` 的哈希可在 C++ 读入时
  边读边算（build 阶段 I/O 已有），不额外计时。
- **派生/诊断文件不进入“决定事件流”的绑定**：`events.jsonl`/`packets.jsonl`/`elements.jsonl`/
  `capture.csv`/`capture.kvdat`/`browse-v3.sqlite`/`tpiu.bin` 等。理由与证据：
  现有真实记录的 `events.jsonl` 1.68 GB，sha256 实测 3.03 s；P3 审计的 publish 阶段
  3.6–4.6 s 主要就是这些派生文件哈希。把它们纳入 ≤1.5 s 预算与“输入核验”语义都不成立。
  处理方式：manifest `bindings.inputs` 只列决定输入；派生文件在 `bindings.derived` 中
  **显式记录大小与 `hashed:false`**，并保留后台/离线核验 CLI（不阻塞发布，不静默降级）。
  **这需要在 R2 实现时与 R1/R0 的 `_binding_files` 口径协调**（见风险 R7）。
- `--no-hash-inputs` 仅诊断路径；验收禁止使用。

---

## 8. 检查点布局、周期与调用状态（正确性风险最高的部分）

### 8.1 允许不同的地方（Codex §5.3）

- 检查点记录在序列块内的编码/负载（tracker 状态用 C++ 自己的紧凑编码）；
- 字符串 ID 分配顺序、静态 ID 分配顺序（但仍按首次出现，便于对照）；
- 块的物理组织（sequence 块切分点可在检查点前、块大小目标一致即可）；
- 包说明文本（沿用 P3 W-P3-2：原生不存文本）。

### 8.2 必须一致的约束（读取端 `trace_store.py` 会校验）

- `magic/SCHEMA_VERSION/字节序/块索引/CRC`；`MAX_BLOCK_BYTES=64 MiB`、`MAX_RECORD_BYTES=256 KiB`、
  `MAX_RUN_INSTRUCTIONS=4096`；
- **每个 sequence 块第一条记录必须是检查点**（写端只在检查点前 rotate）；
- 检查点索引 `(seq, blockIndex, recordOffset)` 严格递增、首项 seq=0、与块范围一致；
- RUN 记录内 `seq` 连续、`count≤4096`、`tail` 状态与 `internal_unknown` 口径：
  **内部谓词未知数 = identifiers[:-1] 中 predicated 数**（f865319 修复，checkpoint flush 时
  末指令不得多计）；
- 事件 seq 与指令 seq 交错覆盖 `[0,lastSeq]` 无空洞；`seq.count == events`；
- 周期：`cycleCount` 只在 range 末指令；`cycleSpan` 上下界；`cycles==0 → span==0`；
- 静态字典/字符串/NAV/AGG/检查点块的读取端交叉校验（数量、递增、越界）。

### 8.3 CallTracker 状态机（C++ 等价实现）

必须逐条等价（否则 `callPath`/analysis 与参考不同）：

- `feed_instruction`：`expected_resume` 校验、空栈初始化、`中断 N` 帧、previous call/return/branch
  的压栈/回退/尾转移/重置、栈顶函数不一致重置、深度 >128 拒绝；
- `feed_event`：type 1/2/6/7/8 清栈；type 9 的 tail-chaining 位/返回地址位、上下文压栈、
  `中断 <payload>` 帧；type 10 弹上下文/expected_resume；
- 检查点保存/恢复：`unknown_restarts、previous(pc,size,flow,executed,function)、expected_resume、
  stack frames(function,returnPC,origin)、contexts`；恢复后 `_sync` 等价；
- analysis 聚合（functions/hotspots/edges/flame）按首次出现顺序；排序规则同参考
  （instructions 降序稳定、hotspots `(cycles,records)` 降序稳定、cycle_functions 按 selfCycles）。

### 8.4 周期归因

- `CYCLE_COUNT` 元素只能折叠到**最近一个挂起范围末指令**（`fold_cycles` 只折叠一次）；
- 无前置范围时丢弃（与参考一致）；`cycles` 影响 run 的 `internal_ok`（周期末指令必须 flush）；
- 周期锚点同时用于 R1 索引的 `SPARSE_CYCLE`/火焰 `valueCycles`（§5）。

---

## 9. R3 集成接口（显式开关、阶段计时、引擎标识）

- **开关**：`ETM_STORE_ENGINE=native|python`（默认 `python`；仅实验路径设 native）。
  `native` 要求：`etm_storebuild --version` 协议/能力匹配、`native_frontend` 版本匹配；
  **任何缺失/版本不符 → 明确失败，不静默回退**（R3 验收要求）。
- **阶段计时**（写入 `decoded.json.traceStore`/`build-result.json`/首屏证据）：
  `frontend`（物理前端，R3）、`decode`（etm_decode 或融合）、`prescan`、`staticPrep`、
  `build`（含 queryIndex/defaultViews/writeBlocks/hash）、`publish`、`firstQuery`。
  引擎标识：`frontend.engine/version/sha256`、`decode.engine=native|python`、
  `store.engine=etm-storebuild/1|trace_store_write.py`、`staticDict.sha256`。
- **组合预算**（R3 表，工程目标）：物理前端 ≤1.5 s、解码+builder+索引 ≤1.2–1.5 s、
  发布/首屏 ≤3 s 总量（200 ms）。本文件给出 R2 侧预算（§11）；R3 负责前端与组合。
- 实验包必须带可区分版本/标识（不覆盖 1.0.13 名称），缺失 helper 的诊断信息进 issue/日志。

---

## 10. 等价与验收计划（R2 门槛）

### 10.1 语义对照（不要求字节一致）

1. **逐事件**：对 2 份真实 200 ms（steady/boundary）+ ETMv3/ETMv4 小样本 + 100× 合成 1 s，
   用 `TraceStore.events()` 逐条比较 `kind/seq/offset/pc/size/bytes/mnemonic/operands/
   function/file/line/executed/executionState/executionReason/flow/conditional/cycleCount/
   cycleSpan/cycles/callPath`（callPath 逐元素）；0 未解释差异。
2. **接口**：`overview/page/window/function_timeline/event/navigate/issues/source/stats` 全部对照；
   允许的表示差异白名单沿用 P4（W-P4-1/W-P3-2）与 R1（默认视图命中不改变结果）。
3. **质量/字节核算**：`quality`/`summary`/`analysis`/`issues` 逐字段；包故障/缺口/重叠最小用例。
4. **检查点缺陷回归**：保留 `f865319` 的最小用例（checkpoint flush 时谓词未知末指令）与
   100× 合成压力输入（15 条坏段 → 0），R2 必须同样通过。
5. **R1 索引**：R2 生成块 12 后用 R1 读取器跑 50 固定种子窗口 + 默认视图对照；
   `PC_CELLS`/默认视图规模指标与 R1 原型一致。
6. **不变量**：`TraceStore(verify='full')` 打开通过；`_stale_inputs` 对故意改写的决定输入报错；
   `--no-hash-inputs` 不用于验收。

### 10.2 性能测量口径

- **计时范围**：`etm.bin → 可查询 store（manifest 已发布）`，含输入核验、静态准备、
  builder、查询索引、默认轨迹、完整发布；不含界面首屏（R3 另计）。
- **方法**：不带 profiler、进程树峰值 RSS、每项 ≥3 次交替运行（Python 参考/native 交错），
  报告全部样本 + 中位；冷/热（OS cache）分开记录；同一记录目录、同一输出盘。
- **门槛**：
  - 200 ms：**≤1.5 s**（争取 ≤1.2 s）；两份真实 200 ms 记录（steady/boundary）都测；
  - 19.86M 合成：**≤4 s**；
  - 输入核验不得关闭；默认 200 ms 上限不放开（上游约束）。
- **结构检查**：profile 证明 Python 动态事件工作量退出主热循环（每事件级 Python 调用≈0，
  跨语言只有阶段级调用）；`staticPrep`/`prescan` 必须单独计时并计入总预算。

### 10.3 体积/RSS 预算

- store 体积：R2 加入块 12 后重测；目标 ≤85 MB（19.86M；R1 预估 +13%～+50%，见 R1 §3.3），
  200 ms ≤15 MB；若超预算，优先把 `CELL_HOT/CELL_FLAME` 改 CSR、`NOTTAKEN` 用位图。
- RSS：decode/frontend 36.6 MB（P5 实测）；builder 目标 ≤150 MB（19.86M）、≤80 MB（200 ms）；
  R2 报告进程树峰值。

---

## 11. 时间/内存预算（基于本任务实测primitive + 估算）

### 11.1 实测原语

| 原语 | 200 ms 类 | 19.86M 合成 | 来源 |
|---|---:|---:|---|
| `etm_decode --binary`（现有 exe，含写 native.bin） | 0.39–0.59 s | 2.03 s | P3/P6 证据 + 本任务 `decode-1s.json`/`io-hash-measure.json` |
| native 预扫描（`prescan`） | 0.16–0.20 s | 0.60–0.86 s | `prescan-200ms.json`/`prescan-1s.json` |
| 静态准备（Python，含旧模板） | 0.7–1.0 s（冷 ELF） | 0.5–1.0 s | `prepare-*.log` |
| build 热路径（无 tracker/无 store 写） | 0.20 s | 1.16 s | `hotloop-*-batch-dense.json` |
| sha256（决定输入） | 0.09 s（native 54.5 MB + 小文件） | 0.42 s（native 306.9 MB） | `io-hash-measure.json` |
| Python 参考 `stream` 阶段（对照） | 25–30 s | 117–155 s | P3/P6 证据 |

### 11.2 v1（读 native.bin）预算（估算，R2 实测）

| 阶段 | 200 ms（冷 ELF） | 200 ms（静态缓存） | 19.86M（冷 ELF） |
|---|---:|---:|---:|
| decode（现有 `etm_decode`；不融合） | 0.45–0.59 | 0.45–0.59 | 2.03 |
| prescan（独立；若并入 decode 则 ≈+0.02/+0.1） | 0.16 | 0.16 | 0.60 |
| staticPrep（优化后） | 0.35–0.55 | 0.03–0.08 | 0.30–0.45 |
| build：热路径 + tracker/路径 + 索引 | 0.25–0.40 | 0.25–0.40 | 1.2–1.6 |
| 输入哈希（native 边读边算 + etm/elf） | 0.01 | 0.01 | ≈0 |
| manifest/发布/校验 | 0.02–0.05 | 0.02–0.05 | 0.05–0.10 |
| **合计** | **1.24–1.75** | **0.92–1.28** | **4.18–4.78** |
| 目标 | ≤1.5（争取 1.2） | ≤1.5 | ≤4 |

结论：
- 200 ms：**有静态缓存时稳定 ≤1.5**；首次冷 ELF 可能 1.5–1.75，需靠「静态准备与 decode 并行」
  或把 prescan 并入 decode 压到 ≤1.5（两者都已在设计中）；
- 19.86M：v1 按最坏估算 **4.2–4.8 s，存在超 4 s 风险**。达标必须做：
  ① prescan 并入 decode（−0.5～0.6 s）；② build 热路径优化（批量追加/tail 合并/避免每指令
  哈希查找，目标 1.0–1.2 s）；③ 输入哈希边读边算（已计入）；④ ELF 静态缓存（−0.2～0.3 s）。
  达到后估算 3.4–3.9 s。**这是估算，R2 第一版必须实测并按 profile 修正，不能写成已达标。**

### 11.3 R2b（融合 decode+build）估算

- 去掉 native.bin 序列化/回读（306 MB 写+读）与独立 prescan：估算 2.2–2.8 s（19.86M），
  200 ms 0.6–0.9 s；前提是 §3.4 路线 B（全 ELF 字典 + 有界补齐）通过等价回归。

### 11.4 内存预算

| 项 | 19.86M | 200 ms |
|---|---:|---:|
| 唯一范围表（341 / 22 项） | 18 KB | 1.2 KB |
| 静态字典（1,719 / 59 条；字符串 149 KB / 5.3 KB） | ~0.4 MB | ~0.1 MB |
| sequence 块缓冲（1 MiB 目标）+ 包块 | ~2–3 MB | ~2–3 MB |
| R1 索引（cell_hot 16.1 + flame 7.8 + sparse ~16 + 默认视图 0.07） | ~40–45 MB（zlib 前） | 按比例 ~2–4 MB |
| 输出缓冲/CRC/sha256 | <8 MB | <2 MB |
| 峰值 RSS 目标 | ≤150 MB | ≤80 MB |

---

## 12. 风险与回退

| # | 风险 | 影响 | 缓解/回退 |
|---|---|---|---|
| R1 | 检查点布局/状态编码错误 | `event()/page/navigate` 回放错误、窗口 path 差异 | 检查点单独单测（参考 `_encode_tracker` 语义）；C++ 写出后由 **Python 参考读取器** `_decode_tracker` 回放并逐事件对照；最小用例：起点/中断/上下文/未知重启 |
| R2 | 周期归属（type-12 折叠/span） | selfCycles/inclusive/flame 差异 | 参考 `fold_cycles` 不变量（只折叠一次、无前置丢弃）；周期专项用例（多周期元素/无前置/跨检查点） |
| R3 | CallTracker 路径/IRQ 边界 | callPath、interruptEntries、analysis 差异 | 以 `flow_analysis.CallTracker` 为唯一规范；C++ 状态机逐条单测 + 逐事件 callPath 对照；ETMv3/ETMv4/异常/中断用例 |
| R4 | seq 连续性（事件/范围交错） | 读取器拒绝或错位 | 元素顺序即 seq 来源；每步校验 expected_seq；失败退出码 5 |
| R5 | 静态字典与参考差异（IT 上下文/函数外 PC） | 事件字段差异/缺失 PC | 路线 A（prescan+首见）为默认；B 仅作为融合优化并用 A 回归；PC 缺失 → 退出码 4，不静默 |
| R6 | 旧记录兼容 | 旧 store 读不了 | 块 12 为可选；`queryIndex=None` 走慢路径；读取器版本不升；用旧 store 目录回归 |
| R7 | manifest 绑定口径变更（派生文件不哈希） | 与 R0 口径/审查冲突 | 与 R0 负责人协调并在 manifest 显式 `bindings.derived.hashed=false` 记录；决定输入仍全量哈希；不删除任何核验能力 |
| R8 | 1 s 合成超 4 s | R2 门槛失败 | 先落 v1 并 profile；prescan 并入 decode、热路径批量化、静态缓存；不够再上 R2b 融合；报告不得写“已达标” |
| R9 | 内存/体积超预算 | 验收失败 | 索引 CSR 化、NOTTAKEN 位图、块流式写出；R2 报告体积/RSS |
| R10 | 字节一致诱惑 | 退回 Python 流内服务 | 明确记录：Codex §5.3 不要求字节一致；等价用规范化对照；任何“为字节一致而保留 Python 热循环”的方案不采纳 |

**回退**：
- 代码回退：`ETM_STORE_ENGINE=python` 默认；native 只在实验开关下启用；R2 不改
  `trace_store_write.py` 的参考路径（仅新增适配层/新文件）。
- 数据回退：失败的 generation 删除；旧 manifest/旧 generation 不受影响；读取端永不混读两代。
- 能力回退：块 12 校验失败 → 读取器 `queryIndex=None`（慢路径），并在 issues 中显式记录。

---

## 13. 最小可验证版本与交付切分

| 里程碑 | 内容 | 通过条件 |
|---|---|---|
| M0 | `prescan` + `prepare_static`（Python）+ 静态字典**逐字段 0 差异** | 200 ms/19.86M 参考 store 对照（已完成原型） |
| M1 | `etm_storebuild build`（v1）：块 1–11 完整 + 无块 12 | `TraceStore` 打开、逐事件/接口对照 0 差异；200 ms 首次 ≤2.5 s（功能先行） |
| M2 | 块 12（R1 索引）+ 默认视图同遍产出 | R1 读取器 + 50 窗口/默认视图对照；200 ms ≤1.5 s、19.86M ≤4 s（实测） |
| M3 | R2b 融合（可选） | 端到端 200 ms ≤1.2 s、19.86M ≤3 s；等价回归通过 |

**第一版（M1/M2）边界**：只读 `native.bin` 文件；不做 OpenCSD 回调融合；不做增量/并发写；
不支持无 ELF 记录（静态字典必需）；`no-hash-inputs` 仅诊断。

---

## 14. 复现命令（证据目录）

```pwsh
cd E:\superlinkPRO
$V='docs\validation\20261020-ds-etm-r2-design-1891916'
# 1) 1 s 合成 native.bin（若未生成）
& etm\build-native\etm_decode.exe --binary tmp\r2-native-probe\native-1s.bin `
    tmp\p6b-scale\record-1s\etm.bin tmp\p6b-scale\record-1s\native-config.txt
# 2) 预扫描
& "$V\prescan.exe" tmp\p3-audit\work\cli-full-1\native.bin --ranges "$V\ranges-200ms.bin" --json "$V\prescan-200ms.json"
& "$V\prescan.exe" tmp\r2-native-probe\native-1s.bin --ranges "$V\ranges-1s.bin" --json "$V\prescan-1s.json"
# 3) 静态字典 + 与参考 store 对照
python -X utf8 "$V\prepare_static.py" "$V\ranges-200ms.bin" `
    etm\records\capture-de9b8e0ed24c41c68e5b374f3cb3c9c5\firmware.elf "$V\out-200ms" `
    --reference-store tmp\p3-audit\stores\cli-full-1
python -X utf8 "$V\prepare_static.py" "$V\ranges-1s.bin" `
    tmp\p6b-scale\record-1s\firmware.elf "$V\out-1s" --reference-store tmp\p6b-scale\record-1s\store
# 4) build 热路径下界
& "$V\hotloop.exe" tmp\p3-audit\work\cli-full-1\native.bin "$V\out-200ms\static-dict.bin" `
    --json "$V\hotloop-200ms-batch-dense.json" --batch --dense-agg `
    --expect-instructions 3675477 --expect-events 3675860
& "$V\hotloop.exe" tmp\r2-native-probe\native-1s.bin "$V\out-1s\static-dict.bin" `
    --json "$V\hotloop-1s-batch-dense.json" --batch --dense-agg `
    --expect-instructions 19861216 --expect-events 19863518
```

（C++ 原型用 `g++ -O2 -std=c++17` 编译；`prescan.exe`/`hotloop.exe` 与源码同目录。
它们**不是**产品代码，只用于本设计证据。）

---

## 15. 证据索引

| 文件 | 说明 |
|---|---|
| `prescan.cpp` / `prescan.exe` | 唯一范围预扫描原型 |
| `prescan-200ms.json` / `prescan-1s.json` / `prescan-etmv3-10ms.json` | 预扫描成本（§4.2） |
| `ranges-200ms.bin` / `ranges-1s.bin` / `ranges-etmv3-10ms.bin` | 唯一范围表（R2RANGES1） |
| `prepare_static.py` / `out-200ms/` / `out-1s/` | 静态字典 + 与参考 store 对照（0 差异） |
| `prepare-200ms.log` / `prepare-1s.log` | 阶段计时与对照输出 |
| `hotloop.cpp` / `hotloop.exe` / `hotloop-*.json` | build 热路径吞吐/计数/单元格聚合原型 |
| `static_option_b.py` / `static-optionb-*.json` | 全 ELF 字典路线（B）的等价性与缺口（13 PC） |
| `decode-1s.json` / `io-hash-measure.json` | decode/hash 实测 |
| `report.md` / `result.json` | 汇总报告与机器可读结果 |
| `build-*.log` / `*.log` | 原始日志 |
