# H743 CDC+ETM：codex 复核后的接管状态（2026-10-17 晚）

> 上位文档：`docs/h743-etm-handoff.md`（环境/脚本/记录/结论/P0-P4）
> 详细实验报告：`docs/h743-cdc-etm-bringup.md`（§1–§9，含全部实测）
> 本文覆盖：**codex 复核后代码状态、它对 DeepSeek 的 review 逐条与回应、本轮补做的连续长窗验证、遗留问题。**

---

## 0. 一句话状态

`master` = `241e616`（codex 的 3 个提交），本轮在其上补了 `--filter-ns` CLI、烧录脚本 Tcl 路径修复、本文与连续长窗验证。
**板子：CPU=60MHz / TRACECLK=30MHz / HCLK=60MHz / TRDT 按 HCLK；COM43 在线；固件与 `build/fw.elf` 机器码一致（verified 31628 B）。**
**未 push。H743 在产品页上仍标"实验适配"。**

---

## 1. 代码状态

```
241e616 (codex) fix(records): 隔离未完成解码并允许并发浏览
1ad1566 (codex) fix(decode): 便携流式解码跟随运行时解码器目录
f28299e (codex) feat(etm): 接入 H743 网页，严格校验帧并显式记录软件滤波
77123fb (deepseek) docs(h743): 补 5.3.1 带宽利用率/异常包/ETF 4KB
... 之前 7 个 deepseek 提交
```

工作树干净；`captures/`、`records/` 仍在 .gitignore 里（约 10GB，交接需另拷）。
codex 的证据根目录（**不在仓库里**）：
`E:/web-etm-trace-coordination/artifacts/h743-acceptance/20261017-h743-web-review-c7a4e9/`
（`review-report.md`、`deepseek-pulse-review.json`、`diagnostic-4ns.csv` 4.4GB、`strict-demux-review.json`、`fresh-capture-validation-v2.json`、`repeat-summary-*.json`、`final-source-commit.patch` 等）

---

## 2. codex 做了什么

**`f28299e`（最大一个，44 文件）**
- **撤销** DeepSeek 的"帧内 FSYNC 容忍"，恢复**严格 TPIU 帧校验**（依据 Arm IHI0029D D4.2.2/D4.2.3 + OpenCSD 1.8.3 `trc_frame_deformatter.cpp`）
- 新增**默认关闭的显式软件滤波**：`trace_port.filter_pulses()` / `extract(..., glitch_ns)` / `decode_capture(..., glitch_ns)`，0–100ns，**不改原始 CSV/KVDAT**
- 网页接入 H743（能力标志 + 真实 IDR 选项 + 时钟页改用当前 RCC 推导，不再写死 H7B0 VCO560）
- JSONL/SQLite 分页 + 真实字节核算（取代 DeepSeek 的写零字段）；`streamed.schema=2`（decoded.json 只存摘要，事件只落 JSONL）；常规内存解码**恢复 200 万条保护**
- 修：240MHz 档误用 VCO=8f 超限、`-AhbDiv 1` 被忽略、USB 模式切换超时/返回码、`ERR UNKNOWN` 去掉、烧录脚本查退出码、`span` 需 `--live-buffer`
- 修：窄屏 iframe 150px、缩放与定位光标竞态、流式重解码混合代次、旧桥混用（`captureProtocol=2`）

**`1ad1566`** 修 DeepSeek `decode_stream.py` 的便携包 bug（按值捕获 ROOT → 冻结后找不到原生解码器），改为运行时读目录 + 回归

**`241e616`** 每条记录读写门：并发浏览、解码独占、`done` 在事务释放后发布、中断/截断标记（避免把截断 JSONL 当成功）

**它做到的真机验证**：4 次采集（10ms + 3×2ms），**63,490 / 63,490 / 63,530 / 210,602 条路径**，严格帧通过，**包错误/overflow/未处理/未核算/重叠/未知源 = 全 0**，392,910 个可验证后继 0 矛盾，Flash==ELF，首条恢复指令 == `trace_hook_entry`。

---

## 3. codex 的 review 逐条 ↔ 回应

