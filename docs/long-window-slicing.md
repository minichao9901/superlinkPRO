# 长窗采集：一次采下来，后台自动分窗（2026-10-18 落地）

## 用户看到的

时长填 > 150 ms，点「采集」。中间什么都不用管，最后得到**一条记录**：

```
长窗采集：服务端将按 200 ms 分窗导出并逐片解码（5 片）
采集 → 导出第 1/5 片 → 解码第 1/5 片 → … → 合成记录
```

序列 / 火焰 / 函数 / 热点这些视图照常能用，读的是**整段**（各片事件已按时间序拼好）。

## 为什么要分窗（不是设计偏好，是硬约束）

Kingst 的**全量导出**在半秒以上会卡死：
实测 1 s（≈470M 采样）时 `capture.csv` 一直是 0 字节、页面永远停在「保存原始数据」，
而 **LA 缓冲里的数据本身是好的**（`get-actual-sample-depth` 仍报 469,650,358）。
CLI 的 `--time-span` 可以只导一段 —— 0.2 s ≈ 481 MB CSV、解码约 7 分钟 ≈ 3.9M 指令。

这正是前人 `tools/h743/long_flow.py` / `tmp/finalize_long500.py` 的做法
（提交 `5ae2429`：一次连续采集 500MSa/s × 1.0 s + 分窗解码 [0,20ms]/[600,620ms] + 4 ns 滤波），
本项目只是把它**固化成产品路径**，不再需要人工分片。

## 服务端做了什么

`etm/server.py`：

| 位置 | 改动 |
|---|---|
| `SLICE_MIN_SECONDS = 0.15` | 超过这个窗口自动走分窗（更短的单窗导出本来就是好的） |
| `DEFAULT_SLICE_SECONDS = 0.2` | 单片时长；`/api/capture` 可用 `sliceSeconds` 覆盖（夹在 0.05 s～总时长） |
| `export_slices()` | 采集完成后：逐片 `do_export(time_span=(t0,t1))` → `decode_capture_stream` 解到 `slices/NN/` → 合并 → 建浏览库 |
| `capture_job()` | `seconds > 0.15 && decode` 走 `export_slices()`，否则走原来的单窗路径 |
| `_decode_job()` | 长窗记录**拒绝按单窗重解**（会只剩第一片），给明确提示而不是悄悄降级 |

记录目录结构：

```
capture-xxxxxxxx/
├── slices/00/{capture.csv, events.jsonl, decoded.json}   ← 每片原始产物，可单独复解
├── slices/01/...
├── slices.json            ← 片表（t0/t1/指令/包/周期/包问题），每解完一片就更新
├── events.jsonl           ← 各片合并（seq 按输出顺序重编、cycles 原样保留、带 slice 字段）
├── decoded.json           ← 合并 summary/quality/**analysis** + slices[] + streamed.schema=3
└── session.json           ← longWindow=true、slices[]、sliceSeconds
```

**多片记录没有顶层 `capture.csv`**（这是与单窗记录最大的契约差异，2026-10-18 审查 R01 修正）：

| 文件/字段 | 单窗记录（`streamed.schema=2`） | 长窗记录（`streamed.schema=3`） |
|---|---|---|
| 原始波形 | 顶层 `capture.csv` | `slices/NN/capture.csv`（逐片） |
| `events.jsonl` | 顶层，全量 | 顶层，各片合并（`seq` 连续、带 `slice`） |
| `packets.jsonl` / `elements.jsonl` | 顶层 | **只在各片目录**（字节偏移互不相通，不能直接拼） |
| `decoded.json.capture` | 含 CSV 指纹 | **没有**（各片指纹在 `slices/NN/decoded.json`） |
| 下载原始波形 | `GET /api/download/<sid>/raw` | `GET /api/download/<sid>/raw?slice=K` |
| 下载整包 | `GET /api/download/<sid>/record`（.etmrecord） | 不提供（数百 MB×N 片）；会返回明确提示 |

