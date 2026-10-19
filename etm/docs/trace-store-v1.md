# trace-store v1 规范

版本：1.0（schemaVersion = 1）｜2026-10-19｜对应实现：`etm/tools/trace_store.py`、
对照器 `etm/tools/trace_store_compare.py`

本文件是方案 §5（存储契约）与 §6（查询兼容）在 P1 阶段的落地规范：定义一种**版本化二进制
记录**，把既有解码结果（`events.jsonl` 或 `decoded.json` 内联 `events`）无损表示成
「静态代码字典 + 动态展开段 + 稀有事件 + 调用状态检查点 + 稀疏/分层索引 + 摘要」，
并提供与旧 `RecordStore`（SQLite v7）语义相同的查询入口。

P1 只验证数据模型，不承担生产性能；参考实现以 Python 写成，便于逐字段审计。
生产写入器（P3）可以换语言重写，但必须逐字节遵守本规范。

---

## 1. 关系与边界

### 1.1 与 SQLite v7（`etm/tools/record_store.py`，`browse-v3.sqlite`）的区别

| 维度 | SQLite v7 | trace-store v1 |
|---|---|---|
| 定位 | **派生浏览索引**，可随时删除重建 | **规范记录**，是可独立读取的持久格式 |
| 事件本体 | 仍在 `events.jsonl` 原始行里，索引只存 `src` 字节偏移；`event_rows` 每条逻辑事件一行 | 事件本体在 `store.bin`：静态字段按唯一 PC 存一份，动态字段按展开段存 |
| 每指令路径 | `events.data`（流式记录）或 `event_templates`+`event_rows`，数千万行 | 调用路径由检查点 + `CallTracker` 重放得出，不存储逐条路径字符串 |
| 依赖 | 依赖原始目录的 JSONL/sidecar；sidecar 指纹不符则重建 | 自包含：manifest 绑定输入指纹，读取不要求原始目录在场 |
| 版本 | `VERSION=7` + `signature()`(size/mtime) | `schemaVersion=1` + magic/字节序/块 CRC/sha256 |
| 损坏处理 | 索引损坏重建；原始 JSONL 是唯一真相 | 拒绝打开：没有降级/拼凑路径 |
| 包/元素 | `packets` 表存说明文本；元素不存 | 包记录存结构化字段；元素只存计数与指纹（见 §10） |

两者可以并存：`browse_store()` 按 manifest 选择后端（P4）。v1 **不重写** v7 索引，
也不要求 v7 理解新格式。

### 1.2 与 streamed schema 2/3 的区别

`streamed.schema` 是**既有记录目录的布局标记**（`etm/tools/stream_store.py`）：

* `schema=2`：单窗流式记录，`events/packets/elements` 三个 JSONL 都在记录根目录。
* `schema=3`：历史长窗记录，只有**合并后**的 `events.jsonl` 在根目录，`packets/elements`
  留在 `slices/NN/`，各片字节偏移互不相通。

trace-store v1 与它们是**同一逻辑语义、不同物理表示**：

* 逻辑 `seq` 定义完全相同：从 0 连续、包括旧实现实际暴露的指令事件与边界事件，
  不等于原生元素编号或物理时间。
* `schema=2` 记录：事件流、元素流、包记录都可校验（§7）。
* `schema=3` 记录：只写合并后的 `events.jsonl`；元素流不在根目录 → 跳过元素交叉校验
  （`meta.verify.elements=false`），包记录若缺失则 `counts.packets=0`。这类记录的
  `summary.ranges` 仍按旧摘要写入，但**不声称**做过原生范围对账（报告必须列出）。

### 1.3 不在本规范内

原始采集证据（`capture.csv`/`capture.kvdat`/`tpiu.bin`）、ELF 与源码快照、`native.jsonl`、
`elements.jsonl` 的完整语义、原生 OpenCSD 二进制元素/包格式（P2）、生产写入器与原生查询
（P3/P4）。这些文件只作为**输入指纹**被 manifest 绑定（§8.3）。

---

## 2. 目录布局与发布

```text
<store_dir>/
  manifest.json                      # 唯一发布点（原子替换），最后写入
  generations/
    g-<YYYYmmddTHHMMSS>-<8 hex>/     # 一代完整结果，写入后不再修改
      store.bin                      # 唯一数据文件（容器，见 §3）
```

规则：

1. 每次写入生成**新的 generation 目录**，文件名含时间戳与随机后缀，用 `mkdir` 独占创建。
2. 全部文件写完、`fsync`、sha256 算出之后，才写 `manifest.json`：先写同目录临时文件
   `.manifest-<rand>.tmp`，`fsync` 后 `os.replace()` 原子替换。**manifest 是唯一提交点。**
