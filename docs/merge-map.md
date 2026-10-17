# superlinkPRO 合并映射表（唯一依据）

> 本文是「三仓 → `E:\superlinkPRO`」的唯一迁移依据：照本文抄即可搬完，不需要再回头读源码。
>
> | 项 | 值 |
> | --- | --- |
> | 生成时间 | 2026-10-17（三仓工作区现场实测） |
> | 源仓 1 | `E:\Share\github\akaLinkPro`，分支 `main`，HEAD `3214872` |
> | 源仓 2 | `E:\web-serial-rtt-tools`，分支 `main`，HEAD `089e383` |
> | 源仓 3 | `E:\web-etm-trace`，分支 **`master`**，HEAD `12746fc` |
> | 目标 | `E:\superlinkPRO`（当前为空、无 `.git`） |
> | 硬件 | **全程不连硬件、不烧录**，只做文件搬迁与静态校验 |

---

## 0. 先看结论（摘要）

1. **只搬 git 跟踪内容**：三仓 `git ls-files` 实测 **680 + 2035 + 5472 = 8187 个文件 / ≈825 MiB**。
   三仓磁盘共 **28.3 GB**，其中 **≈26 GB 是 gitignored 产物**（`captures/` 11.2 GB、`records/` 4.4 GB、
   `tmp/` 5.2 GB、`docs/validation` 未跟踪部分 3.5 GB），一律不搬（§1.3）。
   按本文取舍（D-1 删原板、D-2 只搬 ETM 靶子固件）实际落地 **3188 个文件 / 153 MB**（§7 有逐项期望值）。
2. **目标布局**：`firmware/`（HPM5301EVKLite 固件）+ `web/`（工具箱前端与 bridge）+ `etm/`（ETM 后端、
   OpenCSD、页面）+ `docs/`（固件文档与合并文档）+ `tools/`（跨子树工具）+ 根级 `script_test/`、`Makefile`
   （§2.1）。**该布局使 23 处「同相对路径」冲突天然分到不同子树，文件零覆盖**（§2.3）。
3. **三件不是"拷贝就能完"的事**（都给了可执行步骤）：
   - **探针驱动去重**：`dap-webusb.js` 3 份拷贝/2 个版本、`usb-device.js` 3 份/2 版；需先把 ETM 侧 3 项真机修复
     移植进 toolbox 版，再以「生成副本 + sha256 守卫」让 ETM 页消费唯一真源（§4.1、§4.7、§4.8）。
   - **ETM 页面快照**：`web/bridge/etm/backend/**` 是 `etm/` 的**派生快照**（provenance 指向 ETM `a2922f8`），
     搬完后必须改由 `sync-etm-runtime.py --etm-root ../etm` 生成，不再手工维护（§4.2）。
   - **未提交改动**：工具箱 8 个已改源码 + 4206 个未跟踪产物、akaLinkPro 2 项、ETM 1 改 1 未跟踪，
     逐项给了"搬/不搬"结论（§5）。
4. **6 个 Decision 需用户拍板**：ETM 的 678 MB `firmware/` 搬不搬、akaLinkPro 的 `boards/akaLinkPro` 留不留、
   `.opencode/skills` 留不留、`script_test` 里的目标固件副本是否瘦身、`docs/` 是否收敛成单根、
   是否保留 git 历史（§8）。

---

## 1. 三仓 git 跟踪文件清单

### 1.1 版本与体量（`git ls-files` / 磁盘实测）

| 仓 | 分支 | HEAD | 跟踪文件 | 跟踪字节 | 磁盘占用 | 磁盘文件数 |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| akaLinkPro | main | `3214872` | **680** | 25,715,959 B（24.5 MiB） | 156 MB | 4,168 |
| web-serial-rtt-tools | main | `089e383` | **2035** | 112,425,879 B（107.2 MiB） | 10,176 MB | 29,627 |
| web-etm-trace | **master** | `12746fc` | **5472** | 727,177,580 B（693.5 MiB） | 17,929 MB | 14,130 |
| 合计 | — | — | **8187** | ≈825 MiB | 28.3 GB | 47,925 |

复现命令（三仓通用，下同）：

```powershell
git -C <repo> -c core.quotepath=false ls-files | Measure-Object -Line
git -C <repo> -c core.quotepath=false ls-files -s            # mode/sha/路径
git -C <repo> rev-parse --abbrev-ref HEAD; git -C <repo> rev-parse --short HEAD
```

> ⚠️ **`core.quotepath=false` 必须加**：仓库里有中文文件名（如 akaLinkPro 的
> `docs/代码审查报告.md`、`docs/opus5-5代码审查报告.md`）。不加时 `git ls-files` 输出
> `"docs/\344\273\243..."` 这类转义串，按它取文件会**静默漏掉 2 个文件**（本次实测踩到，见 §6.2）。

### 1.2 各仓跟踪内容的目录分布

**akaLinkPro（680 文件 / 24.5 MiB）**

| 目录 | 文件数 | 字节 | 说明 |
| --- | ---: | ---: | --- |
| `script_test/` | 289 | 10,011,780 | 探针主机侧测试脚本 + 7 套目标固件（含 `stm32h7b0_rtt_speed/sdk` 51 个） |
| `docs/` | 170 | 6,163,064 | 其中 `docs/validation/` 139；`HPM5301EVKLite_UG_V1.1.pdf` 3.6 MB |
| `firmware/` | 145 | 3,469,143 | `application_5301/` 111 + `bootloader_dfu/` 32 + `tools/pack.py` + `version.json` |
| `hardware_pro/` | 28 | 4,745,232 | KiCad 新版原理图/PCB/生产文件 |
| `tools/` | 19 | 81,810 | SPI/面板/LVGL 主机脚本 + `clean-evklite-build.ps1` |
| `.opencode/` | 14 | 123,736 | `.opencode/skills/akalinkpro-firmware/**`（AI 技能配置） |
| `hardware/` | 11 | 1,080,091 | KiCad 旧版原理图/PCB |
| 根 | 4 | — | `.gitignore` `LICENSE` `Makefile` `README.md` |

**web-serial-rtt-tools（2035 文件 / 107.2 MiB）**

| 目录 | 文件数 | 字节 | 说明 |
| --- | ---: | ---: | --- |
| `docs/` | 997 | 23.9 MB | `docs/validation/` 894（10.6 MB）+ `docs/shots/` 61（9.3 MB）+ 说明文档 |
| `tools/` | 574 | 17.3 MB | `target-firmware/` 290（14.3 MB）、`selftest/` 221（2.4 MB）、`dev/` 34、`fixtures/` 18、`la/` 4、`probe-firmware/` 4、3 个 `ocd_*.py` |
| `app/` | 194 | 13.5 MB | `dbg/` 34（10.2 MB，含 SVD）、`core/` 22、`spi/` 18、`ui/` 16、`swo/` 16、`flash/` 15、`vendor/` 5（xterm）… |
| `samples/` | 180 | 22.5 MB | `test_images/` 93（16.6 MB）、`panel_init_many/` 68、`swo/` 13、`anim/` 8、`panel-init/` 2 |
| `bridge/` | 68 | 37.0 MB | `bridge/etm/` 64（36.96 MB，含 24 MB 下载包）+ `rtt-bridge.mjs` + 启动脚本 |
| 根 | 11 | — | `index.html`(206 KB) `app.css` `Makefile`(52 KB) `package.json` `package-lock.json` `README.md` `LICENSE` `start-web.bat` `start-etm.bat` `kill-web.bat` `.gitignore` `.gitattributes` |

**web-etm-trace（5472 文件 / 693.5 MiB）**

| 目录 | 文件数 | 字节 | 说明 |
| --- | ---: | ---: | --- |
| `firmware/` | **5064** | **710,947,213（678 MB）** | 7 个固件工程，每个都带完整 STM32 SDK（`stm32h743_cdc_trace` 1509、`stm32h7b0_cdc_trace` 1503、`stm32f103_cdc_trace` 950、`stm32h7b0_spi_trace` 730、`_archive_…` 230、`stm32h7b0_etm` 66、`stm32h7b0_etm_board_v1` 60）→ **Decision D-2** |
| `vendor/OpenCSD-1.8.3/` | 195 | 2,515,955 | 唯一被跟踪的 vendor（`vendor/OpenCSD/`、`vendor/*.zip` 被 ignore） |
| `tools/` | 78 | 483,371 | `tests/` 27、`h743/` 15、`la/` 6、`selftest/` 3 + 后端 py |
| `docs/` | 91 | 2,420,528 | 其中 `docs/validation/` 53 |
| `web/` | 25 | 322,848 | ETM 页面（24 个 js/css + index.html） |
| `baselines/` | 6 | 10,412,766 | `spi-70-35-v1/`：capture 5.7 MB + decoded.gz 4.5 MB + manifest… |
| 根 | 11 | — | `server.py`(45 KB) `CMakeLists.txt` `native/decoder.cpp` `requirements.txt` `start-web.bat/ps1` `build-decoder.ps1` `capture.ps1` `README.md` `AGENTS.md` `CLAUDE.md` `.gitignore` |

> `baselines/spi-70-35-v1/execution.c`（91.8 MB）与 `execution-simplified.c`（19.7 MB）**在磁盘上存在但被
> `.gitignore` 排除**（`baselines/*/execution*.c`），不是跟踪内容 —— 典型的"看着大、其实不搬"。

### 1.3 排除清单（gitignored / 未跟踪的磁盘大头，实测）

**只搬 `git ls-files` 列出的文件。以下一律不搬：**

