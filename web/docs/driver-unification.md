# 探针驱动统一（合并定稿，t6）

> 目标：三个源仓里**同一段探针驱动有 2~3 份拷贝且已经分叉**（各修各的 bug）。
> 合并后只允许**一份手写真源**；ETM 页需要的副本由脚本生成，并用 sha256 守卫。

---

## 0. 一句话结论

- **唯一手写真源**：`web/app/**` 里的 **8 个文件**（7 个探针驱动 + 1 个驱动依赖）。
- **生成副本**：`etm/web/app/shared/**`（ETM 页用 `./shared/...` import）——**禁止手改**。
- 生成：`node tools/dev/sync-shared-drivers.mjs`（= `make gen-shared`）。
- 守卫：`node tools/selftest/shared-drivers.test.mjs`（已并入 `make test`）+
  `node tools/dev/sync-shared-drivers.mjs --check`（已并入 `make check`）。

---

## 1. 同名文件有 4 处，各自角色不同（别误判为「还有 3 份驱动」）

| 位置 | 角色 | 谁维护 |
| --- | --- | --- |
| `web/app/rtt/dap-webusb.js`、`web/app/core/{usb-device,probe-bus,pace,bin,operation-budget}.js`、`web/app/dbg/{bp,source}.js` | **唯一真源（8 文件）** | 人工 |
| `etm/web/app/shared/**`（同样 8 个路径 + `README.md`） | **生成副本**（ETM 页 `./shared/...` 引用） | `sync-shared-drivers.mjs` 生成 |
| `web/bridge/etm/backend/web/app/shared/**` | ETM **便携包运行快照**（`etm@a2922f8` 的派生，见 `bridge/etm/provenance.json`） | `sync-etm-runtime.py` 生成；本次未重生成（见 §7） |
| `web/docs/validation/20261016-sram-root-cause-134100/baseline-app_rtt_dap-webusb.js` | 2026-10 验收留档的**基线副本**（证据，不参与运行） | 只读证据 |

`grep -r dap-webusb.js` 全仓会命中 4 个文件：**1 真源 + 1 生成副本 + 1 快照 + 1 历史证据**。
没有任何第二份手写驱动。

---

## 2. 合并前的两版差异（实测，`git diff --no-index`）

| | toolbox（`web/app`） | ETM（`etm/web/app/shared`） |
| --- | --- | --- |
| `dap-webusb.js` | 1567 行 / 90,022 B（43 次提交） | 1519 行 / 88,174 B（4 次提交） |
| 差异规模 | 基准 | 152 行（相对 toolbox **+52 / −100**） |
| `usb-device.js` | 162 行（toolbox 多 6 行） | 156 行（+5 / −11 相对 toolbox） |
| 其余 5 个（probe-bus/pace/bin/bp/source） | 逐字节相同 | 逐字节相同 |

### 2.1 逐条取舍（**一个修复都不能丢**）

