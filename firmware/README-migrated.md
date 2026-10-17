# firmware/ 迁移记录（akaLinkPro → superlinkPRO）

| 项 | 值 |
| --- | --- |
| 来源仓库 | `E:\Share\github\akaLinkPro` |
| 来源分支 | `main` |
| 来源 commit | `321487255d2ececea8d56282b07f3c7254e9862b`（短 `3214872`） |
| 来源 commit 时间 / 标题 | 2026-10-15 22:39:18 +0800 · `fix(reset): 将 EVKLite 可切换复位输出改到 PA27` |
| 迁移时间 | 2026-10-17 22:07（+08:00） |
| 迁移方式 | **逐文件复制**（PowerShell `Copy-Item`，清单来自 `git -c core.quotepath=false ls-files`）；**没有**用 `git subtree` / `git archive`，**不带 git 历史**（D-6） |
| 依据 | `docs/merge-map.md` §2.2A / §3 / §6.2；`docs/decisions.md`（D-1 删原板板级、D-3 不搬 `.opencode`、D-4 docs 不收敛、D-5 靶子不瘦身、D-6 不带历史） |
| 目标位置 | `E:\superlinkPRO\`：`firmware/`（本记录所在目录）＋ 根 `script_test/`、`tools/`、`docs/`、`Makefile`、`LICENSE` |

> **为什么 `script_test/` 留在仓库根，而不是 `firmware/script_test/`**：根 `Makefile` 里
> `APP_DIR = firmware\application_5301`、`BOOT_DIR = firmware\bootloader_dfu` 与 **20 处**
> `script_test/…` 引用全部相对**仓库根**。`firmware/` + `script_test/` + `tools/` 都放根之后，
> `Makefile` 可以**零改动**直接用（实测 `make help` / `make test-host` 均通过，见 §5）。

---

## 1. 搬了什么（对账数字）

| 项 | 文件数 | 字节 |
| --- | ---: | ---: |
| 源仓 `git ls-files` 跟踪文件 | 680 | 25,715,959（24.52 MiB） |
| 不迁移（§3 逐项列明） | 71 | 6,007,691 |
| **实际迁移** | **609** | **19,708,268（18.79 MiB）** |
| 目标侧复核（逐文件 SHA-256 比对） | 609 / 609 一致 | 19,708,268 一致 |

### 1.1 落点分布

| 源路径 | 目标路径 | 文件数 | 说明 |
| --- | --- | ---: | --- |
| `firmware/application_5301/**` | `firmware/application_5301/**` | 100 | 111 个跟踪文件中删掉 `boards/akaLinkPro/**`（8）与原板 3 个 `.bat`（D-1） |
| `firmware/bootloader_dfu/**` | `firmware/bootloader_dfu/**` | 26 | 32 个跟踪文件中删掉 `boards/akaLinkPro/**`（6，D-1） |
| `firmware/tools/pack.py`、`firmware/version.json` | 同路径 | 2 | DFU pack 头打包脚本 + 版本号 |
| `script_test/**` | `script_test/**`（**根**） | 289 | 探针主机侧验收脚本 + 7 套靶子固件（D-5：不瘦身） |
| `tools/**` | `tools/**`（根） | 19 | 面板 / SPI 相序 / LA 工具（含 `clean-evklite-build.ps1`） |
| `docs/**` | `docs/**`（根） | 170 | 固件 / 板级 / 协议文档 + `docs/validation/**` 已跟踪验收记录（139） |
| `Makefile` | `Makefile`（根） | 1 | 原样，零改动 |
| `LICENSE` | `LICENSE`（根） | 1 | Apache-2.0（`web/LICENSE` 另行保留一份，见 merge-map §2.3） |
| `README.md` | `firmware/README.md` | 1 | **改名**（根 `README.md` 是合并工程总说明，由 t2 写） |
| `.gitignore` | — | 1 | **不搬**：规则已并入根 `.gitignore`（t2），见 §3.5 |
| **合计** | | **609** | |

> 根 `docs/` 当前共有 **172** 个文件 = 本任务搬入的 170 + t2 的两份跨仓文档
> `merge-map.md` / `decisions.md`（零覆盖，无同名文件）。根 `docs/` 其余文件不归本任务。

### 1.2 目标子树文件数（t7 可直接对账）

| 子树 | 文件数 | 期望来源 |
| --- | ---: | --- |
| `firmware/` | **129** | 100 + 26 + 2 + `README.md`（=145−17+1） |
| `script_test/` | **289** | 源仓跟踪数 |
| `tools/` | **19** | 源仓跟踪数 |
| `docs/` | **172** | 170（本任务）+ 2（t2 跨仓文档） |

---

## 2. 对账手段（可复核）

1. **零覆盖**：复制前对 609 个目标路径逐个断言不存在 → `collisions = 0`；复制过程中 `Test-Path` 断言从未触发（映射表里 `README.md → firmware/README.md` 是唯一改名，目标不存在）。
2. **零差异**：复制后对 609 个文件逐个 `Get-FileHash -Algorithm SHA256` 与**源仓工作区**比对 → `mismatches = 0`；两侧字节总量均为 19,708,268。
3. **集合相等**：目标实际文件集 == 期望文件集（`missing = 0`；`extra` 仅 t2 的 `docs/merge-map.md`、`docs/decisions.md` 两项，已解释）。
4. **中文名**：迁移集含 **2 个非 ASCII 文件名**（`docs/opus5-5代码审查报告.md`、`docs/代码审查报告.md`），已到位且 SHA-256 一致。
   ⇒ 生成清单必须用 `git -c core.quotepath=false ls-files`，否则中文名会被转义成 `"\344\272\244…"` 而**静默漏文件**。
5. **不被忽略**：`git check-ignore --no-index --stdin` 对 610 个路径（609 + 本文件）→ **0 个被忽略**。
   其中专项复核了固件交付物：`firmware/application_5301/build_dfu_evklite/output/akaLinkPro_App.elf`、
   `firmware/bootloader_dfu/build_xip_evklite/output/akaLinkPro_Boot.elf`、
   `script_test/stm32f103_rtt_speed/build-{cb,ze}/fw.{elf,bin,hex}`，以及 `docs/validation/**` 的已跟踪记录。
6. **零产物混入**：`firmware/`、`script_test/`、`tools/`、`docs/` 下没有 `__pycache__/`、`captures/`、
   `records/`、`tmp/`、`node_modules/`、`build-native/`、`.agent-teams/`（`make test-host` 生成的
   `script_test/__pycache__/` 已删除并复核）。
7. **Makefile 引用全在位**：根 `Makefile` 里 **33 个** `firmware/ tools/ script_test/` 路径引用逐个检查存在 → 全部 True；
   `CMakeLists.txt` 引用的 `linker/flash_dfu_app.ld`、`src/**` 各目录亦全部存在。

---

## 3. 刻意没搬（逐项 + 证据）

### 3.1 D-1：akaLinkPro 原板板级（17 文件）+ 原板 3 个 `.bat`

证据（源仓实测）：

| 判据 | 原板 `boards/akaLinkPro/board.h` | EVKLite `boards/hpm5301evklite/board.h` |
| --- | --- | --- |
| SPI 桥 | `:151 #define BOARD_HAS_SPI_BRIDGE (0)` | `:179 (1)` |
| I2C 桥 | `:155 #define BOARD_HAS_I2C_BRIDGE (0)` | `:190 (1)` |
| LED 引脚 | `:114/:115 PB11 / PB12` | `:141 PA10` |

- **构建入口证据**：`build_dfu_evklite.bat:20 set "BOARD=hpm5301evklite"`、`build_xip_evklite.bat:16` 同上；
  被删的 `build_dfu.bat:18 set "BOARD=akaLinkPro"` 是**全仓唯一**编原板的入口（`make` 目标只用 `*_evklite.bat`）。
- **CMake 证据**：`CMakeLists.txt:8 set(BOARD_SEARCH_PATH …/boards)`、`:79 if(BOARD STREQUAL "hpm5301evklite")`
  选 `swd_blob_evklite.h` → 删掉原板目录后 `boards/` 只剩 `hpm5301evklite`，**EVKLite 构建不受影响**。
- **引用检索**：全仓代码/脚本检索 `boards/akaLinkPro` / `build_dfu.bat` / `flash_jlink.bat` / `program.bat`，
  只命中这三个 bat 彼此、`Firmware_Integrity_Plan.md` 文档与 `src/spi_bridge/spi_bridge_proto.h:257` 的一行注释
  → **没有任何构建/测试脚本依赖**。

删除清单（application 板级 8 + bootloader 板级 6 = 14）：

- `firmware/application_5301/boards/akaLinkPro/CMakeLists.txt`
- `firmware/application_5301/boards/akaLinkPro/akaLinkPro.yaml`
- `firmware/application_5301/boards/akaLinkPro/board.c`
- `firmware/application_5301/boards/akaLinkPro/board.h`
- `firmware/application_5301/boards/akaLinkPro/clock.c`
- `firmware/application_5301/boards/akaLinkPro/clock.h`
- `firmware/application_5301/boards/akaLinkPro/pinmux.c`
- `firmware/application_5301/boards/akaLinkPro/pinmux.h`
- `firmware/bootloader_dfu/boards/akaLinkPro/CMakeLists.txt`
- `firmware/bootloader_dfu/boards/akaLinkPro/akaLinkPro.yaml`
- `firmware/bootloader_dfu/boards/akaLinkPro/board.c`
- `firmware/bootloader_dfu/boards/akaLinkPro/board.h`
- `firmware/bootloader_dfu/boards/akaLinkPro/pinmux.c`
- `firmware/bootloader_dfu/boards/akaLinkPro/pinmux.h`

原板 3 个 `.bat`（EVKLite 各有对应 `*_evklite.bat`，见 §4）：

- `firmware/application_5301/build_dfu.bat`
- `firmware/application_5301/flash_jlink.bat`
- `firmware/application_5301/program.bat`

### 3.2 `hardware/**`（11 文件 / 1,080,091 B）

全是 KiCad 设计文件；全仓检索 `hardware/` 在代码/脚本中 **0 处引用**（`merge-map.md` §3.2 实测同结论）。

- `hardware/akaLink.pretty/IDC-Header_2x10_P2.54mm_Horizontal.kicad_mod`
- `hardware/akaLink.pretty/MountingHole_2.5mm.kicad_mod`
- `hardware/akaLink.pretty/PinHeader_2x05_P1.27mm_Vertical.kicad_mod`
- `hardware/akaLink.pretty/PinSocket_2x10_P2.54mm_Horizontal.kicad_mod`
- `hardware/akaLink.pretty/USB_C_Receptacle_HRO_TYPE-C-31-M-12.kicad_mod`
- `hardware/akaLinkPro.kicad_pcb`
- `hardware/akaLinkPro.kicad_prl`
- `hardware/akaLinkPro.kicad_pro`
- `hardware/akaLinkPro.kicad_sch`
- `hardware/fp-lib-table`
- `hardware/hpmmuxtool/akaLink.hpmpc`

### 3.3 `hardware_pro/**`（28 文件 / 4,745,232 B）

同一块板的新版 KiCad 工程 + 生产文件（`production/{bom,designators,netlist,positions}.csv/ipc`、两个 `.zip` 备份）+ 3D 模型；同样 **0 处代码引用**。

> 注意：`firmware/application_5301/src/api/api_param.c` 里的 `HARDWARE_PROD_TS_STR_ADDR` 是**生产日期字符串**宏
> （`BL_INFO_BASE + 0x34`），**不是**对本目录的引用（正则误报，已在验收中排除）。

- `hardware_pro/3d/FTSH-105-01-L-D-K.stl`
- `hardware_pro/3d/FTSH-105-01-L-D-K.wrl`
- `hardware_pro/3d/SFH11-xxPC-D10-RA-BK.STEP`
- `hardware_pro/TC2050-IDC-NL_Datasheet_8.pdf`
- `hardware_pro/akaLink.kicad_sym`
- `hardware_pro/akaLink.pretty/C_0603_1608Metric.kicad_mod`
- `hardware_pro/akaLink.pretty/C_0805_2012Metric.kicad_mod`
- `hardware_pro/akaLink.pretty/IDC-Header_2x10_P2.54mm_Horizontal.kicad_mod`
- `hardware_pro/akaLink.pretty/MountingHole_2.5mm.kicad_mod`
- `hardware_pro/akaLink.pretty/PinHeader_2x05_P1.27mm_Vertical.kicad_mod`
- `hardware_pro/akaLink.pretty/PinSocket_2x10_P2.54mm_Horizontal.kicad_mod`
- `hardware_pro/akaLink.pretty/R_0603_1608Metric.kicad_mod`
- `hardware_pro/akaLink.pretty/USB_C_Receptacle_HRO_TYPE-C-31-M-12.kicad_mod`
- `hardware_pro/akaLinkPro.kicad_pcb`
- `hardware_pro/akaLinkPro.kicad_prl`
- `hardware_pro/akaLinkPro.kicad_pro`
- `hardware_pro/akaLinkPro.kicad_sch`
- `hardware_pro/akaLinkPro.pdf`
- `hardware_pro/fabrication-toolkit-options.json`
- `hardware_pro/fp-lib-table`
- `hardware_pro/hpmmuxtool/akaLink.hpmpc`
- `hardware_pro/production/akaLinkPro.zip`
- `hardware_pro/production/backups/akaLinkPro_2026-08-30_18-51-39.zip`
- `hardware_pro/production/bom.csv`
- `hardware_pro/production/designators.csv`
- `hardware_pro/production/netlist.ipc`
- `hardware_pro/production/positions.csv`
- `hardware_pro/sym-lib-table`

### 3.4 `.opencode/skills/**`（14 文件 / 123,736 B，D-3）

AI 工具私有技能配置（`SKILL.md` 等），与构建/测试无关。

- `.opencode/skills/akalinkpro-firmware/SKILL.md`
- `.opencode/skills/akalinkpro-firmware/jlink/README.md`
- `.opencode/skills/akalinkpro-firmware/jlink/SKILL.md`
- `.opencode/skills/akalinkpro-firmware/jlink/config.example.json`
- `.opencode/skills/akalinkpro-firmware/jlink/references/common_devices.md`
- `.opencode/skills/akalinkpro-firmware/jlink/scripts/jlink_exec.py`
- `.opencode/skills/akalinkpro-firmware/jlink/scripts/jlink_gdb.py`
- `.opencode/skills/akalinkpro-firmware/jlink/scripts/jlink_gdb_common.py`
- `.opencode/skills/akalinkpro-firmware/jlink/scripts/jlink_rtt.py`
- `.opencode/skills/akalinkpro-firmware/jlink/scripts/jlink_runtime.py`
- `.opencode/skills/akalinkpro-firmware/jlink/scripts/jlink_swo.py`
- `.opencode/skills/akalinkpro-firmware/jlink/templates/flash.jlink`
- `.opencode/skills/akalinkpro-firmware/jlink/templates/read_mem.jlink`
- `.opencode/skills/akalinkpro-firmware/jlink/templates/reset.jlink`

### 3.5 根 `.gitignore`（1 文件 / 2,283 B）

**不搬**：其规则已由 t2 并入 `E:\superlinkPRO\.gitignore`（三仓并集 + 对「已跟踪文件」逐条放行）。
本任务已用 `git check-ignore` 复核：610 个路径 **0 个被忽略**（§2.5）。

### 3.6 磁盘上存在、但从未被跟踪的产物（不在 680 之内，也不搬）

| 目录 / 文件 | 规模 | 依据 |
| --- | --- | --- |
| `build/` | 33 文件 / ≈4 MB | 源 `.gitignore` 行首 `build`（CMake 生成目录） |
| `build_dfu/`、`build_xip*/`、`build_dfu_evklite/` 等构建目录（磁盘） | — | 源 `.gitignore` 逐条：`build_dfu`/`build_xip`/`build_dfu_evklite`/`build_xip_evklite`；**仅** `*/output/*.elf` 两个白名单入库（已在 §2.5 复核不被忽略） |
| `tmp/` | 402 文件 / ≈38 MB | 源 `.gitignore` 尾段 `tmp/`（LA 抓包与临时件） |
| `captures/` | 31 文件 | 源 `.gitignore` `captures/`（逻辑分析仪 CSV） |
| `script_test/_upstream_tree.json` | 1 文件 / 69,445 B（2026-09-28 生成的树快照，**未跟踪**） | 根 `.gitignore` `script_test/_*.json` |
| `.zcodeignore`（未跟踪） | 1 | 另一个 AI 工具的忽略配置（D-3 同族） |
| `docs/ai-guides/**`（未跟踪，2 文件） | 2 | `merge-map.md` §5.2 列为「可选」协作文档；本任务按「只搬跟踪集」口径未搬（不影响构建，需要时可逐文件补拷） |

---

## 4. 构建与测试入口（相对目标仓根，原样可用）

工具链：`E:\sdk_env_v1.11.0`（可用 `HPM_SDK_ENV_DIR` 覆盖）；根 `Makefile` 用 `SHELL := cmd.exe`。

| 用途 | 命令（在 `E:\superlinkPRO` 根执行） | 说明 |
| --- | --- | --- |
| 列出目标 | `make help` | 实测通过（§5） |
| 构建 Bootloader + APP | `make build` | = `build-boot` + `build-app` |
| 只构建 APP | `make build-app` | 实际执行 `cd /d firmware\application_5301 && build_dfu_evklite.bat` |
| 只构建 Bootloader | `make build-boot` | 实际执行 `cd /d firmware\bootloader_dfu && build_xip_evklite.bat` |
| APP 产物 | `firmware/application_5301/build_dfu_evklite/output/akaLinkPro_App_pack.bin` / `akaLinkPro_App.elf` | 仓库内保留 ELF 快照（`.gitignore` 白名单） |
| Bootloader 产物 | `firmware/bootloader_dfu/build_xip_evklite/output/akaLinkPro_Boot.elf` | 同上 |
| 无硬件回归 | `make test-host`（含 `test-jtag-host`） | 17 个纯 Python 模型测试，实测通过 |
| USB 描述符 | `make test-usb-descriptors` | 需要 `HPM_SDK_BASE`（或同级 `hpm-sdk-reference`），**不接硬件**；本次未跑（缺 SDK 环境时跳过） |
| 需要硬件的目标 | `make flash` / `flash-app` / `dfu` / `reset-usb` / `sram-test` / `rtt-*` / `uart-*` / `panel*` / `spi-*` / `adc-owner-hw` / `regression-*` | **本任务不接硬件、不烧录，全部未运行** |

直接调脚本（与 `make` 目标等价）：

```powershell
cd E:\superlinkPRO\firmware\application_5301; cmd /c build_dfu_evklite.bat
cd E:\superlinkPRO\firmware\bootloader_dfu;   cmd /c build_xip_evklite.bat
```

---

## 5. 本次校验记录（t4）

- `make help`（`E:\superlinkPRO` 根）→ 正常打印目标列表 ⇒ 根 `Makefile` 的 `APP_DIR` / `BOOT_DIR` / `script_test/` 相对路径在新布局下成立；
- `make test-host` → **exit 0**，17 个 host 测试脚本全 PASS：
  `riscv_jtag_host_test`、`service_gate_host_test`、`analog_host_test`、`adc_stream_host_test`、
  `adc_cache_host_test`、`bus_periodic_host_test`、`scope_host_test`、`rtt_stop_host_test`、
  `spi_drain_host_test`、`spi_tx_dma_host_test`、`spi_adc_owner_host_test`、`spi_cdc_host_test`、
  `spi_cdc_service_host_test`、`spi_clock_host_test`、`cdc_source_host_test`、`target_switch_host_test`、
  `test_scope_hss_test`；随后删除其生成的 `script_test/__pycache__/` 并复核文件数回到 289；
- 源仓工作区状态：**无已修改的跟踪文件**（`git status` 只有 `.zcodeignore`、`docs/ai-guides/` 两项未跟踪）
  ⇒ 本迁移的工作区字节 == 源 `HEAD` 字节；
- 详细数字与逐项证据见 §1 / §2 / §3。

---

## 6. 已知限制与注意事项

1. **无 git 历史**（D-6）：合并仓里拿不到 akaLinkPro 的逐文件 `git blame`。考古请回源仓
   （路径 + commit 见本文头部；用户可能删除源仓，因此 **40 位 commit SHA 是最后凭据**）。
2. **`firmware/README.md` 的相对链接是按「原仓根」写的**：搬到 `firmware/` 后少一层
   （正文里的 `firmware/application_5301/…`、`script_test/…` 现在会解析成 `firmware/firmware/…`、
   `firmware/script_test/…`）。为满足「逐文件内容对账全部相等」，**该文件一个字节都没改**；
   跨仓导航以根 `README.md` 与 `docs/merge-map.md` 为准。
3. **文档里仍会提到已删除的原板路径**（历史记录，未改动、不是构建输入）：
   `docs/development-history.md`、`docs/HPM5301EVKLite_port.md`、`docs/spi-bridge-wiring.md`、
   `docs/web-handoff-spi-bridge.md`、`firmware/application_5301/Flash_Memory_Map.md`、
   `firmware/application_5301/Firmware_Integrity_Plan.md`（该文还提到已删的 `build_dfu.bat` /
   `flash_jlink.bat` / `program.bat`）、`firmware/application_5301/src/spi_bridge/spi_bridge_proto.h:257` 的注释。
4. `firmware/bootloader_dfu/build.bat` / `build_xip.bat` 是**通用**脚本（`BOARD=` 为空，回落到 SDK 默认），
   按 D-1 口径**保留**；受支持入口是 `build_xip_evklite.bat` / `make build-boot`。
5. **D-5**：`script_test/` 里与 `web/tools/target-firmware` 逐字节相同的 **115 个靶子文件两份都保留**
   （+9.5 MB），本任务未瘦身。
6. 根 `docs/` 同时放着 t2 的两份跨仓文档（`merge-map.md`、`decisions.md`），本任务**没有覆盖**它们
   （零同名冲突，见 §2）。
7. 三仓来源版本记录在各自子树的 `README-migrated.md`；**未另建** `docs/source-revisions.txt`
   （merge-map §6.1 的可选项），避免与 t3 / t5 并行写同一文件。
8. **`docs/` 落点的口径冲突（本任务按 merge-map 执行，t7 请注意）**：
   `docs/merge-map.md` §2.2A / §7 第 4 行 / §8 D-4 把 akaLinkPro 的 **170 个 docs 文件放在根 `docs/`**
   （「根 docs/ 只放固件文档 + 合并文档」）；而 `docs/decisions.md` D-4 的字面措辞
   （「保持 web / etm / firmware 各自的 docs 子树」「根 docs/ 只放跨仓文档」）与根 `README.md:22`
   读起来更像 `firmware/docs/`。本任务以 merge-map（团队声明的**唯一依据**）为准 = **根 `docs/`**
   （t2 也记录了「两种落点都无需改 .gitignore」）。
   若要改成 `firmware/docs/`：把除 `merge-map.md` / `decisions.md` 之外的 170 个文件移到 `firmware/docs/`，
   并同步改本记录 §1.2（firmware 130→300、docs 172→2）与 merge-map §7 的期望数字，构建**不受影响**。

---

## 7. 复核命令（照抄）

```powershell
# 1) 源清单（-c core.quotepath=false 必须有，否则中文名被转义）→ 680
git -C E:\Share\github\akaLinkPro -c core.quotepath=false ls-files | Measure-Object -Line

# 2) 目标各子树文件数（期望：firmware 130 / script_test 289 / tools 19 / docs 172）
#    firmware 130 = 129 迁移文件 + README-migrated.md；docs 172 = 170 迁移 + 2 份 t2 跨仓文档
foreach ($p in 'firmware','script_test','tools','docs') {
  "{0} = {1}" -f $p, (Get-ChildItem "E:\superlinkPRO\$p" -Recurse -File -Force).Count
}

# 3) 入库文件数（期望 612 = 609 迁移文件 + 本记录 + 2 份 t2 跨仓文档）
git -C E:\superlinkPRO -c core.quotepath=false ls-files firmware script_test tools docs Makefile LICENSE |
  Measure-Object -Line

# 4) 逐文件 SHA-256 对账（609 个，期望 checked=609 / mismatches=0）
$src = 'E:\Share\github\akaLinkPro'; $dst = 'E:\superlinkPRO'
$skipExact = @('.gitignore','firmware/application_5301/build_dfu.bat',
               'firmware/application_5301/flash_jlink.bat','firmware/application_5301/program.bat')
$n = 0; $bad = 0
foreach ($rel in (git -C $src -c core.quotepath=false ls-files)) {
  if ($skipExact -contains $rel) { continue }
  if ($rel -like 'hardware/*' -or $rel -like 'hardware_pro/*' -or $rel -like '.opencode/*' -or
      $rel -like 'firmware/application_5301/boards/akaLinkPro/*' -or
      $rel -like 'firmware/bootloader_dfu/boards/akaLinkPro/*') { continue }
  $t = if ($rel -eq 'README.md') { 'firmware/README.md' } else { $rel }
  $a = (Get-FileHash -LiteralPath (Join-Path $src ($rel -replace '/','\')) -Algorithm SHA256).Hash
  $b = (Get-FileHash -LiteralPath (Join-Path $dst ($t  -replace '/','\')) -Algorithm SHA256).Hash
  if ($a -ne $b) { $bad++; Write-Host "HASH-DIFF $rel" }
  $n++
}
"checked=$n mismatches=$bad"

# 5) 根 Makefile 在新布局下可用（不接硬件）
cd E:\superlinkPRO; make help; make test-host
```