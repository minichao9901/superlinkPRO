# 探针固件侧改动（J-Scope 采样器）—— 补丁草稿

给 `akaLinkPro` 探针固件（`firmware/application_5301/`）加的**探针侧 HSS 采样**：
探针自己按固定周期用 SWD 读目标 RAM 里 1~8 个变量，组 512 B 自描述包，
从 interface 0 上那个**原本闲置的 bulk IN 端点 `0x83`** 推给网页。
目标固件**一行都不用改**。

| 文件 | 干什么 |
|---|---|
| `scope_sampler.h` / `scope_sampler.c` | 采样器本体（拷到 `src/scope/`） |
| [`patch-notes.md`](patch-notes.md) | **6 处集成改动**（逐段可粘贴的代码 + 放在哪一行） |
| `../../docs/scope-page.md` | 协议/速率模型/网页侧设计（那一边的权威文档） |

> ⚠️ **这是草稿，还没在真机上编译验证过。** 验收步骤见下面第 3 节；
> 网页侧已经全部就绪并自测通过（`make test-scope` 103 项 / `test-scope-page` 37 项）。

---

## 1. 数据通路（一眼看全）

```
目标 RAM ──SWD(45/60 MHz)──▶ [scope_sampler.c]  按 span 块读 → 组帧 → 512 B 包
                                   │
                                   └──▶ usbd_ep_start_write(0, SWO_IN_EP=0x83, pkt, 512)
                                                    │
网页：WebUSB transferIn(0x83, 4096) × 3 条在飞 ◀────┘
      └─ PacketStream → decodeSamples → SampleStore(类型化 + LOD) → canvas/触发/CSV
配置与状态：HID 0x32（`app/scope/protocol.js`）↔ `api_param.c` 的 `SCOPE_CMD` 分支
```

**仲裁**：SWD 只在主循环里碰（`scope_sampler_poll()`）。默认在最近 20 ms 内有 DAP 命令时
**让路一拍**（周期会豁一个口，但绝不打断正在调试的会话）；要绝对稳的周期可以设
`SCOPE_FLAG_NO_YIELD`，代价是独占链路。与 RTT 桥**互斥**（见 patch-notes §6）。

---

## 2. 设计要点（为什么这么写）

1. **一次采样 = 按 span 块读**：变量地址排序后，间隙 ≤ 19 B 的并成一个 span，每个 span 一次
   `swd_read_memory()`。**不要每个变量各读一次** —— 每次块读有 5 次传输的固定开销
   （CSW/TAR/prime/RDBUFF，见 `rtt_bridge.c:106` 的注释），散着读会慢 3~4 倍。
   合并阈值与网页 `planReads()` 的成本模型同源（5.6 µs/块 ÷ 0.284 µs/B ≈ 19.7 B）。
2. **复用桥的 SWD 原语**：`rtt_bridge_swd_ensure_ready()` / `rtt_bridge_read()`。
   那条路径里有 4 MHz 起手、20 MHz 斜坡换挡、换挡后热身、清 sticky、失败返回 -4 让上层降档
   等一堆实测细节 —— 两处各写一份迟早改漏一处（所以 patch-notes 里给桥加了 5 个 adapter）。
3. **名义时间轴**：每个样本 `t_us += period`；追不上而跳拍时**照样推进**时间轴并把跳过的拍数
   计入 `dropped`。这样主机看到的是"时间有洞 + 丢了多少"，而不是"波形被压缩了"。
4. **USB 用 4 个包缓冲轮转**（在飞 FIFO，回调按顺序还）。缓冲用光就丢**当前包**并计入
   `usb_drop`，seq 继续递增 —— 主机按 seq 缺口 + STAT 里的计数就能看出丢了。
5. **包缓冲放非 cacheable 段**：HPM 的 USB 是 DMA，CPU 写进 D-cache 而 DMA 直接读内存的话，
   主机收到的是旧数据（`cdc_interface.c` 的 `uart_rx_buf` 也是这么放的）。
6. **DEF 包里回报 span 数**（`payload[11]`）：主机拿它跟自己的计划对账 ——
   两边合并规则不一致时能立刻发现，而不是"波形看着怪"。

---

## 3. 集成与验收（M0 标定）

### 3.1 集成（6 步，都在 patch-notes.md 里）

1. `rtt_bridge.h/.c` 加 5 个 adapter；
2. `api_param.c` 加 `SCOPE_CMD 0x32` 分支（含 `#include "scope_sampler.h"`）；
3. `main.c` 主循环加 `scope_sampler_poll();`
4. `usb_composite.c` 把空的 `swo_in_callback` 填成 `scope_sampler_tx_complete();`
5. `CMakeLists.txt` 加 `sdk_app_src(src/scope/scope_sampler.c)`；
6. 两个启动分支里互相 `stop()`（互斥）。

