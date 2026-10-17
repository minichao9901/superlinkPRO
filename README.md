# superlinkPRO

面向 **HPM5301EVKLite 调试器（akaLinkPro 探针，VID `0x0D28` / PID `0x0204`）** 的一体化工程：
探针固件 + 网页工具箱 + ETM 执行轨迹后端与页面。

本仓是**三个既有仓库的合并产物**（逐文件复制 + 按取舍清单裁剪，见 `docs/merge-map.md`），
不是其中任何一个仓的 fork，也**不携带原仓的 git 历史**（决策 D-6，理由与代价见 `docs/decisions.md`）。

> **默认不接硬件**：本仓的日常开发与自测面（纯 Node 套件、纯 Python 单元测试、语法与守卫检查）
> 都在**不接探针、不烧录**的前提下可跑通。真机流程（烧录、RTT、J-Scope、逻辑分析仪抓包）
> 属于显式的手动步骤，不在默认测试面内。

---

## 1. 目录职责

| 目录 | 职责 | 来源 |
| --- | --- | --- |
| `firmware/` | HPM5301EVKLite 探针固件：`application_5301/`（探针主程序 + 板级 + SWD blob）、`bootloader_dfu/`（DFU bootloader）、`tools/pack.py`、`version.json` | akaLinkPro |
| `script_test/` | 探针主机侧验收脚本（Python，含 `hpm6800_flash_probe.py`、`regression_swd.py`、SPI/I2C 桥测试…）与各目标板靶子固件（F103 / H743 / H7B0 / HPM6800EVK …） | akaLinkPro |
| `tools/` | 根级主机侧工具：SPI 相序扫描、面板初始化表、逻辑分析仪解码、`clean-evklite-build.ps1` | akaLinkPro |
| `docs/` | **跨仓文档**：本合并工程的地图、决策、迁移对账与驱动去重（决策 D-4：各子树的 `docs/` 仍留在各自子树内，不向根收敛） | 本仓新增 |
| `web/` | 网页工具箱（RTT Viewer/转发、烧录、J-Scope、SPI-QSPI 桥与屏、USB→I2C、调试器、ETM 内嵌页、工程生成）+ 便携 RTT 桥 + 它自己的 `tools/`（自测/dev/靶子固件）、`samples/`、`docs/`、`bridge/` | web-serial-rtt-tools |
| `etm/` | ETM 执行轨迹：后端（`server.py`、`tools/`、`native/` 解码器、`vendor/OpenCSD-1.8.3`、`baselines/`）+ ETM 页面源码（`web/`）+ 保留的 ETM 靶子固件工程 | web-etm-trace |

根级 `Makefile` 来自 akaLinkPro：它的 `APP_DIR=firmware\application_5301`、`script_test/`、
`tools/clean-evklite-build.ps1` 全部按**相对仓库根**寻址，因此在这个布局下**零改动**。
`web/` 整树保持原样也是同样的理由（它的 Makefile 有 293 处 `tools/` 相对引用、`tools/**` 里还有
416 处 `app/` 等相对引用；把 `web/tools` 拆到根就要改 ≈600 处）。

> 目标布局与逐路径映射：`docs/merge-map.md` §2.2（A/B/C/D 四张表）。
> 搬家后需要改路径引用的只有 5 个文件 ≈10 行：`docs/merge-map.md` §2.4。

---

## 2. 与三个原仓的关系

| 源仓 | 本地路径 | 分支 | commit | 在本仓的落点 | 取舍摘要 |
| --- | --- | --- | --- | --- | --- |
| akaLinkPro | `E:\Share\github\akaLinkPro` | `main` | `321487255d2ececea8d56282b07f3c7254e9862b` | `firmware/`、`script_test/`、`tools/` + 根 `Makefile`/`LICENSE`；`docs/`（落点见 D-4） | 只保留 **HPM5301EVKLite** 相关：不迁 `hardware/`、`hardware_pro/`、`boards/akaLinkPro` 原板板级与 3 个原板 `.bat`（D-1），不迁 `.opencode/skills`（D-3）；`script_test/` 里的重复靶子暂不瘦身（D-5） |
| web-serial-rtt-tools | `E:\web-serial-rtt-tools` | `main` | `089e38392127029025c9c4bcd6d803ad60df3742` | `web/`（整树平移，含它自己的 `tools/`、`samples/`、`docs/`、`bridge/`、`Makefile`） | 不迁团队状态 `.agent-teams/`；不迁未跟踪的验收产物（`docs/validation` 里 4206 个文件 ≈3.5 GB）；**带走** 8 个未提交的真源码改动 + `docs/h743-web-review-2026-10-17.md`（§5.1） |
| web-etm-trace | `E:\web-etm-trace` | **`master`** | `12746fc035833e90f695d45e15a1f7ffa1e132ad` | `etm/`（后端 + 页面 + OpenCSD + 基线） | ETM 的 `firmware/` 只保留靶子工程（D-2；其余 678 MB 带 SDK 的工程与 toolbox 的 HAL/CMSIS 逐字节重复，不搬） |