3. 读取端只认 manifest 指向的那一代（`manifest.generation`），不扫描、不猜测其它目录。
4. 旧 generation 保留不删（可后续 GC），因此「上一代完整结果」与「新结果」不会混读；
   容器头部还有 `generationTag`，即使把两个 generation 的文件互换也会被拒绝（§6.3）。
5. Windows：写入进程在发布前关闭全部句柄；读取端使用只读句柄与按块读取，不 mmap 长期
   持有文件（geneneration 目录不可变，不存在“替换正在被读的文件”）。

### 2.1 失败/中止语义

| 情形 | 落盘结果 | 读取行为 |
|---|---|---|
| 写入过程中异常/中止 | 该 generation 目录被删除；manifest 未改动 | 读不到这一代；旧一代仍完整可读 |
| 发布 manifest 失败 | 同上；临时 manifest 删除 | 同上 |
| 进程崩溃在 generation 中 | 残留 `generations/g-*/store.bin(.payload.tmp)`，**无 manifest 指向** | `open` 报「不是已发布的 trace-store：缺少 manifest.json」 |
| manifest 指向的 generation 缺失/文件缺失 | — | 拒绝打开（`TraceStoreError`） |
| 盘满 / 短写 | `assemble()` 校验实际文件长度不等于声明长度 → 抛错 | 该代不可达 |
| 块数据损坏 | — | 读取该块时报「块 N 校验和错误」；**不返回短结果** |

`store_dir` 里已有旧 manifest 时，新写入失败**不会**影响旧记录（见 §8.4 与测试
`test_aborted_generation_is_not_published`）。

---

## 3. 容器文件格式（`store.bin`）

所有整数**小端**；变长整数为无符号 LEB128，有符号增量用 zigzag(LEB128)（§10.1）。
禁止直接把编译器结构体落盘：本规范显式给出每个字段的偏移、宽度与含义，padding 恒为 0。

### 3.1 文件头（56 字节）

| 偏移 | 宽度 | 字段 | 取值/含义 |
|---:|---:|---|---|
| 0 | 8 | `magic` | `b'ETMTSv1\x00'` |
| 8 | 2 | `schemaVersion` | `1`；其它值一律拒绝 |
| 10 | 2 | `fileType` | `1` = TRACE（v1 只有这一种） |
| 12 | 4 | `byteOrder` | `0x01020304`（小端写入）；读到 `0x04030201` 明确报“大端” |
| 16 | 8 | `generationTag` | 随机 63 位；必须与 manifest 一致，否则拒绝混读 |
| 24 | 4 | `blockCount` | 1 … 1 048 576 |
| 28 | 4 | `requiredFeatures` | 必须为 0；非 0 表示文件要求读取器不认识的强制特性 → 拒绝 |
| 32 | 8 | `payloadBytes` | 所有块长度之和 |
| 40 | 8 | `fileBytes` | 整个文件长度；必须等于实际文件尺寸（截断检测） |
| 48 | 4 | `headerCrc32` | `crc32(header[0:48])` |
| 52 | 4 | `optionalFeatures` | 读取器可忽略的可选特性位（v1 恒为 0） |

### 3.2 块索引（`blockCount` × 24 字节，紧随文件头）

| 偏移 | 宽度 | 字段 | 含义 |
|---:|---:|---|---|
| 0 | 8 | `offset` | 块在文件中的绝对偏移；必须**紧接**前一块末端（不允许空洞/重叠） |
| 8 | 4 | `length` | 块负载字节数，≤ 64 MiB（超过即拒绝，防巨量分配） |
| 12 | 4 | `count` | 块内条目数；**压缩块**（`flags.bit0=1`）时表示解压后字节数 |
| 16 | 2 | `blockType` | §3.3 |
| 18 | 2 | `flags` | bit0 = zlib 压缩；其它位必须为 0 |
| 20 | 4 | `crc32` | 块**存储字节**的 CRC32（压缩块按压缩后字节算） |

校验顺序（打开时）：`fileBytes == 实际尺寸` → `blockCount` 上界 → `24×blockCount + 56 ≤ fileBytes`
→ 每块 `blockType` 合法、`offset` 连续、`length ≤ 64 MiB`、`flags` 无未知位 →
`Σlength == payloadBytes` → 末块末端 `== fileBytes`。任一不符即拒绝打开。
块 CRC 在**首次访问该块**时校验；解压时先按 `count` 声明值做上界检查再解压。

### 3.3 块类型

