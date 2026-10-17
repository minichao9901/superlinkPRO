# H743 ETM 会话交接（2026-10-17 夜）— 供 review

会话：DSH `session-4d688a58`。本文覆盖本轮全部改动、证据、**脚本路径**与复现方式，
供 codex 复核。仓库：`E:\web-etm-trace`（ETM 后端/网页）、`E:\web-serial-rtt-tools`（工具箱 + 便携桥包）。

---

## 1. 原生 Chrome WebUSB 端到端：从"永远连不上"到跑通

### 1.1 三个缺陷（前两个是真 bug，第三个是我自己引入又撤掉的）

| # | 缺陷 | 决定性证据 | 修法 | 提交 |
|---|---|---|---|---|
| 1 | `resetDevice()` 的 **lease 分支没有 try/catch**，`device.reset()` 异常一路抛到 `_claim()`，打开流程中断，而 UI 已置"已连接" | 日志 `Failed to execute 'reset' on 'USBDevice': Unable to reset the device`，之后点任何操作都不写日志；断开/重连被 disabled 挡住 | 整体 try/catch，**永不抛**；reset 降级为"上次会话挂起传输"的尽力清理。**参照 pyOCD：打开探针只做 `claim_interface`，从不 reset**（`pyocd/probe/pydapaccess/interface/pyusb_backend.py:126-130`） | `964039e` |
| 2 | `target.connect()` 传 **`skipClearHalt: true`**，把 `_claim()` 里唯一的端点复位整段跳过 → 陈旧 IN 包永远吃不掉 | 日志 `响应回显 0x3 ≠ 命令 0x0` | 不再跳 clearHalt | `3ccce81` |
| 3 | 我第一版"修法"是在 `_claim` 里用 8 次短超时 `transferIn` 排空 IN | 反而把「回显不匹配」变成 **`USB 读 超时`**（更糟） | **撤销**，并在代码注释里写明原因：**WebUSB 没有取消接口，超时的 transferIn 会一直挂着偷走后续响应** | `3ccce81` |

### 1.2 验收（真浏览器 + 真探针，H743 @ CPU70/TRACECLK35）

流程一次走完：载 ELF（Flash 校验 31,628 B 一致）→ 硬件断点 `trace_hook_entry 0x08000B50`
→ 核时钟（SWD 回读 CPU 70.000 / TRACECLK 35.000 MHz）→ LA 联动采集（50 ms @500 MSa/s + 4 ns 软件滤波）
→ 解码 → 记录已保存。

| 指标 | 值 |
|---|---:|
| 指令 | **1,098,860**（执行 937,170 / 源码映射 1,060,490） |
| ETM 原始输入 / 未处理 | 78,064 B / **0 B** |
| 已识别包 | 74,698 |
| **溢出 / 包问题** | **0 / 0** |
| TPIU 帧 | 5,248，结构错误 **0**，`id_changes=[2]` |
| 严格帧 | `mid_frame_fsyncs 0`、`idle_resyncs 0` |
| LA 面板滤波快照 | **全部关闭**（可审计） |