- **无 git 历史**（D-6）：合并采用逐文件复制，本仓拿不到原仓的逐文件 `git blame`。需要考古时回原仓，
  按上表的绝对路径 + 40 位 commit 定位（**原仓若被删除，上表是最后凭据**）。
  迁移方式、裁剪清单与逐项对账结果分别记在各子树的 `README-migrated.md` 与
  `docs/merge-verification.md`（t7 产出）。
- **探针驱动去重**：`dap-webusb.js` 等 7 个文件在三个源仓里有 2~3 份拷贝且已经分叉。
  合并方案是「toolbox 版为唯一真源 + `etm/web/app/shared/**` 为生成副本 + sha256 守护」，
  详见 `docs/driver-unification.md`（t6 产出）。
- 两处**故意不同**的已知重复（不是漏合并，别误当冲突）：
  1. `etm/web/app/target.js` 是 ETM 独有的目标抽象（`EtmTarget`），与 `web/app/swo/target.js` 同名不同物；
  2. `etm/tools/la/kingst_la.py` 是上位的超集版本，`web/tools/la/kingst_la.py` 仍在 toolbox 侧使用（§4.6）。

---

## 3. 入口与自测（都不需要硬件）

| 子树 | 常用入口 |
| --- | --- |
| 根（固件） | `make help`；构建 `cd firmware\application_5301 && build_dfu_evklite.bat`（需本机 `E:\sdk_env_v1.11.0` 工具链）；主机侧逻辑回归 `make test-host`、`make test-jtag-host`、`make test-usb-descriptors` |
| `web/` | `make serve`（静态服务 8899）/ `make open`（起 CDP 浏览器 9333）/ `make test`（纯 Node 套件）/ `make check`（语法 + Liquid 守卫）；改了 `web/app/**` 要重建 `web/app/site/main.js` |
| `web/` + `etm/` | `make serve-etm`（8878，把 `etm/` 挂到 `/etm/` 给内嵌页用）/ `make test-etm`、`make test-etm-integration` |
| `etm/` 单独 | `pwsh -File etm/start-web.ps1`（8766）或 `python etm/server.py`；后端测试 `python -m unittest discover -s etm/tools/tests`、`node --test etm/tools/tests/*.test.mjs` |

> 目标名与前置条件以各子树自己的 `Makefile` / `README.md` 为准（合并**不改**这些文件）。
> CDP 页面套件共用同一套 8899/9333 服务与浏览器，**不要并发跑两个**。

---

## 4. 入库约定（`.gitignore` / `.gitattributes` 的关键点）

1. **固件交付物必须入库**：`*.elf` / `*.bin` / `*.hex` 是本项目的交付物（EVKLite 的 DFU 镜像 ELF、
   F103 的 `fw.elf/.bin/.hex`、H743/H7B0/HPM5300EVK 靶子固件……）。根 `.gitignore` **不写**这些
   扩展名的通用忽略规则；只忽略具体构建目录，并对随仓库发布的镜像逐个白名单（`.gitignore` 文件头有说明）。
2. **验收记录入库、验收产物不入库**：三个源仓在 `docs/` 各子树下的 `validation/` 里共有 **1086 个已跟踪**
   记录（web 894 / akaLinkPro 139 / ETM 53），而 web 侧另有 **4206 个未跟踪产物（2494 MB；
   连同源仓本就忽略的 814 个文件，整目录约 3.5 GB）**。
   根 `.gitignore` 的写法是「**默认忽略整层内容 + 放行已跟踪记录所在的目录与文件类型 +
   对未跟踪产物族按类型压掉（实测 92.7% 文件 / 98.3% 字节）**」。
   **不要**改成整目录 `docs/validation/` 忽略：目录一旦被排除，git 就无法再放行其内部文件，
   搬迁时这 1086 个文件会被 `git add -A` **静默丢掉**（`docs/merge-map.md` §7 的对账会直接红）。
3. **临时件/产物一律忽略**：`tmp/`、`captures/`、`records/`、`build*/`、`build-native/`、`decoded/`、
   `node_modules/`、`__pycache__/`、`*.log`、`*.map`、`*.csv`、`.agent-teams/`（团队状态，**绝不入库**）……
4. **字节保真**：三个源仓都是 `core.autocrlf=true`（本仓沿用）。对账基线
   （`web/tools/fixtures/gen/**`、`web/tools/fixtures/dwarf/**`）、素材（BMP/GIF/APNG/WebM）、
   ETM 记录（`*.etmrecord` / `*.kvdat`）与压缩包（`*.gz` / `*.zip`）在 `.gitattributes` 里标成
   `-text` / `binary`，**不要**让换行转换碰它们。
