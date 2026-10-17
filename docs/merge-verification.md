# superlinkPRO 合并验收（t7 · 静态 / 离线）

> 范围：**不接硬件、不烧录**。只做完整性对账、构建、单测、页面冒烟。
> 被验对象：`E:\superlinkPRO`，分支 `main`，HEAD **`a76ee5b`**（工作树除 Captain 未提交的 `docs/decisions.md` +39 行外干净）。
> 依据：`docs/merge-map.md`（t1）§7 的精确期望 + Captain 2026-10-17 的逐子树期望。
> 验收机工具链：Node **24.13.1** / Python **3.13** / cmake **4.0.3** / ninja **1.13.0** / msys2 g++ **16.1.0** / Chrome **155.0.8059.84**（CDP 9334，独立 profile `%TEMP%\superlinkpro-verify`）。

## 0. 结论（先看这段）

**GO —— 可以关闭/删除三个原仓**（`E:\Share\github\akaLinkPro`、`E:\web-serial-rtt-tools`、`E:\web-etm-trace`），
但删除前建议先做 §7 的 3 件事（归档被排除项 / 给三仓打 tag 或留 bare 镜像 / 便携包若要重出需先补料）。
合入库本身没有阻塞项：

| 项 | 结果 |
| --- | --- |
| 完整性 | 3188 条基线 **3171 逐字节相同 + 16 处已记录编辑 + 1 处已记录缺失**；全仓 3254 文件 / **175,039,703 B**，账目**逐字节闭合、0 未解释**（§1） |
| 驱动统一 | `dap-webusb.js` 全仓 **4 处**角色明确；8 个生成副本与真源 **sha256 全等**；`make gen-shared` 幂等、`--check` 全绿（§2） |
| 构建 | OpenCSD 原生解码器 **configure + 54/54 步构建成功**（exe 4,167,203 B，可执行）；静态/ETM 服务 **HTTP 200**（§3） |
| 单测 | web `make test` **exit 0**（95 套件，21 个计数套件合计 **1651 断言 0 失败**）、`make check` exit 0；ETM unittest **119 项 / 114 过 / 5 未过（全部因未迁移产物，已逐项归类）**（§4） |
| 页面冒烟 | 首页 **15 个标签齐全**；烧录页、调试器页、ETM 页均可加载；**控制台报错 0 / 异常 0 / 网络失败 0**（§5） |

---

## 1. 完整性对账（merge-map §7 的精确期望）

### 1.1 先修正 merge-map §7 自身的三处重复计数

§7 声明的期望是 **3188 文件 / 160,855,216 B**。逐项重算后发现该字节数是三处"口径不一致"叠加的结果，
**文件数 3188 是对的**，字节数需修正：

| 项 | §7 的算法 | 与 §2.2 的迁移规则冲突 | 修正 |
| --- | --- | --- | --- |
| akaLinkPro | 610 文件 / 19,710,551 B（含根 `.gitignore` 2,283 B） | §2.2A 又明确"`.gitignore` 不单独搬" | **609 / 19,708,268** |
| web | 2036 文件，但字节按 2035 算（112,425,879） | 漏了 §2.2B 要求额外带走的未跟踪文档 `docs/h743-web-review-2026-10-17.md` 11,967 B | **2036 / 112,437,846** |
| etm | 543 文件 / 28,718,786 B（含 `docs/逻辑分析仪攻略.md` 69,516 B） | §2.2D 要求删掉这份重复文档 | **543 / 28,649,270** |
| 合计 | 3188 / 160,855,216 | — | **3188 / 160,795,384** |

> 三处修正的实测字节：`akaLinkPro/.gitignore` **2,283**、`docs/h743-web-review-2026-10-17.md` **11,967**、
> `docs/逻辑分析仪攻略.md` **69,516**（`Get-Item … .Length`）。修正后的 160,795,384 B 才是 3188 文件的真实基线。

### 1.2 逐文件 sha256（3188 条基线逐条比对源仓工作区）