| 差异点 | toolbox 侧 | ETM 侧 | 合并后 | 理由 |
| --- | --- | --- | --- | --- |
| `OperationBudget` 分阶段超时 | **有**（`_budgeted`/`_operationBudget` 13 处） | 无 | **保留 toolbox** | 打开探针最坏 30~60 s 静默；有它才能把「卡在哪一阶段」说清楚 |
| DAP 会话失效跟踪 | **有**（`_targetNeedsReconnect` 7 处、`_dapPort`、`DAP_SESSION_LOST`） | 无 | **保留 toolbox** | USB 自愈后旧的 DAP 口已失效，继续发 Transfer 只会误导 |
| `WriteABORT` 报文 | `Uint8Array.of(0, 0x1e, 0, 0, 0)`（命令 + DAP index + 4 B ABORT，CMSIS-DAP 规范） | `Uint8Array.of(0x1e,0,0,0)`（**少 DAP index = 缺陷**） | **保留 toolbox** | 不把缺陷抄回来；守卫里钉了「副本中不得出现 4 字节版本」 |
| `resetDevice()` 失败语义 | lease 分支裸 `await`，**会抛** → 一路抛到 `_claim()`，页面停在「已连接但无通信」 | `try/catch + withTimeout`，**永不抛**，失败只告警 → 继续 claim | **移植 ETM** | 2026-10 真机定因：这颗复合探针在 Windows 上 `USBDevice.reset()` 经常失败（pyOCD 打开探针根本不 reset）。没接 NRST 时尤其不能要求拔插 |
| 复位不可用时的提示 | 无 | `_note('USB 端口复位不可用（这颗探针常见）→ 跳过，直接认领接口')` | **移植 ETM** | 排障时能一眼看出"不是没连上，是复位做不到" |
| 清陈旧响应纪律 | 有 clearHalt ×2 + `resync()` | 同，且有注释：**不要**用一串短超时 `transferIn` 排空（WebUSB 无取消接口，挂着的那次会偷响应） | **移植注释** | 防回归；这条是 ETM 侧真机踩出来的 |
| FAULT 自愈 | `_healFaultLocked()` 在 `_withLock()` **内**（`_targetInit` 只走私有 `_ctrl/_transfer`，不重入） | `_healIfFaulted()` 在锁**外** | **保留 toolbox** | merge-map §4.8 明确：两套各自自洽，混用会自锁或漏清 `_faulted`；2026-10-16 页面审查也把「heal 绕锁」列为必修 |
| AP 发现 + CPUID 核验 | 3 个 CSW 候选 × APSEL `[0,2,1,3]`，严格 ARM CPUID 掩码，**初始化后再读 CPUID 复核** | 宽松 `v && v!==0xffffffff`，无复核 | **保留 toolbox** | 更严；假 AP 不会被当成命中 |
| 块读"无进展" | 抛 `内存块读取无进展：…` | 静默 `break` | **保留 toolbox** | 静默截短会导致自校验"通过" |
| 响应回显错位自愈 | `_detectFraming` 里 `/响应回显/` → 复位 + `_claim()` 清队列 + 按短包重试；认领失败 ≤3 次退避 | 逐字节相同 | **保留（两边共有）** | 不是某一侧独有；守卫里钉住 |
| `cswUsed` 重连时清空 | 有（重连必须重新发现目标） | 无 | **保留 toolbox** | |
| `usb-device.js` 的 `USB_RESOURCE_BUSY` 语义 | **有**（`usbResourceConflict()` / `isUsbResourceConflict()`，6 行） | 直接用 `new Error` | **保留 toolbox** | 类型化错误，页面才能区分"USB 被占"与其它失败 |

> 合并方式：以 toolbox 版为基线（它包含最近 43 次提交的修复），把 ETM 独有的 3 项（`resetDevice` 永不抛、
> 复位不可用提示、禁 drain 注释）移植进来；**不**移植 ETM 的 `abort()` 4 字节写法、不**移植**其锁外 heal、
> 不移植其宽松 CPUID 判定。合并后 `dap-webusb.js` 1593 行 / 92,208 B。

### 2.2 为什么是 8 个文件（merge-map 写的是 7 个）

toolbox 的 `dap-webusb.js` 新增依赖 `web/app/core/operation-budget.js`（`OperationBudget`，20 行）。
第一次只拷 7 个文件时，用 import 解析检查立刻抓到
`DANGLING etm/web/app/shared/rtt/dap-webusb.js -> ../core/operation-budget.js`——
那会让 ETM 页一加载就 `ERR_MODULE_NOT_FOUND`。所以副本集 = **7 驱动 + 1 依赖 = 8 个文件**。

---

## 3. 生成与守护（以后只改这一份）

```
唯一真源：web/app/{rtt/dap-webusb, core/{usb-device,probe-bus,pace,bin,operation-budget}, dbg/{bp,source}}.js
        ↓  node tools/dev/sync-shared-drivers.mjs      （make gen-shared）
生成副本：etm/web/app/shared/**（逐字节，含 README.md 说明"别手改"）
```

