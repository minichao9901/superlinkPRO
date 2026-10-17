# etm/ 迁移记录（web-etm-trace → superlinkPRO/etm）

| 项 | 值 |
| --- | --- |
| 来源仓库 | `E:\web-etm-trace` |
| 来源分支 | **`master`**（注意：不是 main） |
| 来源 commit | `12746fc035833e90f695d45e15a1f7ffa1e132ad`（短 `12746fc`） |
| 来源 commit 时间 / 标题 | 2026-10-17 20:33:06 +0800 · `fix(fw): .hex 改为由稠密 .bin 生成 —— 旧 hex 省略整段 0x00，烧 hex 会缺字节` |
| 迁移时间 | 2026-10-17 22:15（+08:00） |
| 迁移方式 | **逐文件复制**（PowerShell `Copy-Item`，清单来自 `git -c core.quotepath=false ls-files`）；**没有**用 `git subtree` / `git archive`，**不带 git 历史**（D-6） |
| 依据 | `docs/merge-map.md` §2.2C / §4.2 / §4.5 / §4.6 / §4.7 / §2.2D；`docs/decisions.md`（D-2 firmware 裁剪、D-4 docs 不收敛、D-6 不带历史） |
| 目标位置 | `E:\superlinkPRO\etm\`（`etm/web/**` 是 ETM 页面源码，**t6 负责并入 `web/`**） |

---

## 1. 数量对账（硬口径）

| 项 | 文件数 | 字节 |
| --- | ---: | ---: |
| 源仓 `git ls-files` 跟踪文件 | 5,472 | 727,177,580（693.5 MiB） |
| 不迁移（§3 逐项列明） | 4,878 | 686,542,885（654.7 MiB） |
| **实际迁移** | **594** | **40,634,695（38.75 MiB）** |
| 目标侧复核（逐文件 SHA-256） | 594 / 594 一致 | 40,634,695 一致 |

### 1.1 落点分布

| 源路径 | 目标路径 | 文件数 | 字节 | 说明 |
| --- | --- | ---: | ---: | --- |
| `server.py`、`requirements.txt`、`CMakeLists.txt`、`build-decoder.ps1`、`capture.ps1`、`start-web.bat`、`start-web.ps1`、`README.md`、`AGENTS.md`、`CLAUDE.md` | `etm/…` | 10 | 67,180 | 后端 + 文档；**不含** `.gitignore`（规则已并入根 `.gitignore`） |
| `tools/**` | `etm/tools/**` | 78 | 483,371 | 后端 py + `tools/tests`(24) + `tools/h743`(15) + `tools/la`(6) + `tools/selftest`(3) |
| `vendor/OpenCSD-1.8.3/**` | `etm/vendor/OpenCSD-1.8.3/**` | 195 | 2,515,955 | 解码库源码（`decoder/{build,docs,include,source}`），CMake 直接 `add_subdirectory` |
| `native/decoder.cpp` | `etm/native/decoder.cpp` | 1 | 6,718 | OpenCSD C-API → JSONL 的解码器源码 |
| `web/**` | `etm/web/**` | 25 | 322,848 | ETM 页面（`index.html`、`style.css`、`app/**` 23），**t6 并入 `web/`** |
| `baselines/spi-70-35-v1/**` | `etm/baselines/spi-70-35-v1/**` | 6 | 10,412,766 | `manifest.json`、`validation.json`、`README.md`、`capture-10ms.etmrecord`(5.7 MB)、`decoded.json.gz`(4.5 MB)、`web-integrity.png` |
| `configs/f103ze-etmv3.json` | `etm/configs/…` | 1 | 312 | ETMv3 配置 |
| `docs/**` | `etm/docs/**` | 90 | 2,351,012 | 91 个跟踪文件 − 1 份重复的《逻辑分析仪攻略》（§3.3） |
| `firmware/**` | `etm/firmware/**` | 188 | 24,474,533 | **裁剪式**：见 §1.2 |
| **合计** | | **594** | **40,634,695** | |

### 1.2 `firmware/` 裁剪明细（D-2）

任务/决策原文有两条硬要求：**「只搬 2 个靶子工程（136 文件 / 12.5 MB）」** 与 **「必须包含
`stm32h743_cdc_trace`（本会话真机验证过的）与 H7B0 那个靶子」**。实测该仓 `firmware/` 共
**5,064 文件 / 710,947,213 B**，其中：

| 源目录 | 处理 | 文件数 | 字节 |
| --- | --- | ---: | ---: |
| `firmware/stm32h7b0_etm/**` | **保留**（含 `sdk/` 51、`segger_rtt/` 5、`src/` 3、`fw.{elf,bin,hex}`、`build.ps1`/`flash.ps1`/`ld`/`README`） | 66 | 6,296,679 |
| `firmware/stm32h7b0_etm_board_v1/**` | **保留**（板级 v1 变体，同上结构） | 60 | 6,168,594 |
| `firmware/stm32h743_cdc_trace/**`（**去掉 `sdk/`**） | **保留** source + LD + cherryusb + 预编译 `fw.*`（含 5 个 CPU/trace 变体） | 52 | 11,986,114 |
| `firmware/stm32h743_cdc_trace/sdk/**` | **丢弃**（HAL/CMSIS/SDK 全量） | 1,457 | 253,482,167 |
| `firmware/stm32f103_cdc_trace/**` | **丢弃** | 950 | 69,125,323 |
| `firmware/stm32h7b0_cdc_trace/**` | **丢弃** | 1,503 | 258,293,746 |
| `firmware/stm32h7b0_spi_trace/**` | **丢弃**（稳定基准 `spi-70-35-v1` 的固件在这里，见 §6.2） | 730 | 85,402,999 |
| `firmware/_archive_stm32f103ze_cdc_trace_20261012/**` | **丢弃** | 230 | 19,904,052 |
| `firmware/_compare/**` | **丢弃** | 6 | 264,393 |
| `firmware/Makefile`、`firmware/README.md` | **保留**（原样） | 2 | 5,170 |
| `firmware/tools/**` | **保留**（`build_*.ps1`、`cdc_test.py`、`ocd_flash.sh` 等 8 个） | 8 | 17,976 |
| **保留合计** | | **188** | **24,474,533** |
| **丢弃合计** | | **4,876** | **686,472,680** |

⇒ 文字结论：**firmware 只保留这两个靶子工程（`stm32h743_cdc_trace` + H7B0 的 `stm32h7b0_etm` /
`stm32h7b0_etm_board_v1`），其余按 D-2 丢弃**；H743 的 `sdk/` 因同一理由（HAL/CMSIS 重复 + 253 MB）一并丢弃，
但**预编译 `fw.*` 全部保留**，所以该靶子仍可直接烧录用。

**为什么 H743 只留 `sdk/` 之外的部分**：`sdk/` 一个目录就 **1,457 文件 / 253,482,167 B**（`Drivers/` 1,442、
`Core/` 7、`RTT_TRACE_LOG/` 7、1 个 `.ioc`），是 D-2 要避免的「678 MB 级固件重复」主体；把它搬进来会让本仓
体积翻 6 倍。**去掉 `sdk/` 不影响烧录用现成产物**：`fw.elf` / `fw.bin` / `fw.hex` 与 5 个 CPU/trace 变体
（`fw-cpu{30,60,90,120}-trace30.*`）都在，`server.py` 的 `h743-cdc-60` 档位实测**可用**（§5.3）。
若要重新编译该工程：把原仓的 `firmware/stm32h743_cdc_trace/sdk/` 整个拷回（源 commit 见本文头部），或按
`firmware/stm32h743_cdc_trace/build.ps1` 里引用的 HAL/CMSIS 路径自行准备。

> 另注：H7B0 两个工程的 `sdk/` 是**故意保留**的 —— 它们属于 merge-map §2.2C 的 136 文件口径（66+60+2+8），
> 且让这两个靶子工程保持「拷走即可编译」；其中 57/66 与 51/60 个文件与 toolbox `web/tools/target-firmware/stm32h7b0_rtt_speed`
> 逐字节相同（merge-map §4.5 的 249 个 firmware 重复文件正是散在 5 个固件目录里的 HAL/CMSIS/RTT 库），
> 这部分**小体量重复（≈9 MB）按 merge-map 推荐值接受**。

---

## 2. 对账手段（可复核）

1. **零覆盖**：复制前对 594 个目标路径逐个断言不存在 → `collisions = 0`；复制过程中 `Test-Path` 断言从未触发。
2. **零差异**：复制后对 594 个文件逐个 `Get-FileHash -Algorithm SHA256` 与**源仓工作区**比对 →
   `mismatches = 0`；两侧字节总量均为 40,634,695。
3. **集合相等**：目标 `etm/` 实际文件集 == 期望文件集（594 个迁移文件 + 本记录 1 个；无多余文件）。
4. **工作区状态优先**：源仓 `git status` 有 **1 个已修改跟踪文件** —— `firmware/stm32h743_cdc_trace/fw.hex`
   （2026-10-17 提交之后重建：`.hex` 改为由稠密 `.bin` 生成，旧 hex 省略整段 `0x00`，烧 hex 会缺字节）。
   按 merge-map §5.3「若决定搬，取工作区这份」，已复制**工作区**版本：
   目标 sha256 前缀 `39A6349AB211` == 源工作区；源 HEAD 版本为 `A394A1085822`（不同）。
5. **不被忽略**：`git check-ignore --no-index --stdin` 对 595 个路径（594 + 本文件）→ **0 个被忽略**。
   专项：`vendor/OpenCSD-1.8.3/decoder/build/**`（16 个，t2 显式白名单）、`baselines/**/*.etmrecord`、
   `*.json.gz`、`etm/firmware/**/fw.{elf,bin,hex}` 都未被吞。
6. **零产物混入**：`etm/` 下没有 `captures/`、`records/`、`tmp/`、`decoded/`、`build-native/`、`__pycache__/`
   （`build-decoder.ps1` 的产物目录、服务端运行时创建的 `records/` 与 `__pycache__/` 均已清理并复核为 594 个迁移文件，加本记录共 595）。
7. **中文名**：`etm/tools/la/毛刺滤波使用说明.md`、`etm/docs/…(中文文档)` 等非 ASCII 文件名全部到位且 SHA-256 一致
   ⇒ 清单必须 `git -c core.quotepath=false ls-files`。
8. **忽略自检（t3 踩坑后 captain 对 t5 的硬要求）**：`git ls-files --others --ignored --exclude-standard -- etm`
   = **0**；`git ls-files --others --exclude-standard -- etm` = **0**；`git ls-files etm` = **595**。
   子目录里唯一的嵌套 `.gitignore` 是 `etm/vendor/OpenCSD-1.8.3/.gitignore`（上游 OpenCSD 自带、随库原样保留），
   实测**没有吞掉任何迁移文件**（check-ignore 对 595 路径 0 命中）。

---

## 3. 刻意没搬（逐项 + 证据）

### 3.1 五个大固件工程 + H743 的 `sdk/`（4,876 文件 / 686,472,680 B）
见 §1.2 的表与理由。判据：D-2「其余 249 个 firmware 文件与 toolbox 的 HAL/CMSIS 逐字节重复且总量 678 MB，
按 D-2 丢弃」+ merge-map §2.2C「默认不搬」+ §4.5 的重复清单。

### 3.2 根 `.gitignore`（1 文件 / 689 B）
**不搬**：规则已由 t2 并入 `E:\superlinkPRO\.gitignore`（三仓并集 + 对已跟踪文件逐条放行）；
本任务已用 `git check-ignore` 复核（§2.5）。

### 3.3 `docs/逻辑分析仪攻略.md`（1 文件 / 69,516 B）
**重复文件**：与 `web/docs/逻辑分析仪攻略.md` **逐字节相同**
（sha256 = `D7CAD17153084796233FD011F5F5F1071120B76E8DB164CF393AFA8ADAF4D440`，两侧实测一致）。
按 merge-map §2.2D：保留 `web/docs/` 那份，删 `etm/docs/` 这份（全仓检索无任何文档按路径引用它）。

### 3.4 磁盘上存在、但从未被跟踪的产物（不在 5,472 之内，也不搬）
| 目录 | 规模（merge-map §1.3 实测） | 依据 |
| --- | --- | --- |
| `captures/` | **11.2 GB** | 源 `.gitignore: captures/`（Kingst LA 采集 CSV/KVData） |
| `records/` | **4.4 GB** | 源 `.gitignore: records/`（记录库；`/api/stable-baseline` 依赖它，见 §6.3） |
| `tmp/` | — | 源 `.gitignore: tmp/` |
| `decoded/` | — | 源 `.gitignore: decoded/` |
| `build-native/` | — | 源 `.gitignore: build-native/`（`build-decoder.ps1` 的输出） |
| `docs/reference/`、`vendor/OpenCSD/`、`vendor/*.zip`、`vendor/opencsd-tree.json` | — | 源 `.gitignore` 逐条 |
| `firmware/**/build/`、`firmware/stm32h7b0_spi_trace/reference/` 等 | — | 源 `.gitignore` 逐条 |

### 3.5 未跟踪文件（1 个）
`firmware/_archive_stm32f103ze_cdc_trace_20261012/src/boot_probe.c.bak` —— 手工备份，且所在 `_archive` 目录
整体按 D-2 丢弃，不搬。

---

## 4. 怎么起 ETM 后端 / 跑解码（相对 `E:\superlinkPRO\etm\`）

### 4.1 依赖与启动
```powershell
cd E:\superlinkPRO\etm
python -m pip install -r requirements.txt      # Flask 2.2.5 / Werkzeug / pyelftools / capstone
python -X utf8 server.py --port 8766           # 或：start-web.ps1（默认 8766）/ start-web.bat
# 打开 http://127.0.0.1:8766
```
`server.py:21` 用 `ROOT = Path(__file__).resolve().parent`，所以**从任何 cwd 启动都能找到
`tools/`、`web/`、`baselines/`、`firmware/`**（实测见 §5.3）。

### 4.2 构建原生解码器（OpenCSD + `native/decoder.cpp`）
```powershell
cd E:\superlinkPRO\etm
pwsh -NoProfile -File .\build-decoder.ps1      # cmake -G Ninja + MSYS2 gcc/g++（脚本里写死 C:/msys64/mingw64/bin）
# 产物：etm\build-native\etm_decode.exe（已被根 .gitignore 忽略，不入库）
```
本次已用同一份 CMake 工程在**临时目录**实跑验证：`cmake` 配置 + 构建 **54 步全过**，产出
`etm_decode.exe` **4,167,203 B**（见 §5.2）。`/api/status` 的 `decoder` 字段就是检查
`build-native/etm_decode.exe` 是否存在（`server.py:292`）。

### 4.3 命令行解码
```powershell
cd E:\superlinkPRO\etm
python -X utf8 tools\decode_trace.py <输入.trace> --elf firmware\stm32h7b0_etm_board_v1\fw.elf --out <输出目录>
```
`tools/decode_trace.py:141` 找 `build-native/etm_decode.exe`（未构建会报「缺少 OpenCSD 解码器，请先运行
build-decoder.ps1」）；默认 ELF 就是保留的 `firmware/stm32h7b0_etm_board_v1/fw.elf`。

### 4.4 采集（需要 Kingst LA + KingstVIS，不属「无硬件」范围）
```powershell
pwsh -NoProfile -File .\capture.ps1                       # 500 MSa/s、10 ms、CH0..4
pwsh -NoProfile -File .\capture.ps1 -Rate 200000000 -Seconds 0.01
python -X utf8 .\tools\la\kingst_la.py info               # Socket API: %LOCALAPPDATA%\kingst\vis.config
```

### 4.5 后端测试（纯 Python，不需要硬件）
```powershell
cd E:\superlinkPRO\etm
python -B -m unittest discover -s tools\tests -p 'test_*.py'
```
本次实跑：**114 个测试，104 通过**；10 个失败/错误的成因**全部是 D-2 裁剪或未迁移的记录产物**，见 §6。

### 4.6 与 `web/` 的集成（t6 的入口）
```powershell
cd E:\superlinkPRO\web
python tools\dev\serve-etm.py --etm-root ..\etm --port 8878
# 或 make serve-etm（Makefile 第 41-42 行只调 tools/dev/start-etm.ps1，无路径字面量）
```
t6 需要按 merge-map §2.4 改 5 个文件里的 `..\web-etm-trace` → `..\etm`。

---

## 5. 本次校验记录（t5）

1. **`py_compile` 逐个编译**：`etm/tools/**/*.py`（54）+ `etm/server.py`（1）+ `etm/firmware/tools/*.py`（2）
   = **57 / 57 全部 OK，0 失败**（`PYTHONPYCACHEPREFIX` 指向 %TEMP%，仓内零 `.pyc`）。
2. **原生解码器可构建**：`cmake -S etm -B %TEMP%\… -G Ninja -DCMAKE_BUILD_TYPE=Release` + `cmake --build`
   → **exit 0，54 步**，`etm_decode.exe` 4,167,203 B；证明 `CMakeLists.txt`、`native/decoder.cpp`、
   `vendor/OpenCSD-1.8.3`（195 文件）在迁移后仍自洽。
3. **后端冒烟（无硬件）**：`python server.py --port 8879` 起在 `etm/` 新位置 →
   - `GET /api/status` → **200**，`{"captureProtocol":2,"connected":true,"decoder":false,…}`（`decoder:false` 正常：
     `build-native/etm_decode.exe` 未构建）；
   - `GET /` → **200**，14,604 B，`<title>ETM 执行轨迹 · 采集与历史分析</title>`；
   - `GET /api/profiles` → **200**：`h743-cdc-60`（用保留的 `stm32h743_cdc_trace/fw.elf`）与 `fixture`
     （用 `stm32h7b0_etm_board_v1/fw.elf`）**available=true**；`spi-70` 等 7 个档位 false（固件在被丢弃的
     `stm32h7b0_spi_trace/` 里，§6.2）；
   - `GET /api/stable-baseline` → **200** `{"available":false}`（`records/` 未迁移，§6.3）。
   测试后已停服务并删除它创建的 `etm/records/`、`__pycache__/`，文件数复核回到 **594** 个迁移文件（加本记录共 595）。
4. **`baselines` manifest 在位**：`etm/baselines/spi-70-35-v1/manifest.json` 可解析，字段
   `version/id/profile/record/previewRecord/firmware/parameters/artifacts/summary/quality/spiValidation` 齐全。
5. **单测**：见 §4.5 / §6.1 的 104/114 与失败成因分类。

---

## 6. 已知限制与下游交接（诚实记录）

1. **10 个单测失败/错误全部是「有意没搬」造成的**（不是迁移出错）：
   - 5 个读 `firmware/stm32h7b0_spi_trace/stable-70-35-v1/fw.elf`（D-2 丢弃）→ `test_breakpoint_resolver` setUpClass、
     `test_server` 的 4 个用例；
   - 1 个读 `decoded/<sample>/decoded.json`、1 个读 `records/capture-…/capture.kvdat`、1 个读
     `captures/trace-20261011-000319.csv` → 这三类都是 gitignored 的产物目录（§3.4）；
   - 1 个 `test_browse_baseline` 依赖 `records/` 里的长记录（同上）；
   - 1 个 `test_etm_serves_its_committed_source_store_copy` 默认去 `ROOT.parent/'web-serial-rtt-tools'`
     找上游 `app/dbg/source.js` —— 合并后该兄弟目录变成 `web/`，**t6 需改成 `ROOT.parent/'web'`**（或设
     `SERIAL_ROOT` 环境变量）。
2. **`spi-*` 固件档位不可用**：`server.py` 的 `PROFILES` 里 7 个档位指向 `firmware/stm32h7b0_spi_trace/**`
   （D-2 丢弃）。`h743-cdc-60` 与 `fixture` 两档实测可用（§5.3）。若要让稳定基准档位也能用，从原仓补回
   `firmware/stm32h7b0_spi_trace/stable-70-35-v1/fw.elf`（及 `fw-*.elf`）即可，**不用恢复整个工程**。
3. **`/api/stable-baseline` 报不可用是预期**：`server.py:340` 的 `stable_baseline()` 要求
   `records/<record>/decoded.json` 与 `records/<record>/spi-validation.json` —— 即**记录库 `records/`（4.4 GB，
   gitignored）没迁移**。`baselines/spi-70-35-v1/manifest.json` 本身仍在（`tools/tests/test_server.py` 直接读它），
   页面要回放基准需要先导入 `.etmrecord`/记录包。
4. **baseline manifest 引用的两个非跟踪文件**：`baselines/spi-70-35-v1/execution.c`
   （91,825,433 B）与 `execution-simplified.c` 在源仓**就是未跟踪**（`.gitignore: baselines/*/execution*.c`），
   因此不在迁移集里，manifest 的 `artifacts` 会引用到不存在的文件（页面按需重建即可）。
5. **探针驱动是派生副本，别手改**：`etm/web/app/shared/**` 与 `web/app/{rtt,core,dbg}/**` 是同一套驱动的
   多份拷贝（merge-map §4.1/§4.7）。唯一真源在 `web/app/**`；t6 用 `web/tools/dev/sync-shared-drivers.mjs`
   统一（含 3 项 ETM 真机修复的回灌顺序，见 §4.8）。
6. **`etm/tools/la/kingst_la.py` 是超集**（1,068 行 / 54,079 B，比 `web/tools/la/` 多 4 个函数：
   `read_glitch_settings`、`glitch_preset_bytes`、`kingst_running`、`cmd_glitch_preset`）→ t6 按 merge-map §4.6
   回灌到 `web/tools/la/kingst_la.py`。
7. **`etm/web/**` 是 t6 的输入**：ETM 页面并入 `web/` 后本目录的去留由 t6 决定（当前按 merge-map §2.2C 保留）。
8. **无 git 历史**（D-6）：考古请回 `E:\web-etm-trace`（`master`，commit 见本文头部）；40 位 SHA 是最后凭据。
9. 本任务**未连硬件、未烧录**：`capture.ps1`、`tools/la/*`、`tools/h743/*` 等需要 Kingst LA / 探针的目标都未运行。

---

## 7. 复核命令（照抄）

```powershell
# 1) 源清单（master！-c core.quotepath=false 必须有）→ 5472
git -C E:\web-etm-trace rev-parse --abbrev-ref HEAD        # master
git -C E:\web-etm-trace rev-parse HEAD                     # 12746fc035833e90f695d45e15a1f7ffa1e132ad
git -C E:\web-etm-trace -c core.quotepath=false ls-files | Measure-Object -Line

# 2) 目标各子树文件数（期望：etm 总计 595 = 594 迁移文件 + 本记录；baselines 6 / configs 1 / docs 90 / firmware 188 / native 1 / tools 78 / vendor 195 / web 25 + 根 10）
(Get-ChildItem E:\superlinkPRO\etm -Recurse -File -Force).Count
foreach ($p in 'baselines','configs','docs','firmware','native','tools','vendor','web') {
  "{0} = {1}" -f $p, (Get-ChildItem "E:\superlinkPRO\etm\$p" -Recurse -File -Force).Count
}

# 3) 入库文件数（期望 595 = 594 + README-migrated.md）
git -C E:\superlinkPRO -c core.quotepath=false ls-files etm | Measure-Object -Line

# 4) 逐文件 SHA-256 对账（594 个，期望 mismatches=0）：跳过 .gitignore 与 docs/逻辑分析仪攻略.md
$src='E:\web-etm-trace'; $dst='E:\superlinkPRO\etm'
$skip=@('.gitignore','docs/逻辑分析仪攻略.md')
$n=0;$bad=0
foreach ($rel in (git -C $src -c core.quotepath=false ls-files)) {
  if ($skip -contains $rel) { continue }
  if ($rel -like 'firmware/*' -and $rel -notlike 'firmware/Makefile' -and $rel -notlike 'firmware/README.md' -and
      $rel -notlike 'firmware/tools/*' -and $rel -notlike 'firmware/stm32h7b0_etm/*' -and
      $rel -notlike 'firmware/stm32h7b0_etm_board_v1/*' -and
      -not ($rel -like 'firmware/stm32h743_cdc_trace/*' -and $rel -notlike 'firmware/stm32h743_cdc_trace/sdk/*')) { continue }
  $a=(Get-FileHash -LiteralPath (Join-Path $src ($rel -replace '/','\')) -Algorithm SHA256).Hash
  $b=(Get-FileHash -LiteralPath (Join-Path $dst ($rel -replace '/','\')) -Algorithm SHA256).Hash
  if ($a -ne $b) { $bad++; "HASH-DIFF $rel" }; $n++
}
"checked=$n mismatches=$bad"

# 5) 起后端（需先 pip install -r etm/requirements.txt）+ 解码器
cd E:\superlinkPRO\etm; pwsh -NoProfile -File .\build-decoder.ps1
python -X utf8 server.py --port 8766
```