方法：按 merge-map §2.2 的映射把 3188 条基线路径映射到目标路径，逐个对**源仓工作区字节**算 SHA-256 与目标比对
（脚本见 §附录 A；用工作区而非 git blob —— 三仓 `core.autocrlf=true`，blob 比对会把行尾差异误判成不同）。

| 结果 | 条数 | 说明 |
| --- | ---: | --- |
| ok（逐字节相同） | **3171** | — |
| mismatch（已记录的故意修改） | **16** | 明细见 §1.3 |
| missing（已记录的缺失） | **1** | `etm/.gitignore`，见 §1.4 |

### 1.3 16 处不一致：**全部**是已记录的故意修改（无一处意外）

| 目标文件 | 源→目标大小 | 原因 / 出处 |
| --- | --- | --- |
| `web/.gitignore` | 648 → 1,353 | t3（`6f60665`）补 2 行否定规则，救回 121 个被深层 .gitignore 重新忽略的已跟踪证据 |
| `web/Makefile` | 52,214 → 52,675 | t6（`a76ee5b`）加 `gen-shared` / `check` 目标 |
| `web/app/rtt/dap-webusb.js` | 90,022 → 92,208 | t6 驱动合并（toolbox 为底 + 移植 ETM 3 项修复） |
| `web/app/site/main.js` | 1,327,153 → 1,328,739 | t6 改真源后重建 bundle |
| `web/app/site/manifest.json` | 16,319 → 16,319 | 同上（大小同、哈希变） |
| `web/index.html` | 205,812 → 205,812 | 同上（`?v=` cache-buster 变化） |
| `web/bridge/etm/README.md` | 2,063 → 2,053 | t6 §2.4 路径改写 |
| `web/docs/etm-integration.md` | 9,306 → 9,271 | t6 改为"本仓 `etm/` 子树" |
| `web/start-etm.bat` | 5,908 → 5,881 | t6 `..\web-etm-trace` → `..\etm`（5 处路径 + 4 处文案） |
| `web/tools/dev/serve-etm.py` | 7,227 → 7,224 | 同上（示例 + 默认值） |
| `web/tools/dev/start-etm.ps1` | 592 → 582 | 同上 |
| `web/tools/dev/sync-etm-runtime.py` | 6,873 → 6,863 | 同上（provenance 来源名） |
| `web/tools/selftest/etm-integration-hw.mjs` | 5,084 → 5,074 | 同上 |
| `etm/tools/tests/test_server.py` | 13,106 → 13,089 | t6：兄弟目录 `web-serial-rtt-tools` → `ROOT.parent/'web'` |
| `etm/web/app/shared/core/usb-device.js` | 7,116 → 7,532 | t6 生成副本（现与真源 `web/app/core/usb-device.js` 逐字节相同） |
| `etm/web/app/shared/rtt/dap-webusb.js` | 88,174 → 92,208 | 同上（与真源逐字节相同） |

净值 **+9,266 B**。每一处都能在 `git log -1 --format=%h -- <path>` 找到对应提交。

### 1.4 唯一缺失：`etm/.gitignore`（689 B）

ETM 的忽略规则在 t2 已并入**根** `.gitignore`，按 merge-map §2.6 不单独搬运。这不是漏搬：
源仓该文件仍在 `E:\web-etm-trace\.gitignore`，且根 `.gitignore` 覆盖了其中全部规则（`captures/ records/ tmp/
decoded/ build-native/ vendor/OpenCSD/ baselines/*/execution*.c` 等）。属 **D-2/§2.6 已记录的口径**。

### 1.5 目标多出的 57 个文件（全部有源对应，54 个逐字节相同）