| # | codex 的指控 | 我的回应 |
|---|---|---|
| 1 | **帧内完整 FSYNC 不可能合法**（IHI0029D D4.2.2 帧间同步、D4.2.3 帧内只允许半字同步；OpenCSD 也拒绝）。放宽规则会掩盖采样错误 | ✅ **我错了，认**。我当时只证明"数据字节恒为偶数 ⇒ `FF FF FF 7F` 不是数据"，**没有**证明"它是合法同步"——实际含义相反：**它证明字节流已被错位**（毛刺 → nibble 位移）。这是本轮最实质的方法错误 |
| 2 | 流式质量字段不能写零（`overlapBytes` 硬置 0、`unaccounted = input - processed`） | ✅ 接受（我上一轮自己也已承认措辞错误） |
| 3 | "流式 O(1)" 不成立（末尾把整份 decoded.json 读回字符串，且 events 双份） | ✅ 接受。我那个实现确实不是 O(1) |
| 4 | **不要**按 mtime/size 缓存输入 SHA | ✅ **接受它的反对**。我提的"哈希缓存"会削弱证据链；保留每次真 SHA（分块读）更正确 |
| 5 | H743 网页原来不允许配置 | ✅ 接受，已由它修 |
| 6 | 固件/脚本细节：240 档 VCO 超限、`-AhbDiv 1` 被忽略、`ERR UNKNOWN`、USB 超时/返回码、烧录退出码、`span --live-buffer` | ✅ 全部接受——**前两条和 `ERR UNKNOWN` 都是我留下的 bug/待办** |
| 7 | 窄屏 iframe 退回 150px | ✅ 接受，已修 |
| 8 | 缩放完成后覆盖新定位光标 | ✅ 接受，已修 |
| 9 | 流式重解码期间暴露混合代次 | ✅ 接受，已修 |
| 10 | 新页面混用旧桥会静默忽略滤波参数 | ✅ 接受，已修（`captureProtocol=2`） |

**对我上轮三处过度声明的处理**：

| 我的原话 | 状态 |
|---|---|
| "ETM 字节一个没丢" | ❌ **撤回**（只证明无未消费字节，异常点由 gaps/RESERVED 标出） |
| "CPU 越高越好（33,848 / 68,403）" | ⚠️ **降级为待复验**（那两份记录严格帧校验失败；各记录毛刺数不同，跨记录比较不公平） |
| "TRACECLK 40MHz 是物理上限" | ❌ **作废**（dense40 原始 CSV：CH0 最小宽度 2ns、**11,744 个 2ns 脉冲**、21.7% 异常半周期；它证明该配置失败，不足以定成 LA 通用上限） |
| "带宽利用率线性外推到 1.76GHz" | ⚠️ **降级**：平均带宽不可线性外推，也不能保证突发不 overflow（ETF 4KB 只吸收 ms/10µs 级突发） |

---

## 4. 本轮补做：连续长窗（初始化 → 枚举 → hello 回显）

codex 明确列为**未通过**的一项（"本轮未重做'初始化→Windows 枚举→单次 hello'的完整连续长窗硬件验收"）。

**采集条件**（一次连续采集，非拼接）：
`captures/h743/longflow` · CPU/HCLK=60MHz / TRACECLK=30MHz · 500MSa/s × **1.0s** · 触发 `--reset --neg-edge 1` ·
`trace_hook_entry`(0x08000B50) 入口断点 → 配 ETM/TPIU → 武装 LA → 放行 → **300 次连续 `hello world!\n`（每 4ms，首次 588ms）** →
回读 `hello world!\n` · 完整波形留在 `capture.kvdat`。

**解码条件**：严格帧校验（codex 恢复的规则）+ **解码前软件滤波 4ns**（原始 CSV/KVDAT 不改写）。