| 仓 | 路径 | 磁盘 | 文件数 | 依据 |
| --- | --- | ---: | ---: | --- |
| web-etm-trace | `captures/` | **11,168 MB** | 935 | `.gitignore: captures/`（LA/ETM 原始抓包） |
| web-etm-trace | `records/` | **4,360 MB** | 109 | `.gitignore: records/`（采集记录） |
| web-etm-trace | `tmp/` | 20 MB | 154 | `.gitignore: tmp/` |
| web-etm-trace | `build-native/` | 9 MB | 80 | `.gitignore: build-native/`（`etm_decode.exe` 及 OpenCSD 静态库） |
| web-etm-trace | `decoded/`、`docs/reference/`、`vendor/OpenCSD/`、`vendor/*.zip`、`firmware/**/build/`、`firmware/stm32h7b0_spi_trace/{reference/}` | — | — | `.gitignore` 逐条 |
| web-etm-trace | `baselines/*/execution*.c` | 112 MB | 2 | `.gitignore` |
| web-serial-rtt-tools | `tmp/` | **5,175 MB** | 12,409 | `.gitignore: tmp/`（含 `tmp/old-*` 两个 detached worktree） |
| web-serial-rtt-tools | `node_modules/` | 13 MB | 1,066 | `.gitignore` |
| web-serial-rtt-tools | `docs/validation/**` 未跟踪部分 | （整目录 3,532 MB / 5,914 文件 = 跟踪 894 + 未跟踪 4,206 + 被 ignore 814） | 4,206 | 见 §5.1（验收产物，非源码） |
| web-serial-rtt-tools | `.agent-teams/` | — | 2 目录 | **团队状态目录，绝不搬**（`superlinkpro-merge`、`archive`） |
| web-serial-rtt-tools | `validation/etm/**`、`codex-log.txt`、`ds-log.txt`、`docs/h743-web-review-2026-10-17.md` | — | 5 | 见 §5.1 |
| akaLinkPro | `tmp/` 38 MB/402、`build/` 4 MB/33、`captures/` 31 文件、`__pycache__/` | 42 MB | 466 | `.gitignore` |
| akaLinkPro | `script_test/_*`、`*/build*/`、`sram_test.bin` 等 | — | — | `.gitignore` 逐条 |
| 全部 | `.git/` | — | — | 不搬（历史按 §6.0 方案 B 另议） |

> 三仓合计 28.3 GB 磁盘里，**≈26 GB 属于上表**（17.9 GB 是 ETM 的 captures+records，5.2 GB 是 toolbox 的 tmp，
> 3.5 GB 是 toolbox 的 docs/validation）。这就是"17.8 GB / 9.8 GB 大头都是产物"的量化依据。

### 1.4 「只搬跟踪内容」的判定与复制原语（已实测）

判定 = `git ls-files` 的输出即白名单，**不做"整目录镜像后删"**（会白拷 26 GB）。

复制原语 **用 PowerShell 逐文件 `Copy-Item`**（实测 680 文件 / 24.5 MB / 8.6 秒，字节保真、中文名正常）：

```powershell
$prev = [Console]::OutputEncoding
[Console]::OutputEncoding = [Text.Encoding]::UTF8          # 让 git 输出的 UTF-8 文件名正确解码
$list = git -C $src -c core.quotepath=false ls-files
[Console]::OutputEncoding = $prev
foreach ($rel in $list) {
  $s  = Join-Path $src ($rel -replace '/','\')
  $d  = Join-Path $dst ($rel -replace '/','\')
  $dd = Split-Path $d -Parent
  if (-not (Test-Path -LiteralPath $dd)) { New-Item -ItemType Directory -Force -Path $dd | Out-Null }
  Copy-Item -LiteralPath $s -Destination $d -Force
}
```

> ❌ **不要用 `git ls-files -z | tar --null -T -`**：本机 bsdtar 3.8.9 在这两个中文名上直接失败
> （`tar: : Couldn't visit directory`），实测只搬了 678/680；`chcp 65001` 变体更糟（0 个）。
> ✅ `Copy-Item` 逐文件方案实测 **680/680、中文名 2 个都在、字节总量与源一致**。

---

## 2. 目标布局与映射表

### 2.1 目标树

```
E:\superlinkPRO\
├─ Makefile                  ← akaLinkPro/Makefile（原样：SHELL=cmd.exe，路径相对根成立）
├─ README.md                 ← 【新写】合并工程总说明（三仓 README 原样留在各自子树）
├─ LICENSE                   ← Apache-2.0（akaLinkPro 与 toolbox 两份仅行尾/空行差异，保留一份）
├─ .gitignore                ← 【合并】三仓 .gitignore 规则（§2.6）
├─ .gitattributes            ← 【新建】见 §2.6（toolbox 那份为主 + ETM 二进制规则）
├─ firmware\                 ← akaLinkPro/firmware（application_5301 + bootloader_dfu + tools + version.json）
├─ script_test\              ← akaLinkPro/script_test（根 Makefile 有 20 处引用，保持原位置）
├─ tools\                    ← akaLinkPro/tools（19 个 SPI/面板脚本；根 Makefile 1 处引用）
├─ docs\                     ← akaLinkPro/docs（170）+ 合并文档（merge-map.md、迁移记录）
├─ web\                      ← web-serial-rtt-tools 整棵树（app/ bridge/ samples/ tools/ docs/ Makefile …）
└─ etm\                      ← web-etm-trace 子集（server.py tools/ web/ native/ vendor/ baselines/ configs/ docs/）
```

**为什么这样分（三条硬理由，都已实测计数）**

1. `web/` 必须**整棵保持原样**（含它自己的 `tools/`、`docs/`、`samples/`、`bridge/`）：
   该仓 Makefile 有 **293 处 `tools/` 引用**，`tools/**` 脚本里还有 **416 处 `app/`、94 处 `index.html`、
   38 处 `bridge/`、32 处 `samples/`、25 处 `docs/`** 相对引用。只要 Makefile 仍在 `web/` 且 `web/` 内部结构不变，
   这些引用**全部零改动**；一旦把 `web/tools` 拆到根 `tools/`，就要改 ≈600 处。
2. `script_test/` 必须留在根：`akaLinkPro/Makefile` 有 **20 处 `script_test/`** 引用，且 `APP_DIR=firmware\application_5301`
   与 `tools/clean-evklite-build.ps1` 都是**相对根**的 —— 把该 Makefile 放根、`firmware/`+`script_test/`+`tools/`
   放根，**Makefile 零改动**。
3. `etm/` 独立成树：ETM 页要能单独起（`start-web.ps1`）并进便携包，见 §4.7。

### 2.2 逐路径映射表（源 → 目标）

#### A. akaLinkPro → 根

| 源路径 | 目标路径 | 文件数 | 动作 |
| --- | --- | ---: | --- |
| `firmware/application_5301/**` | `firmware/application_5301/**` | 111 | 原样 |
| `firmware/bootloader_dfu/**` | `firmware/bootloader_dfu/**` | 32 | 原样 |
| `firmware/tools/pack.py`、`firmware/version.json` | 同路径 | 2 | 原样 |
| `script_test/**` | `script_test/**` | 289 | 原样（D-5 需拍板是否瘦身） |
| `tools/**` | `tools/**` | 19 | 原样 |
| `docs/**` | `docs/**` | 170 | 原样（与合并文档同根，无重名冲突） |
| `Makefile` | `Makefile` | 1 | 原样（0 改动） |
| `LICENSE` | `LICENSE` | 1 | 原样（与 toolbox 那份内容等价，见 §2.3） |
| `README.md` | `firmware/README.md` | 1 | **改名**（根 README 用新写的总说明） |
| `.gitignore` | —— | 1 | **不单独搬**，规则并入根 `.gitignore`（§2.6） |
| `hardware/**` | —— | 11 | **不搬**（§3.2） |
| `hardware_pro/**` | —— | 28 | **不搬**（§3.2） |
| `.opencode/skills/**` | —— | 14 | **不搬**（D-3） |
| `firmware/application_5301/boards/akaLinkPro/**`、`firmware/bootloader_dfu/boards/akaLinkPro/**` | —— | 8 + 6 | **不搬**（§3.2，D-1 若回心转意则整目录拷回） |

#### B. web-serial-rtt-tools → `web/`（整树平移）

| 源 | 目标 | 文件数 | 备注 |
| --- | --- | ---: | --- |
| `app/**` | `web/app/**` | 194 | 前端（含 `app/vendor/xterm`） |
| `bridge/**` | `web/bridge/**` | 68 | `bridge/etm/**` 为派生快照（§4.2） |
| `samples/**` | `web/samples/**` | 180 | 测试素材（BMP/动图/面板表） |
| `tools/**` | `web/tools/**` | 574 | dev/selftest/fixtures/target-firmware/la/probe-firmware |
| `docs/**` | `web/docs/**` | 997 | 含 `docs/validation/`、`docs/shots/` |
| `index.html` `app.css` `main.js`* | `web/…` | 3 | *`app/main.js` 已在 app 内 |
| `Makefile` `package.json` `package-lock.json` | `web/…` | 3 | Makefile **0 改动**（cwd=web 时相对路径仍成立） |
| `README.md` `LICENSE` | `web/…` | 2 | 保留子树自述 |
| `start-web.bat` `start-etm.bat` `kill-web.bat` | `web/…` | 3 | `start-etm.bat` 需改 ETM 相对路径（§2.4） |
| `index.html` 之外根文件 `.gitattributes` `.gitignore` | `web/…` | 2 | 子树保留；根另建合并版 |
| `.agent-teams/**` | —— | 2 目录 | **不搬**（团队状态） |
| `tmp/**` `node_modules/**` | —— | — | 不搬（§1.3） |

#### C. web-etm-trace → `etm/`