编译：`-Wall` 下应 0 warning（`SCOPE_PACKET` 与 `DAP_PACKET_SIZE` 都是 512）。

### 3.2 先做 M0 标定（不开真采样）

不接目标也能跑：`0x32 action 7` 配好变量表 → `action 8/9` 标定。网页上点
「标定真实速率」，它会显示 **µs/样本** 与推算的上限。**这一步的答案决定后面所有取舍**：

| 变量怎么放 | 模型估算 @45 MHz | 标定实得 |
|---|---|---|
| 8 个变量同在一个结构体（1 个 span，24 B） | ≈12.4 µs → 81 kHz | ⬜ |
| 跨两个 span（14 B + 56 B，靶子固件的布局） | ≈31 µs → 32 kHz | ⬜ |
| 8 次 `swd_read_memory()`（不合并） | ≈54 µs → 18 kHz | ⬜ |

### 3.3 上真机验收（用仓库里的靶子固件）

`tools/target-firmware/stm32f103_scope/`（10 kHz 契约波形，已验收通过）。步骤：

1. 烧靶子固件：`pwsh -File flash.ps1`；`python check.py` 应打印"✅ 全部通过"；
2. 网页 →「J-Scope 波形」→ 连探针（HID）→ 连接数据端点（WebUSB）→ 载入 `build/fw.elf`；
3. 选 8 个变量（`g_pack` 那 8 个字段 = **一个 span**，`g_tick` 在另一侧 → 也可以试跨 span）；
4. 周期 100 µs（10 kHz），时长 20 s → 开始采样；
5. **逐项对账**（都能自动化）：
   - `g_tick` 的斜率 = 采样率（相邻两点差 1，除非丢样本）；
   - 丢样本：`g_tick` 跳变量应等于页面「丢样本」计数；
   - 波形：按 `g_tick` 反算其余通道应有值（`check.py` 里那张契约表就是参照）；
   - `g_pair_a ^ g_pair_b != 0xFFFF` 的比例 = **撕裂率**（这就是"8 个变量不是一个原子快照"的量化证据）；
   - 把采样率降到 6 kHz，`g_sq5k`（5 kHz 方波）应出现明显的**混叠**假波；
   - `g_sq5k` 与 `g_pack.i_sq1k` 的时间轴与预期周期（5 kHz / 1 kHz）一致。

### 3.4 通过标准

- 20 s 采集：`seq` 无缺口（或缺口数与 STAT 的 `dropped` 对得上）、零重同步；
- 实测速率与设定周期一致（±0.5%，目标 HSI 自身有 ±1% 误差，别拿它当绝对基准）；
- 10 kHz 下 `dropped = 0`；极限档允许丢，但**必须如实显示**。

---

## 4. 风险与已知边界

| 风险 | 说明 / 缓解 |
|---|---|
| **探针 CPU 可能饱和** | SWD 是 bit-bang（45 MHz 档 CPU 基本全程在打时序），再叠采样循环 + USB 推流。→ 先跑 M0 标定；不够就降时钟或减变量数 |
| **`0x83` 的 FIFO 配额未核** | `DAP_PACKET_COUNT=4 × DAP_XFER_SIZE=1024` 已占不少；要确认再加一条 512 B IN 流够不够（不够就减小 `DAP_PACKET_COUNT`）。见 docs/scope-page.md §12.5 |
| 60 MHz 档长跑不稳 | 固件自报 3×10 s 里有 1 次出错 → 默认 45 MHz；60 MHz 走 `SCOPE_FLAG_ALLOW_60M` + 自动降档兜底 |
| 采样与调试抢链路 | 默认让路一拍；要独占就设 `SCOPE_FLAG_NO_YIELD` 并在网页上别同时开 RTT Viewer / 烧录 |
| 目标运行中读 | 8 个变量**不是一个原子快照**（撕裂窗口可量化）；64 位变量不是原子读。靶子固件的 `g_pair_a/b` 就是用来量这个的 |
| H7 的 D-Cache | AHB-AP 读绕过内核 D-cache，刚写没回写的脏行会读到**旧值** → 被采样变量要放 non-cacheable / write-through 段（MPU）。F103 没这问题 |
| 目标侧 AHB 读带宽 | 采样会占一点 AHB 带宽（对目标影响很小，但高频采样时值得量一下目标主循环是否变慢） |

---

## 5. 不在范围

探针侧触发（v2，当前主机侧触发够用）、数组整段采样、指针跟踪、局部变量、数据压缩/差分。