`decoded.json.analysis` 也不是"第一片的分析"：函数/热点/火焰/调用边/周期都按片合并，
`summary.totalCycles` 取各片 `analysis.timing.totalCycles` 之和（旧实现按"每事件 cycles 最大值"
算，1 s 记录实测只有 912，正确值约 3,600 万）。


合并在 `etm/tools/slice_merge.py`（纯逻辑，可单测）：

- `merge_slice_events(dest, slice_dir, index, seq_off)` —— 追加一片并把 `seq` **按输出顺序重编**、
  写上 `slice`；`cycles` 是**每元素量**，原样保留（不再累加偏移）；
- `aggregate_slices(base, slices, step, la_filter, parts)` —— 合成记录级 `decoded.json`：
  summary/quality 逐片相加、`analysis`（函数/热点/火焰/调用边/timing）按片合并。

单测：`python -X utf8 etm/tools/test_slices.py`；读取契约回归：
`python -X utf8 -m unittest discover -s etm/tools/tests -p "test_slice_contract.py"`。
两处都曾抓出真 bug：
① `seq` 用"片内最大 seq"推偏移会撞号（0 起的片）；② 周期按每元素最大值求和 → 912 vs 3,600 万。

**旧记录可以离线修好**（不重新解码、不碰 LA）：各片 `slices/NN/` 仍是好的，只要重放一次合并：

```powershell
python -X utf8 etm/tools/refinalize_slices.py etm/records/capture-xxxxxxxx
```

脚本先写 `events.jsonl.rebuild`、成功后原子替换，并顺手作废旧 `browse-v3.sqlite`
（下次浏览按新签名重建）。1 s / 5.7 GB 记录实测约 1~2 分钟。


## 页面做了什么

`etm/web/app/view.js`：

```js
const captureSeconds = Number($('duration').value)/1000;
const sliceSeconds = captureSeconds > 0.15 ? 0.2 : null;
if (sliceSeconds) log(`长窗采集：服务端将按 200 ms 分窗导出并逐片解码（N 片）`);
await api('capture', { seconds: captureSeconds, sliceSeconds, ... });
```

即：页面只多传一个 `sliceSeconds`，其余流程完全不变。

## 顺便修掉的两个真 bug（同在这条链路上）

1. **H743 的系统复位会清空 FPB 比较器**（实测：复位前 `comp[0]=0x88000b51`，`reset halt`
   之后 `=0`）。原来"先装业务断点再复位"在 H7 上**永远停不到起点**。已改成
   `VC_CORERESET` 停在 Reset_Handler → **停住之后**再装比较器 → 转普通 halt → 清 VC_CORERESET → 放行
   （`etm/web/app/target.js` 的 `reset()`）。
2. **放行必须回读校验**：这颗探针的 PPB 写偶发不落地，`DHCSR.C_HALT` 清不掉时核会一直停着，
   15 s 后误报"未命中起点"。现在最多重写 6 次并在日志里写清哪一次没生效。

## 曾经的误判（已推翻，2026-10-18 复查）

~~"H743 + 未接 NRST 的板子，`AIRCR.SYSRESETREQ` 之后核显示在跑却一条指令都不执行；
软复位只复位 CPU 核、不复位时钟/电源树。"~~

**真因（`7006219` 已修）**：H743 的软复位会把**调试端口本身**一起复位。复位后不重建 SWD，
写 `DHCSR` 会"看着成功"（回读 running），但核其实没有恢复执行 —— 症状恰好是
`PC` 与 `g_trace.loops` 一动不动、无 fault。修法是复位停住后先 `probe.recoverDebugPort()`
重建 DP/AP，再装比较器、放行；`f9daf69` 又加了"失败自动重试一次"和"核在跑吗"自检。

复查记录见 `docs/handoff-2026-10-18.md` §3 与 §9（"这次会话里被证伪/纠正过的结论"）。
真机复跑（断电后首次采集）仍待安排 —— 那是验证，不是未修代码。