| 值 | 名称 | 内容 | 条目粒度 |
|---:|---|---|---|
| 1 | STRINGS | UTF-8 字符串表（varint 长度 + 字节） | 1 个字符串 |
| 2 | STATIC | 静态代码字典条目（§4） | 1 个唯一 PC |
| 3 | SEQUENCE | 序列记录流（§5），**按 seq 递增** | 1 条记录 |
| 4 | PACKETS | 包记录（§6.4） | 1 个包 |
| 5 | NAV | 导航索引（§6.5） | 1 个 seq |
| 6 | AGG | 块级聚合/稀疏索引第二层（§6.2） | 1 个序列块 |
| 7 | META | zlib JSON：格式/计数/字段集合/校验声明（§8.1） | 1 个 JSON |
| 8 | SUMMARY | zlib JSON：`overview` 摘要（§8.2） | 1 个 JSON |
| 9 | SOURCES | zlib JSON：`文件 → 源码文本` | 1 个 JSON |
| 10 | ISSUES | zlib JSON：派生问题列表（§6.6） | 1 个 JSON |
| 11 | CHECKPOINTS | 检查点索引（§6.1） | 1 个检查点 |

块大小目标 1 MiB（`layout.targetBlockBytes`）。**块只在检查点前切分**，因此每个 SEQUENCE
块的第一条记录必然是检查点：任意检查点索引项都能独立解码，不依赖前一块。

---

## 4. 静态代码字典（每个唯一 PC 一份）

条目字段（按写入顺序）：

| 字段 | 编码 | 说明 |
|---|---|---|
| `pc` | varint | 指令地址（32 位范围） |
| `size` | u8 | 机器码长度 1…16（= `len(bytes)`，与旧事件 `size` 相同） |
| `flags` | u8 | bit0 `predicated`（IT 保护且 flow=linear）；bit1 `conditional`；bit2 有 function；bit3 有 file；bit4 有 line |
| `flow` | u8 | 0 linear / 1 call / 2 return / 3 branch / 4 unknown（与 `flow_analysis.instruction_info` 同义） |
| machine code | `size` 字节 | 原始机器码；读取端 `bytes.hex()` 得旧事件 `bytes` |
| `mnemonic` | varint | 字符串 ID |
| `operands` | varint | 字符串 ID |
| `function` | varint（可选） | 字符串 ID；旧事件里 `function` 可能为空串（字符串表含 `''`） |
| `file` | varint（可选） | 字符串 ID |
| `line` | varint（可选） | 行号；缺失表示旧事件的 `line` 为 `null` |

约束与校验：

* **每个唯一 PC 只存一份**；同一 PC 再次出现时必须与首次的
  `(size, bytes, mnemonic, operands, function, file, line, flow, conditional, predicated)`
  完全一致，否则写入失败（静态字典不允许歧义）。
* `predicated` 的判定：有 ELF 时 `pc ∈ predicate_addresses(image) 且 flow=='linear'`，
  并要求 `(executed is None) == predicated`；没有 ELF 时退化为 `executed is None`
  （两者在旧记录上等价，见 §5.2）。
* 有 ELF 时逐条交叉校验：镜像字节 `image.read(pc, size)` 必须等于事件 `bytes`，
  `image.location(pc)` 必须等于事件的 `(function, file, line)`；不一致即写入失败。
* 读取端按 `pc` 建 `dict`，并检查无重复 PC、条目数等于 `meta.counts.staticPcs`。

---

## 5. 动态执行区间与稀有事件（SEQUENCE 块）

SEQUENCE 块负载是一串记录：`u8 recordType` + `varint payloadLen` + `payload`。
记录不得跨块；读取端要求记录长度不越出块尾。记录按 `seq` 严格递增。

### 5.1 记录类型 1：展开段（RUN）

“展开段”是**本次实现定义的动态单位**，不一定等于一个原生 OpenCSD 范围（见 §5.3）。

| 字段 | 编码 | 含义 |
|---|---|---|
| `startSeq` | varint | 本段第一条指令的逻辑 seq |
| `count` | varint | 指令条数，1…4096 |
| `tailCode` | u8 | 末指令状态：0 executed / 1 not-taken / 2 unknown |
| `offset` | varint | trace 源偏移（整段同一值） |
| `cycleCount` | varint | 挂在末指令上的周期数（含紧随周期元素折叠，§5.4） |
| `cycleSpan` | varint | 拥有该末指令的**原生范围**指令数（无周期时为 0） |
| `internalUnknown` | varint | 段内前 `count-1` 条中 `predicated` 的条数 |
| `firstStaticId` | varint | 第一条指令的静态 ID |
| `delta[i]` | zigzag varint × (count−1) | 后续静态 ID 相对前一条的增量 |

展开规则（读取端必须逐条复刻）：

* 第 1…count−1 条：`executed = None if static.predicated else True`，
  `cycleCount = cycleSpan = 0`。
* 第 count 条（末指令）：`executed = None if static.predicated else TAIL_VALUES[tailCode]`，
  `cycleCount = 记录值`，`cycleSpan = 记录值`。
* `executionState` = `executed/ not-taken / unknown`；
  `executionReason` = `IT 非分支条件指令：当前 trace 未记录条件结果`（仅 unknown）否则 `""`。