| 组 | 数量 | 大小 | 结论 |
| --- | ---: | ---: | --- |
| `etm/firmware/stm32h743_cdc_trace/**` | 52 | 11,986,114 B | t5 的 D-2 **扩展**（任务要求"必须含 H743"）。**52/52 与源工作区逐字节相同**，含 `fw.elf/fw.hex/fw.bin` 与 5 个 CPU/trace 变体、cherryusb、src、ld；已剔除 `sdk/`（1,457 文件 / 253.5 MB） |
| `etm/firmware/stm32h7b0_spi_trace/stable-70-35-v1/fw.elf` | 1 | 2,068,492 B | `82a285a` 用 1 个文件换回 7 个 `spi-*` 固件档位；与源逐字节相同 |
| `etm/web/app/shared/core/operation-budget.js` | 1 | 782 B | t6 第 8 个共享副本（toolbox 版的依赖）；与 `web/app/core/operation-budget.js` 相同 |
| 根 `.gitignore` / `.gitattributes` / `README.md` | 3 | 14,404 / 3,565 / 12,534 B | t2 骨架；与源同名文件内容不同（三仓规则并集 / 合并说明），全新内容 |

### 1.6 目标新增的 10 个文件（合并过程产生，无源对应）

`docs/merge-map.md`(61,880) · `docs/decisions.md`(11,258) · `docs/driver-unification.md`(1,007) ·
`firmware/README-migrated.md`(20,333) · `web/README-migrated.md`(9,087) · `etm/README-migrated.md`(19,716) ·
`etm/web/app/shared/README.md`(1,826) · `web/docs/driver-unification.md`(12,774) ·
`web/tools/dev/sync-shared-drivers.mjs`(5,680) · `web/tools/selftest/shared-drivers.test.mjs`(6,290)。
合计 149,851 B。

### 1.7 账目闭合（逐字节相等，0 未解释）

```
修正基线（3188 文件）                 160,795,384
− 缺失 etm/.gitignore（§1.4）               −689
+ 16 处编辑净值（§1.3）                   +9,266
= 基线实际落地（3187 文件）             160,803,961   ← 与目标侧实测逐字节相等
+ 57 个多出（54 同源 + 3 根级新内容）  +14,085,891
+ 10 个新增                              +149,851
= 目标全仓（不含本报告）                175,039,703   ← 与 `git ls-files` 3254 文件实测总和一致
```

### 1.8 逐子树计数：与 Captain 期望逐项对齐（并解释那 1 个差额）

| 子树 | Captain 期望 | 实测 | 差 | 说明 |
| --- | ---: | ---: | ---: | --- |
| `web/` | 2040 | **2040** | 0 | 2035 跟踪 + 1 未跟踪文档 + 1 README-migrated + t6 新增 3 |
| `etm/` | 598 | **598** | 0 | 594 迁 + README-migrated + `82a285a` 的 spi fw.elf + t6 新增 2 |
| `firmware/` | 130 | **130** | 0 | 129 源 + README-migrated |
| `script_test/` | 289 | **289** | 0 | — |
| `tools/` | 19 | **19** | 0 | — |
| `docs/` | 172 | **173** | **+1** | **Captain 待解的那 1 个差额 = `docs/driver-unification.md`（1,007 B / 13 行）**：t6 在 `a76ee5b` 里同时提交了 `web/docs/driver-unification.md`（正文 12,774 B）与根 `docs/` 的 **13 行索引**（D-4 口径：根 `docs/` 只留跨仓索引，正文跟驱动走）。属有意为之，非漏记 |
| 根文件 | 5 | **5** | 0 | `.gitignore` `.gitattributes` `LICENSE` `Makefile` `README.md` |
| **合计** | **3253** | **3254** | **+1** | 即上面这一个索引文件 |

逐子树字节（`git ls-files` 实测）：root 55,184 · docs 6,237,209 · etm 42,729,944 · firmware 3,447,266 ·
script_test 10,011,780 · tools 81,810 · web 112,476,510 = **175,039,703 B**。
（本报告写入 `docs/merge-verification.md`（25,410 B）并提交后 → docs 174 / 全仓 **3255 文件 / 175,065,113 B**；
若把本报告计入上表，+1 文件 / +25,410 B，账目仍闭合。）

### 1.9 三个原仓的跟踪内容去向总表

