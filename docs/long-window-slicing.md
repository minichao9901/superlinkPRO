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
├── events.jsonl           ← 各片合并（seq 递增、cycles 累加、带 slice 字段）
├── decoded.json           ← 合并 summary + slices[] + captureScope='sliced-window'
└── session.json           ← longWindow=true、slices[]、sliceSeconds
```

合并在 `etm/tools/slice_merge.py`（纯逻辑，可单测）：

- `merge_slice_events(dest, slice_dir, index, seq_off, cyc_off)` —— 追加一片并把
  `seq` 递增、`cycles` 累加、写上 `slice`；
- `aggregate_slices(base, slices, step, la_filter)` —— 合成记录级 `decoded.json`。

单测：`python -X utf8 etm/tools/test_slices.py`（16 项，含一个抓到的真 off-by-one：
`seq` 本身就是"第几条"，偏移不能 `+1`，否则第二片会跳号）。

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

## 仍未解（诚实记录）

**H743 + 未接 NRST 的板子，`AIRCR.SYSRESETREQ` 之后核"显示在跑但一条指令都不执行"**：
实测 `pcReset=0x080063f8`（Reset_Handler，说明复位发生了）、`DHCSR=0x01010001`（running）、
但两次采样 PC 都是 `0x0800111a`、`g_trace.loops` 精确不变、`CFSR/HFSR=0`。
判断是软复位只复位 CPU 核、不复位时钟/电源树（D1/D3 域），核拿不到取指时钟。
只有 POR（断电）或拉 NRST 能恢复 —— 所以**"复位到入口"这条路在产品里不该当默认**：

- 需要初始化段（复位→初始化→枚举→echo）：用**上电即采**（页面武装 LA → 提示上电 → 从 t=0 全程记录）；
- 只关心业务（echo/命令）：用**不复位·运行至起点**（可反复采）。

上电即采还没做，是下一步。