**改驱动的正确顺序**：

1. 只改 `web/app/**` 这 8 个文件；
2. `node tools/dev/sync-shared-drivers.mjs`（生成副本）；
3. `node tools/selftest/shared-drivers.test.mjs`（或 `make check`：里面还会跑 `--check`）；
4. 改的是 `web/app/**` 里的任意模块 → 还要 `npm run build:web` 重建 `app/site/main.js`（否则页面加载旧 bundle）。

**守卫咬 4 件事**（`web/tools/selftest/shared-drivers.test.mjs`，24 项）：

1. 8 对文件**逐字节相同**（改完真源忘了重生成 = 红）；
2. 副本目录里**没有多余的手写 `.js`**，且有说明 README；
3. **合并成果都在、缺陷没回来**：`OperationBudget`、`DAP_SESSION_LOST`、`_targetNeedsReconnect`、
   5 字节 `WriteABORT`、`resetDevice` 永不抛、禁 drain 注释、复位不可用提示、`/响应回显/` 自愈、
   CPUID 复核；且不得出现 4 字节 `abort()`；
4. **不做跨子树 import**：ETM 页只许 `./shared/**`（实测 3 个模块 6 条 import），副本内部的相对 import 也不许爬出 `shared/`。

---

## 4. 目录口径：ETM 页面源码放在哪（与 t6 任务书的一处差异，已按 merge-map 执行）

t6 任务书写「ETM 页面源码并入 `web/app/etm/`」。实测与 `docs/merge-map.md` §4.1.3 的明确结论冲突，本次**按 merge-map 执行**：

- `web/app/etm/`（web 侧入口）是 ETM 标签页的**宿主**：探测 `/etm/integration.json` + 嵌 iframe（t3 已迁入，本次未动）。
- ETM 页面源码仍在 **`etm/web/**`**，URL 仍是 `/etm/`，由 `web/tools/dev/serve-etm.py` 把 `etm/` 挂到 `/etm/`、接口走 `/etm/api/`。
- 物理搬进 `web/app/etm/` 会同时打断三件已成立的事：① `etm/start-web.ps1` 独立启动（只服务 `etm/` 树）；
  ② `web/bridge/etm/backend/**` 便携包快照（独立、不引用跨子树路径）；③ `serve-etm.py` **只改写 HTML 的
  href/src、不改 JS import** —— 三处都要改，且正是 §4.1.3 判定「不采用」的方案。
- 所以「ETM 并入 web」落在：**web 首页的 ETM 标签入口 + 宿主视图 + 驱动真源在 web**；
  ETM 页面/后端保持自包含子树（可独立启动、可进便携包）。

---

## 5. 本次同时修的路径引用（merge-map §2.4 + t5 交接项）

| 文件 | 改动 |
| --- | --- |
| `web/tools/dev/serve-etm.py` | docstring 示例 `--etm-root ../web-etm-trace` → `../etm`；默认值 `ROOT.parent/'web-etm-trace'` → `ROOT.parent/'etm'`；两句英文说明改为「本仓 etm/ 子树」 |
| `web/tools/dev/start-etm.ps1` | 默认 `EtmRoot` `..\..\..\web-etm-trace` → `..\..\..\etm` |
| `web/tools/selftest/etm-integration-hw.mjs` | `resolve(root,'../web-etm-trace')` → `resolve(root,'../etm')` |
| `web/start-etm.bat` | 5 处路径 + 4 处提示文案（兄弟仓库 → 本仓 etm/ 子树）；CRLF 与 ASCII 引导段保持不变 |
| `web/bridge/etm/README.md` | `--etm-root E:/web-etm-trace` → `--etm-root ../etm` |
| `web/tools/dev/sync-etm-runtime.py` | provenance 的 `sourceRepository='web-etm-trace'` → `'etm'` |
| `web/docs/etm-integration.md` | 开发源码运行说明改为 `../etm`，并注明 ETM 原始工程已并入本仓 etm/（来源 master@12746fc） |
| `etm/tools/tests/test_server.py` | 默认上游路径 `ROOT.parent/'web-serial-rtt-tools'` → `ROOT.parent/'web'`（t5 交接第 2 条；该测试正是"副本 == 上游"的守卫） |