| 源仓（分支/HEAD） | 跟踪 | 迁入 | 不迁 | 不迁原因 |
| --- | ---: | ---: | ---: | --- |
| akaLinkPro（`main` `3214872`） | 680 | **609**（19,708,268 B） | 71 | D-1 原板板级 14 + 原板 3 个 .bat + `hardware/` 11 + `hardware_pro/` 28 + `.opencode/` 14 + 根 `.gitignore` 1 |
| web-serial-rtt-tools（`main` `089e383`） | 2035(+1 未跟踪) | **2036**（112,437,846 B） | 0 | 全部迁移（含 8 个未提交改动，按工作区状态） |
| web-etm-trace（**`master`** `12746fc`） | 5472 | **594**（40,634,695 B） | 4878 | D-2：ETM 的 `stm32f103/h7b0_cdc_trace`、`stm32h7b0_spi_trace`、`_archive`、`_compare` 与 H743 的 `sdk/`（686,472,680 B）；`docs/逻辑分析仪攻略.md` 去重 |

---

## 2. 驱动统一（独立复核 t6 的核心承诺）

### 2.1 `dap-webusb.js` 全仓 4 处，角色明确

| 路径 | 大小 | sha256(12) | 角色 | 跟踪 |
| --- | ---: | --- | --- | --- |
| `web/app/rtt/dap-webusb.js` | 92,208 | `1AD30E4735AB` | **唯一真源（手写）** | ✓ |
| `etm/web/app/shared/rtt/dap-webusb.js` | 92,208 | `1AD30E4735AB` | 生成副本（与真源逐字节相同） | ✓ |
| `web/bridge/etm/backend/web/app/shared/rtt/dap-webusb.js` | 88,174 | `F78ED3D47282` | 便携包旧快照（`a2922f8` 期，包内自洽） | ✓ |
| `web/docs/validation/20261016-sram-root-cause-134100/baseline-app_rtt_dap-webusb.js` | 89,814 | `ACD5E1E4E7B8` | 历史证据（当时基线） | ✓ |

**不存在第二份手写驱动**：除真源外三处分别是生成物、快照、证据，且没有任何模块 import 它们。

### 2.2 8 个共享文件真源 vs 副本（不是只测 1 个）

| 文件 | 真源 sha256(12) | 副本 sha256(12) | 结果 |
| --- | --- | --- | --- |
| `rtt/dap-webusb.js` | `1AD30E4735AB` | `1AD30E4735AB` | ✅ |
| `core/usb-device.js` | `D4FEC5A8FA11` | `D4FEC5A8FA11` | ✅ |
| `core/probe-bus.js` | `E40BE1AC37B1` | `E40BE1AC37B1` | ✅ |
| `core/pace.js` | `AC27D5DB130C` | `AC27D5DB130C` | ✅ |
| `core/bin.js` | `0A236F6FB516` | `0A236F6FB516` | ✅ |
| `core/operation-budget.js` | `92C3C1A9C97F` | `92C3C1A9C97F` | ✅ |
| `dbg/bp.js` | `FAA19159365B` | `FAA19159365B` | ✅ |
| `dbg/source.js` | `E95670C37146` | `E95670C37146` | ✅ |

**8/8 相同**（注意：是 8 个，不是 merge-map §4.1 写的 7 个 —— 真源 `dap-webusb.js` 依赖新增的
`core/operation-budget.js`，只拷 7 个会 `ERR_MODULE_NOT_FOUND`；t6 已把此口径修正写进 `docs/decisions.md` D-7）。

### 2.3 生成器与守卫必须能跑且一致

| 命令 | 结果 |
| --- | --- |
| `make gen-shared`（`node tools/dev/sync-shared-drivers.mjs`） | **写入 0 个 / 内容已相同 8 个**（幂等，跑完 `git status` 无变化） |
| `node tools/dev/sync-shared-drivers.mjs --check` | 8/8 `ok`（`共享驱动副本一致（8 个文件）`） |
| `node tools/selftest/shared-drivers.test.mjs` | **24 通过 / 0 失败**（含"没有 import 指回 web/app"、"ETM 页确实在消费副本：3 模块 / 6 条 import"） |
| `make check` 内的 `sync --check` | 通过（见 §4.1） |