记录：`E:\web-etm-trace\records\capture-ab4f7bdcac984a758085cee69930b544\`。

---

## 2. "目标仍暂停，未命中本页断点"（包路径确定性失败）→ 定因 + 自愈

### 2.1 现象
独立桥包（8878）上，采集走到 `准备参数 → LA 等待触发` 后立刻失败，
**连续 3 次可复现**；同一流程在工具箱服务上曾成功过（比较器恰好被正常摘掉的运气）。

### 2.2 诊断（先补数据，再动手）
在 `target.js` 的失败分支里补 PC/DFSR/DHCSR/DEMCR 与"同地址断点状态"（提交 `66b758e` 一并）：

```
PC 0x08000B50    ← 正是业务起点
DFSR 0x00000003  ← bit0 断点命中 + bit1 BKPT
DHCSR 0x00030003 ← S_HALT=1
同地址断点：0x08000B50(未租用)
```

### 2.3 根因
`target.reset({address})` 为了"复位后停在业务起点"，会临时在入口装一个 FPB 比较器，
cleanup 里本该 `clearEntryBreak()` 摘掉；但**这颗探针的 PPB 写偶发丢失**（项目文档早记过），
比较器残留 → 放行瞬间又命中同一地址 → `run()` 判定"仍暂停"，而该地址的页面断点已被
`breakpoints.suspend()`（未租用）→ 落到"未命中本页断点"分支，**采集永远起不来**。

### 2.4 修法
`target.js` 的 `run()` 增加 `clearStaleComparator(pc)`：按 `FP_CTRL` 现场回读比较器个数/版本，
找出与该地址匹配（忽略 match 位与使能位，掩码 `0x1FFFFFFC`）的残留比较器 → 写 0 →
**回读确认**（失败补写一次）→ 重新放行一次；仍跑不起来才抛错，且抛错保留全部诊断字段。

### 2.5 复测：独立包反复启动 **3/3 稳定**
用重新构建的包（包自带 bridge 跑 8878，H743 CPU60/TRACECLK30）：

| 次 | 时间 | 指令 | 溢出/包问题 | gaps |
|---:|---|---:|---:|---:|
| 1 | 19:01:29 | 945,686 | **0 / 0** | 1 |
| 2 | 19:04:17 | 945,677 | **0 / 0** | 1 |
| 3 | 19:07:25 | 945,677 | **0 / 0** | 1 |

指令数几乎完全一致（945,677 出现两次）⇒ 采集链路是**确定性**的，不是碰运气。

---

## 3. 新用户痛点修复

| 痛点 | 处理 | 提交 |
|---|---|---|
| H743/F103 没有 HSE 默认值 → 第一次采集必失败 | `state.hseValues` 补 `stm32h743:25`、`stm32f103ze:8`（依据固件 `HSE_VALUE`）。**实测：连接后 HSE 自动为 25**（此前为空） | `d358d1d` |
| 时钟核对失败提示太绕 | 改为「实测 CPU … / TRACECLK …；预期 …」+ HSE 为空时点名"最可能的原因：HSE 没填（本板 25 MHz / F103 8 MHz）" | `d358d1d` |
| ETM 页在 `/etm/` 而 `/` 是工具箱 | 写进 README | 工具箱 `180490e` |
| WebUSB 授权按「来源+端口」记 | 写进 README（固定 8878 只授权一次） | 同上 |
| 客户没有示例 ELF | 包内本就带 3 份（H743/H7B0/F103），README 指明路径 | 同上 |
| `start-etm.bat` 依赖兄弟仓 `web-etm-trace` | 加护栏：检测不到就提示改用便携包 `start-etm-bridge.bat` | 工具箱 `56c1ca6` |
| 探针互斥（浏览器占着时 OpenOCD 烧不进） | 只写进文档（浏览器查不到进程，无法自动检测） | README |
| 包与源码不同步 | 发布必须两步：`sync-etm-runtime.py` → `build-etm-package.py`（写进审计报告） | — |

痛点审计全文：[`docs/h743-newcomer-audit-2026-10-17.md`](h743-newcomer-audit-2026-10-17.md)。

---

## 4. TRACECLK 上限修正：**40MHz 可用**（旧"30MHz 上限"作废）

CPU 固定 480MHz 密集流；KingstVIS 重启 + 面板滤波确认全关；严格帧 + 4ns 软件滤波：

| TRACECLK | 次数 | CH0 最小宽度 | 2ns 脉冲 | 结果 |
|---:|---:|---:|---:|---|
| 30 MHz | 多次 | 2–14 ns | 0–58 | ✅ 通过（示例 497,074 条 / 包问题 0） |
| 35 MHz | 1 | 12 ns | 0 | ✅ 通过（102,036 条 / 包问题 0） |
| **40 MHz** | **3** | 2 / **8** / **8** ns | 43 / **0** / **0** | ✅ **3/3 通过**（441,808 与 461,648 条，后两次包问题 0） |
| **48 MHz** | 2 | 2 ns | 24,342 / **7** | ❌ 2/2 失败 |
| 60 MHz | 2 | 2 ns | 128 / 361 | ❌ 2/2 失败 |

**48MHz 的失败不是毛刺**：其中一次只有 7 个 2ns 脉冲仍失败，且**扫遍 19 个取样相位（0.05–0.95）全灭**
（`可用相位: []`，各相位取样裕量仅 0–0.2 ns，异常半周期恒为 633）。
⇒ 半周期 10.4 ns ≈ 5.2 个采样点，**瓶颈从"信号毛刺"转成"500MSa/s 采样裕量不足"**。
**要上 48MHz+ 只能换更高采样率的 LA。**

记录：`captures/h743/{tr40-run1..3,tr48-run1..2,tr60-run1..2}`；明细 `tmp/retest40.json`、`tmp/retest-tr48.json`、`tmp/retest-tr60.json`。

---

## 5. 长采集（500ms）分片流式：完整简化 C

- 驱动：`tools/h743/long_flow.py`（LA 逐窗导小 CSV → `decode_capture_stream` → 事件按全局 seq 追加到
  `merged/events.jsonl` → 合并摘要 + 流式导出**一份完整**的 `etm-simplified.c`）。
- 结果：9/10 窗（第 10 窗被 LA `sampling in progress` 拒绝），**10,071,001 条指令**，包问题 **0**，
  简化 C **2,016,872 行 / 77.4 MB**。
- 产物：`captures/h743/long500/merged/etm-simplified.c`（`decoded.json` 里记了缺失窗）。
  ⚠️ 磁盘清理时已删除 4GB 的 `events.jsonl` 与分窗目录，**只保留这份 .c 与摘要**。
- 收尾脚本：`tmp/finalize_long500.py`（可续跑，不重建事件文件）。

---

## 6. 桥包重建（本次发布内容）

```powershell
cd E:\web-serial-rtt-tools
python -X utf8 tools/dev/sync-etm-runtime.py --etm-root E:\web-etm-trace
python -X utf8 tools/dev/build-etm-package.py
```

- **必须先 sync**：`build-etm-package.py` 打的是工具箱里的快照 `bridge/etm/backend/**`，
  漏了第一步就会出现"包内还是旧 dap-webusb.js"（本次第一版就是这样，已记录）。
- 本次产物：`bridge/etm/downloads/etm-bridge-win64.zip`
  `sourceRevision=d358d1d`、`sha256 78465d43a850df316d44147c22abd8edda7b648f8b7dd6a3b31d2837f68b5279`、
  24,964,316 B。
- 包里现在含：reset 永不抛、clearHalt 真执行、FPB 残留自愈、HSE 默认值 + 新报错文案。

---

## 7. 磁盘事件（有教训，记录在案）

- E: 曾**满到 0 GB**，连脚本都写不进去（ENOSPC）；清理后回到 41.6 GB。
- 清掉：`captures` 旧实验 9.45 GB、`records` 旧记录 161 个 14.71 GB、`long500` 中间产物 ~10 GB、
  `tmp/etm-package-*` 20 个、`E:\web-etm-package-test`、`E:\tmp-clone-audit`、`decoded/`。
- 用户批准后删了 `E:\web-etm-trace-coordination\artifacts\h7b0-acceptance`（9.11 GB）。
- 🚨 **误删教训**：`records/capture-926a4f2b899542f5b566e68a1d1eb631` 是
  `baselines/spi-70-35-v1/manifest.json` 的 `previewRecord`，删掉后 `sync-etm-runtime.py`
  直接 `FileNotFoundError`、打包跟着失败。已从工具箱验证副本
  `docs/validation/20261016-web-review-062721/etm-records/demo-spi-70-35-v1/` 恢复（其
  `spi-validation.json` 的 `record` 正是该 ID）。
  ⇒ 以后清 `records/` 只能删早于最后一个 baseline 的记录，或先跑一次 sync 确认。

---

## 8. 脚本与工具路径清单（review 用）

**ETM 仓（`E:\web-etm-trace`）**

| 路径 | 用途 |
|---|---|
| `web/app/shared/rtt/dap-webusb.js` | 探针驱动；本轮改 `resetDevice()`（永不抛）与撤销 drain 循环 |
| `web/app/target.js` | 目标会话；本轮改 `connect()`（不再 skipClearHalt）、`run()`（`clearStaleComparator` 自愈 + 诊断） |
| `web/app/view.js` | 页面装配；本轮改 `hseValues` 默认值、时钟核对失败文案 |
| `tools/h743/long_flow.py` | 500ms 分片流式驱动（新增） |
| `tools/h743/review-captures.py` | 只读物理质量体检（CH0 最小宽度/2ns 计数 + 已存 TPIU 严格帧复核） |
| `tools/h743/h743-flash.ps1` | 烧录（注意：路径要转成正斜杠，Tcl 会吃 `\f`/`\b`/`\t`） |
| `tools/h743/ocd.ps1` | OpenOCD 一次性命令（读寄存器/IDCODE） |
| `tools/h743/h743-phase-scan.py` | 取样相位扫描（48MHz 全灭的判据就来自这里） |
| `tools/h743_etm.py` | 主入口：info/etm-on/etm-off/capture/span/decode（含 `--filter-ns`） |
| `docs/h743-newcomer-audit-2026-10-17.md` | 新用户审计（含本节 §1–§5 的精简版） |
| `docs/h743-handoff-after-codex.md` | 与 codex 交接的主文档（§9–§11 是本轮新增） |
| `tmp/pkg-drive.mjs` | **独立包端到端 CDP 驱动**（导航→ELF→连探针→断点→采集→读摘要），本次所有包验收都用它 |
| `tmp/etm-drive.mjs` / `etm-reconnect.mjs` / `etm-disconnect.mjs` / `etm-dump.mjs` / `etm-loadelf.mjs` / `etm-capture2.mjs` | 工具箱服务那侧的同类驱动/诊断脚本 |
| `tmp/retest40.py` / `tmp/retest-tr.py` | 频率复测（build+flash+采集+解码，输出 JSON） |
| `tmp/finalize_long500.py` | 长采集收尾（汇总 + 导出简化 C，可续跑） |
| `tmp/cleanup-disk.ps1` | 磁盘清理（显式路径；因 ENOSPC 未能落盘，最终用等价内联命令执行） |

**工具箱仓（`E:\web-serial-rtt-tools`）**

| 路径 | 用途 |
|---|---|
| `start-etm.bat` | 起 ETM 桥 + 默认用 Chrome 带 `--remote-debugging-port=9340` 打开 `#tab-etm`；含兄弟仓护栏 |
| `tools/dev/serve-etm.py` | 把 ETM 挂到 `/etm/`、注入 `data-api-base="/etm/api/"` |
| `tools/dev/sync-etm-runtime.py` | 把 ETM 仓源码同步进 `bridge/etm/backend` 快照（**打包前必跑**） |
| `tools/dev/build-etm-package.py` | 打 `bridge/etm/downloads/etm-bridge-win64.zip` |
| `bridge/etm/downloads/etm-bridge-win64.zip` | 便携包（`etm-bridge.exe` + 内置 OpenCSD 解码器 + 3 份示例 ELF） |
| `bridge/etm/backend/web/**` | 包内网页快照（与 ETM 仓对应文件逐字节相同） |

---

## 9. 复现步骤（reviewer 可直接照做）

```powershell
# 1) 起 ETM 服务（工具箱式，同源托管 /etm/）
cd E:\web-serial-rtt-tools
python -X utf8 tools\dev\serve-etm.py --etm-root ..\web-etm-trace --port 8878
#    或：解压便携包后双击 start-etm-bridge.bat（默认也是 8878）

# 2) 页面里授权探针（首次）
#    http://127.0.0.1:8878/etm/  → 「连接探针」→ 选 akaLinkPro (0D28:0204)

# 3) 目标准备（H743：CPU60/TRACECLK30 固件）
powershell -File tools\h743\h743-flash.ps1 -Elf E:\web-etm-trace\firmware\stm32h743_cdc_trace\build\fw.elf

# 4) 页面里：载 ELF → 加 trace_hook_entry 断点并选为起点 → 核对时钟（HSE 已默认 25）
#    → 「开始采集」（50ms / 500MSa/s / 4ns 软件滤波）

# 5) 无浏览器时用 CDP 驱动整套（本次验收就是这么跑的）
node tmp\pkg-drive.mjs
```

**验收判据**：`job` 走到「记录已保存」；摘要里 `未处理输入 0 字节`、`溢出 / 包问题 0 / 0`；
`SWD 时钟回读` 与预期一致；`LA 毛刺滤波快照 全部关闭`。

---

## 10. 尚未完成 / 留给下一轮

1. **48MHz+ 未通**：需要更高采样率 LA，或改 TPIU 2 位模式（未做）。
2. **探针互斥无法自动检测**：浏览器拿不到进程列表，只做了文档与提示。
3. **网页装不了超长记录**（前端持有事件数组）：500ms 级别仍走 `tools/h743/long_flow.py`。
4. **H743 仍是"实验适配"**：`traceStatus` 文案未改；同组 H742/H750/H753 未宣称。
5. **物理拔插/重枚举未单独验收**（目标没接 NRST，恢复只能走软件路径）。
6. 工具箱里有几处**不是本次改动**的未提交修改（`app/etm/style.css` 1 行、`docs/etm-integration.md`、
   `tools/dev/build-etm-package.py`、`tools/dev/sync-etm-runtime.py`、`tools/selftest/etm-history-races.test.mjs`），
   留给 codex 判定是否要一并提交。