| 窗口 | 指令 | 执行 | 范围内存 | **包问题** | gaps | TPIU 帧 | 4ns 移除的时钟跳变 | 关键函数 |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| 初始化 `[0, 20ms]` | 379,277 | 322,416 | 75,892 | **0** | 1 | 1,796 | CH0=2（CH4=14,248） | `OTG_FS_IRQHandler`(7) `USBD_IRQHandler`(38) `dwc2_get_glb_intstatus`(7) `usbd_event_suspend_handler`(7) + main/SysTick |
| 回显 `[600, 620ms]` | 428,867 | 375,436 | 80,908 | **0** | 1 | 1,984 | CH0=6 | `bulk_out` → `cdc_write`(100) → `usbd_ep_start_write`(456) → `dwc2_ep_write`(196) → `handle_command`(228) → `strcmp`(120) → `trace_log_printf`(92) → `SEGGER_RTT_Write`(68) |

物理质量（codex 的只读工具 `tools/h743/review-captures.py`）：初始化窗 CH0 共 1,200,033 个跳变，**只有 1 个 2ns 脉冲**（早期 `full60` 是 66 个），最小宽度 2.0ns → 这一份的时钟质量明显优于此前记录。

**边界（必须如实标注）**：这是**同一次连续采集**（一份 KVDAT、一个触发点），但**解码是分窗做的**。
把 587ms 一次解完要 ~11M 条指令、GB 级文件、十几分钟，属于"能但代价大"；按 codex 的口径，**分窗解码不能冒充"单窗完整通过"**。

产物：`captures/h743/longflow/`（`decoded.json`、`etm-simplified.c`、`span-0p6-0p62/`、`capture.kvdat`）。

---

## 4.5 LA 面板滤波 vs 脚本滤波（实测核查，2026-10-17）

问：**KingstVIS 面板上的"毛刺滤波"是不是也是软件滤波、没改原始信号？它和我们在导出后做的脚本滤波是一回事吗？**

### 已证明的部分

| 事实 | 证据 |
|---|---|
| 面板滤波是 **KingstVIS 内部的数字后处理**（对已采样的 0/1 序列做窄脉冲抑制），**不可能改变模拟信号** | LA 采样是 500MSa/s 的硬判决，滤波只能删/挪跳变 |
| **Socket API 不能实时设置它** | `kingst_la.py glitch-filter-preset --apply` 在 VIS 运行时直接拒绝：**"Socket API 不支持实时设置滤波；请先正常退出"** |
| 它是 **`%LOCALAPPDATA%\kingst\vis.config` 里的"预设"**，**下次启动才生效** | 写入后必须 `Stop-Process KingstVIS` → 重启；仓库工具自己写"改完**必须重启** KingstVIS" |
| **kvdat / vis.config 的快照不能证明导出已被滤波** | `longflow` 的 kvdat 快照写着 CH0–4 = `enabled, 2 样本 / 4ns`，但它导出的 CSV 里**仍有 1 个 2ns 脉冲**；仓库工具注释也写"`vis.config` 只代表磁盘预设，可能落后于 GUI" |
| **同一固件、同一采样率，早前的窄脉冲现在不可复现** | 本轮三次全新采集（面板滤波 关 / 4ns / 8ns，每次都是"改预设→重启 VIS→再抓"）：**CH0–CH4 全部 0 个 2ns、0 个 4ns，最小宽度 12–14ns**；而今天早些时候的记录是每 5ms 约 10 个 2ns 脉冲 |

**⇒ 这说明早前那些 2ns 窄脉冲是采集侧/会话态产物（可复现性差），不是稳定的物理属性**——
与"那些记录严格帧校验失败"是同一个来源。codex 的措辞"是数字采样观测，不能仅据此断言是振铃/串扰/硬件故障"是对的。

### 没证成的部分（如实记）

我本想用 0ns / 4ns / 8ns 三档 A/B **直接判定"面板滤波是否作用于导出"**，但**撞上地板效应**：
当前信号里**已经没有 <12ns 的窄脉冲可滤**，三档导出的统计完全相同（0/0，最小宽度 12–14ns）。
⇒ **本次实验无法判定**该问题；要判定必须让一个**确实含窄脉冲**的采集来对比（当前配置已不产生）。

### 4.5.1 决定性实验（2026-10-17 15:40，推翻上节"地板效应"的判断）