| 源 | 目标 | 文件数 | 动作 |
| --- | --- | ---: | --- |
| `server.py` `requirements.txt` `CMakeLists.txt` `build-decoder.ps1` `capture.ps1` `start-web.bat` `start-web.ps1` | `etm/…` | 8 | 原样 |
| `native/decoder.cpp` | `etm/native/decoder.cpp` | 1 | 原样（**未打补丁版**，见 §4.3） |
| `web/**` | `etm/web/**` | 25 | ETM 页面源码 |
| `tools/**` | `etm/tools/**` | 78 | 后端 py + `tests/` + `h743/` + `la/` + `selftest/` |
| `vendor/OpenCSD-1.8.3/**` | `etm/vendor/OpenCSD-1.8.3/**` | 195 | 解码库（构建用） |
| `baselines/spi-70-35-v1/**` | `etm/baselines/spi-70-35-v1/**` | 6 | 基准记录/解码样本 |
| `configs/f103ze-etmv3.json` | `etm/configs/…` | 1 | 原样 |
| `docs/**` | `etm/docs/**` | 91 | ETM 文档 |
| `README.md` `AGENTS.md` `CLAUDE.md` | `etm/…` | 3 | 保留子树自述 |
| `firmware/stm32h7b0_etm/**` `firmware/stm32h7b0_etm_board_v1/**` `firmware/{Makefile,README.md}` `firmware/tools/**` | `etm/firmware/…` | 66+60+2+8 = **136** | **推荐搬**（ETM 靶子固件源码，D-2 推荐项） |
| `firmware/stm32f103_cdc_trace/**` `stm32h743_cdc_trace/**` `stm32h7b0_cdc_trace/**` `stm32h7b0_spi_trace/**` `_archive_…/**` `_compare/**` | —— | **4928 / 678 MB** | **默认不搬**（D-2：这些是带 SDK 的固件工程，与工具箱 `web/tools/target-firmware` 重复，见 §4.5） |
| `.gitignore` | —— | 1 | 规则并入根 `.gitignore` |
| `captures/` `records/` `tmp/` `build-native/` `decoded/` | —— | — | 不搬（§1.3） |

#### D. 文档与工具的去重落点

| 项 | 落点 | 依据 |
| --- | --- | --- |
| `akaLinkPro/docs/**` | 根 `docs/**` | 固件与板级文档；与 toolbox/ETM 的 `docs/` 不在同一子树，零冲突 |
| `web-serial-rtt-tools/docs/**` | `web/docs/**` | 997 个文件被 Makefile / `check-liquid.mjs` / `docs/shots` 截图脚本引用，不动 |
| `web-etm-trace/docs/**` | `etm/docs/**` | 同理 |
| `逻辑分析仪攻略.md`（toolbox 与 ETM 各一份，**内容逐字节相同**） | 保留 `web/docs/` 那份；删 `etm/docs/` 那份，并在 `etm/README.md` 加一行指针 | 全仓检索：无任何文档引用它的路径，删除安全 |
| `tools/la/{cmsis_dap_raw,dap_busy,swd_min_seq}.py`（两仓**完全相同**） | 各自保留（两边 README/Makefile 都按 `tools/la/…` 引用） | 若想单份 → D-6 |
| `tools/la/kingst_la.py`（两仓**不同**，ETM 是超集 +4 函数） | 以 **ETM 版**为准回灌 `web/tools/la/kingst_la.py` | §4.6 |
| `script_test/` 里的目标固件副本（115 个文件与 `web/tools/target-firmware` 逐字节相同） | 默认两份都留（+9.5 MB）；瘦身 → D-5 | §4.5 |

### 2.3 源仓之间的「同相对路径」冲突（23 条）与目标布局的化解

实测（按相对路径比对两仓跟踪清单）：

| 对比 | 条数 | 明细（SAME=内容相同，DIFF=内容不同） |
| --- | ---: | --- |
| toolbox ↔ etm | 8 | `SAME`：`docs/逻辑分析仪攻略.md`、`tools/la/cmsis_dap_raw.py`、`dap_busy.py`、`swd_min_seq.py`；`DIFF`：`.gitignore`、`README.md`、`start-web.bat`、`tools/la/kingst_la.py` |
| akaLinkPro ↔ toolbox | 13 | `DIFF` 全部：`.gitignore`、`LICENSE`、`Makefile`、`README.md`、`docs/{development-history,probe-periodic-bus,spi-cdc,usb-analog-dac-hid,usb-analog-page}.md`、`docs/validation/{2026-10-04-f103cb.md,2026-10-04/commits.json,2026-10-07-hpm6800-hss-rate.md,2026-10-08-spi-cdc-regression.md}` |
| akaLinkPro ↔ etm | 2 | `DIFF`：`.gitignore`、`README.md` |

**结论：§2.2 的布局把三仓分到 `firmware/ web/ etm/ docs/ tools/ script_test/` 六个互不重叠的前缀，
因此上表 23 条在目标树里一条都不会互相覆盖**（`docs/` 只放 akaLinkPro 的 docs）。
搬运脚本仍应把"目标文件已存在"当作**错误**处理（`Copy-Item` 前 `Test-Path` 断言），以此证明零覆盖。

**两份 LICENSE**：均 Apache-2.0，差异只是行尾/首行空行（akaLinkPro 201 行/11,558 B，toolbox 202 行/11,358 B）。
搬一份到根 `LICENSE`，另一份以 `web/LICENSE` 保留（子树自洽）。若要求根只留一份 → 用 `LICENSE` + `NOTICE`，
或直接保留两份（D-6 附带项）。

### 2.4 搬家后必须改的引用（实测清单，共 5 个文件 ≈10 行）

搬完后 `web/` 与 `etm/` 变成同级兄弟目录，凡是硬编码 `../web-etm-trace` 的地方都要改成 `../../etm`（相对 `web/tools/dev`）
或 `../etm`（相对 `web/`）。**清单（已逐条核过行号）：**

| 文件（目标路径） | 行 | 现值 | 改为 |
| --- | --- | --- | --- |
| `web/tools/dev/serve-etm.py` | 3（docstring） | `python tools/dev/serve-etm.py --etm-root ../web-etm-trace --port 8878` | `--etm-root ../etm` |
| `web/tools/dev/serve-etm.py` | 114 | `default=ROOT.parent/'web-etm-trace'` | `default=ROOT.parent/'etm'`（`ROOT` 已指向 `web/`） |
| `web/tools/dev/start-etm.ps1` | 1 | `Join-Path $PSScriptRoot '..\..\..\web-etm-trace'` | `'..\..\..\etm'`（`$PSScriptRoot`=`web/tools/dev`） |
| `web/tools/selftest/etm-integration-hw.mjs` | 6 | `resolve(root,'../web-etm-trace')` | `resolve(root,'../etm')`（`root`=`web/`） |
| `web/start-etm.bat` | 34, 36, 52, 83, 84 | `..\web-etm-trace`（存在性检查/回显/启动参数） | `..\etm` |
| `web/bridge/etm/README.md` | 29 | `--etm-root E:/web-etm-trace` | `--etm-root ../etm` |
| `web/docs/etm-integration.md` | 19, 22, 45 | `../web-etm-trace/…` 描述"两个原始仓库" | 改为"本仓库 `etm/`"，并删掉"另开两个仓"的措辞 |
| `web/tools/dev/sync-etm-runtime.py` | 76 | `sourceRepository='web-etm-trace'` | 改 `'etm'`（provenance 里的来源名） |

> **不需要改**：`web/Makefile` 的 `serve-etm` 目标只是调 `tools/dev/start-etm.ps1`，无路径字面量（第 41-42 行）。
> **不需要改**：`web/**` 里所有 `tools/ app/ bridge/ samples/ docs/` 相对引用（§2.1 理由 1）。
> **不需要改**：`Makefile`（根，akaLinkPro 那份）——`firmware\application_5301`、`script_test/`、
> `tools/clean-evklite-build.ps1` 在目标布局下全部仍成立。

### 2.5 搬完后的入口命令（每个子树怎么跑）

| 子树 | 服务/构建 | 测试（无硬件） |
| --- | --- | --- |
| 根（固件） | `make help`；`cd firmware\application_5301 && build_dfu_evklite.bat`（需 `E:\sdk_env_v1.11.0`） | `make test-host`、`make test-jtag-host`、`make test-usb-descriptors` |
| `web/` | `make serve`（8899）/ `make open`（起 CDP 浏览器）/ `npm run build:web`（改 `app/**` 后必须重建 `app/site/main.js`） | `make test`（纯 Node，多套件）、`make check`（语法/Liquid 守卫） |
| `web/` + `etm/` | `make serve-etm`（8878，把 `etm/` 挂到 `/etm/`） | `make test-etm`、`make test-etm-integration` |
| `etm/` 单独 | `pwsh -File etm/start-web.ps1`（8766）/ `python etm/server.py` | `python -m unittest discover -s etm/tools/tests`、`node etm/tools/tests/*.test.mjs` |

### 2.6 需要新建/合并的四个根级文件

| 文件 | 来源 | 内容要求 |
| --- | --- | --- |
| `.gitignore` | akaLinkPro + toolbox + etm 三份规则并集 | 必须包含：`tmp/`、`captures/`、`records/`、`node_modules/`、`__pycache__/`、`*/build*/`、`build-native/`、`.agent-teams/`、`web/docs/validation/` 下未纳入版本的部分、`baselines/*/execution*.c`、`vendor/OpenCSD/`、`vendor/*.zip`、`firmware/**/build*/`（但保留两条 `!.../output/*.elf` 白名单，见 akaLinkPro `.gitignore` 尾部） |
| `.gitattributes` | toolbox 那份为底 + ETM 二进制规则 | 保留 `tools/fixtures/gen/** -text`、`tools/fixtures/dwarf/** binary`、`samples/anim/*.{gif,apng,webp,mp4,webm} binary`、`samples/test_images/**/*.bmp binary`、`*.bat -text`、`app/dbg/svd/STM32H7*.svd -text -whitespace`；**新增** `etm/baselines/**/*.etmrecord binary`、`*.gz binary`、`*.kvdat binary`、`*.zip binary`（否则 autocrlf 会毁掉这些样本） |
| `Makefile` | akaLinkPro 原样 | 想在根也跑网页任务时再加一个 `web-%:` 转发目标（可选，D-6） |
| `README.md` | 新写 | 指向 `firmware/README.md`、`web/README.md`、`etm/README.md`、`docs/merge-map.md`；写明"不连硬件、不烧录"的默认测试面 |

