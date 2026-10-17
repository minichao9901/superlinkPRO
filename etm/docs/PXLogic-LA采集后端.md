# PXLogic 采集后端（PXView CLI）

日期：2026-10-13。设备：**PX_Tool PX-Logic U3 channel 32**（`PX USB3.0 Logic 16`，`VID_16C0&PID_05DC`，USB 3.0 枚举）。
入口：`tools/la/pxlogic_la.py`（Python，纯标准库；通过 subprocess 调 `C:\Program Files\PXView\pxview-cli.cmd`）。
目的：让 **PXLogic 与 Kingst 成为两个可互换的 LA 后端**，在文件层收敛到同一份跳变 CSV，既有解码链不用改。

## 0. 溯源纪律（按 Codex review 2026-10-13 修正）

CLI 读不到设备实际采样率，所以工具**不把请求值当已实测**：

- 清单里 `capture.requested` 与 `capture.actual` 分开；`actual.verified=false` 时，时间轴与频率都只是
  「按请求采样率换算」，不能当已证实；
- 导出容量与请求样本数不符（差不在 0~7 个补齐位内）时**默认拒绝**，给出 `--force`（放行但记 `count_mismatch`）
  和 `--verify-hz`（用采集里已知频率的参考信号反推实际采样率，通过则 `actual.verified=true`）两条明确出路；
- 每次采集用**唯一目录**（`<out 同名>.pxlogic/<时间戳-UUID>/`）；`--raw-dir` 指向已有目录时默认拒绝，
  `--reuse-raw` 才允许并先清掉里面的旧 `channel_*.bin`；导出后还会核对每个文件的 mtime 属于本次导出、
  各通道长度一致，避免把上次波形当本次结果；
- 清单记录每个 bin 的字节数、SHA256、mtime 与 `stale_removed`。

已知设备行为（实测，原因未在 CLI 层查明，故只记事实）：10 MSa/s 下 1 ms / 5 ms 窗口分别返回
**10,048 / 50,048** 样本（比请求多 48），而 10 MSa/s 的 20 ms / 100 ms 与 500 MSa/s 的 2 ms / 10 ms
精确相符。用 `--verify-hz 1000`（CH0 接 LA 自身 PWM0）核对：实测采样率 10.000000 MSa/s、偏差 0.0000%，
即**采样率没变，只是窗口长了一点**——这正是 `--verify-hz` 存在的意义。


集成复核补充（Codex）：

- 秒级时间戳追加 UUID，快速连续采集也不覆盖旧位流；自检同样使用独立目录并拒绝空、缺失、过期的导出。
- 参考信号核验至少需要三个完整周期，以同极性沿的中位完整周期推算采样率。核验通过后，CSV、窗口时长和统计均使用核验值，记录 `time_axis_rate_hz`；未核验时明确标为请求值。
- 正常的末字节补齐单独记录 `padding_samples`，不会误标为采集样本数不符。
- 跳变提取改为分块处理，避免密集 Trace 波形对每个边沿重复处理整个百万位整数。

## 1. 为什么这样做

Kingst 那条路是「外部程序采集 → 导出跳变 CSV → Python 解析」。PXLogic 采用同样形态，只是外部程序换成
PXView 自带的 `pxview-cli`：

```text
pxview-cli capture ...            # 采集（阻塞到完成）
pxview-cli export --format binary # channel_N.bin，位打包
（Python）channel_N.bin → 跳变 CSV「Time[s], 通道 N, ...」
    ↓
tools/trace_port.py / tools/decode_trace.py（与 Kingst 采集完全相同的下游）
```

不使用 MCP / HTTP 直连，也不 `import pxview_automation`：生产路径只有 subprocess + 文件。

## 2. 命令

```powershell
cd E:\web-etm-trace

# 设备与服务状态
python -X utf8 tools\la\pxlogic_la.py info
python -X utf8 tools\la\pxlogic_la.py devices --json

# 采集（ETM 形态：5 通道、500 MSa/s、2 ms）
python -X utf8 tools\la\pxlogic_la.py capture `
  --channels 0,1,2,3,4 --rate 500M --time 2ms --threshold 1.5 `
  --out captures\f103ze-pxlogic.csv --stats
# 有已知频率参考时顺带核对实际采样率（本机 CH0 = LA 自身 PWM0 = 1 kHz）
python -X utf8 tools\la\pxlogic_la.py capture --channels 0 --rate 10M --time 5ms `
  --out captures\ref.csv --verify-hz 1000 --stats