* `cycles`（旧字段）等于该指令被计入的周期数 = 末指令的 `cycleCount`，其余为 0。
* `callPath` 由检查点恢复的 `CallTracker` 重放得出（§6.1），与旧实现同一条状态机。
* 校验：`seq` 必须等于期望值；`count` 在 [1,4096]；静态 ID 在字典范围内；
  `cycleCount==0 ⇒ cycleSpan==0`；`cycleCount>0 ⇒ 1 ≤ cycleSpan ≤ 4096`；
  段内 `predicated` 条数必须等于 `internalUnknown`。

`cycleSpan` **可能大于本段 `count`**：当原生范围在周期锚点处被切开时，拥有末指令的范围
指令数比本段长。实现只校验上下界，不假设 `cycleSpan == count`
（见证据：ETMv4 10 ms 记录里 `seq=386` 的 `cycleSpan=2`、段长 1）。

### 5.2 展开段切分规则（无损证明）

写入端按以下规则切段，**只使用可观测字段**，不依赖原生元素：

1. 指令 E 可“内部化”当且仅当
   `executed == (None if predicated else True)` 且（若有周期字段）`cycleCount==0 且 cycleSpan==0`。
2. 若当前段非空且 `E.offset != 段 offset` 或段长已达 4096 → 结束当前段，另起新段。
3. 追加 E；若 E 不可内部化 → 以 E 为末指令结束该段（`tailCode` 取 E 的 `executed`）。
4. 稀有事件、检查点前必须先把当前段落盘（保证记录按 seq 递增）。

**无损性**：内部指令的四个可观测动态字段由静态字典 + 固定规则唯一确定
（`executed` 由 `predicated` 决定、`cycleCount=cycleSpan=0`、`callPath/cycles` 由状态机
重放决定）；末指令的 `executed/cycleCount/cycleSpan` 显式存进记录。因此
「旧事件序列 → 展开段序列 → 旧事件序列」是恒等映射。若真实原生范围边界落在两条都可
内部化的指令之间且同 offset，本实现会合并它们；合并前后每条事件的字段完全相同
（被合并掉的边界其 `executed=True, cycleCount=cycleSpan=0`），因此仍然无损。
这也解释了 `meta.counts.runs ≤ meta.counts.ranges`。

### 5.3 与原生范围的关系（元素交叉校验）

写入端可选（默认开启）读取元素流，逐范围核对：

* `type 5` 元素的 `payload` = 该范围反汇编出的指令条数；指令必须按
  `start → end` 连续覆盖（`pc` 依次递增、最后一条 `pc+size == end`）。
* 指令 `offset` 必须等于元素 `offset`；末指令 `executed` 必须等于 `lastExecuted`
  （谓词未知的末指令不检查，因为旧实现同样用 `None` 覆盖）。
* 末指令 `(cycleCount, cycleSpan)` 必须等于该元素折叠后的
  `(element.cycleCount + 紧随 type 12 的 cycleCount, payload if 有任何周期 else 0)`。
* 范围内的非范围元素（1/2/3/6/7/8/9/10）被跳过；`type 12` 只按上面的规则折叠一次。
* 要求消费完所有范围：元素数 == `summary.ranges`，事件流与元素流都不许有剩余。

结果写入 `meta.verify.elements/elementsVerified`；无法做（schema 3、无元素）时置 `false`
并在证据里列为未覆盖项。

### 5.4 周期折叠

旧实现把 `OCSD_GEN_TRC_ELEM_CYCLE_COUNT`（type 12）折叠到**前一个范围的末指令**：
`cycleCount += cycle.cycleCount`，且 `cycleSpan` 由 0 变为该范围指令数。
`decode_stream` 只折叠紧随其后的**一个**周期元素（内层 `pending` 已被 flush），
写入端按同一规则折叠；后续周期元素计入 `quality.timing.cycleElements` 但不改写事件。

### 5.5 记录类型 2：稀有事件

| 字段 | 编码 | 说明 |
|---|---|---|
| `seq` | varint | 逻辑 seq |
| `etype` | u8 | 1/2/3/6/7/8/9/10（与 `decode_stream` 的显式事件类型一致；4/5/12 不入事件流） |
| `offset` | varint | trace 源偏移 |
| `payload` | varint | 旧事件 `payload`（异常号/结束码等） |
| `start` | varint | 旧事件 `start` |
| `end` | varint | 旧事件 `end`（异常首选返回地址等，32 位值） |
| `flags` | varint | 旧事件 `flags`（bit6=有返回地址、bit12=tail chaining 等，原样保存） |
| `textId` | varint | 字符串 ID，保存 OpenCSD 说明文本原文 |

异常、同步、缺口、上下文、未知源等**不允许压缩成“指令 + 猜测”**：它们就是显式类型，
前向展开时原样还原，并驱动调用状态机（`CallTracker.feed_event`）。