---

## 6. 本次验证记录（全部离线，不接硬件、不烧录）

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 副本一致 | `node tools/dev/sync-shared-drivers.mjs --check` | 8/8 `ok` |
| 统一守卫 | `node tools/selftest/shared-drivers.test.mjs` | **24 通过 / 0 失败** |
| ESM 解析 | 真源 + 8 个副本（临时 `.mjs` 包内 `node --check`）+ 2 个新脚本 + `etm-integration-hw.mjs` | 全部通过 |
| import 解析 | 自写检查：`web/app` + `etm/web/app` 全部相对 import 解析到真实文件 | 497 条 / 207 文件，**dangling 0** |
| 驱动回归（纯 Node） | `dap-abort`、`dap-ap-discovery`、`dap-disconnect`、`dap-register-ready`、`dap-resume`、`usb-transports`、`diagnostics`、`riscv-sba-async`(21)、`flash-reset-flow`、`web-review-fixes`、`web-review-maintenance`、`rtt`(55)、`swo-capture`、`swo-ports` | 全 PASS（`flash-reset-flow`/`rtt` 等需 `linkedom`，临时挂 `node_modules` 跑；devDependency 未随仓迁移） |
| ETM 侧守卫 | `etm/` 下 `python -m unittest tools.tests.test_server.ServerTests.test_etm_serves_its_committed_source_store_copy` | **OK**（把 `web/app/dbg/source.js` 与 ETM 副本逐字节比对，正好验证本方案） |
| 仓库守卫 | `node tools/dev/build-web.mjs --check` / `check-syntax.mjs` / `check-liquid.mjs` | 170 modules 一致 / 411 模块语法通过 / 158 篇 Markdown 无 Liquid 地雷 |
| 打包入口 | `node tools/dev/build-web.mjs`（改了真源必须重建） | 1,328,739 B；`index.html` `?v=592bd608c91e9a5e` |

---

## 7. 已知遗留（刻意留给后续，不是漏）

1. **`web/bridge/etm/backend/**` 运行快照未重生成**：`sync-etm-runtime.py` 需要
   `etm/build-native/vendor/OpenCSD/*.a`（gitignored、未迁）、`etm/records/`（未迁，Demo 来源）与 msys64 g++。
   条令：改了 `etm/**` 之后跑
   `python tools/dev/sync-etm-runtime.py --etm-root ../etm` + `python tools/dev/build-etm-package.py`，
   并确认 `provenance.json` 里 `sourceRepository='etm'`。快照里的 8 个 shared 副本因此仍是 a2922f8 那版
   （旧 ETM 驱动）——**它是自洽的旧包**；页面运行走 `etm/web`（`make serve-etm`）就是统一后的驱动。
2. `web/kill-web.bat` 的进程匹配串仍写死 `*web-serial-rtt-tools*`（按命令行匹配 node 进程），合并后开发
   服务器路径是 `superlinkPRO\web` → 它杀不到；`make serve-stop` 按端口杀不受影响。改它要小心该文件的
   GBK 编码（不在 §2.4 清单，未动）。
3. `etm/firmware/**` 里仍有 `E:\web-etm-trace\...` 绝对路径（固件构建脚本 / H743 工具），属 ETM 子树历史
   路径，不在 §2.4 的 5 文件内；重建固件前需按新路径调整。
4. `web/docs/etm-integration.md` 里保留「来源 `web-etm-trace` master@12746fc」的出处说明 —— 那是
   provenance（最后凭据），不是运行依赖。