---

## 3. akaLinkPro 取舍清单

### 3.1 保留（逐项 + 理由）

| 项 | 文件数 | 为什么保留 |
| --- | ---: | --- |
| `firmware/application_5301/src/**` | 79 | 探针主程序（DAP/SWO/RTT 桥/scope/SPI 桥/I2C 桥/ADC/CDC/HID/MSC/DFU/easyflash/riscv） |
| `firmware/application_5301/boards/hpm5301evklite/**` | 8 | **唯一启用扩展桥的板级**：`board.h:179 BOARD_HAS_SPI_BRIDGE (1)`、`:190 BOARD_HAS_I2C_BRIDGE (1)`（HPM_I2C3 / PA28 / PA29）；`BOARD_LED1/2 = PA10`、`BOARD_HAS_USER_KEY_DFU (1)` |
| `firmware/application_5301/linker/flash_dfu_app.ld` | 1 | DFU 内存布局（APP @ `0x80020000`，entry `0x80020100`） |
| `firmware/application_5301/{CMakeLists.txt,build_dfu_evklite.bat,flash_jlink_evklite*.bat,program_evklite.bat,gdb_server.bat}` | 6 | EVKLite 构建/烧录/调试脚本；`build_dfu_evklite.bat` 明确 `BOARD=hpm5301evklite`、`HPM_BUILD_TYPE=flash_xip`、输出 `build_dfu_evklite`（不碰原板产物） |
| `firmware/application_5301/build_dfu_evklite/output/akaLinkPro_App.elf` | 1 | 随仓发布的 APP ELF 快照（`.gitignore` 特意白名单） |
| `firmware/application_5301/src/dap/SW_DP/swd_blob_evklite.h` + `swd_blob/{blob.ld,build.py}` | 3 | EVKLite 专用 SWD bit-bang blob（`CMakeLists.txt:79` 按 `BOARD STREQUAL "hpm5301evklite"` 选用） |
| `firmware/bootloader_dfu/**` | 32 | DFU bootloader（含 `boards/hpm5301evklite/**` 6、`build_xip_evklite.bat`、`build_xip_evklite/output/akaLinkPro_Boot.elf`） |
| `firmware/tools/pack.py`、`firmware/version.json` | 2 | pack 头打包 + 版本号 |
| `script_test/**` | 289 | 探针主机侧验收脚本（`hpm6800_flash_probe.py`、`regression_swd.py`、`i2c_bridge_test.py`、`spi_bridge_test.py` 等）与 7 套靶子固件；根 Makefile 有 20 处引用 |
| `tools/**` | 19 | 面板初始化/SPI 相序扫描/LA 解码（`panel_axs15352.py`、`spi_p4_*`、`kingst_la.py`…）|
| `docs/**` | 170 | 含 `HPM5301EVKLite_port.md`（引脚/构建/升级）、`Custom HID Protocol.md`（HID 协议真源）、`web-handoff-{spi,i2c,riscv-scope}.md`（网页侧契约）、`spi-bridge-wiring.md`（接线）、`HPM5301EVKLite_UG_V1.1.pdf`、`docs/validation/**`（139 个验收记录） |
| `Makefile`、`README.md`（→ `firmware/README.md`）、`LICENSE` | 3 | 一键化目标 / 板型兼容矩阵（README:107-116）/ 许可证 |
| **webusb 相关文件**（全部在上面已覆盖，单列以便核对） | — | `firmware/application_5301/src/usb/**`（9：`usb_composite.c/h`、`usb_config.h`、`hid_interface.c`、`cdc_interface.c/h`、`msc_interface.c`、`swo_rx_clock.c/h`）、`src/api/**`（3：`api_param.c/h`、`service_gate.h`）、`src/rtt/**`（2）、`src/spi_cdc/**`（3）、`src/i2c_bridge/**`（3）、`src/spi_bridge/**`（3）、`src/bus_periodic/**`（3）、`src/analog_bridge/**`（5）、`docs/web-handoff-*.md`（3）、`Custom HID Protocol.md`、`docs/usb-{spi,i2c}-bridge-plan.md`、`docs/usb-analog-page.md`、`docs/hss-tick-protocol.md`、`docs/probe-periodic-bus.md`、`docs/spi-bridge-wiring.md` |

### 3.2 待删项（逐项 + 证据）

| 项 | 文件数/体积 | 证据（可复核） | 结论 |
| --- | ---: | --- | --- |
| `hardware/**` | 11 / 1.03 MB | 全是 KiCad 设计文件：`hardware/akaLinkPro.kicad_{pcb,prl,pro,sch}` + `akaLink.pretty/*.kicad_mod` + `fp-lib-table` + `hpmmuxtool/akaLink.hpmpc`；**全仓检索 `hardware/`、`hardware_pro` 在代码/脚本/文档中 0 处引用**（`Get-ChildItem … | Select-String 'hardware_pro|hardware/'` 无命中） | **不搬** |
| `hardware_pro/**` | 28 / 4.53 MB | 同一块板的新版 KiCad 工程 + 生产文件（`production/{bom.csv,designators.csv,netlist.ipc,positions.csv,akaLinkPro.zip,backups/*.zip}`）+ `3d/*.stl|wrl|STEP`；同样 0 处代码引用 | **不搬** |
| `build/`（磁盘） | 33 / 4 MB | akaLinkPro `.gitignore` 第 5 行 `build`；CMake 生成目录 | 不搬（未被跟踪） |
| `build_dfu/`、`build_xip*/`（磁盘） | — | `.gitignore`：`build_dfu`、`build_xip`、`build_dfu_evklite`、`build_xip_evklite`、`firmware/application_5301/build_uart*/`；只有 `*/output/*.elf` 两个白名单入库 | 不搬 |
| `tmp/`（磁盘） | 402 / 38 MB | `.gitignore` 尾段 `tmp/`；LA 抓包与临时件 | 不搬 |
| `captures/`（磁盘） | 31 | `.gitignore` `captures/`；逻辑分析仪 CSV | 不搬 |
| `script_test/_*`、`script_test/_openocd_probe.log`、`script_test/swd/_*.bin`、`script_test/stm32f103_rtt_speed/{build,build-*}/`（除白名单） | — | `.gitignore` 逐条 | 不搬 |
| `firmware/application_5301/boards/akaLinkPro/**` | 8 | **原板（非 EVKLite）板级**：`board.h:151 BOARD_HAS_SPI_BRIDGE (0)`、`:155 BOARD_HAS_I2C_BRIDGE (0)`、`BOARD_LED1/2 = PB11/PB12`、`BOARD_HAS_SWDIO_DIR (1)`、`BOARD_HAS_JTRST (1)`、`BOARD_HAS_5V_EN (1)`、`BOARD_HAS_VREF_ADC (1)`；只有 `build_dfu.bat:5 set "BOARD=akaLinkPro"` 用到它。README:111 说明"当前板型未开放这些扩展" | **建议不搬**（D-1） |
| `firmware/bootloader_dfu/boards/akaLinkPro/**` | 6 | 同上；`build_xip.bat`/`build.bat` 的 `BOARD=` 为空（回落到 CMake 默认），EVKLite 走 `build_xip_evklite.bat` | **建议不搬**（D-1） |
| `firmware/application_5301/{build_dfu.bat,flash_jlink.bat,program.bat}`（原板脚本，非 EVKLite） | 3 | 与 `*_evklite.bat` 成对；`build_dfu.bat:5 set "BOARD=akaLinkPro"` | **建议不搬**（D-1 同族） |
| `.opencode/skills/akalinkpro-firmware/**` | 14 / 124 KB | AI 技能配置（`SKILL.md` 等），与构建/测试无关 | **建议不搬**（D-3） |

### 3.3 拿不准 → 交用户拍板

> akaLinkPro 侧相关的三个（D-1 原板板级、D-3 `.opencode`、D-5 `script_test` 瘦身）列在下面；
> **D-2（ETM 678 MB 固件）、D-4（`docs/` 是否收敛）、D-6（git 历史 / 单份工具）**见 §2.2C、§2.2D、§6.0，
> 全部 Decision 汇总在 **§8**。

| 编号 | 问题 | 选项 | 建议 |
| --- | --- | --- | --- |
| **D-1** | `boards/akaLinkPro`（原板）与 3 个原板 .bat 留不留？ | A 删（只留 EVKLite）；B 留（可编原板） | **A**：目标是 EVKLite 调试器；删后 CMake 板搜索路径仍有 `hpm5301evklite`，构建不受影响；`build_dfu.bat` 一起删可避免误编原板 |
| **D-3** | `.opencode/skills/**` 留不留？ | A 删；B 留（AI 助手提示词） | **A**：与工程构建无关，且属于某个 AI 工具私有约定 |
| **D-5** | `script_test/` 里 115 个与 `web/tools/target-firmware` 逐字节相同的文件怎么处理？ | A 两份都留（+9.5 MB，零风险）；B 删除 `script_test/` 下的重复靶子，改动 20 处 Makefile 引用 | **A（本次）**，B 另开任务（§4.5 给了精确清单，可一键删） |

---

## 4. 重复文件冲突清单（本次合并的核心）

### 4.1 探针驱动家族：一份逻辑、多份拷贝

以 SHA-256 实测（工作区字节，**不是 git blob**——三仓 `core.autocrlf=true`，blob 被规整成 LF，直接比 blob 会
把"内容相同但行尾不同"误判成 DIFF，本次先踩后纠）：