---

## 6. 索引、检查点与查询

### 6.1 调用状态检查点（CHECKPOINT 记录 + 索引块）

检查点保存 `CallTracker` 的完整可恢复状态，**按帧 ID/字符串 ID 共享**，不重复路径字符串：

| 字段 | 编码 |
|---|---|
| `seq` | varint：该检查点对应的“下一条记录”的 seq |
| `unknownRestarts` | varint |
| `previous` | u8 存在位；`pc, size` varint；`flow` u8；`executed` u8(0/1/2)；`function` 可选字符串 ID |
| `expectedResume` | u8 存在位 + varint |
| `stack` | varint 帧数 + 每帧：`function` 可选字符串、u8 flags(bit0 有 returnPC、bit1 有 origin)、可选 `returnPC` varint、可选 `origin` 字符串 ID |
| `contexts` | varint 个数 + 每项：`stack`、`previous`、`expectedResume`（同编码） |

调度规则：**首个检查点在 seq = first_seq**（trace 起点调用栈为空，必须显式保存）；
之后每 `checkpointInstructions`（默认 4096）条指令或块满时落一个新检查点。
因此读取任意 seq 最多重放 4096 条指令（实测 steady：904 个检查点 / 3 675 435 条指令，
任意页 `replayInstructions ≤ 4096`）。调用深度上限 128（与 `CallTracker` 一致）。

检查点索引块（类型 11）：`varint count` + 每项 `seq 增量, blockIndex, recordOffset`，
`seq` 严格递增，首项必须等于 `meta.seq.first`；`recordOffset` 指向块内该检查点记录的类型字节。

### 6.2 稀疏/分层索引与块级聚合

三层定位，避免“整段扫描”：

1. **块级**：AGG 块每个 SEQUENCE 块一条聚合记录（14 个 varint）：块序号、`firstSeq`、
   `lastSeq`、记录数、指令数、executed/unknown/notTaken、周期和、稀有事件数、
   call/return/exception/boundary 计数。读取端按 `firstSeq` 二分即可定位块。
   相邻块 `firstSeq == 前块 lastSeq + 1`，否则视为索引损坏。
2. **检查点级**：§6.1 的检查点索引（seq 二分）。
3. **记录级**：块内从检查点偏移顺序解码。

`overview()`/`stats()` 只需读取 AGG 块（O(块数)），无需展开事件。

### 6.3 跨代混读防护

* manifest 记录 `generation`、`generationTag`（十六进制字符串），并逐文件记 `bytes`+`sha256`。
* 容器头部 `generationTag` 必须等于 manifest 的值 → 用另一代的 `store.bin` 覆盖时立刻拒绝。
* 打开时按 manifest 校验每个文件的尺寸与 sha256（`verify='full'`，默认）；`verify='sizes'`
  只校验尺寸（用于超大记录的性能探测），`verify='none'` 仅供损坏注入测试使用。

### 6.4 包记录（PACKETS 块）

`varint offsetDelta`（相对前一个包的 offset，首个为绝对值）+ `varint size` +
`varint hexLen + 原始字节` + `varint textLen + UTF-8 说明`。
还原出的字典与旧 `packets.jsonl` 一行反序列化结果逐字段相同
（`kind='packet', offset, size, hex, text`）。包 offset 必须单调不减；`len(hex) == size`。
`page(view='packets')` 的 `offset` 钳制与旧 SQL 语义一致。

元素（`elements.jsonl`）**不单独存储**：v1 只用它做写入期范围校验（§5.3）；
`meta` 记录元素条数与指纹。这是有意的省略——元素文本是 P2 的交付物，
P1 不把它当成浏览所需数据；未覆盖项在报告中列出。

### 6.5 导航索引（NAV 块）

每个块：`u8 mode` + `varint count` + `varint seq0` + 增量 varint。
`mode`：1 call、2 return、3 exception、4 issue、5 event。语义与 `record_store` 的
`event_navigation` 表逐项一致：

* call = `flow=='call' 且 executed is True`
* return = `flow=='return' 且 executed is True`
* exception = 稀有事件 `type ∈ {9,10}`
* issue = 指令 `executionState=='unknown'` 或稀有事件 `type ∈ {1,2,6,7,8}`
* event = 全部稀有事件

`instruction`/`source` 两种导航不建索引：前者按 seq 前后找最近的指令记录（稀有事件很稀疏），
后者按块回退并比较 `(file, line)`；两者都可能回退为区间扫描（§11 记录代价）。

### 6.6 查询契约（与 `RecordStore` 同名同义）