5. **中文文件名的坑**：三仓都有中文文件名。取清单必须 `git -c core.quotepath=false ls-files`，
   否则中文名被转义成 `"docs/\344\272\244..."`，按它复制会**静默漏文件**（merge-map §1.1/§6.2 实测踩过）。
6. **自检忽略规则**：搬迁前后都可跑
   `git check-ignore --no-index -v --stdin < 待搬迁清单.txt`，确认输出里没有「应当入库」的路径。

---

## 5. 文档索引

| 文档 | 内容 | 状态 |
| --- | --- | --- |
| `docs/merge-map.md` | 三仓清单、逐路径映射表、重复冲突清单、执行步骤与对账口径（**合并的唯一依据**） | ✅ 已完成（t1） |
| `docs/decisions.md` | D-1 ~ D-6 的已批准决策与两条必须传给下游的踩坑 | ✅ 已完成 |
| `docs/merge-verification.md` | 迁移对账（文件数 / 字节数 / 逐文件 sha256 与已知差异） | ⏳ t7 |
| `docs/driver-unification.md` | 探针驱动去重方案（真源/生成副本/sha256 守卫） | ⏳ t6 |

各子树自带的说明文档仍在原位：`firmware/README.md`、`web/README.md`、`etm/README.md`
（以及各自的 `docs/`）。

---

## 6. 当前状态（骨架阶段）

- ✅ 已建：`git init`（分支 `main`）、根 `.gitignore`（三仓并集）、根 `.gitattributes`、本 `README.md`、
  `docs/`（`merge-map.md` + `decisions.md`）、六个子树的目录占位（`docs/` 已有文档，其余五个为空目录）。
- ✅ 忽略规则已复核（`git check-ignore --no-index -v -n --stdin`，共 3 轮）：
  - 检查集 **8725 个路径** = akaLinkPro 可迁移集 609（按 D-1/D-3 去掉原板硬件、`.opencode`、原板 `.bat` 等，
    **docs 的两种落点都测**：根 `docs/` 与 `firmware/docs/`）+ toolbox 2035 + **ETM 全部 5472**
    （比 D-2 的保留集更大，检查更严）→ **被忽略的 0 个**；
  - 三个源仓**全部 8187 个已跟踪文件**的朴素映射 → **只有 3 个被忽略**，且全是
    `hardware_pro/production/*.csv`（KiCad 生产 BOM/坐标表，按 D-1 不迁移）；
  - 关键交付物逐个复核「不被忽略」：`akaLinkPro_App.elf`、`akaLinkPro_Boot.elf`、
    `script_test/stm32f103_rtt_speed/build-{cb,ze}/fw.{elf,bin,hex}`、
    `etm/firmware/{stm32h743,stm32h7b0_*}/fw.{elf,bin,hex}`、`web/tools/target-firmware/stm32f103_*/build-cb/fw.elf`、
    `etm/vendor/OpenCSD-1.8.3/decoder/build/**`、`web/bridge/etm/backend/build-native/etm_decode.exe`、
    以及 `web/docs/validation` 下 894 个已跟踪记录（含 120 个 `.log`、1 个 `.csv`）；
  - 未跟踪验收产物：4206 个里有 **3897 个（92.7% 文件 / 98.3% 字节）** 被 `.gitignore` §E2 的产物族规则压掉，
    剩余 309 个（43 MB，零散截图/文本/补丁）与已跟踪证据同类型、无法按类型安全忽略，且不在迁移清单内；
  - `.gitattributes` 复核：`web/tools/fixtures/**` 为 `-text`/`binary`、BMP/GIF/WebM 与大文件为 `binary`、
    `*.etmrecord`/`*.kvdat`/`*.gz`/`*.zip`/`*.elf`/`*.bin` 为 `binary`、`*.bat` 与 Keil SVD 为 `-text`。
- ⏳ 待搬：`firmware/ script_test/ tools/ docs/ web/ etm/` 的业务文件按 `docs/merge-map.md` §6.2 复制
  （逐文件 `Copy-Item`，含已跟踪文件的**工作区最新内容**），随后做 §7 的 12 项对账。
- ⚠️ 本仓的 `core.autocrlf` 与三个源仓一致为 `true`；`docs/validation` 的放行清单见 `.gitignore` §E。

## 7. 已知限制

- 无原仓 git 历史（D-6）；逐行考古需回原仓（见 §2 的 commit 表）。
- 真机项（探针、烧录、RTT/J-Scope、逻辑分析仪）不在默认测试面内，相关脚本会显式提示需要硬件。
- `script_test/` 与 `web/tools/target-firmware/` 之间有 115 个逐字节相同的靶子固件文件（D-5 暂不瘦身）。
- ETM 的内嵌页依赖同源服务（`web/tools/dev/serve-etm.py` 或包内 `runtime/serve-etm.py`），
  直接双击 `index.html` 打不开 ETM 页。