| 文件 | `web/app/**`（toolbox 原位） | `etm/web/app/shared/**` | `web/bridge/etm/backend/web/app/shared/**`（派生快照） | 结论 |
| --- | --- | --- | --- | --- |
| `rtt/dap-webusb.js` | 1567 行 / 90,022 B | 1519 行 / 88,174 B | **与 ETM 版逐字节相同** | **两版分叉，必须合并**（§4.8） |
| `core/usb-device.js` | 161 行 / 7,532 B | 155 行 / 7,116 B | 与 ETM 版相同 | **toolbox 版更新**（多 6 行） |
| `core/probe-bus.js` | 182 行 / 9,793 B | 182 行 / 9,793 B | 相同 | **三份一致** |
| `core/pace.js` | 62 行 / 4,439 B | 62 行 / 4,439 B | 相同 | 三份一致 |
| `core/bin.js` | 43 行 / 1,663 B | 43 行 / 1,663 B | 相同 | 三份一致 |
| `dbg/bp.js` | 94 行 / 4,613 B | 94 行 / 4,613 B | 相同 | 三份一致 |
| `dbg/source.js` | 318 行 / 17,154 B | 318 行 / 17,154 B | 相同 | 三份一致 |

#### 4.1.1 `dap-webusb.js` 差异摘要（关键函数在不在）

导出面完全相同（`CMD`、`withTimeout`、`WebUsbDapProbe`），实例方法 ETM 页实际用到的是
`readMemDiagnostic / writeMemDiagnostic / recoverDebugPort / run / halt / isHalted / disconnect / onLog` + 内部
`_faulted / _recovering` —— **两版都有**，所以 API 层可互换，差异在"错误处理策略"。

| 特性 | toolbox 版（`app/rtt/dap-webusb.js`） | ETM 版（`web/app/shared/rtt/dap-webusb.js`） |
| --- | --- | --- |
| 提交量 | **43 次**（末次 `b5dfee2` 2026-10-16） | 4 次（末次 `3ccce81` 2026-10-17） |
| `OperationBudget` 分阶段超时 | **有**（`_budgeted` / `_operationBudget` 13 处引用，"连接探针 / 认领接口 / 协商时钟 / DAP 命令 xxx"逐段倒数） | **无**（0 处） |
| DAP 会话失效跟踪 | **有**：`_targetNeedsReconnect`(7 处) + `_dapPort` + `DAP_SESSION_LOST` 错误码（USB 自愈后对 Transfer 类命令直接报"连接已失效，请重连目标"） | **无** |
| `WriteABORT` 报文 | `Uint8Array.of(0,0x1e,0,0,0)` = **DAP index + 4 字节 ABORT**（CMSIS-DAP 规范形状） | `Uint8Array.of(0x1e,0,0,0)` = **少了 DAP index 字节**（⚠️ 疑似缺陷，**不要抄过去**） |
| `resetDevice()` 失败处理 | lease 分支 `await lease.reset()` **会抛**（异常一路上抛到 `_claim()`）；非 lease 分支只 `console.warn` | **永不抛**：`withTimeout(lease.reset(),3000)` 包 try/catch，失败只 `console.warn`；文件头有大段真机注释（pyOCD 打开探针根本不 reset；Windows 上这颗复合探针 `reset()` 报 `Unable to reset the device`，原来没有 try/catch → 页面停在"已连接但无通信"） |
| 清陈旧响应策略 | `_claim()` 里 `clearHalt(dir, ep)` ×2 + `resync()` | 同上，但**加了禁令注释**："不要用一串短超时 transferIn 去排空 —— WebUSB 没有取消接口，超时的那次会一直挂着把后续响应偷走"（来自 `3ccce81` "撤掉会制造挂起传输的 drain 循环"） |
| FAULT 自愈位置 | `_healFaultLocked()` 在 `_withLock(...)` **内**（`_targetInit` 只走私有 `_ctrl/_transfer`，不会重入，安全） | `_healIfFaulted()` 在锁**外**，先清 `_faulted` 再 `_recovering=true`，并累计 `faultHeals`；注释理由"`_targetInit` 内部会用公开读写路径，锁里调用会自锁" |
| AP 探测 / CPUID 核验 | 3 个 CSW 候选 × APSEL `[0,2,1,3]`，`(v>>>24)===0x41 && ((v>>>16)&15)===15 && ((v>>>4)&4095)` 才算命中；初始化后再用 `_readMemOnce(0xE000ED00) → CPUID` 复核，失败置 `_faulted` | 相同算法但无后置复核段（`CPUID` 出现 3 次 vs toolbox 5 次） |
| 其他 | — | `_recovering` 20 处（与 toolbox 同）、`skipClearHalt` 5 处（同） |

#### 4.1.2 `usb-device.js` 差异（toolbox 是新的一侧）

唯一差异 = toolbox 多了一套"USB 资源冲突"语义（6 行）：

```js
// toolbox 版独有
export function usbResourceConflict(message){ return Object.assign(new Error(message), { code:'USB_RESOURCE_BUSY' }); }
export const isUsbResourceConflict = error => error?.code === 'USB_RESOURCE_BUSY';
...
try { await resetGuard?.(this.owner, this.device); }
catch (error){ if (error instanceof Error){ error.code ||= 'USB_RESOURCE_BUSY'; throw error; }
               throw usbResourceConflict(String(error)); }
```

ETM 版对应位置直接 `throw new Error(...)`。toolbox 侧这次改动来自 `00b34d0`（2026-10-16 第二轮审查修复 F01–F13）。

#### 4.1.3 建议的唯一真源位置

| 层次 | 位置 | 说明 |
| --- | --- | --- |
| **真源** | `web/app/rtt/dap-webusb.js`、`web/app/core/{usb-device,probe-bus,pace,bin}.js`、`web/app/dbg/{bp,source}.js`（共 **7 个文件**） | 保持 toolbox 现有路径不动物理位置，Makefile/打包脚本的引用全部不变 |
| **副本（生成物）** | `etm/web/app/shared/{rtt/dap-webusb,core/{usb-device,probe-bus,pace,bin},dbg/{bp,source}}.js` | 由新脚本 `web/tools/dev/sync-shared-drivers.mjs` 复制（纯函数、可 `--check`）；ETM 页的 `import './shared/…'` 一字不改 |
| **守护** | `web/tools/selftest/shared-drivers.test.mjs`（新，挂进 `make test`） | 用 sha256 断言"副本 == 真源"，模式照抄现成的 `tools/dev/embed-bridge.mjs` + `bridge-kit.test.mjs`（桥源码漂移守卫） |
| **不采用的方案** | ETM 页直接 `import '../../../../web/app/rtt/dap-webusb.js'` | ❌ 会让 ETM 页**无法独立启动**（`etm/start-web.ps1` 只服务 `etm/` 树），也进不了便携桥包（`bridge/etm/backend/` 是独立快照）；且 `serve-etm.py` 只改写 HTML 的 `href/src`，不改 JS `import` |

### 4.2 ETM 页面整棵树 vs `web/bridge/etm/backend/**` 快照

| 事实 | 数值/证据 |
| --- | --- |
| 快照规模 | `bridge/etm/` **64 文件 / 36,963,269 B**（含 `downloads/etm-bridge-win64.zip` ≈24 MB 与 `downloads/manifest.json`） |
| 快照来源 | `bridge/etm/provenance.json`：`sourceRepository=web-etm-trace`、`sourceRevision=a2922f8ec33c…`（ETM 上一个提交；ETM HEAD `12746fc` 只改了 `firmware/stm32h743_cdc_trace/fw.hex`） |
| 逐字节核对 | **24 个 `web/**` 文件、10 个 `tools/*.py`、`requirements.txt`、`vendor/OpenCSD-1.8.3/LICENSE` 全部与 ETM 当前文件逐字节相同**（SHA-256 逐对相等） |
| 两处**故意**的本地补丁 | `server.py`（+2/−1）：`import os` + `RECORDS = Path(os.environ.get('WEB_ETM_RECORDS', str(ROOT/'records')))`；`native/decoder.cpp`（+29/−3）：Win32 `wmain` + UTF-16 argv/文件路径（中文路径与 UTF-8 清单） |
| 生成链 | `tools/dev/sync-etm-runtime.py --etm-root <ETM>` 复制 12 个后端 py + 整个 `web/` + `native/decoder.cpp` + `build-native/etm_decode.exe` + 3 份示例 ELF + Demo，打补丁，重编原生解码器，写 `provenance.json`；`tools/dev/build-etm-package.py` 再出 zip + manifest |
| 结论 | 合并后 `web/bridge/etm/**` **仍是派生快照**，唯一改法：`--etm-root ../etm`（§2.4 已列）。**禁止**再手工编辑 `web/bridge/etm/backend/**` 里的源码（会和 `etm/` 漂移） |

### 4.3 「打包器补丁」类重复（不是内容重复，是**同一文件的两个变体**）

| 文件 | 原始（`etm/`） | 打包副本（`web/bridge/etm/backend/`） | 处理 |
| --- | --- | --- | --- |
| `server.py` | 无 env 覆盖：`RECORDS = ROOT/'records'` | `WEB_ETM_RECORDS` 环境变量重定向 | 由 `sync-etm-runtime.py` 的文本替换生成，不手改 |
| `native/decoder.cpp` | 单一 `main()`，窄字符 `argv` | `decode_main()` + Win32 `wmain()` + `file_path()` 宽字符转换 | 同上（脚本内联生成，重编 `etm_decode.exe` 用 msys64 g++ 静态链接） |

### 4.4 `target.js`：同名不同物，别误当重复

- `etm/web/app/target.js`（321 行 / 25,126 B）= **ETM 目标抽象**（`EtmTarget`：SWD 访问、断点、ETM/TPIU 寄存器、
  采集生命周期），依赖 `./shared/rtt/dap-webusb.js`、`./shared/core/probe-bus.js`、`./shared/dbg/bp.js`、
  `./shared/core/pace.js`、`./clock.js`、`./breakpoints.js`、`./operation.js`、`./target-f103.js`、`./trace-options.js`。
  **toolbox 没有对应物** —— 所以它是 ETM 独有源码，跟着 `etm/web/` 走，不参与去重。