用户从 LA 面板确认：**毛刺过滤对话框自己写着"勾选某一通道后，小于设定宽度的脉冲将被删除"**——它是**删除数据**的操作。

据此设计**不依赖稀有窄脉冲**的实验：把 CH0 的滤波设到 **10 采样周期 = 20ns**，它**大于 TRACECLK 的 16.7ns 半周期**，若导出被滤波，CH0 会整体塌掉。

| 记录 | CH0 面板滤波 | 导出 CSV 里 CH0 跳变 | 最小宽度 | 2ns 脉冲 |
|---|---|---:|---:|---:|
| `filt0`（以为关了） | 关闭 | 300,007 | 14.0ns | 0 |
| **`filt20ns`** | **10 样本 / 20ns** | **0** | — | 0 |
| **`filt-off-final`** | **真·关闭** | **300,041** | **2.0ns** | **17** |

**⇒ 两条结论，都是硬证据：**

1. **LA 面板滤波确实作用在导出的 CSV 上**（CH0 从 30 万跳变变成 **0**，不可能是"没有窄脉冲可滤"的地板效应）。
   **它不是"只改显示"**——导出数据已被删改，**不能再当原始波形**。
2. **上一节"地板效应"的判断是错的**：那些 2ns 脉冲一直存在（真关闭后有 17 个/5ms、最小宽度 2.0ns），
   之前三份"0 个"是因为**滤波实际上仍在生效**——又一次印证 **`vis.config`/kvdat 快照 ≠ 生效状态**。

### 4.5.2 最终口径

| | LA 面板滤波 | 脚本滤波（`trace_port.filter_pulses`） |
|---|---|---|
| 作用点 | **导出之前**（厂商应用内部） | 导出之后（在跳变列表上） |
| 会不会改证据 | **会**：CSV 里窄脉冲被删除，原始波形只剩 kvdat | **不会**：原始 CSV/KVDAT 一字不改 |
| 单位 | 采样点数（×2ns） | ns |
| 生效条件 | **必须重启 VIS**；Socket API 不能实时设 | 每次解码命令行指定 |
| 可审计性 | **差**：只有一个快照，且已证明不靠谱 | 好：`software_filter.removed_transitions` 逐通道记录 |
| 如何判断当前是否开着 | **抓一份看最小宽度**：有 2ns 脉冲=关；全是 ≥设定宽度=开 | 看命令参数 |

**推荐做法（本仓最终采用）**：**LA 面板滤波保持关闭**，一律用 `--filter-ns 4` 的脚本滤波，
并用"抓一份看有没有 2ns 脉冲"来**现场验证面板确实关着**（本轮已验：`filt-off-final` 最小宽度 2.0ns、17 个 2ns 脉冲 ⇒ 面板已关）。
这样原始 CSV/KVDAT 保真，滤波量可审计——`longflow` 的严格帧 + 4ns 脚本滤波解出 0 包问题，就是这个口径下的结果。

### 结论（工程口径）

1. **两者不是一回事**：
   - **面板滤波**：导出**之前**、在厂商应用内部做、阈值单位是**采样点数**、**必须重启才生效**、**从我们这边不可审计**（只有一个快照，而快照已被证明不保真）。
   - **脚本滤波**（`trace_port.filter_pulses`）：导出**之后**、在跳变列表上做、阈值单位 **ns**、**原始 CSV/KVDAT 一字不改**、每次记录 `software_filter.removed_transitions`（逐通道可审计）。
2. **效果同类**（都是删窄脉冲），因此**不要叠加使用而不记录**：若面板滤波是开着的，导出的 CSV 可能已经"干净"，我们的脚本滤波就变成二次处理——而**原始波形只剩 kvdat**。
3. **可审计的做法**：**面板滤波保持关闭**，一律用脚本滤波；需要对照时用 `--filter-ns 0/4` 各解一次，`removed_transitions` 会告诉你删了什么。
4. 本轮结束时已把 `vis.config` **恢复成核查前的原值**（CH0–4 = 启用 / 2 样本），并重启过 VIS（所以**当前运行中的 VIS 确实加载着 2 样本滤波**）。若要严格可审计，建议手动关掉；一条命令即可：
   `python -X utf8 tools/la/kingst_la.py glitch-filter-preset --channels 0 1 2 3 4 --disable --apply`（先退出 VIS，再重启）。