| 方法 | 语义 |
|---|---|
| `overview()` | `decoded.json` 元数据（除 events/elements/packets/sources）+ `eventCount`、`packetCount`、`sourceFiles`、`firstSeq`；新增 `traceStore` 摘要键（白名单 W1） |
| `page(view='events'/'packets', offset, limit, search, hide_skipped, start, end)` | `total` = 匹配行数；`offset` 钳制为 `min(offset, ((total-1)//limit)*limit)`；搜索列与旧 sidecar 的 `search` 规则一致 |
| `event(seq)` | `{event, callPath, stackBasis}`；seq 越界报错 |
| `first()` / `last()` / `bounds()` | 有效 seq 范围 |
| `seq_position(seq)` | 块序号、块起止、命中检查点及其块内偏移、需重放指令数 |
| `window(start, end, bins)` | 与 `RecordStore._window` 的 JSONL 回退路径逐字段一致：桶、路径共同前缀、mixed 标记、flame、函数/热点、周期归并 |
| `function_timeline(start, end, ...)` | 与 `RecordStore.function_timeline` 一致 |
| `navigate(seq, direction, mode, pc, start, end)` | 与 `RecordStore.navigate` 一致（含 pc 的 wrapped 语义） |
| `issues(offset, limit)` | 写入期按 `RecordStore` 口径派生的谓词未知段/边界/包字节账/解码器/物理问题 |
| `source(file)` | `{file, text}`，来自 SOURCES 块 |
| `events(start, end)` | 流式展开（O(1) 内存） |
| `stats()` | 计数 + 块数/检查点数 + AGG 与 `meta.counts` 的一致性结论 |

**区间裁剪**：`window(start, end)` 只展开 `[start, end]` 内的事件，但会从检查点重放
`start` 之前的指令以恢复调用栈（与旧实现“窗口继承栈”一致）；计数、周期、marker
只统计区间内事件，不套用整块汇总。

### 6.7 摘要与派生问题

* SUMMARY = 旧 `decoded.json` 元数据原样 + 计数 + `firstSeq` + `traceStore` 块。
  写入时与事件流逐项对账：`streamed.events[=events]`、`summary.instructions/executed/
  unknownExecution/exceptions/gaps/ranges`、`quality.unknownExecution/firstPathOffset`。
  任一不符**拒绝写入**（防止把“拼凑结果”发布成完整记录）。
* ISSUES = 与 `RecordStore` 口径一致的派生列表（谓词未知段、边界事件、包覆盖缺口/重叠、
  包问题、解码器错误、物理边界）。写入期生成并按 seq 顺序保存，读取端原样返回。
* SOURCES = `sources.json` 或 `decoded.json.sources` 的 `文件 → 源码文本`。

---

## 7. 字段表：旧事件 → trace-store → 展开

指令事件（旧 `events.jsonl` 的两/三种字段集合必须逐字节复原）：

| 旧字段 | trace-store 来源 | 是否逐条存储 |
|---|---|---|
| `kind='instruction'` | 常量 | 否 |
| `seq` | RUN.startSeq + 序号 | 否（区间） |
| `offset` | RUN.offset | 否（区间） |
| `pc` | 静态字典 | **每 PC 一次** |
| `size` | 静态字典（=len(bytes)） | 每 PC 一次 |
| `bytes` | 静态字典机器码 → hex | 每 PC 一次 |
| `mnemonic`, `operands` | 静态字典（字符串 ID） | **每 PC 一次** |
| `executed` | 末指令 tailCode / 内部规则 + `predicated` | 每段 1 字节 |
| `function`, `file`, `line` | 静态字典（字符串 ID / varint） | **每 PC 一次** |
| `cycleCount`, `cycleSpan` | RUN 字段（末指令），内部为 0 | 每段两个 varint |
| `flow`, `conditional` | 静态字典 | 每 PC 一次 |
| `executionState`, `executionReason` | 由 `executed` 按固定规则派生 | 否 |
| `callPath` | 检查点 + `CallTracker` 重放 | **否**（结构共享） |
| `cycles` | = 末指令 `cycleCount` | 否 |

字段集合（`meta.fieldPresence.instruction`）按旧记录实际字段原样保存，三种变体：

1. `base + flow/conditional/executionState/executionReason/callPath/cycles +
   cycleCount/cycleSpan`（当前 ETMv4 流式记录，20 字段）
2. `base + tail + cycles`（无周期字段的旧记录，18 字段）
3. `base + tail`（旧版连 `cycles` 也没有，17 字段；实测 ETMv3 10 ms 记录）

读取端**只**输出 `fieldPresence` 里的字段，绝不补造旧记录没有的键。

边界事件（稀有事件）字段：`kind, seq, type, offset, text, payload, start, end, flags`
全部逐条保存（见 §5.5）。

---

## 8. manifest 与指纹绑定

### 8.1 META（zlib JSON）