- `web/app/swo/target.js`（30 行 / 3,462 B）= SWO 页的"探针目标类型描述"，与 ETM 无关。
- 真正的耦合点是上面 4 个 `shared/` **import 说明符**：去重方案必须保持 `etm/web/app/shared/**` 这 7 个路径存在（§4.1.3）。

### 4.5 目标固件与 SEGGER RTT 的多份拷贝（跨仓重复）

按 SHA-256 逐文件比对，**内容相同**的跨仓重复共：

| 对 | 相同文件数 | 分布 |
| --- | ---: | --- |
| akaLinkPro `script_test/**` ↔ toolbox `tools/target-firmware/**` | **115** | `stm32h7b0_rtt_speed` 59、`stm32f103_rtt_speed` 12、`stm32h7b0_scope` 8、`stm32h743_dbgstress` 7、`stm32h743_rtt_speed` 6、`stm32h743_scope` 6、`stm32h743_fault` 5、`hpm6800evk_scope` 4、`hpm6800evk_rtt_flood` 3、`stm32f103_scope` 2、`docs/uart-200mhz-acceptance.json`↔`samples/swo/baud-200mhz-acceptance.json` 1、`stm32f103_rtt_speed/src/stm32f103_regs.h`↔`tools/fixtures/readme/…` 1 |
| akaLinkPro ↔ ETM `firmware/**` | **60** | `script_test/stm32h7b0_rtt_speed` ↔ `firmware/stm32h7b0_etm`+`stm32h743_cdc_trace/sdk`+`docs/provenance/*` 等（ETM 明确写了"固件底座来自 toolbox 的 `stm32h7b0_rtt_speed`，README/实测记录存 `docs/provenance`"） |
| toolbox ↔ ETM | **103 个内容哈希组**（在 ETM 侧展开为 **249 个 `firmware/**`** + 25 个 `web/**` + 19 个其它路径） | `firmware/**` 那 249 个是 HAL/CMSIS/SEGGER RTT 等**库文件**在 ETM 的 5 个固件目录里各存一份（同一个 `stm32h7xx_hal_rcc.c` 在 `stm32h743_cdc_trace`、`stm32h7b0_cdc_trace`、`stm32h7b0_etm`、`stm32h7b0_etm_board_v1`、`stm32h7b0_spi_trace` 下各一份，且与 toolbox `stm32h7b0_rtt_speed/sdk` 逐字节相同）；`web/**` 那 25 个就是 §4.2 的页面快照 |

**建议**：目标固件的**活副本**是 `web/tools/target-firmware/**`（202 个文件、14.3 MB，被 `web/Makefile` 大量引用）；
`akaLinkPro/script_test/**` 按任务要求整体保留（+9.5 MB，零风险），但**知道它有 115 个文件是重复的**；
ETM 的 `firmware/**` 默认只搬两个 ETM 靶子工程（D-2），其余不搬。

### 4.6 `kingst_la.py`：反向不一致（ETM 更新）

| | toolbox `tools/la/kingst_la.py` | ETM `tools/la/kingst_la.py` |
| --- | --- | --- |
| 行数/字节 | 942 行 / 46,480 B | **1068 行 / 54,079 B** |
| 末次提交 | `9a8abf4` 2026-09-30 | `292939b` 2026-10-15 |
| 函数集 | 27 个 | **31 个** = toolbox 全部 + `read_glitch_settings`、`glitch_preset_bytes`、`kingst_running`、`cmd_glitch_preset`（毛刺滤波预置、运行状态查询） |

→ **ETM 版是超集**，回灌到 `web/tools/la/kingst_la.py`（toolbox 侧 `Makefile` 的 `LA = tools/la/kingst_la.py`
与 `la-capture`/`la-info` 目标都指向它）。回灌后跑 `make la-info` 冒烟（不连硬件时会报 Socket 未连，属预期）。

### 4.7 建议的唯一真源总表

| 内容 | 唯一真源 | 副本/派生 | 守护手段 |
| --- | --- | --- | --- |
| 探针驱动 7 文件 | `web/app/{rtt/dap-webusb,core/{usb-device,probe-bus,pace,bin},dbg/{bp,source}}.js` | `etm/web/app/shared/**`（生成） | 新增 `sync-shared-drivers.mjs` + `shared-drivers.test.mjs`（sha256） |
| ETM 后端 + 页面 | `etm/{server.py,tools,web,native,vendor,baselines,configs}` | `web/bridge/etm/backend/**`（生成）、`web/bridge/etm/downloads/*.zip`（打包） | 现成的 `sync-etm-runtime.py --etm-root ../etm` + `provenance.json` + `build-etm-package.py` |
| 工具箱前端 bundle | `web/app/**`（源码） | `web/app/site/main.js`（esbuild 产物，已入库） | `web/tools/dev/build-web.mjs`（会校验 `index.html` 里的 `?v=<sha16>` 与 manifest 一致，`make check-web`） |
| 便携 RTT 桥 | `web/bridge/rtt-bridge.mjs` | `web/app/gen/bridge-src.js`（嵌入） | 现成的 `embed-bridge.mjs` + `bridge-kit.test.mjs`（sha256） |
| 目标固件 | `web/tools/target-firmware/**` | `script_test/**`（历史副本，115 个重复） | 本表记录，不再新增第三份 |
| LA 工具 | `web/tools/la/**`（含回灌后的 `kingst_la.py`） | `etm/tools/la/**`（ETM 采集侧） | 本表记录；改了任一侧要同步（D-6） |

### 4.8 合并 `dap-webusb.js` 前必须先做的三件事（顺序不能反）

1. **把 ETM 侧的 3 项真机修复移植进 `web/app/rtt/dap-webusb.js`**：
   - (a) `resetDevice()` **永不抛**：lease 分支包 `withTimeout(...,3000)` + try/catch，失败只告警
     （ETM 注释给出的真机依据：这颗复合探针在 Windows 上 `USBDevice.reset()` 必失败；pyOCD 打开探针根本不 reset）；
   - (b) 保留"不要用短超时 transferIn 排空"的注释与其后的 `clearHalt + resync` 唯一路径（`3ccce81`）；
   - (c) 在 `resetDevice` 失败后仍继续 `claim`（现有 toolbox 流程已如此，但要确认不会因 (a) 的改动而退化成抛错）。
2. **反向修正**：**不要**把 ETM 的 `abort()` 实现（`Uint8Array.of(0x1e,0,0,0)`，缺 DAP index）搬过来；
   toolbox 的 `Uint8Array.of(0,0x1e,0,0,0)` 才是规范形状（`DAP_WriteABORT` = 命令 + DAP Index + 4 字节 ABORT）。
   移植后跑一次 `make test-hid` + `make test-dbg`（纯 Node，覆盖驱动状态机）。
3. **替换副本**：`Copy-Item web/app/rtt/dap-webusb.js etm/web/app/shared/rtt/dap-webusb.js`，
   `usb-device.js` 同理（toolbox 版为准），其余 5 个文件本来逐字节相同、直接覆盖即可；
   然后 `node web/tools/dev/sync-shared-drivers.mjs --check` 应为绿，`node web/tools/selftest/shared-drivers.test.mjs` 全过。

> ⚠️ 不要顺手把 ETM 的 `_healIfFaulted()`（锁外）搬进 toolbox 版：toolbox 的 `_healFaultLocked()` 在锁内是**有意的**
> （它的 `_targetInit` 只走私有 `_ctrl/_transfer`，不会重入公开读写路径）。两套写法各自自洽，混用会自锁或漏清 `_faulted`。

---

## 5. 未提交改动清单

### 5.1 web-serial-rtt-tools：`git status --porcelain` **512 行**（`-uall` 展开 4221 项 = 8 改 + 4213 未跟踪）

**A. 8 个已修改的跟踪文件（都是真源码，必须带走）**

| 文件 | 差异 | 归属 | 搬运方式 |
| --- | --- | --- | --- |
| `app/etm/style.css` | +1/−1 | 真源码 | 随 `web/` 整树复制（含工作区未提交状态） |
| `docs/etm-integration.md` | +13/−0 | 文档 | 同上 |
| `start-etm.bat` | +17/−6 | 真源码（启动脚本） | 同上；**搬后还要按 §2.4 改 `..\web-etm-trace`** |
| `tools/dev/build-etm-package.py` | +8/−5 | 真源码（打包器） | 同上 |
| `tools/dev/sync-etm-runtime.py` | +3/−3 | 真源码（快照器） | 同上 |
| `tools/selftest/etm-history-races.test.mjs` | +9/−0 | 测试 | 同上 |
| `tools/selftest/etm-ui-harness.mjs` | +1/−1 | 测试 | 同上 |
| `tools/selftest/etm-ui-sequences.test.mjs` | +10/−0 | 测试 | 同上 |

> 全部 8 个都是 ETM 相关（本轮另一路 AI 在改 ETM 内嵌页），**不是**同步/打包产物。
> 若采 §6.0 方案 B（subtree 带历史），这 8 个文件的改动**不会**被 subtree 带过去（subtree 只取 HEAD），
> 必须在搬完后用 `Copy-Item` 单独覆盖（§6.2 步骤 4）。

**B. 4213 个未跟踪文件（默认 504 个目录条目）**

| 分组 | 文件数 | 性质 | 结论 |
| --- | ---: | --- | --- |
| `docs/validation/**` | 4,206 | **验收产物**：截图/BMP/PNG、ETM 记录（`.etmrecord`/`.kvdat`/`elf`）、`.patch`、日志、以及一份**「独立 桥包」全套副本**（`portable-validation/独立 桥包-174e4d/etm-bridge-kit/{runtime,toolbox}`，含 Python 运行时与工具箱整树） | **不搬**（整目录磁盘 3,532 MB；已跟踪的 894 个照搬） |
| `.agent-teams/**` | 2 目录 | **本合并任务的团队状态**（`superlinkpro-merge/team.json`、`archive/wsrtt-review`） | **绝不搬** |
| `validation/etm/**` | 2 | `run-20261016-062929-1bc546/result.json`、`run-20261016-081200-a34c5e/result.json`（ETM 测试运行结果） | 不搬（可重新生成） |
| `docs/h743-web-review-2026-10-17.md` | 1 | **真文档**（H743 网页侧审查，2026-10-17） | **搬**到 `web/docs/` |
| `codex-log.txt`、`ds-log.txt` | 2 | AI 会话日志 | 不搬 |