## 8. TRACECLK 上限重测（2026-10-17 15:45，取代先前作废的"40MHz 上限"结论）

**条件**：CPU 固定 480MHz（密集流，最有利于严格帧）· LA 面板滤波**关闭**（原始保真，已用"有 2ns 脉冲"验证）·
解码前脚本滤波 **4ns** · **严格 TPIU 帧规则** · 500MSa/s × 5ms。

| TRACECLK | CH0 最小宽度 | **2ns 脉冲 / 5ms** | 指令 | 包问题 | gaps | 结果 |
|---:|---:|---:|---:|---:|---:|---|
| **30 MHz**（R=16） | 2.0ns | **58** | **497,074** | **0** | 1 | ✅ **通过** |
| 40 MHz（R=12） | 2.0ns | **24,050** | — | — | — | ❌ 帧内完整同步/非法 ID 0x7f（物理偏移 788） |
| 48 MHz（R=10） | 2.0ns | 2,249 | — | — | — | ❌ 没有 FSYNC |
| 60 MHz（R=8） | 2.0ns | 160 | — | — | — | ❌ 帧内完整同步（偏移 539） |

**结论**

1. **加脚本 4ns 滤波之后，上限仍是 ~30MHz**：30MHz 干净解出 497,074 条指令、0 包问题；40/48/60MHz 全部在**严格帧**下失败（表现为字节流错位：帧内完整同步 / 没有 FSYNC）。
2. **失败方式与"毛刺"高度相关但不单调**：2ns 脉冲数 58 → 24,050 → 2,249 → 160。
   非单调说明它们**更像 500MSa/s 采样网格与边沿的相位拍频产物**，而不是干净的"带宽滚降"。
   ⇒ 以现有仪器**无法把"LA 采样/边沿判决"与"接线/目标驱动能力"分开**（要分开得用示波器直接看 PE2）。
3. 因此正确的表述是：**本设置（Kingst 500MSa/s + PE2–PE6 现接线）下 TRACECLK 的可用上限约 30MHz**，
   而不是"这块板的物理上限"。换更好的探头/接线或更高采样率的 LA 有可能提高，未验证。
4. 之前"40MHz 是物理上限"的结论作废（依据是 21.7% 异常半周期的烂采集），本次是**在可审计条件下重测**得到的同向结论。

记录：`captures/h743/tr30`、`tr40`、`tr48`、`tr60`；明细 `tmp/trace-clock-ceiling.json`。

## 9. 原生 Chrome WebUSB 端到端（2026-10-17 16:30，**未通过，已定因**）

用户授权后实测（Chrome + 测试 profile + `http://127.0.0.1:8766/`，服务为 codex 版 `captureProtocol=2`）：

| 步骤 | 结果 |
|---|---|
| 点「连接探针」→ Chrome 设备选择 | ✅ 用户已授权（`akaLinkPro 0D28:0204`），页面**成功认领**设备（按钮状态变为「连接探针」disabled、「断开」可点） |
| 首页日志 | ❌ **`Failed to execute 'reset' on 'USBDevice': Unable to reset the device.`** |
| 之后点「重新读取 RCC 与频率」 | ❌ **完全无反应**：不新增日志、CPU/TRACECLK 仍「未知」 |
| 「断开」→ 重连 | ❌ 同前，仍无通信、无日志 |
| **物理拔插探针**后再「断开 → 重连」 | ❌ 仍无通信、无日志；`#connect` 一直是 disabled（页面自认为已连接） |

**结论：原生 WebUSB 路径在这颗探针上"能认领、不能通信"。**
根因是 `USBDevice.reset()` 失败后，应用的连接流程**没有降级/超时**，把操作锁拖住了——
之后所有动作都静默不执行（连日志都不写），断开/重连/拔插都无法自愈。