### 2.4 驱动回归套件（Captain 要求独立重跑；实际为 **15** 个，全部 exit 0）

`dap-abort` · `dap-ap-discovery` · `dap-disconnect` · `dap-register-ready` · `dap-resume` ·
`usb-device` · `usb-transports` · `diagnostics` · `riscv-sba-async`（21 passed/0 failed）·
`flash-reset-flow` · `web-review-fixes` · `web-review-maintenance` · `rtt`（55 通过/0 失败）·
`swo-capture` · `swo-ports` → **15 suites / failed=0**。

---

## 3. 构建

### 3.1 ETM 的 OpenCSD 原生解码器（vendor 齐全，按构建脚本尝试）

用的是 `etm/build-decoder.ps1` 的同一条 cmake 命令，但把 `-B` 指到 `%TEMP%\etm-decoder-build`（**不污染仓库**；
仓库内确认没有 `build-native/` 目录）：

| 步骤 | 结果 |
| --- | --- |
| `cmake -S etm -B %TEMP%\etm-decoder-build -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_C_{,XX}_COMPILER=msys64` | **exit 0** |
| `cmake --build … --parallel 4` | **exit 0，54/54 步**（OpenCSD 静态库 + `opencsd_c_api` + `etm_decode.exe` 链接） |
| 产物 | `etm_decode.exe` **4,167,203 B**（与 t5 报的大小一致） |
| 可执行性 | 无参运行 → `Usage: etm_decode trace.bin config-and-memory.txt`，退出码 **2**（参数缺失的预期行为）；`--help` 同样走 usage |
| 说明 | vendor（`etm/vendor/OpenCSD-1.8.3`，195 文件）齐全；构建需要 msys64 g++ 16.1.0，本机具备 |

### 3.2 静态服务与 ETM 服务（HTTP 200）

**统一工作台** `python web/tools/dev/serve-etm.py --etm-root ../etm --port 8890`（即 `make serve-etm` 的路径，
t6 已把默认值改成 `web/` 的同级 `etm/`）：

| URL | 状态 | 大小 |
| --- | --- | --- |
| `http://127.0.0.1:8890/index.html` | **200** | 205,812 B |
| `http://127.0.0.1:8890/app/main.js` | **200** | 9,984 B |
| `http://127.0.0.1:8890/etm/integration.json` | **200** | `{"backend":"kingst","embedded":true,"protocol":1}` |
| `http://127.0.0.1:8890/etm/?embedded=1` | **200** | 20,026 B |
| `http://127.0.0.1:8890/etm/api/status` | **200** | 220 B |
| `http://127.0.0.1:8890/etm-launcher.json` | **200** | `{"etmRoot":"E:\\superlinkPRO\\etm","serialRoot":"E:\\superlinkPRO\\web"}` ← 确认服务的是新仓，不是源仓 |

**纯静态服务** `node web/tools/dev/serve-nocache.mjs 8911`：`/index.html` **200**（205,812 B）、`/app/main.js` **200**。

> ⚠️ 另注：本机 **8878** 端口上有一个**旧实例**（`python`，`etmRoot=E:\web-etm-trace`、`serialRoot=E:\web-serial-rtt-tools`），
> 是之前会话留下的，**本次未使用**；验收一律打 8890。若后续有人按 8878 打开页面，会验到源仓而不是 superlinkPRO。

---

## 4. 单测

### 4.1 web 侧：`make test` / `make check`

前置：`npm ci`（17 包，esbuild 0.25.12 + linkedom 0.18.12）——`node_modules` 按 merge-map 不迁移，所以新克隆必须先装。