```json
{
  "format": "trace-store", "formatName": "trace-store v1", "schemaVersion": 1,
  "byteOrder": "little",
  "record": {"name": "...", "eventsSource": "events.jsonl|decoded.json:events",
             "streamedSchema": 2, "decodedVersion": 3, "protocol": "ETMv4"},
  "counts": {"events": 0, "instructions": 0, "ranges": 0, "declaredRanges": 0,
             "runs": 0, "rareEvents": 0, "staticPcs": 0, "strings": 0, "packets": 0,
             "sequenceBlocks": 0, "checkpoints": 0, "executed": 0, "unknownExecution": 0,
             "notTaken": 0, "exceptions": 0, "gaps": 0},
  "eventTypes": {"1": 1, "3": 1, "9": 190, "10": 190},
  "seq": {"first": 0, "last": 0, "contiguous": true, "count": 0,
          "firstInstruction": 2, "offsetRange": [25, 384518]},
  "fieldPresence": {"instruction": ["..."], "event": ["..."]},
  "hasCycleFields": true,
  "verify": {"elements": true, "elementsVerified": 735326, "image": true,
             "staticPcs": 30, "machineCodeCrossCheck": true},
  "layout": {"headerBytes": 56, "indexEntryBytes": 24, "targetBlockBytes": 1048576,
             "maxBlockBytes": 67108864, "runInstructionLimit": 4096,
             "checkpointInstructions": 4096},
  "created": {"unix": 0, "iso": "..."}, "sourceSha256": {"decoded": "..."},
  "issues": 385
}
```

### 8.2 SUMMARY

`overview()` 的返回值：旧 `decoded.json` 除 `events/elements/packets/sources` 之外的全部
键（`summary`/`quality`/`analysis`/`config`/`elf`/`streamed`/`status`/`physical`/`framing`/
`capture`/`targetSnapshot`/`captureScope`/`captureStart`/`laFilter`/`version`/`protocol`/
`inputBytes`/`timeBasis` …）原样保存，再加 `eventCount`、`packetCount`、`sourceFiles`、
`firstSeq` 与 `traceStore{format, generation, runs, instructions}`。

### 8.3 manifest.json

| 字段 | 含义 |
|---|---|
| `format`, `formatName`, `schemaVersion`, `byteOrder` | 同容器 |
| `state` | 必须为 `"complete"`；其它值（如 `building`）→ 拒绝打开 |
| `generation`, `generationTag` | 指向的 generation 目录与随机标记（`0x%016x`） |
| `files[]` | `{name, bytes, sha256, blocks, payloadBytes}`；`name` 不得含路径分隔符 |
| `counts`, `fieldPresence` | 与 META 一致 |
| `bindings.inputs` | 记录目录里全部输入文件的 `{bytes, sha256}`：`etm.bin`、`image-*.bin`、`native-config.txt`、`firmware.elf`、`native.jsonl`、`elements.jsonl`、`packets.jsonl`、`events.jsonl`、`sources.json`、`session.json`、`physical.json`、`framing.json`、`capture.csv`/`.bin`/`.kvdat`/`.la.json`、`spi-validation.json`、`phase-validation.json` |
| `bindings.record` | 事件源类型、streamed schema、`decoded.json` sha256、`events.jsonl` sha256/尺寸 |
| `bindings.config` | 解码配置（ETM 寄存器/traceid，来自 decoded.json） |
| `bindings.processing` | `traceStore` 版本、写入器标识、`decodedVersion`、解码库版本、协议 |
| `checksums` | 声明算法：文件 sha256、块 crc32 |
| `notes` | 发布语义说明（manifest 唯一提交点、旧代保留） |

指纹口径：**构建时**对全部输入文件计算完整 sha256（不是只看 size/mtime）；
**打开时**校验存储文件本身的 sha256 与每块的 CRC。原始输入是否变化由 manifest 里的
输入 sha256 判断，旧记录的 `capture.sha256`/`elf.sha256` 仍由上层 `verify_record_inputs`
检查，二者不重复也不冲突。

### 8.4 原子发布流程（实现顺序）

1. 解析输入、建 generation 目录、写 `store.bin.payload.tmp`（块流）。
2. 拼装 `store.bin`（header + index + payload 流式拷贝），边写边算 sha256，`fsync`。
3. 删除临时 payload。
4. 计算输入指纹，组装 manifest。
5. `manifest.json.tmp` → `fsync` → `os.replace()`。
6. 任一步异常：关闭句柄 → 删除整个 generation 目录 → 重新抛出。

---

## 9. 溢出、边界与安全读取

* 所有长度乘加先做上界检查（`_checked_mul`/`_checked_add`）：块索引长度、块偏移、
  块总长、文件总长；任何溢出抛 `TraceStoreError`。
* 上界：`blockCount ≤ 2^20`、单块 ≤ 64 MiB、块内记录 ≤ 256 KiB、字符串 ≤ 1 MiB、
  展开段 ≤ 4096 条指令、调用深度 ≤ 128。压缩块解压前检查声明长度并限量解压。