这正好解释了 codex 为什么在验收里用了一个"仅存在于证据目录的 USB 字节转发适配层"：
**原生路径从未真正跑通**。要修，需要改网页工具的 WebUSB 打开路径：

1. `device.reset()` **失败当作非致命**（记 warning 继续 `claimInterface`），这只探针对 reset 的支持本就不可靠；
2. 给 reset/claim 加**硬超时**（如 2 s），超时就把设备标脏并**关闭句柄重开**，绝不让操作锁悬着；
3. 认领失败按已有经验做**退避重试**（3 次）；
4. 失败必须在日志里写清"哪一步、什么错、建议拔插还是重开"——现在是一句不响。

> 对照：同一条链路的**原生 CLI/SWD 路径**（pyOCD）一直完全正常，本轮所有采集/解码都基于它；
> 只有"浏览器内 WebUSB 直连"这一条没通。

## 10. 原生 Chrome WebUSB 端到端：**已打通**（2026-10-17 17:13，取代 §9 的"未通过"）

§9 记录的是失败现象；本节是**定位过程 + 修复 + 成功验收**。

### 10.1 三个缺陷（前两个是真 bug，第三个是我自己引入的）

| # | 缺陷 | 证据 | 修法 | 提交 |
|---|---|---|---|---|
| 1 | `resetDevice()` 的 **lease 分支没有 try/catch**，`device.reset()` 的异常一路抛到 `_claim()`，打开流程中断而 UI 已置"已连接" | 日志 `Failed to execute 'reset' on 'USBDevice': Unable to reset the device`，之后点任何操作都不写日志 | 整体 try/catch，**永不抛**；reset 降级为"上次会话挂起传输"的尽力清理；失败照常 claim。**参照 pyOCD：打开探针只做 `claim_interface`，从不 reset**（`pyocd/probe/pydapaccess/interface/pyusb_backend.py:126-130`） | `964039e` |
| 2 | `target.connect()` 传了 **`skipClearHalt: true`**，把 `_claim()` 里唯一的"端点复位（clearHalt）"整段跳过 → 陈旧响应包永远吃不掉 | 日志 `响应回显 0x3 ≠ 命令 0x0`；断开/重连被 disabled 挡住，重试代码走不到；重载后自动重连又卡住 | 不再跳 clearHalt，让 clearHalt + resync 正常执行 | `3ccce81` |
| 3 | 我第一次的"修法"是在 `_claim` 里用 8 次短超时 `transferIn` 排空 IN | 结果把「回显不匹配」变成 **`USB 读 超时`**（更糟） | **撤销**，并在代码里写明原因：**WebUSB 没有取消接口，超时的 transferIn 会一直挂着偷走后续响应**（本项目早有记录） | `3ccce81` |

### 10.2 两个现场前提（不是代码问题，但会挡住验收）

1. **KingstVIS 必须在跑**：否则采集第一步就报 `连不上 KingstVIS（127.0.0.1:23367）：WinError 10061`。
2. **时钟页的 HSE 必须填**（本板 **25** MHz）：否则"HSE 待填写"→ 算不出 CPU/TRACECLK →
   采集会在 `核对实际时钟` 这步失败（错误信息：`实际 CPU/TRACECLK 未确定或与预期不一致`）。

### 10.3 成功验收（H743 @ CPU 70 / TRACECLK 35 MHz，全程真浏览器 + 真探针）

页面流程一次走完：**载入 ELF（Flash 校验 31,628 B 一致）→ 硬件断点 `trace_hook_entry 0x08000B50`
→ 核对时钟（SWD 回读 CPU 70.000 / TRACECLK 35.000 MHz）→ 联动 LA 采集（50 ms @500 MSa/s，4 ns 软件滤波）
→ 解码 → 记录已保存**。

页面生成的记录（`records/capture-ab4f7bdcac984a758085cee69930b544/`）：