### 5.2 akaLinkPro：**2 项**（都未跟踪，无已修改文件）

| 项 | 内容 | 性质 | 结论 |
| --- | --- | --- | --- |
| `.zcodeignore` | 另一个 AI 工具的忽略配置 | 工具私有 | 不搬（D-3 同族） |
| `docs/ai-guides/` | `Git日志与验证证据全局约定.md`、`双AI结对编程通用指导.md` | **协作文档**（另一路 AI 写的），未跟踪但内容有价值 | 建议搬到 `docs/ai-guides/`（可选；不影响构建） |

### 5.3 web-etm-trace：**2 项**

| 项 | 状态 | 内容 | 结论 |
| --- | --- | --- | --- |
| `firmware/stm32h743_cdc_trace/fw.hex` | `M`（+12/−12 行） | 2026-10-17 提交 `12746fc` 之后又重建的 hex（"`.hex` 改为由稠密 `.bin` 生成"） | **产物**：可重建（`firmware/stm32h743_cdc_trace/build.ps1`）。若搬 `firmware/` 则该目录默认不搬（D-2），无需处理；若决定搬，取工作区这份（比 HEAD 新） |
| `firmware/_archive_stm32f103ze_cdc_trace_20261012/src/boot_probe.c.bak` | `??` | 手工备份文件 | 不搬 |

### 5.4 搬运方式总规则

1. **已修改的跟踪文件**：用 `Copy-Item` 从**工作区**取（不要用 `git archive`/`subtree`，它们只取 HEAD）。
2. **未跟踪但保留的少数项**（`web/docs/h743-web-review-2026-10-17.md`、可选 `docs/ai-guides/**`）：显式逐文件拷。
3. **其余未跟踪/忽略项**：一律不拷，并在新仓 `.gitignore` 里继续忽略（§2.6）。
4. 搬完后用 §7 的对账命令证明：**跟踪集合 = 目标集合（除显式例外）**。

---

## 6. 执行步骤（照抄即可）

### 6.0 先选搬运方式

| 方式 | 命令 | 得到什么 | 代价 |
| --- | --- | --- | --- |
| **A. 纯文件搬迁（推荐先做）** | §6.2 的 `Copy-Item` 循环 | 目标目录内容 = 三仓 HEAD+工作区差异，**无 `.git`** | 无 git 历史 |
| **B. subtree 带历史** | `git init E:\superlinkPRO` → `git -C E:\superlinkPRO remote add aka E:\Share\github\akaLinkPro` … → `git subtree add --prefix=web toolbox main --squash=false` 等 | 三仓历史并入一条线（original commit 可见） | 需要先 `git init`；subtree 只取 HEAD，**8 个未提交改动要另拷**（§5.1A）；ETM 源分支是 **`master`**，别写成 `main` |

> 两种方式都以 §2.2 的"路径映射"为准；B 只是多了一层历史注入，**搬完同样要执行 §6.3~§6.7**。

### 6.1 准备

```powershell
New-Item -ItemType Directory -Force -Path E:\superlinkPRO, E:\superlinkPRO\docs, E:\superlinkPRO\firmware, E:\superlinkPRO\web, E:\superlinkPRO\etm | Out-Null
# 记录源头版本，写进迁移日志
foreach ($r in 'E:\Share\github\akaLinkPro','E:\web-serial-rtt-tools','E:\web-etm-trace') {
  "$r`t$(git -C $r rev-parse --abbrev-ref HEAD)`t$(git -C $r rev-parse HEAD)" | Tee-Object -Append E:\superlinkPRO\docs\source-revisions.txt
}
```

### 6.2 三次复制（用 §1.4 的原语；**带存在性断言**）

```powershell
function Copy-Tracked([string]$src,[string]$dst,[string[]]$exclude = @()) {
  $prev = [Console]::OutputEncoding; [Console]::OutputEncoding = [Text.Encoding]::UTF8
  $list = git -C $src -c core.quotepath=false ls-files
  [Console]::OutputEncoding = $prev
  $n = 0; $skipped = @()
  foreach ($rel in $list) {
    if ($exclude | Where-Object { $rel -like $_ }) { $skipped += $rel; continue }
    $s = Join-Path $src ($rel -replace '/','\'); $d = Join-Path $dst ($rel -replace '/','\')
    if (Test-Path -LiteralPath $d) { throw "目标已存在（说明映射有冲突，停下核对）：$d" }
    $dd = Split-Path $d -Parent
    if (-not (Test-Path -LiteralPath $dd)) { New-Item -ItemType Directory -Force -Path $dd | Out-Null }
    Copy-Item -LiteralPath $s -Destination $d -Force; $n++
  }
  "copied $n from $src；skipped $($skipped.Count)"
}

# ① akaLinkPro → 根（按 §2.2A / §3.2）
Copy-Tracked 'E:\Share\github\akaLinkPro' 'E:\superlinkPRO' @(
  'hardware/*','hardware_pro/*','.opencode/*',
  'firmware/application_5301/boards/akaLinkPro/*',
  'firmware/bootloader_dfu/boards/akaLinkPro/*',
  'firmware/application_5301/build_dfu.bat',
  'firmware/application_5301/flash_jlink.bat',
  'firmware/application_5301/program.bat'
)
# 根 README 改名（保留原固件 README）
Move-Item E:\superlinkPRO\README.md E:\superlinkPRO\firmware\README.md -Force
# .gitignore 不单独搬（并入 §2.6）
Remove-Item E:\superlinkPRO\.gitignore -Force

# ② toolbox → web\（整树，排除团队状态与未跟踪产物）
Copy-Tracked 'E:\web-serial-rtt-tools' 'E:\superlinkPRO\web'
# 额外带走 1 个未跟踪文档
Copy-Item 'E:\web-serial-rtt-tools\docs\h743-web-review-2026-10-17.md' 'E:\superlinkPRO\web\docs\' -Force

# ③ ETM → etm\（默认不含 678 MB 的 firmware；D-2 若拍板"全搬"则去掉下面 exclude）
Copy-Tracked 'E:\web-etm-trace' 'E:\superlinkPRO\etm' @(
  'firmware/*'          # 默认全不搬；推荐单搬两个 ETM 靶子工程，见下
)
# 推荐：只搬「跟踪的」136 个靶子固件文件（不要用 Copy-Item -Recurse：这些目录里有 gitignored 的 build/ 产物，
#       磁盘上 149 个文件 vs 跟踪 136 个）
$prev = [Console]::OutputEncoding; [Console]::OutputEncoding = [Text.Encoding]::UTF8
$keep = git -C E:\web-etm-trace -c core.quotepath=false ls-files -- firmware/stm32h7b0_etm firmware/stm32h7b0_etm_board_v1 firmware/tools firmware/Makefile firmware/README.md
[Console]::OutputEncoding = $prev
foreach ($rel in $keep) {
  $s = Join-Path 'E:\web-etm-trace' ($rel -replace '/','\')
  $d = Join-Path 'E:\superlinkPRO\etm' ($rel -replace '/','\')
  $dd = Split-Path $d -Parent; if (-not (Test-Path -LiteralPath $dd)) { New-Item -ItemType Directory -Force -Path $dd | Out-Null }
  if (Test-Path -LiteralPath $d) { throw "目标已存在：$d" }
  Copy-Item -LiteralPath $s -Destination $d -Force
}
"kept ETM firmware: $($keep.Count) files (expect 136)"
# 删掉重复的那份「逻辑分析仪攻略」（内容与 web/docs 那份逐字节相同）
Remove-Item 'E:\superlinkPRO\etm\docs\逻辑分析仪攻略.md' -Force
```

### 6.3 覆盖 8 个未提交改动（方案 B 必做；方案 A 已在 6.2 的 Copy-Item 里自然带上）

```powershell
$fixes = @('app/etm/style.css','docs/etm-integration.md','start-etm.bat',
           'tools/dev/build-etm-package.py','tools/dev/sync-etm-runtime.py',
           'tools/selftest/etm-history-races.test.mjs','tools/selftest/etm-ui-harness.mjs',
           'tools/selftest/etm-ui-sequences.test.mjs')
foreach ($f in $fixes) { Copy-Item "E:\web-serial-rtt-tools\$($f -replace '/','\')" "E:\superlinkPRO\web\$($f -replace '/','\')" -Force }
```

### 6.4 按 §2.4 改 5 个文件的路径引用（10 行左右）

```powershell
$repl = @(
  @{p='E:\superlinkPRO\web\tools\dev\serve-etm.py';            from="ROOT.parent/'web-etm-trace'"; to="ROOT.parent/'etm'"},
  @{p='E:\superlinkPRO\web\tools\dev\start-etm.ps1';           from="'..\..\..\web-etm-trace'";     to="'..\..\..\etm'"},
  @{p='E:\superlinkPRO\web\tools\selftest\etm-integration-hw.mjs'; from="'../web-etm-trace'";        to="'../etm'"}
)
foreach ($r in $repl) {
  $t = Get-Content -LiteralPath $r.p -Raw
  if ($t -notlike "*$($r.from)*") { throw "未找到待替换串：$($r.p)" }
  # 用 WriteAllText：默认 UTF-8 无 BOM，且不改行尾（Set-Content 在 PS5 会加 BOM，.bat/.mjs 首行可能被它弄坏）
  [System.IO.File]::WriteAllText($r.p, ($t -replace [regex]::Escape($r.from), $r.to))
}
# start-etm.bat 里 5 处 ..\web-etm-trace → ..\etm（批量）
$b = 'E:\superlinkPRO\web\start-etm.bat'
[System.IO.File]::WriteAllText($b, ((Get-Content -LiteralPath $b -Raw) -replace '\.\.\\web-etm-trace','..\etm'))
# 文档里的描述（可选）
"web\bridge\etm\README.md: --etm-root E:/web-etm-trace → ../etm"
"web\docs\etm-integration.md: ../web-etm-trace/… → etm/…（改成'本仓库 etm/ 子树'）"
```