# 已知信号自检（CH0 接 LA 自身 PWM0：1 kHz、50%），失败返回非 0，可当验收脚本
python -X utf8 tools\la\pxlogic_la.py selftest --rate 10M --time 100ms

# 不重新采集，导出当前 buffer
python -X utf8 tools\la\pxlogic_la.py export --dir captures\raw --channels 0,1,2,3,4 --format both

# 由已有位流目录补生成跳变 CSV / 统计
python -X utf8 tools\la\pxlogic_la.py csv captures\raw --rate 500M --samples 1000000 --out captures\a.csv
python -X utf8 tools\la\pxlogic_la.py stats captures\raw --rate 500M --samples 1000000 --json
```

`capture` 产出三样东西：

| 产物 | 说明 |
|---|---|
| `<out>.csv` | 与 Kingst 同构的跳变 CSV（UTF-8 BOM，表头 `Time[s], 通道 N`），直接喂 `tools/trace_port.py` |
| `<out 同名>.pxlogic/<时间戳-UUID>/channel_N.bin` | PXView 原始位打包位流，8 样本/字节、LSB-first、逐通道、无文件头 |
| `<out 同名>.pxlogic/<时间戳-UUID>/capture.json` | 采集清单：设备、请求/实际采样率与样本数、阈值、各文件 SHA256 与 mtime、`stale_removed`、实际用过的命令行 |

`--raw-dir` 可把位流目录指到别处（已有旧位流时默认拒绝，加 `--reuse-raw` 才清空复用）；
`--with-csv` 额外导出 PXView 的逐样本 CSV（每通道一个大文件，仅人工查看用）；
`--force` / `--verify-hz` 见第 0 节。

## 3. 本机实测结论（不是推测）

| 项 | 实测值 |
|---|---|
| 采样率/时长是否被如实执行 | 500 MSa/s × 2 ms × 5 通道 → 每通道 125,000 B = **1,000,000 样本**（= 500M × 2ms，精确） |
| 500 MSa/s × 10 ms × 5 通道 | 2.98 MB 位流，采集 + 导出 + 转 CSV **总耗时 1.6 s** |
| 位打包语义 | 字节 i 的 bit k = 样本 `i*8+k`；`ceil(N/8)` 字节；与 `.pxc/.pxl`、MCP `get_samples` 负载逐字节一致 |
| CH0 参考信号（LA 自身 PWM0） | 1 kHz、50% 占空比；10 MSa/s 下半周期恒为 5000 样本，99.0% 落在中位值 ±0.1% 内 |
| 采样率上限抽查 | 100M / 250M / 500M 在 5 通道下均 `state=completed` |

## 4. 踩过的坑（都要继续遵守）

1. **`export` 不传 `--digital-channels` 时，csv/vcd/hex/bits 一个文件都不生成**，但 stdout 打印 `exported`、退出码 0。
   只有 `binary` 默认导出全部已使能通道。工具里已固定带通道列表。
2. **bin 按字节补齐，末字节补齐位会产生假跳变**：样本数不是 8 的倍数时，尾部会凭空多出“电平掉到 0”的跳变。
   所以 `transitions()` / `write_transition_csv()` / `channel_stats()` 都必须传**真实样本数**（`capture` 内部按
   采样率 × 时长算并核对容量，差 <8 样本才接受）。已加回归测试。
3. **频率别用“首末边沿跨度”算**：采集起点常有一个 1 样本毛刺，跨度法会把 1 kHz 算成 1008~1023 Hz。
   用**同极性沿之间的中位完整周期**（工具默认，不要求 50% 占空比），同时报告 `frequency_from_span_hz` 与毛刺数以便对账。
4. **CLI 没有 stop/取消子命令**（子命令只有 14 个：list-devices、scan、channels、capture、decode、results、
   export、export-table、samples、status、save、load、run、list-decoders）。等待触发的采集挂住只能去界面里停，
   所以所有调用都必须带超时（工具默认 60 s）。
5. **`capture --trigger 通道:类型` 目前不生效**：CLI 把它塞进服务器 `start_capture` 不接收的
   `digitalCaptureMode` 字段。工具会打印告警并如实把 `trigger_effective: false` 写进清单。
6. **客户端把未显式指定超时的调用钳到 30 s**（`client.py:194`），超大导出可能超时；本工程 10 ms 级采集实测无风险。
7. **所有 CLI 调用共享 PXView 的同一个会话**，不能并发跑两条采集/导出（并发会互相清数据）。
8. **`channels`/`list-decoders` 在 GBK 控制台会崩**（中文返回 + 管道编码）→ 子进程固定 `PYTHONIOENCODING=utf-8`。
9. GUI 正在运行时 CLI 可直接用，**不要另起 headless PXView 抢设备**。
10. **跳变 CSV 里不能有 `#` 注释行**：`tools/trace_port.py:read_tracks()` 把第一个非空行当表头，
    多一行注释就会报「不是 Kingst 秒数时间列：'Time[s]'」。溯源信息一律写进 `capture.json`
    （已加回归测试 `test_generated_csv_has_no_comment_line`）。

