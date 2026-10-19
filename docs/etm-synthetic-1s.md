# 合成 1 s 数据集（非真机）：构造规范与使用说明

状态：P6a 交付（2026-10-20）｜分支 `ds/etm-1s-p3p6`｜方案依据
`docs/etm-1s-native-pipeline-plan-20261019.md` §3.2、§8 P6、§9.1、§9.2｜实现
`tools/synthetic/build_synthetic.py`、`tools/synthetic/synthetic_events.py`

> **合成数据不是连续真机 trace。**
> 本工具把一份 10 ms / 200 ms 的既有记录按段重复拼接：段边界是**显式会话复位**，段与段之间
> 的时间、电平、调用栈、周期都不保证连续。方案 §3.2 明确允许拼接多个独立 capture 做压力
> 测试，但**不能冒充一段连续真机 trace**；§8 P6 要求真实连续 1 s 记录才能完成验收。
> 因此本数据集只能用于 P3/P4/P5 的规模化压力测试与跨块等价验证，不能用于 1 s 硬件验收、
> 不能用于宣称"1 s 达标"。真机 1 s 验收清单见 `docs/etm-1s-hardware-checklist.md`（P6b 交付）。

## 1. 产物与目录

```
<out>/
  synthetic.json                       合成说明（机器可读）：来源指纹、段边界、复位标记、声明、命令
  synthetic-result.json                最近一次构造的结果摘要（耗时/峰值 RSS/产物指纹）
  physical/
    capture-synthetic-1s.kvdat         物理 1 s 合成输入（KVDAT，优先路径）
    capture-synthetic-1s.csv           CSV 兼容替代（--kind csv）
    segments.json                      物理段表：采样位置/时间/复位点/行范围
    session-synthetic.json             合成输入的取样配置（rate/clock/data/fraction/glitch）
  unit/
    unit.events.jsonl                  单元事件 spool（canonical JSONL，可复用）
    unit.json                          单元描述（计数、来源/ spool 指纹、offsetSpan）
    decoded-meta.json                  来源 decoded.json 去掉 events/elements/packets 后的元数据
  events-record/                       streamed 记录目录（--event-sink jsonl 时生成）
    events.jsonl                       1 s 规模事件流（seq 连续、含段边界复位标记）
    decoded.json                       streamed schema 2 + 按段线性放大的 summary/quality
    firmware.elf / native-config.txt / session.json / sources.json   来源记录拷贝
    synthetic-events.json              该记录目录的合成溯源
  events-shards/                       （--event-sink shards）gzip 分片 + events.index.json
  verify/
    physical-layout.json               kingst_record.layout() 接受报告
    physical-verify.json               逐段行核对 + 逐段取样等价
    events-verify.json                 事件流自检（条数/seq/边界/标记/抽样 JSON）
  store-proof.json                     （--mode store-proof）trace-store 写入器消费证明
```

大产物（几百 MB 到数 GB）应放 `tmp/`，证据目录只保留报告/JSON/日志/指纹；报告里给出
重建命令与哈希即可。

## 2. 物理合成（KVDAT 优先，CSV 兼容）

### 2.1 拼接方式

来源记录 `capture.kvdat` 的布局（`etm/tools/kingst_record.py`）：

```
<settings>…</settings> [CRLF 等]  "kvdat\0\0\0"
  <4Q: depth, rate, trigger, channelCount>
  通道块 × N： <IBBHQ: 0x442323, channel, initialLevel, 0, edgeCount>
               edgeCount × <IB: position, 0>     # 最后一行是哨兵 position==depth
```

合成文件 = 原头部（到 `kvdat` 标记之前，逐字节复制）+ 新的采样头 + 逐通道拼接的边沿：

* `depth = 段数 × 单元 depth`（500 MSa/s × 1 s = 500,000,000 < 2³²）；
* `rate`、`trigger` 与来源一致（时间原点与来源相同）；
* 第 k 段的边沿位置 = 来源边沿 + k × 单元 depth（绝对采样位置）；
* 末尾哨兵位置 = 拼后总 depth。

`initialLevel` 只有一个，而通道电平由「首电平 ^ 边沿序号奇偶」决定（`read_tracks()`）：

* 若某通道累计真实边沿数为奇数，下一段的首电平会被整体翻转；
* 因此在段首**补 1 条边界边沿**（位置 = 段首采样位置），把奇偶补回；补边早于该段所有取样点
  （取样点在时钟边沿之间），不改变该段任何取样值；
* 若该通道首条边沿恰好在采样位置 0，补边落在段首前 1 个采样，并校验它仍晚于上一段最后一个
  取样点（否则明确失败，不会静默写出错误电平）；
* 常数通道（只有哨兵）永不补边。

### 2.2 段表与复位

`synthetic.json` 的 `physical.segments[]` 与 `physical/segments.json` 给出每一段：

| 字段 | 含义 |
|---|---|
| `index` | 段号（0 起） |
| `sampleOffset` | 该段起始采样位置（= k × 单元 depth） |
| `timeSeconds` | 该段起点时间（= (sampleOffset − trigger) / rate） |
| `seconds` | 段长（10 ms/200 ms） |
| `reset` / `resetSampleOffset` | 段边界是否为显式会话复位及其采样位置 |
| `sourceSampleSpan` | 该段在来源记录里的采样范围 |