| 指标 | 值 |
|---|---:|
| 指令 | **1,098,860**（执行 937,170，源码映射 1,060,490） |
| ETM 原始输入 / 未处理 | 78,064 B / **0 B** |
| 已识别包 | 74,698 |
| **溢出 / 包问题** | **0 / 0** |
| TPIU 帧 | 5,248，**结构错误 0**，`id_changes=[2]`（单一 trace 源） |
| 严格帧 | `mid_frame_fsyncs 0`、`idle_resyncs 0` ✓ |
| 软件滤波移除跳变 | CH0 **0**、CH1 58、CH2 252、CH3 164、CH4 18,194 |
| LA 毛刺滤波快照 | **全部关闭**（原始波形保真，滤波可审计） |

### 10.4 仍然成立的限制

- **网页不能装超长记录**（前端持有事件数组）：500 ms 级别的看历史记录仍应走 CLI 分片（`tools/h743/long_flow.py`）。
- H743 在产品里仍标 **"实验适配"**，未 push；H742/H750/H753 未宣称验收。
- 物理拔插/重枚举**未单独验收**（本次是同一根线一路上跑通；目标没接 NRST，恢复只能靠软件路径）。

## 11. TRACECLK 上限：修正为 **40MHz 可用、48MHz 起不可用**（2026-10-17 17:35，取代 §8 的 30MHz 结论）

§8 那次测出"30MHz 可用、40/48/60 全失败"，但随后发现那批采集本身是脏的（`dense40` 有 11,744 个 2ns 时钟毛刺、21.7% 异常半周期）。
**KingstVIS 重启、面板滤波确认全关之后，同一块板重测结论完全不同。**

### 11.1 结果（CPU 固定 480MHz 密集流；严格帧 + 解码前 4ns 软件滤波；500MSa/s × 5ms）

| TRACECLK | 次数 | CH0 最小宽度 | 2ns 脉冲 | 结果 |
|---:|---:|---:|---:|---|
| 30 MHz | 多次 | 2–14ns | 0–58 | ✅ 通过（示例 497,074 条 / 包问题 0） |
| 35 MHz | 1 | 12ns | 0 | ✅ 通过（102,036 条 / 包问题 0） |
| **40 MHz** | **3** | 2 / **8** / **8** ns | 43 / **0** / **0** | ✅ **3/3 通过**（441,808 与 461,648 条，**包问题 0**） |
| **48 MHz** | 2 | 2ns | 24,342 / **7** | ❌ 2/2 失败 |
| 60 MHz | 2 | 2ns | 128 / 361 | ❌ 2/2 失败 |

### 11.2 关键判断

1. **"30MHz 上限"作废** —— 那是脏会话的假象；干净会话下 **40MHz 能稳定通过**，两次还是零包问题。
2. **48MHz 的失败不是毛刺造成的**：其中一次只有 **7 个** 2ns 脉冲，仍然「没有 FSYNC」。
   ⇒ 48MHz 时半周期 10.4ns ≈ **5.2 个采样点**（500MSa/s），取样相位 `fraction=0.25` 会系统性贴近边沿。
   **瓶颈从"信号毛刺"转成"LA 采样比 / 取样相位"**。
3. 因此正确的表述：**本设置（Kingst 500MSa/s + PE2–PE6 现接线）的 TRACECLK 可用上限约 40MHz**；
   再往上需要**更高采样率的 LA**，或对 48MHz+ 单独调 `fraction`（0.05–0.95 扫描）——未做。
4. 顺带记一条："同一配置不同会话的物理质量差异极大"（2ns 毛刺数在 0–24,000 之间跳），
   所以**任何频率结论都必须标注采集会话的质量**（最少给出 CH0 最小宽度 + 2ns 脉冲数）。

记录：`captures/h743/{tr40-run1..3, tr48-run1..2, tr60-run1..2}`；明细 `tmp/retest40.json`、`tmp/retest-tr48.json`、`tmp/retest-tr60.json`。

## 5. 遗留问题（按优先级）