### 6.5 统一探针驱动（按 §4.8 顺序做）

```powershell
# 1) 人工把 ETM 的 3 项修复移植进 web\app\rtt\dap-webusb.js（见 §4.8 清单）
# 2) 以 toolbox 版覆盖 ETM 侧副本（7 个文件）
$pairs = @('rtt\dap-webusb.js','core\usb-device.js','core\probe-bus.js','core\pace.js','core\bin.js','dbg\bp.js','dbg\source.js')
foreach ($p in $pairs) { Copy-Item "E:\superlinkPRO\web\app\$p" "E:\superlinkPRO\etm\web\app\shared\$p" -Force }
# 3) 新增 sync-shared-drivers.mjs（复制 + --check）与 shared-drivers.test.mjs（sha256 断言），挂进 web/Makefile 的 test
```

### 6.6 新建/合并根级文件（§2.6）

```powershell
# .gitignore：三份并集（务必含 tmp/ captures/ records/ node_modules/ __pycache__/ */build*/ build-native/ .agent-teams/）
# .gitattributes：toolbox 那份 + etm 二进制规则（etm/baselines/**/*.etmrecord binary、*.gz binary、*.zip binary）
# README.md：新写总说明
# Makefile：akaLinkPro 原样（已在 6.2 拷好）
```

### 6.7 收尾校验（不连硬件）

```powershell
# a) 三棵子树的纯 Node / 纯 Python 测试
cd E:\superlinkPRO\web ;  make test            # 纯 Node 套件；CDP 类需 8899/9333，本次不要求
cd E:\superlinkPRO     ;  make test-host       # 固件逻辑回归（无需硬件）
python -m unittest discover -s E:\superlinkPRO\etm\tools\tests      # ETM 后端
node --test E:\superlinkPRO\etm\tools\tests\*.test.mjs
# b) 语法/守卫
cd E:\superlinkPRO\web ;  make check           # check-syntax + check-liquid（所有 .md 不得含 Liquid 定界符）
```

---

## 7. 验收（对账命令 + 期望数字）

| # | 检查 | 命令 | 期望 |
| --- | --- | --- | --- |
| 1 | 根固件子树文件数 | `(Get-ChildItem E:\superlinkPRO\firmware -Recurse -File).Count` | `111+32+2 = 145`（含 `firmware/README.md` 时 146） |
| 2 | `script_test` 完整 | `(Get-ChildItem E:\superlinkPRO\script_test -Recurse -File).Count` | **289** |
| 3 | `tools`（根） | `(Get-ChildItem E:\superlinkPRO\tools -Recurse -File).Count` | **19** |
| 4 | 根 `docs` | `(Get-ChildItem E:\superlinkPRO\docs -Recurse -File).Count` | **170**（+ 新增 merge-map.md / source-revisions.txt） |
| 5 | `web` 整树 | `(Get-ChildItem E:\superlinkPRO\web -Recurse -File -Force).Count` | **2035** +1（未跟踪的 h743 审查文档）= 2036 |
| 6 | `etm`（推荐子集） | `(Get-ChildItem E:\superlinkPRO\etm -Recurse -File).Count` | `5472−5064+136 = 544`（含 5 个根文件；再减去删掉的 1 份 逻辑分析仪攻略 → 543） |
| 7 | 总跟踪字节 | `(Get-ChildItem E:\superlinkPRO -Recurse -File | Measure-Object Length -Sum).Sum` | **160,855,216 B（153 MB）** = akaLinkPro 子集 19,710,551 + web 112,425,879 + etm 子集 28,718,786（不含任何 ignored 产物） |
| 8 | **逐文件内容对账** | 对每个源仓：`git -C <src> -c core.quotepath=false ls-files` 的每个文件，比较 `(Get-FileHash $src/$f).Hash` 与 `(Get-FileHash $dst/$mapped).Hash` | **全部相等**（唯一允许的例外：`README.md→firmware/README.md` 改名、`etm/docs/逻辑分析仪攻略.md` 删除、§6.2 的 exclude 列表） |
| 9 | 零覆盖 | 6.2 的 `Copy-Tracked` 里 `Test-Path $d → throw` 从未触发 | 无异常 |
| 10 | 无 ignored 产物混入 | `(Get-ChildItem E:\superlinkPRO -Recurse -Directory).Name` 中不得出现 `captures`/`records`/`tmp`/`node_modules`/`build-native`/`.agent-teams`/`__pycache__` | 只有 `firmware/**/build*/` 之外的构建目录为空 |
| 11 | ETM 快照可再生成 | `python web/tools/dev/sync-etm-runtime.py --etm-root E:\superlinkPRO\etm` 后 `git diff --stat`（若已 init） | 只有 `provenance.json` 的 revision/哈希变化，源码零差异 |
| 12 | 驱动副本同步 | `node web/tools/dev/sync-shared-drivers.mjs --check` | 7 个文件 sha256 全部匹配 |

---

## 8. Decision 汇总（需用户拍板）

| 编号 | 问题 | 默认（本文按此写） | 影响 |
| --- | --- | --- | --- |
| **D-1** | akaLinkPro 的 `boards/akaLinkPro`（17 文件）与 3 个原板 .bat 留不留 | **删**（只保留 `hpm5301evklite`） | 删后无法再编"原板 akaLinkPro"（SPI/I2C 桥在它上面是关的）；EVKLite 构建完全不受影响 |
| **D-2** | ETM 的 `firmware/**`（5064 文件 / 678 MB）搬不搬 | **只搬** `stm32h7b0_etm` + `stm32h7b0_etm_board_v1` + `firmware/{Makefile,README.md,tools}` = **136 文件 / 12.5 MB**；其余（5 个带 SDK 的固件工程 + 归档 + `_compare`）不搬 | 不搬则 ETM 的「按 README 重建 H7B0 ETM 固件」需回原仓；搬则 +678 MB，且其中 **249 个 `firmware/**` 文件与 `web/tools/target-firmware` 的 HAL/CMSIS 库逐字节相同**（§4.5），属纯冗余 |
| **D-3** | akaLinkPro `.opencode/skills/**`（14）与 `.zcodeignore` | **不搬** | 与构建无关；属于某个 AI 工具私有约定 |
| **D-4** | `docs/` 是否收敛成单根（`docs/{firmware,web,etm}/`） | **不收敛**：根 `docs/` 只放固件文档 + 合并文档，`web/docs`、`etm/docs` 原位 | 收敛要改 `web/**` 里 25 处 `docs/` 引用 + Makefile/`check-liquid`/截图脚本，收益低、风险高 |
| **D-5** | `script_test/**` 里 115 个与 `web/tools/target-firmware` 重复的文件是否瘦身 | **不瘦身**（两份都留，+9.5 MB） | 瘦身要改根 Makefile 20 处引用；清单已在 §4.5，可另开任务一键删 |
| **D-6** | 是否保留 git 历史；`tools/la`、`LICENSE` 是否只留一份 | **保留历史用 subtree（§6.0 B）**；`tools/la` 两份都留（各自 README/Makefile 引用），`LICENSE` 根一份 + `web/LICENSE` 一份 | 影响 §6.0 的搬法选择，其余步骤不变 |

---

## 9. 附：本文所有实测命令（可复现）

```powershell
# 清单与体量
git -C <repo> -c core.quotepath=false ls-files
git -C <repo> -c core.quotepath=false ls-files -s
git -C <repo> status --porcelain=v1 ; git -C <repo> status --porcelain=v1 -uall
git -C <repo> config --get core.autocrlf          # 三仓都是 true
(Get-ChildItem <repo> -Recurse -File -Force | Measure-Object Length -Sum)

# 跨仓内容相同（用工作区字节，不用 blob）
Get-FileHash -LiteralPath <file> -Algorithm SHA256      # 逐文件
# 目录体积
Get-ChildItem <repo>\captures -Recurse -File | Measure-Object Length -Sum

# 差异摘要
git -c core.quotepath=false diff --no-index --stat -- <A> <B>
git -C <repo> log -1 --format='%h %ad %s' --date=short -- <file>

# 关键事实取证
Select-String -Path firmware\application_5301\boards\hpm5301evklite\board.h -Pattern 'BOARD_HAS_|BOARD_LED'
Select-String -Path firmware\application_5301\boards\akaLinkPro\board.h      -Pattern 'BOARD_HAS_|BOARD_LED'
Select-String -Path firmware\application_5301\CMakeLists.txt -Pattern 'BOARD|hpm5301evklite'
Get-Content firmware\application_5301\build_dfu_evklite.bat | Select-String 'BOARD=|HPM_BUILD_TYPE='
Get-ChildItem E:\Share\github\akaLinkPro -Recurse -Include *.md,*.py,*.c,*.h,*.bat | Select-String 'hardware_pro|hardware/'   # 0 命中

# ETM 快照来源
Get-Content E:\web-serial-rtt-tools\bridge\etm\provenance.json -TotalCount 4
git -C E:\web-etm-trace cat-file -t a2922f8ec33c689bbcbdd523055cfa715cd1f71f
```

---

### 变更记录

| 日期 | 变更 |
| --- | --- |
| 2026-10-17 | 初版：三仓清单、布局映射、akaLinkPro 取舍、重复冲突、未提交清单、执行步骤与验收 |