| 命令 | 结果 |
| --- | --- |
| `cd web && make test` | **exit 0**；共调用 **95 个套件**（`make test-stability/dbg-features/board-matrix/random-flow/etm` 展开）；其中 **21 个带计数的套件合计 1,651 断言 / 0 失败**，其余 PASS 风格套件全过 |
| `cd web && make check` | **exit 0**：`check-syntax`（411 模块）+ `check-liquid`（**159 篇 Markdown 无 Liquid 地雷**）+ `build-web --check`（**bundle current: 170 source modules**）+ `sync-shared-drivers --check`（8/8） |
| `make gen-shared` | 写入 0 / 相同 8（幂等） |

`make test` 里的 ETM 相关项也跑了：`python tools/dev/test-etm.py` → **4/4 PASS**
（产出目录 `web/validation/etm/run-20261017-222912-2bd1a1`，已被 .gitignore 覆盖）。

### 4.2 点名的套件（单独跑，数字与套件自报一致）

| 套件 | 结果 |
| --- | --- |
| `node tools/selftest/flash-image.test.mjs` | **28 通过 / 0 失败** |
| `node tools/selftest/shared-drivers.test.mjs` | **24 通过 / 0 失败** |
| 驱动回归 15 套件（§2.4） | 15/15 exit 0 |

### 4.3 ETM 侧：`python -m unittest discover -s etm/tools/tests`（重跑，非引用 t5 旧数字）

**Ran 119 tests — 114 passed / 1 failure + 4 errors**（20.2 s）。

| 未过项 | 直接原因（实测） | 归类 |
| --- | --- | --- |
| `test_execution.test_real_samples_branch_targets_workload_calls_and_exceptions` | `FileNotFoundError: etm/decoded/500m/decoded.json` | `decoded/` 是 gitignored 产物（merge-map §1.3），按规则未迁移 |
| `test_kingst_filter.test_real_snapshot_and_rate_mismatch` | `FileNotFoundError`（读 `records/` 下的抓包快照） | `records/` 未迁移（同上） |
| `test_server.test_record_package_reload_decode_and_export` | `FileNotFoundError: etm/captures/trace-20261011-000319.csv` | `captures/` 未迁移（同上） |
| `test_server.test_spi_profile_uses_matching_elf_and_known_capture_entry` | `FileNotFoundError: etm/firmware/stm32h7b0_spi_trace/fw-70.bin` | D-2 裁剪（该固件工程被裁，只补回了 `stable-70-35-v1/fw.elf`） |
| `test_browse_baseline.test_real_long_record_summary_queries_and_symbol_table` | `AssertionError: 400 != 200`（`/api/browse/<sid>/overview`） | 该用例浏览真实长记录，依赖 `records/`（未迁移） |

**结论**：5 项未过**全部**由"按决策不迁移的产物/被裁固件"引起，**没有一项指向合并错误**。
恢复方式（任选）：把源仓的 `captures/`+`records/`+`decoded/` 放回 `etm/`（仅本地，仍被 .gitignore），
或补回 `etm/firmware/stm32h7b0_spi_trace/`（至少 `fw-70.bin`）—— 后者 t5 已给过"1 文件换 7 档"的做法。

> 与 t5 报的 114/104 不可直接比较：本轮 discover 到 119 项，t6 修好 `test_server.py` 的兄弟目录路径后
> 少了若干"整文件报错"，剩余的 5 项就是上表这一族（同属未迁移产物），数量虽变，**性质未变**。

---

## 5. 页面冒烟（CDP，独立 profile，**不连探针、不烧录**）

环境：`serve-etm.py` @ 8890（superlinkPRO）→ Chrome 155 @ CDP 9334 → `Page.navigate` 到 `http://127.0.0.1:8890/`。

