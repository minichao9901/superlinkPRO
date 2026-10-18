# 长窗采集为什么"看起来只能 10 ms"—— 根因与修复（2026-10-18）

> 🚫 **2026-10-18 晚更新：本文的「4 s / 21 亿样本」上限已作废**。产品改为**单次采集 + 解码 +
> 页面显示统一上限 200 ms**（一次解码完成，不再分窗）。下面关于 1 / 2 / 4 s **深度**的实测仍然
> 是**设备能力**（LA 采得下），但**产品侧不再允许**；历史长窗记录仍可用分窗代码离线重建。
> 决策、根因与真机验收见 `docs/capture-200ms-2026-10-18.md`。

## 结论先说

**Kingst 这台 LA 采得了长窗**（实测 50 ms → 深度 25,000,000；页面 200 ms 单窗成功）。
之前"只能 10 ms"是我**用错接口**得出的错误结论，不是设备或产品缺陷。

## 三条根因（都是调用姿势，不是能力）

1. **直连 socket 的设参顺序不对**
   仓库 CLI（`tools/la/kingst_la.py capture`）是这么下的：

   ```
   set-sample-rate <rate>
   set-sample-time <seconds>      # 或 set-sample-depth <n>，二选一
   set-sample-depth <depth>
   ```

   我直接 `Vis.cmd('set-sample-depth 500000000')` 时，设备回**空串**（不报错），随后
   `get-actual-sample-depth` 仍是默认 5,000,000 —— 设参根本没落地。**判据只能看
   `get-actual-sample-depth` / `get-actual-sample-time`**，不能看 set 的返回值。

2. **"武装未触发"的采集会一直占住硬件**
   `kingst_la.py:974` 的注释已经写明：上一次 armed-but-not-triggered 的采集会让后续
   `set-sample-rate` 全部 NAK/静默失效。必须先 `stop`（必要时 `set-trigger --reset`）再设参。

3. **深度是"跳变存储"语义**
   `docs/逻辑分析仪攻略.md`：`set-sample-depth <n>` 是**跳变存储**的深度，
   不是"时间 × 采样率"的直觉换算 —— 稀疏信号能覆盖很长时间。

## 实测证据

```
$ python tools/la/kingst_la.py capture --rate 500000000 --time 0.05 \
      --channels 0 1 2 3 4 --trigger=--reset --out tmp/la-50ms.csv --no-kvdat
设置采样率 500 MHz ... OK
采样时间 0.05 s
采集完成，用时 0.62 s
实际深度 25000000，时间窗 0 0.05      ← 50 ms，深度 25M
```

**单窗上限实测（500 MSa/s，2026-10-18）**：设备内存足够，按需要设多长都行。

| `--time` | 实际深度 | 时间窗 |
|---|---|---|
| 0.05 s | 25,000,000 | 0–0.05 s |
| **1.0 s** | **500,000,000** | 0–1 s |
| **2.0 s** | **1,000,000,000** | 0–2 s |
| **4.0 s** | **2,000,000,000** | 0–4 s |

页面侧 200 ms 单窗（复位→初始化→枚举→echo 全在一次采集里）：

```
记录 capture-2b0c20fd… · seconds=0.2 · config=0x4113（RS+CC）
capture.csv 477.7 MB · native.jsonl 274 MB · events.jsonl 1.62 GB
解码：3,704,392 指令 / 302,217 包 / 包问题 0 / 11,398,739 周期（约 7 分钟）
```

对照：同一台设备在 500 MSa/s 下**默认**深度是 5,000,000（= 10 ms，我最早测到的那个数，
它只是默认值，不是上限）。

## 产品侧改动（本次）

`etm/server.py` 的深度闸门：

```diff
- if not (0 < seconds <= 2 and 0 < rate <= 5e8 and seconds*rate <= 1e8):
+ if not (0 < seconds <= 4 and 0 < rate <= 5e8 and seconds*rate <= 21e8):
```

* 原来 1e8 样本 ⇒ 500 MSa/s 下最多 **200 ms**；
* 现在 21e8 样本 / 4 s ⇒ 500 MSa/s 下最长 **4 s**（100 MSa/s 可 21 s）。
* `seconds > 0.02` 会自动走**流式解码**（`decode_stream`，内存 O(1)）。

## 代价与建议（实测口径）

| 窗口 | CSV | 解码 | 适用 |
|---|---|---|---|
| 10 ms | 26 MB | 20–30 s | 常规抓取 |
| **50 ms** | ~120 MB | ~1 min | echo / 单次事务 |
| **200 ms** | **478 MB** | 数分钟 | 复位→初始化→枚举→echo 全流程 |
| 500 ms–1 s | 1.2–2.5 GB | 十几分钟以上 | 建议**分窗解码**（见下） |

长窗分窗解码（历史 `captures/h743/long500` / `longflow` 就是这么做的）：

```bash
# 一次连续采集 1.0 s 后，按 50 ms 窗口解码
python -X utf8 tools/h743/long_flow.py --record captures/h743/longflow \
       --start 0 --end 0.5 --window 0.05            # 导出窗口
python -X utf8 -m tools.decode_stream ...           # 或直接调 decode_capture_stream
```

历史那次的具体口径（提交 `5ae2429`）：**一次连续采集 500MSa/s × 1.0 s**、
复位停在 `trace_hook_entry`、300 次连续 `hello world!`（每 4 ms，首次 588 ms）；
解码分窗 `[0,20ms]` 与 `[600,620ms]`，并在此之前做 **4 ns 软件滤波**。

## 另一个会咬人的点：残留的 FPB 比较器

`复位并停在业务起点失败：复位后停在 0x08000914，未到达起点 0x08000b50` ——
复位后先命中的是**上一次失败采集留在硬件里的 `main` 断点**（FPB 比较器），
页面的断点列表里看不到它。采集脚本/流程必须**先清 FPB**：

```js
// window.__etm.target
const ctrl = await t.read(0xE0002000);
const n = ((ctrl >>> 8) & 0x70) | ((ctrl >>> 4) & 0xf);
for (let i = 0; i < n; i++) await t.write(0xE0002008 + i * 4, 0, false);
await t.write(0xE0002000, 0, false);
```

## 复现

```bash
# 设备能力
python tools/la/kingst_la.py capture --rate 500000000 --time 0.05 --channels 0 1 2 3 4 --trigger=--reset --out tmp/x.csv --no-kvdat

# 长窗（页面）：#duration 设 200（ms）、#glitch-ns 设 4、入口 trace_hook_entry + 复位
node tmp/etm-echo-capture.mjs     # DURATION_MS=200 GLITCH=4 RESET=1 BP=trace_hook_entry
```