1. **40MHz / 频率上限**：结论作废，需在**时钟无毛刺**的条件下重测（当前手段：4ns 软件滤波 + 严格帧；先要弄清 LA 的滤波是否作用于 Socket 导出——codex 说这项 A/B 仍未做）。
2. **长窗单次解码**：分窗可做，单窗代价大；若要"一气呵成"的单窗，需要更大内存 / 更长的解码预算（网页侧首建索引也是线性）。
3. **LA 滤波 ↔ CSV 导出的关系**：`cpu60-trace30` 的 KVDAT 存了 2 样本/4ns 滤波配置，但当时导出的 CSV 仍含 2ns 脉冲 → **配置快照不能证明导出已滤波**，需同一次采集做 0/4ns 两次导出的 A/B。
4. **H743 仍标实验适配、未 push**；同 DEV_ID 的 H742/H750/H753 未宣称验收。
5. **原生 Chrome WebUSB 授权、物理拔插重枚举、其他芯片实板回归**未做（codex 用只在证据目录存在的 USB 转发适配层完成了网页验收，已在报告里声明）。
6. **240MHz 档仅构建通过**，未烧录。
7. 我提出的"哈希缓存"优化**不做**（会削弱证据链）。

---

## 6. 工具与命令（本轮新增/变化）

```powershell
# 解码时开软件滤波（本轮给 CLI 补的，之前只有网页能传）
python -X utf8 tools/h743_etm.py decode --record <record> --filter-ns 4
python -X utf8 tools/h743_etm.py span   --record <record> --start 0.60 --end 0.62 --live-buffer --filter-ns 4
python -X utf8 tools/h743_etm.py capture --record <record> --at-entry trace_hook_entry `
    --trigger "--reset --neg-edge 1" --time 1.0 --rate 500000000 `
    --echo --echo-repeat 300 --echo-interval 0.004 --export-span 0 0.020 --overwrite

# 物理质量复核（codex 的只读工具：每通道跳变数/最小宽度/2ns/4ns 计数 + 已存 TPIU 的严格帧校验）
python -X utf8 tools/h743/review-captures.py <record> [<record2>...] --out <新的报告.json>

# 烧录（已修 Tcl 反斜杠问题）
pwsh -NoProfile -File tools/h743/h743-flash.ps1 [-Elf <abs path>]
```

固件构建（codex 版 `build.ps1` 参数有变化：`-AhbDiv` 默认 **0** 表示沿用档位自带 HPRE，另有 `-OutputDir` / `-NoPublish`）：

```powershell
pwsh -NoProfile -File firmware\stm32h743_cdc_trace\build.ps1 -CoreMHz 60 -TraceR 8 `
     -Gcc 'E:/Share/env-windows/tools/gnu_gcc/arm_gcc/mingw/bin/arm-none-eabi-gcc.exe'
```

---

## 7. 新踩的坑（本轮）

1. **`h743-flash.ps1` 的路径不能带反斜杠进 OpenOCD 的 `-c "..."`**：那是 **Tcl** 字符串，`\f`/`\b`/`\t` 会被当转义，表现为
   `couldn't open E:web-etm-traceirmwarestm32h743_cdc_traceuildw.elf`。已修（`$Elf -replace '\\','/'`）。
   **凡把路径塞进 OpenOCD `-c` 的地方都要注意。**
2. **入口断点地址随固件变**：codex 重建固件后 `trace_hook_entry` 从 `0x08000B48` 变成 `0x08000B50`。
   用旧 `build/fw.elf` 会表现为"复位后没有命中入口（超时）"。**换固件后必须重建再拿地址**。
3. 板子被复位/重新烧录后，`g_trace` 在 `0x20003000`：`34524354`(TRC4) + sysclk + ready，可做一键盘点。

### 11.3 48MHz 的相位扫描（补证：不是相位选错）

对 tr48-run2（只有 7 个 2ns 毛刺）扫遍 **19 个取样相位（0.05-0.95）**：
**全部失败**（可用相位 []），且每个相位的取样裕量只有 **0-0.2 ns**，异常半周期恒为 633。

=> 48MHz 的失败**不是取样相位选错**，而是 **500MSa/s 在该速率下采样裕量不足**
（半周期 10.4ns 约 5.2 个采样点，边沿抖动直接把取样点压到跳变上）。
**要上 48MHz 以上只能换更高采样率的 LA**，本 LA 到此为止。