| 检查项 | 结果 |
| --- | --- |
| 应用启动 | `window.__tools` 就绪；`#build-stamp` = **"2026-10-09-r9 版"**；页面标题正常 |
| 首页标签 | **15 个，齐全无多余**：`serial terminal rtt rttcdc scope flash dbg spi panel spicdc swo etm i2c analog gen`（缺失 0 / 多余 0） |
| 烧录页 `#tab-flash` | 点击后 `active=true`；`f-backend=webusb`、`f-chip=stm32f103`、`f-idcode`/`f-path`/`f-flash`/`f-verify` 控件在位；状态栏 `空闲`（未连接，符合预期） |
| 调试器页 `#tab-dbg` | `active=true`；`d-backend=webusb`、`d-elf-file` 在；命令框 placeholder 正常（`h / r / md 0x20000000 / b main.c:192 / …`） |
| ETM 页 `#tab-etm` | `active=true`；iframe `src=/etm/?embedded=1`、`hidden=false`；**`contentWindow.__etmBridge` 已挂上**（父子握手成功）；子页标题 "ETM 执行轨迹 · 采集与历史分析"；子页 `body[data-api-base]="/etm/api/"`、`data-embedded="1"` |
| 控制台 | **console error 0 / 未捕获异常 0 / Log error 0 / Network.loadingFailed 0**（两次独立运行均如此） |
| 说明 | ETM 页的状态文案在 attach 成功后仍是"正在载入 ETM 执行轨迹…"——这是宿主页的既有设计（该文案只在加载中/出错时更新，成功不改写）；判定"已拉起"用 `__etmBridge` + 子页标题 + `/etm/api/` 三个证据 |
| 明确未做 | 未点击「连接探针」、未选设备、未执行任何烧录/擦除/复位动作（全程无 USB 设备交互） |

---

## 6. 已知限制（不是缺陷，但不能瞒）

1. **没有逐文件 git blame**：合并采用逐文件复制（D-6），不是 subtree。**考古要回原仓**，凭据是：
   - `firmware/README-migrated.md` → akaLinkPro `main` **`321487255d2ececea8d56282b07f3c7254e9862b`**
   - `web/README-migrated.md` → web-serial-rtt-tools `main` **`089e383…`**
   - `etm/README-migrated.md` → web-etm-trace **`master`** **`12746fc035833e90f695d45e15a1f7ffa1e132ad`**
   - 根 `README.md` 另有三仓绝对路径/分支/完整 commit 的对应表。
2. **便携包快照未重生**（`web/bridge/etm/backend/**`）：其中 7 个 shared 副本里 **5 个与新版相同、2 个仍是 `a2922f8` 旧版**
   （`usb-device.js` `0011FE57D1E3`、`dap-webusb.js` `F78ED3D47282`）。**旧包自洽**（包内驱动版本一致），
   页面走 `make serve-etm`（`etm/web`）用的才是统一后的驱动。重生成需要 OpenCSD 静态库 + `records/` + msys64 g++，
   命令：`python tools/dev/sync-etm-runtime.py --etm-root ../etm` → `npm run build:web` → `python tools/dev/build-etm-package.py`。
3. **根 `docs/` 是混合目录**：跨仓文档（`merge-map.md`、`decisions.md`、`driver-unification.md` 索引）+ akaLinkPro 的 170 个根级文档。
   见 `decisions.md` D-4，**有意为之**（收敛要改 33 处 Makefile/CMake 引用，纯负收益）。
4. **`web/.gitignore` 比源仓多 2 行否定规则**（`6f60665`，救回 121 个被深层 .gitignore 重新忽略的已跟踪证据），
   所以 `web/` **不是逐字节等同源仓**（除这 16 处编辑外其余全同，见 §1.3）。
5. **`node_modules` 未入库**：`make test` / `make check` 前必须 `cd web && npm ci`（本轮已装 17 包，esbuild + linkedom）。
6. **5 个 ETM 测试依赖未迁移产物**（§4.3）：`captures/`、`records/`、`decoded/`、`stm32h7b0_spi_trace` 固件工程。
   不补料时这 5 项会长期红；补料后即可全绿（补料仍被 .gitignore 覆盖，不会进版本库）。
7. **8878 端口有旧实例**指向源仓（§3.2 注），容易误验；新仓一律用 8890（或 `make serve-etm` 的默认端口）。