**消费方必须按复位点重置状态**：分块前端要 `reset()`、解码器要重新建会话、统计要分段累计。
把合成文件当一整段连续输入处理，得到的跨段结果没有真机语义。

### 2.3 CSV 兼容路径

CSV 是 KingstVIS 的跳变表（`Time[s], 通道 0, …`，编码可能是 gb18030/UTF-8/UTF-16）。
拼接步骤：按整数采样网格重算时间列（`position = round(t × rate + trigger)`，要求偏离 ≤ 0.25
个采样，否则明确失败），段 k 的行位置整体 + k × depth，再写回 `repr((position − trigger)/rate)`。
输出编码与来源一致（`utf-8-sig`/`utf-16` 的 BOM 只写一次，不会每行带 BOM）。
CSV 段文件回卷时间轴后与来源段逐行同值，因此现前端 `trace_port.extract` 的物理报告可与来源
逐项比较。

## 3. 事件/规模合成

### 3.1 单元 spool

以既有记录为单元（10 ms ≈ 210,706 条指令；200 ms ≈ 3,675,435 条指令）：

* 来源可以是 `events.jsonl`（streamed schema 2/3）或 `decoded.json` 内联 `events`；
* 事件按固定字段顺序重新序列化为 `unit.events.jsonl`（canonical JSONL），来源逻辑 `seq`
  必须从 0 连续（既有记录都满足）；
* 字段集合按 `trace_store` 的三类指令变体与 9 字段边界事件校验，未知集合明确拒绝。

单元 spool 只读一次来源；生成 1 s 流时**逐行**把 `seq` 换成全局序号、把 `offset` 加上段偏移，
其余字节不动（不重新做 JSON 解析）。所以：

* 不整体驻留内存（只保留当前行 + 计数器）；
* 每段内容与来源逐字节同构（字段顺序、字符串、数值都保留）。

### 3.2 复位标记

段边界插入 1 条显式复位标记（`--markers emit`，默认）：

```json
{"kind":"event","seq":210731,"type":2,"offset":22342,
 "text":"[synthetic-session-reset] 合成段边界：显式会话复位。前后两段是同一独立 capture 的重复拷贝，时间/电平/调用栈不保证连续",
 "payload":0,"start":0,"end":0,"flags":0}
```

* `type=2`（TRACE_ON，含义=解码器重新开始）、`payload=0`（不记缺口），字段集合与
  `trace_store` 的 `EVENT_FIELDS` 完全一致 → 现 store 写入器可直接消费，并把它记为边界 issue；
* 标记**不是真机包**：真实 TRACE_ON 来自 ETM 流，这里只是人工插入的合成边界；
* 生成器 API `iter_event_dicts(marker_style='native')` 可得到 `kind='session-reset'` 的显式
  结构（带 `segmentIndex`/`sampleOffset`/`reason`），供需要区分标记的消费者使用。

### 3.3 规模与 seq/offset 重映射

对 N 段、单元 U 条事件：

* 总事件数 = N × U + (N − 1)，总指令数 = N × 单元指令数；
* 段 k 的事件 seq ∈ [k×(U+1) + 1, k×(U+1) + U]，标记 seq = k×(U+1)；
* 段 k 的 trace offset = 来源 offset + k × `offsetSpan`，默认
  `offsetSpan = 来源最大 offset + 1`（可用 `--offset-span` 指定，例如 `etm.bin` 字节数）。

10 ms × 100 段 = **21,070,600 条指令 / 21,073,199 条事件**（P6a 实测；
证据 `docs/validation/20261020-ds-etm-p6-synthetic-b2d8148/`）；200 ms × 5 段 = 18,377,175 条指令。

### 3.4 可被 store 写入器直接消费

`events-record/` 是标准 `streamed.schema=2` 记录目录：

* `decoded.json`：`streamed={schema:2, events:N×U+(N−1), packets:0}`；
* `summary` / `quality` / `inputBytes` 里的**计数类字段按段线性放大**（`instructions`、
  `executed`、`unknownExecution`、`exceptions`、`gaps`、`ranges`、`functions`、字节账等），
  与 store 写入器逐项对账口径一致；`packets`/`elements` 被移除（合成记录不伪造包/元素），
  `streamed.packets=0`；
* 其余字段（`analysis`/`physical`/`capture`/`config`/`sources` 等）保留单元值，并在
  `decoded.json.synthetic` 与 `synthetic.json.declaration` 里标注"不是 1 s 真机统计"。

`--mode store-proof` 用 `etm/tools/trace_store.py`（P1 参考写入器，只读引用）对该目录跑一遍
完整写入，证明"可直接消费"；它不评价 P3 性能。

## 4. 校验方法（构造时自动执行）