更细的 CLI 事实（每个子命令的参数、输出字段、退出码、静默陷阱、源码行号）见
[PXView CLI 技术参考](PXView-CLI技术参考.md)。

## 5. 与 Web 界面集成（两个 LA 后端）

文件层兼容已验证；Web 断点联动还需要就绪通知、有效触发、停止和错误清理接口，当前不能把两个后端视为等价联动实现：

| 接口 | Kingst（现状） | PXLogic（本次） |
|---|---|---|
| 设备枚举 | `kingst_la.py info`（KingstVIS socket） | `pxlogic_la.py info/devices --json` |
| 采集 | `capture.ps1` / `kingst_la.py capture` | `pxlogic_la.py capture --channels --rate --time --threshold` |
| 原始数据 | `.kvdat` + 跳变 CSV | `channel_N.bin` + 跳变 CSV（同构） |
| 溯源 | `laFilter`、KVDAT 快照 | `capture.json`（设备、采样率、阈值、样本数、SHA256、原始命令） |

解码入口不变：两者都产出 `Time[s], 通道 N` 跳变 CSV → `tools/trace_port.py`（DDR 提取）；
PXLogic 另有直读位流的 `tools/pxlogic_trace_port.py`（不经过 CSV，少一层转换）。
界面接入前必须补齐 arm → ready → CPU run → stop/export 的流程并实板验收，再增加后端选择与采集清单保存。当前 Web 仍使用 Kingst，PXLogic 工具仅提供命令行采集与离线转换。

```powershell
# 复现：PXLogic 采集 → 与 Kingst 完全相同的 DDR 提取（本机已跑通）
python -X utf8 tools\la\pxlogic_la.py capture --channels 0,1,2,3,4 --rate 500M --time 2ms `
  --threshold 1.5 --out tmp\pxlogic-test\etm-2ms.csv
python -X utf8 tools\trace_port.py tmp\pxlogic-test\etm-2ms.csv --out tmp\pxlogic-test\etm-2ms
```

第二步实测输出（本次只有 CH0 接了 1 kHz 参考信号，数据线无信号，故字节流是空载形态；
关键是既有消费者能吃这份 CSV 并照常出报告）：

```text
TRACECLK 均值 = 0.001280 MHz，中位 = 0.0010 MHz；半周期 = 500000.0 ns
时钟跳变 5，异常半周期 1
数据跳变：{'CH1': 0, 'CH2': 0, 'CH3': 0, 'CH4': 0}
phase0: 2 bytes，FSYNC 候选 0
报告：tmp\pxlogic-test\etm-2ms.trace.json
```

## 6. 测试

```powershell
python -X utf8 -m unittest tools.tests.test_pxlogic_la -v     # 23 项
```

覆盖：采样率/时长/通道解析、位打包位序、跳变序号语义、**末字节补齐不产生假跳变**（回归）、
CSV 往返一致性（逐通道跳变时刻对齐位流）、以及**合成 DDR 波形经 CSV 交给 `trace_port.extract`
必须还原出原始载荷**（证明 PXLogic 采集能走通既有解码链，不需要硬件）。

完整回归须在集成目录执行：独立 worktree 不包含忽略的采集/解码记录和完整 SDK，相关数据依赖测试会缺少输入。

## 7. 尚未验证（明确保留）

- 真实 **ETM 波形**（24 MHz TRACECLK + 4 条数据线）经 PXLogic 采集后能否解出指令路径：
  需要目标板跑独立 ETM 固件并把 5 根线接到 PXLogic，属硬件验收范畴，本次只做到 1 kHz 参考信号与
  合成波形的层间验证。
- 触发、RLE、毛刺滤波、模拟/DSO 通道、`.pxc/.pxl` 存档往返：CLI 或本工具暂未覆盖。
- 长时间连续采集（Stream 模式）与磁盘缓存行为。
