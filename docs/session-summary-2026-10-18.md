# 2026-10-17/18 会话总结：三仓合并 + H743 真机验收 + 探针 60 MHz 定因

## 一、三仓合并 → `E:\superlinkPRO`（已完成）

`E:\Share\github\akaLinkPro`（只取 hpm5301evklite）+ `E:\web-serial-rtt-tools` + `E:\web-etm-trace`
→ 一个仓库，**3296 个跟踪文件 / 174 MB**，7 个 DAG 任务由 3 个 agent 成员完成。

| 子树 | 内容 | 文件数 |
|---|---|---|
| `firmware/` | akaLinkPro 5301 应用 + bootloader（仅 evklite） | 130 |
| `script_test/` | 原样留根（Makefile 相对根引用，零改动可构建） | 289 |
| `tools/` | akaLinkPro 根级工具 | 19 |
| `web/` | 工具箱前端整树 + bridge + tools + samples | 2042 |
| `etm/` | ETM 后端 + 解码 + vendor/OpenCSD + 页面源码 + 靶子固件 | 599 |
| `docs/` | 170 个 akaLinkPro 文档 + 跨仓文档 | 约 175 |

关键决策（`docs/decisions.md`）：D-1 不迁原板板级；D-2 ETM firmware 只留 2 个靶子；
D-4 根 `docs/` 混合但**有意为之**；**D-6 逐文件复制而非 subtree**（保留源 commit SHA，代价是无逐文件 blame）；
**D-7 ETM 页面不物理搬迁**（三条真实依赖：独立启动 / 便携包自洽 / serve-etm 只改 HTML 不 JS import）。

**交付验收**：`docs/merge-verification.md` —— 账目逐字节闭合 175,039,703 B、0 未解释；驱动统一后
`dap-webusb.js` 全仓仅 4 处（1 真源 + 1 生成副本 + 1 便携包快照 + 1 历史证据），8 个共享文件 sha256 全等，
`make gen-shared --check` 8/8。

## 二、合并暴露并修掉的真问题

| 提交 | 问题 |
|---|---|
| `089e383` / `a162001` | **ELF 节间空隙被填 0xFF**（`.isr_vector` 与 `.text` 之间 8 B 对齐洞）→ `0x08000298 目标 ff ≠ ELF 00`；改为用 PT_LOAD 段字节填充，并给带空隙的 `.hex` 加确认门 |
| `26e22a3` | **探针连通 ≠ RTT 就绪**：`_startRtt()` 的异常走 `_connectProbe` 外层 catch → 断开已连上的探针并报"连接失败"；改为分离报错 + 手填地址失效时自动扫描找回 + 换芯片清地址 |
| `437adf9` | ETM 线上页 + 本地桥**跨源打通**：发布 `web/etm/`（27 文件生成副本 + `--check`）、桥地址可配、CORS 白名单、放宽 `attach` 同源断言 |
| `2b016c2` / `fd6e5ce` | harness 两处**陈旧假设**：`#h-reconnect` 是死 id（页面已改 `#h-refresh`）；写存盘前没建 `tmp/` |
| `d791732` | 便携包重打**不再依赖 gitignored 产物**（缺 demo 降级、缺 OpenCSD 静态库自动 cmake） |
| `5f247b8` | Viewer 判决 **300 → 250 KB/s**（实测分布 269~420 全跟宿主负载走，非放宽标准） |
| `130de20` | **探针 60 MHz 滑档三处修复**（见第五节） |
| `7102488` | 去掉两个开发脚本写死的老仓绝对路径 |

## 三、H743 真机性能（全部在**裸克隆**里跑，与老仓无关）

`make hw-campaign-h743 --local` = **20 通过 / 0 失败**（修固件前）：

| 项 | 数值 |
|---|---|
| RTT Viewer（60 MHz 请求） | 411.0 / 419.8 KB/s（轮询 34~35 Hz） |
| RTT 转发 | 2.45 MB/s（探针侧 2.25）—— 当时被**降到 45 MHz** |
| 10 s 存盘 | 24.92 / 24.85 MB，一致性 PASS，可读 `hello world!` |
| J-Scope 1 变量 @2µs | 491.96 kHz / 1,475,972 样本 |
| J-Scope 1 变量 @20µs | 50.00 kHz · 探针跳拍 **26.7 ppm** · USB 0 · 缺口 0 |
| J-Scope 3 变量 @20µs | 50.00 kHz · 20.0 ppm · USB 0 · 缺口 0 |
| 交替烧录 5 遍 | flood 均 2.66 s / scope 均 2.69 s |