| 级别 | 内容 | 判据 |
|---|---|---|
| `--verify layout` | `kingst_record.layout()` 接受合成 KVDAT | 深度/采样率/触发位置/通道数与声明一致 |
| `--verify segments` | 逐段行核对 | 每段每通道边沿 = 来源边沿 + 段偏移；补边位置符合计划；末哨兵 = 总深度 |
| `--verify segments` | 逐段取样等价 | 从**合成文件**抽出该段、重排成独立段文件，用现前端 `trace_port.extract` 取样：phase0/phase1 字节相同，物理报告除输入路径/指纹外逐项相同；KVDAT 段文件与来源文件**逐字节一致**（SHA256 相等） |
| `--verify events` | 事件流自检 | 总条数/指令数；seq 从 0 连续；段边界与复位标记位置/offset/text；`seq`/`offset` 之外的字节不变；每 `--sample-every` 行做一次完整 JSON 语义比对 |
| `--mode store-proof` | store 兼容 | `trace_store.write()` 成功，counts 与合成声明一致 |

## 5. 复现命令

```powershell
# 物理 1 s（10 ms KVDAT × 100 段）
python -X utf8 tools/synthetic/build_synthetic.py --mode physical `
  --record etm/records/capture-4395612c313f47ce97af1e1c17e0a0d5 `
  --kind kvdat --segments 100 --out tmp/p6a-1s --verify all

# 1 s 规模事件流（同一条记录 × 100 段，≈2,107 万条指令）
python -X utf8 tools/synthetic/build_synthetic.py --mode events `
  --record etm/records/capture-4395612c313f47ce97af1e1c17e0a0d5 `
  --segments 100 --out tmp/p6a-1s --event-sink jsonl --verify all --force

# CSV 兼容（10 ms CSV × N 段；1 s 的 CSV 约 3 GB，按需运行）
python -X utf8 tools/synthetic/build_synthetic.py --mode physical --kind csv `
  --record etm/records/capture-4395612c313f47ce97af1e1c17e0a0d5 `
  --segments 10 --out tmp/p6a-csv --verify all --verify-segments 3

# 按 synthetic.json 切逐段文件（给分块前端做跨块等价）
python -X utf8 tools/synthetic/build_synthetic.py --mode split `
  --meta tmp/p6a-1s/synthetic.json --out tmp/p6a-1s/physical/segments

# store 兼容性证明（对 3 段合成记录）
python -X utf8 tools/synthetic/build_synthetic.py --mode store-proof `
  --record tmp/p6a-1s/events-record --out tmp/p6a-1s

# 单元测试
python -X utf8 -m unittest discover -s tools/synthetic -p "test_*.py" -q
```

`--reuse-unit`（默认）会复用已校验的 `unit/unit.json`（sha256 与来源一致），`--rebuild-unit`
强制重建；`--force` 只覆盖产物目录，不删单元。

## 6. 已知限制与失败模式

* **不是真机 1 s**：段间复位、重复内容、时间/调用栈不连续；不能作为 1 s 验收证据。
* **段数与体积**：KVDAT 1 s ≈ 447 MiB（10 ms×100）；事件流 21M 条 ≈ 9.1 GiB 文本
  （`--event-sink shards` 可压到约 1/4）；CSV 1 s ≈ 2.4 GB，按实测约 13 万行/s 需约 11 分钟。
* **内存**：`decoded.json` 内联 events 的来源（如 10 ms USB 记录，119 MB）在抽单元时峰值约
  570 MiB（一次性）；超过 512 MiB 的内联记录会被拒绝，请改用带 `events.jsonl` 的 200 ms 记录。
  1 s 流生成本身只占当前行，峰值 RSS 与段数无关（P6b 报告有实测）。
* **字节相位**：拼接后整段字节流的 TPIU 相位在段边界不保证连续；必须按复位段重新定帧。
* **统计口径**：`decoded.json` 只放大计数类字段；`analysis`/`physical` 仍是单元值。
* **CSV 网格**：时间列必须落在采样网格上（±0.25 采样），否则明确失败而不是静默四舍五入。
* **补边冲突**：通道首边沿在采样 0 且段深不足时拒绝构造（避免影响上一段取样）。
* **不覆盖已有证据**：输出目录已有本工具产物时需 `--force`；证据目录只放报告/JSON/日志/指纹。

## 7. 与其他任务的接口

* **P5 分块物理前端**（t2/t5）：用 `physical/segments.json` 的 `sampleOffset`/`reset` 驱动
  `reset()`；或直接消费 `--mode split` 产出的逐段 KVDAT（与来源逐字节一致），逐段与
  `capture-synthetic-1s.kvdat` 的分块结果比对。KVDAT 段文件保留源采样坐标系，可与来源
  报告的每一字段直接对比。
* **P3 融合写出器**（t1）：`events-record/` 可直接喂 `trace_store.write()`；`events.jsonl`
  是 streamed schema 2，复位标记是 `type=2`/`payload=0` 的边界事件（`kind=event`）。
  需要逐段切分时可读 `synthetic.json.events.resets[]`（seq/offset/段号）。
* **P6b 规模化验证**（t6）：本数据集提供"1 s 物理输入 + 1 s 规模事件流 + 复位表"三件套；
  真机 1 s 验收必须另用连续真机记录，并逐项对照方案 §3.1。