---

## 7. 结论：go / no-go

**GO —— 可以关闭/删除三个原仓。** 依据：§1 的账目逐字节闭合（0 未解释），§2 的驱动统一可机器复核（8/8 sha256 + 24 项守卫 + 15 套回归），
§3 的可构建可服务，§4 的测试面（web 全绿；ETM 5 红全部归因于已决策的裁剪），§5 的页面冒烟零报错。

**删除前建议做的 3 件事（不阻塞合入库本身）**：

1. **归档"按决策没搬"的东西**（删仓即丢）：`hardware_pro/`（KiCad 生产文件，4.5 MB）、ETM 的 4,878 个固件文件（686 MB，含 5 套 SDK 工程）、
   `captures/`（11.2 GB）+ `records/`（4.4 GB）+ `decoded/`（LA 抓包与解码记录，也是 §4.3 那 5 个测试的料）、
   akaLinkPro 的 `hardware/`、`.opencode/skills`。建议打到外部归档盘或云存储。
2. **保留可考古的历史**：给三仓各打 tag（或 `git clone --mirror` 一个 bare 镜像），因为新仓没有逐文件 blame，
   只能凭 §6.1 的 commit SHA 回原仓比对。
3. **便携包要重出的话**先补料：OpenCSD 静态库（`build-native`）+ `records/` 示例 + msys64 g++，再跑 §6.2 的三条命令；
   否则下载包保持旧驱动（自洽但落后于 `web/app`）。

若上述三项都不需要，**可以直接删除三个原仓**；合入库的完整性、构建、测试、页面验收均已通过。

---

## 附录 A · 复现命令（本报告全部数字的来源）

```powershell
# 0) 仓库现状
git -C E:\superlinkPRO rev-parse --short HEAD; git -C E:\superlinkPRO status --porcelain
git -C E:\superlinkPRO -c core.quotepath=false ls-files | Measure-Object -Line   # 3254
# 逐子树计数/字节：按 ls-files 前缀分组 + Get-Item Length

# 1) 完整性：把 3188 条基线映射后逐文件比 SHA-256（Node 脚本，见本次 tmp 目录 t7-verify2.mjs）
#    基线 = ak 609 + web 2036 + etm 543；输出 ok/missing/mismatch 与 extras/news 及字节账目

# 2) 驱动统一
cd E:\superlinkPRO\web
node tools/dev/sync-shared-drivers.mjs --check
node tools/selftest/shared-drivers.test.mjs
make gen-shared

# 3) 构建
cmake -S E:\superlinkPRO\etm -B "$env:TEMP\etm-decoder-build" -G Ninja -DCMAKE_BUILD_TYPE=Release `
  -DCMAKE_C_COMPILER=C:/msys64/mingw64/bin/gcc.exe -DCMAKE_CXX_COMPILER=C:/msys64/mingw64/bin/g++.exe
cmake --build "$env:TEMP\etm-decoder-build" --parallel 4

# 4) 服务
cd E:\superlinkPRO\web; python -X utf8 tools\dev\serve-etm.py --etm-root ..\etm --port 8890
# 另开：node tools\dev\serve-nocache.mjs 8911

# 5) 测试
cd E:\superlinkPRO\web; npm ci; make test; make check
cd E:\superlinkPRO\etm; python -m unittest discover -s tools/tests

# 6) 页面冒烟（不接硬件）
pwsh -NoProfile -File E:\superlinkPRO\web\tools\selftest\launch-browser.ps1 -Port 9334 -Profile "$env:TEMP\superlinkpro-verify"
# 然后用 CDP（tools/selftest/cdp-lib.mjs）导航到 http://127.0.0.1:8890/ ，收集 console/exception，点 15 个标签
```

---

### 变更记录

| 日期 | 变更 |
| --- | --- |
| 2026-10-17 | 初版：完整性对账（含 merge-map §7 三处重复计数的修正）、驱动统一复核、构建、单测、页面冒烟、go/no-go |