`test-dbg-stress` **82/0**；`hw-random-flow-h743` **3/3 场景 PASS**。

## 四、ETM（合并后真机采集）

链路：OpenOCD 烧 H743 → 页面载 ELF（`c610192ce4ac`）→ 断点 `trace_hook_entry@0x08000b50` →
`ELF 校验通过：31628 字节` → LA 触发 → 解码 → 记录已保存（24 s）。

| 指标 | 值 |
|---|---|
| 指令 / 已执行 / 已映射源码 | **210,486 / 181,996 / 172,116** |
| ETM 包 / 区间 / 包问题 | 14,894 / 46,120 / **0** |
| 解码状态 | `done` |
| 采样 | 500 MSa/s · 10 ms · 深 5,000,000 · 阈值 1.65 V |
| 目标 | `targetVerified: true` · Cortex-M7 · ETMv4 |
| 时钟 | core 60 MHz · **physicalTraceHz 30 MHz** |
| ETM 选项 | branchBroadcast / returnStack / cycleCount **全开** |

线上：`https://minichao9901.github.io/superlinkPRO/etm/` → **200**；跨源发现本地桥成功
（`已找到本地桥 http://127.0.0.1:8899（跨源）`、子页 `本地服务在线`）。
便携包：**24,986,639 B / sha256 `16C77434…`**，包内三处驱动 = 真源。

## 五、探针 60 MHz 滑档：定因与修复（`docs/probe-60mhz-instability-analysis.md`）

**结论：不是信号质量问题，是代码在换档那一下没收尾。**

五条独立证据：① 稳态 60 MHz `regression-swd` 2977 KB/s **LOSSLESS**；② 失败只在换档后**第一次访问**；
③ 失败后 0~100 ms 连探 18/18 全失败，清 sticky 也不行；④ 只有整条重 init（线复位+DP 上电）能救；
⑤ 失败后再 start 在 60 MHz 成功率 ≈87% ⇒ 链路本身能跑。

三处修复（`firmware/application_5301/src/rtt/rtt_bridge.c`）：

1. `rtt_bridge_link_recover()`：**同档重 init 优先**，同档失败才降档（旧实现无条件降档）；
2. `rtt_swd_init()`：**逐级爬坡** 20→30→36→45→60（每跳 ≤1.5×）+ 每跳 `rtt_swd_settle_delay()` 静默期
   （旧实现 20→60 是 3× 直跳且立刻发第一笔 DP 读）；
3. 换挡返回值不再 `(void)` 丢弃，任一级失败即返回 -1。

**验收**：

| | 修前 | 修后 |
|---|---|---|
| 请求 60 的实际档位 | 45 MHz | **60 MHz** |
| RTT 转发 | 2.45 MB/s | **2.90 / 2.91 MB/s（+18.4%）** |
| 探针侧搬运 | 2.25 MB/s | 2.65 MB/s |
| 10 s 存盘 | 24.9 MB | 29.5 MB |
| **留存率（20 次 start）** | ≈87%（13%/670 滑档） | **20/20 = 100%** |
| campaign 判决 | 20/0（在 45 MHz 下） | **20/0（在 60 MHz 下）** |

## 六、其它

- **探针固件 WebUSB landing page** 改为 `minichao9901.github.io/superlinkPRO` 并烧录验证
  （二进制含新串、不含旧串；DFU 写 192,752 B、probe is back）。bootloader 里的上游链接按用户意见**未改**。
- **GitHub**：`minichao9901/superlinkPRO`，`main` 已推（Pages 由 Actions 从 `web/` 发布）；
  `gh-pages` 分支作为备用发布通道保留。
- **删老仓已具备条件**（静态 0 引用 + 裸克隆跑通全部真机流程）。唯一注意：`etm/records/` 不入库 →
  删老仓后重打包**不带内置 demo**（会打印警告，包照常可用）。

## 七、遗留（按优先级）

1. **harness 探针掉线兜底**：现在 `transferIn: A transfer error` 会升级成
   `TypeError: Cannot read properties of undefined (reading 'flash')` 并中断整轮；应加"重连+重试一次"。
2. **线上 ETM 采集**仍需用户在线上页点一次 WebUSB 授权（页面探测已确认走 WebUSB，按用户意见不改）。
3. `etm/records/` 的 demo 记录要不要单独归档（决定重打包是否带示例）。
4. 三条老仓（`E:\web-etm-trace`、`E:\web-serial-rtt-tools`、`E:\Share\github\akaLinkPro`）保留中，未删。