* `seq`、`offset`、周期累计与计数一律 varint/u64 编码，不假设 32 位足够
  （`seq` 与 `offset` 在格式上没有 32 位截断）。
* 读取端同样检查：未知块类型、未知块标志、未知强制特性、非递增导航/检查点 seq、
  越界静态 ID、越界字符串 ID、记录越出块边界、块级 seq 范围不连续、
  `meta.seq.first/last` 与块级范围不符。

---

## 10. 编码细节

### 10.1 变长整数

无符号 LEB128：每字节低 7 位为数据、高位为续位；最多 10 字节（>63 位移位即报错）。
有符号增量：`zigzag(v) = (v<<1) ^ (v>>63)`，再按 LEB128 编码。

### 10.2 字符串表

块内顺序：`varint len + UTF-8 bytes`，ID = 序号（0 号恒为空串 `""`）。
同一字符串只出现一次；字符串表在**静态字典之后**写入（静态条目会新增助记符等字符串），
块顺序不影响 ID 引用。

### 10.3 JSON 块

`meta/summary/sources/issues` 用 zlib（level 6）压缩后作为块负载，索引 `count` 记录
解压后字节数；解压时必须 `eof` 且无多余数据。JSON 一律 UTF-8、`ensure_ascii=False`、
`separators=(',',':')`。

---

## 11. 版本与兼容规则

* **schemaVersion**：当前 = 1。读取器只接受 1；不认识即拒绝（不做“尽力解析”）。
* **requiredFeatures**：v1 恒 0。未来新增**不兼容**字段时置位；旧读取器遇到未知位必须拒绝。
* **optionalFeatures**：v1 恒 0；未来可加“可忽略”的扩展位。
* **块/记录类型**：新增类型时旧读取器按未知类型拒绝（而不是跳过）。
* **读取器版本兼容**：v1 读取器可以读 v1 写入器写出的任何记录（同一 schemaVersion），
  因为字段集合由 `meta.fieldPresence` 描述，展开规则不依赖写入器实现语言。
* **降级**：新格式记录不伪装成旧 schema。旧软件遇到 trace-store 目录应明确报
  “需要 trace-store 支持”，而不是当成没有 `events.jsonl` 的坏记录。
* **兼容导出**：需要旧 JSONL/CSV 时由 `trace_store.py export`（或 P4 的导出适配器）
  流式生成，不要求存储内保留逐指令 JSON 行。
* **旧记录例外**：`schema=3`（长窗）与旧版解码器产物由 §1.2/§7 的字段变体覆盖；
  未做元素交叉校验的记录在 `meta.verify` 与交付报告中明确标注。

### 11.1 已知限制（v1 明确不做）

1. 只支持小端；大端文件明确拒绝。
2. `window()`/`function_timeline()` 的代价是 O(区间事件数)：v1 不做块级预聚合的
   精确窗口合成（P3 的“分层汇总/区间前缀”）。`page`/`event`/`navigate(call/return/
   exception/issue)` 是 O(log 块 + 一个检查点间隔 + 页大小)。
3. `navigate(source)` 与 `navigate(pc=...)` 可能退化为区间扫描（无变化点/PC 出现索引）。
4. 原生 range 边界不单独存储：展开段是“可无损合并/切分”的粗化单位，
   精确的原生范围聚合留给 P3（写入器可在保存前重建基本块）。
5. 元素文本、原生包文本不存储；包说明文本按旧记录字符串保存。
6. 未实现 `.etmrecord` 打包/导入导出（P4/P6）。

---

## 12. 参考实现入口

```powershell
# 写入（生成新 generation 并原子发布）
python -X utf8 etm/tools/trace_store.py write <record_dir> <store_dir>

# 检查
python -X utf8 etm/tools/trace_store.py info <store_dir>
python -X utf8 etm/tools/trace_store.py page <store_dir> --offset 0 --limit 150
python -X utf8 etm/tools/trace_store.py event <store_dir> 12345
python -X utf8 etm/tools/trace_store.py window <store_dir> --start 0 --end 200000
python -X utf8 etm/tools/trace_store.py export <store_dir> out.jsonl --start 0 --end 1000

# 逐事件 + 逐接口对照（差异必须为 0）
python -X utf8 etm/tools/trace_store_compare.py <record_dir> <store_dir> \
    --build-reference-index --json-out report.json
```

Python API：

```python
from trace_store import write, open_store
result = write(record_dir, store_dir)
with open_store(store_dir) as store:
    store.overview(); store.page(offset=0, limit=150); store.event(42)
    store.window(0, 200000); store.navigate(0, 1, 'call'); store.issues()
    for event in store.events(): ...          # 流式，O(1) 内存
```
