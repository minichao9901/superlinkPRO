# 工程生成页（.uvprojx → 调试配套文件）

第 5 个标签页 `#gen`。目标很简单：**把另一个 Python 工具（`uvprojx2cmake.py`）里"Debug / Download"那一组的产物，
在网页上点几下就拿到**，不用装 Python、不用记命令行。

页面是纯前端：不连串口、不连探针、不需要本地桥。

---

## 1. 怎么用

页面分**三个 tab**（右列），**左栏的卡片跟着 tab 走**（只显示当组，见 §1.1）：

1. 打开页面 → 顶部「工程生成」。
2. 把 Keil 工程的 `.uvprojx` **拖到页面里**（或点「选择 .uvprojx…」）。
   页面会读 `<Device>`、`<TargetName>`、`<Cpu>` 里的 `IROM(...)`/`IRAM(...)`：
   - **项目名**：拖文件夹进来时取 `.uvprojx` 所在目录名（与 Python 工具一致）；否则取 `<TargetName>`；
   - **器件**：按内置器件表归族（`STM32F103C8` → `STM32F103RB`，与 Python 工具的映射一致）；表外型号原样透传；
   - **Flash 起址**、**PyOCD 目标**、**OpenOCD 目标**：一并填好。
3. 「工程文件」tab：左侧勾选要哪些产物、按需改参数（改动立刻反映到右侧预览）。
4. 右侧文件栏切换文件，看内容 / 复制 / 单独下载。
5. 落地（左栏「落地方式」）：
   - **写入文件夹…**（Edge/Chrome，File System Access API）：选一次目录，多个文件**直接写入**，不打包；
     若目录里已有同名文件，会先列出来问你（勾了「已存在就直接覆盖」才不问）。
   - **打包 ZIP**：不支持上面那个 API 的浏览器走这里，ZIP 落「下载」文件夹。
   - 单独文件也可以只下那一个。

> 浏览器安全模型决定了：网页**不能**静默写你的项目目录，必须由你亲手选一次目录。

### 1.1 三个 tab 与左栏的对应（2026-10 重构）

| tab | 右列 | 左栏卡片 |
|---|---|---|
| **工程文件**（默认） | `uvprojx2cmake.py` 的 6 件模板产物预览 + 复制/下载 | 工程 · 生成哪些 · 落地方式 |
| **调试参数** | **参数一览表**：把左栏填的值**逐项列全**（长路径不再被侧栏宽度截断，**点值即复制**；「复制全部」给 `键=值` 文本） | 参数 |
| **本地桥** | 安装包那 7 个文件的预览 + 「下载桥包（ZIP）」 | 本地桥（J-Link / OpenOCD） |

三条设计口径：

- **右列 tab 与左栏卡片是同一套分组**：卡片上的 `data-dock` 就是它的归属，切 tab 只切 `hidden`
  （不删节点 —— 表单值、事件绑定、`store` 持久化都不受影响）。
- **参数表故意列全五张卡的值**（包括当前 tab 藏起来的那些）：它的用途就是"一屏复核我到底填了什么"，
  分组标题写着每个值属于哪张卡 —— **隐藏 ≠ 没填**。表是**遍历 DOM** 生成的，左栏加字段这里自动就有，
  不会出现"表里漏一项"的漂移。
- tab 选择记在 `localStorage` 的 `gen.dock`（切页/刷新回来还在）；tab 栏的胶囊显示当前 tab 的一句话状态
  （`6 个文件 · 118.5 KB · CRLF` / `38 项参数 · 点值即复制` / `7 个文件 · 77 KB`）。
- ⚠️ 页面自测（`make test-gen-page`）**必须自带前置**：`gen.dock` 是持久化设置，上一轮跑崩留在别的 tab
  会让下一轮整片红（与 `#i2c` / `#spi` 的页面套件同一个纪律）。

---

## 2. 五个产物是什么

| 勾选 | 文件 | 关键 targets / 内容 |
|---|---|---|
| J-Link Makefile | `Makefile.jlink` | `cmake/build/clean` + `jlink-erase`、`jlink-prog`(.hex)、`jlink-prog-bin`(.bin)、`jlink-rtt`(`JLinkRTTLogger`)、`jlink-swo`、`jlink-gdb`(`JLinkGDBServerCL`)、`jlink-debug`(arm-none-eabi-gdb + `jlink_gdb.script`)、`jlink-check`。擦/烧/查这几条是**现场拼一个临时 Commander 脚本**（`@echo r > jlink_prog.script`）跑完即删 |
| GDB 脚本 | `jlink_gdb.script` | `set logging on` / `print pretty` / 清断点，自定义宏 `do_more`（连按 500 次 `next`）、`do_func`（`next` 到 `$sp` 变化 = 跳出函数）；`target remote localhost:3333` → `monitor reset halt` → `load` → `break main` → `continue`。`Makefile.openocd` / `Makefile.pyocd` 里的 `GDB_SCRIPT` 也指向它 |
| PyOCD Makefile | `Makefile.pyocd` | `pyocd-erase/prog/prog-bin(--base-address)/gdb(gdbserver)/rtt(-a <RTT 地址> -d rtt.txt)/reset/debug` |
| OpenOCD Makefile | `Makefile.openocd` | `openocd-erase`(`flash erase_sector 0 0 last`)、`openocd-prog`(elf)、`openocd-prog-bin`、`openocd-gdb`、`openocd-reset/halt`、`openocd-sram`（`load_image test_sram.bin` → `dump_image sram_dump.bin` 回读比对）、`openocd-rtt`（`rtt setup/start ; rtt server start 9090 0`）。`OPENOCD_ROOT` 决定 `$(OPENOCD) = $(OPENOCD_ROOT)/bin/openocd` |
| （随 OpenOCD 附带） | `rtt_logger.py` | 连 `127.0.0.1:9090`、把 RTT 落 `rtt_log.txt`，每秒打印 `Speed / Avg / Total` 的 socket 小脚本 |
| SRAM test bin | `test_sram.bin` | **不是固件**：`bytes(i % 256)` 的递增图案（大小可填，默认 20480），配合 `make -f Makefile.openocd openocd-sram` 量 RAM 读写吞吐 |

---

## 3. 参数对应关系（填的框 → 产物里的哪一行）

| 界面 | 产物里的变量 | 模板默认 |
|---|---|---|
| 项目名 | 文件头注释、`HEX_FILE`/`BIN_FILE`、`add_executable` 名字的来源 | `project` |
| 器件 | `JLINK_DEVICE` | 按器件表推出 |
| Flash 起址 | `loadfile $(BIN_FILE) <addr>`、`pyocd --base-address`、`openocd program $(BIN_FILE) <addr>` | `0x08000000` |
| 接口 | `JLINK_IF` | `SWD` |
| 时钟 kHz | `JLINK_SPEED` | `25000` |
| RTT 地址 / 范围 | `RTT_ADDR` / `RTT_SIZE`（J-Link 的 `-RTTSearchRanges`、OpenOCD 的 `rtt setup`、PyOCD 的 `-a`） | `0x20002000` / `0x5000` |
| GDB 端口 | `GDB_PORT`（三个 Makefile 共用） | `3333` |
| build 目录 | `BUILD_DIR` | `build` |
| CMake 生成器 / 构建类型 | `CMAKE_GENERATOR` / `CMAKE_BUILD_TYPE` | `Ninja` / `Debug` |
| PyOCD 目标 / Hz | `PYOCD_TARGET` / `PYOCD_FREQ` | 按器件表 / `10000000` |
| OpenOCD 根 / 接口 / 目标 / kHz | `OPENOCD_ROOT` / `OPENOCD_INTERFACE` / `OPENOCD_TARGET` / `OPENOCD_FREQ` | MounRiver 路径(*) / `interface/cmsis-dap.cfg` / `target/stm32f1x.cfg` / `10000` |
| RTT 端口 | `RTT_PORT`（`rtt server start`） | `9090` |
| bin 大小 | `test_sram.bin` 字节数 | `20480` |

(*) 页面里 OpenOCD 根目录预填的是**本机 Python 工具自动探测到的那个 MounRiver 路径**；换机器要改成自己的
（OpenOCD 目录里得有 `bin/` 和 `scripts/`）。

**只改你动过的项**：某个框等于模板默认值时，产物里那一行原样保留 —— 所以"填了默认值"和"没填"产出的文件逐字节相同。

---

## 4. 与 Python 工具的一致性（硬指标）

- 模板是**原样移植**的：`app/gen/templates.js` 里就是 `uvprojx2cmake.py` 里那几个
  `JLINK_MAKEFILE_TEMPLATE` / `PYOCD_MAKEFILE_TEMPLATE` / `OPENOCD_MAKEFILE_TEMPLATE` /
  `RTT_LOGGER_TEMPLATE` + `generate_gdb_script` 的 f-string + `generate_test_bin` 的图案规则。
- 对账基线：`tools/fixtures/gen/mdk-arm/`（Python 工具对真实工程 `CubeMX_Config.uvprojx` 的原始产物）。
- 跑对账：

  ```powershell
  make test-gen            # 等价于 node tools\selftest\gen-parity.mjs
  ```

  **55 项**，检查：6 个产物逐字节相同（含 CRLF；对账时传 `fixes: null`，即不带下面的修正）、
  LF 模式只差换行、单项覆盖只改那一行、"填了默认值 = 没填"、`test_sram.bin` 图案、
  ZIP 能被自己解回来、`.uvprojx` 解析结果，以及第 7 节：4 项修正**只**改该改的行、别的文件一字节不动。

- 再跑一遍**真页面**验收（要 `make open` 起的浏览器）：

  ```powershell
  make test-gen-page       # 等价于 node tools\selftest\gen-page.test.mjs
  ```

  **47 项**：页面接好没、产物字节 = 基线 + 4 项修正（在页面里生成、取回来比）、4 项修正逐条生效、
  预览内容、勾选/换行符/SWD 频率开关、拖入 `.uvprojx` 自动填参、「打包 ZIP」真的下下来、
  「写入文件夹」的写入逻辑（用假目录句柄跑真实的 `saveToFolder()`：同名冲突确认、覆盖开关、每个文件的字节）。

### 故意不同的三处

1. **项目名**：Python 工具固定取 `.uvprojx` 所在**目录名**（脚本在工程目录里跑）；网页拿不到目录名时退回
   `<TargetName>`，再不行用文件名。三者都可以手改。
2. **换行符**：默认 CRLF（= Python 产物）；给 git 用可以切 LF，除此之外内容一致。
3. **4 项固定修正**：见下一节 —— 这是唯一会让默认产物与 Python 工具**不同**的地方。

---

## 5. 默认固定套用的 4 项修正

模板虽然逐字节移植，但 Python 那份模板里有几处用起来会硌人。2026-09-27 逐条过审后，
**采纳的 4 条直接固化进生成器**（`app/gen/fixes.js`，页面不需要你勾）：

| # | 修正 | 改哪一行 | 为什么 |
|---|---|---|---|
| 1 | `Makefile.jlink`：`RTT_SIZE ?= 0x5000` → `0x2000` | 1 行改写 | F103 只有 20KB RAM，`0x20002000+0x5000` 会越过 RAM 顶 |
| 2 | `Makefile.jlink`：`clean-jlink` 里删掉 `@del *.log` | 1 行删除 | 原来会连 J-Link 自己写的 `JLinkLog.txt` 一起删掉 |
| 3 | `Makefile.openocd`：`openocd-rtt` 的 `-c '…'` → `-c "…"`（内层 `\"SEGGER RTT\"`） | 1 行改写 | `-c '…'` 在 sh 下能跑、在 cmd.exe 里单引号不是引号 → 直接报错 |
| 4 | `Makefile.jlink`：去掉 `jlink-swo` 目标 | 删 7 行 + 改写 `.PHONY` 1 行 | 硬编码 `-cpufreq 72000000` 只对 F103 成立；且工程里没开 PB3/TRACESWO，跑出来是空日志 |

实现细节（都在 `app/gen/fixes.js`）：

- **逐行定点替换**，绝不做全局替换；**匹配不到期望内容就抛错**，不静默产出半成品。
- **用户填的值优先**：RTT 范围被改成 `0x1000` 时不会被"纠正"回 `0x2000`（修正只认模板原值 `0x5000`）。
- 改动范围有硬断言：`Makefile.jlink` 少 9 行（7 删 + 2 改写）、多 2 行；`Makefile.openocd` 只换 1 行；
  `Makefile.pyocd` / `jlink_gdb.script` / `rtt_logger.py` / `test_sram.bin` 一个字节都不动（见自测第 7 节）。
- 想看**原汁原味**（与 Python 工具逐字节一致）就传 `fixes: null` —— 对账测试走的正是这条路。

### 明确不采纳的 4 条（保持与 Python 模板一致）

| 建议 | 决定 |
|---|---|
| `jlink-rtt` 改用 `-RTTAddress`（不用搜索范围）、默认速度提到 50000 | 不采纳 |
| `rtt polling_interval 1`（OpenOCD 默认 100ms）改回默认 | 不采纳 |
| `openocd-erase` 只擦 bank 0 → 双 bank 器件（H743/H750/H7B3）第二个 bank 擦不到 | 不采纳 |
| `openocd-sram` 加"会覆盖目标 RAM 内容"的警告 | 不采纳 |

（这几条记在这里，是为了下次别人再提同样建议时不用重新讨论。）

---

## 6. 代码结构

```
app/gen/templates.js   模板 + render*(params)（逐字节移植自 Python 工具，改模板要同步改那侧）
app/gen/fixes.js       4 项固定修正（逐行定点替换 + 硬断言）
app/gen/model.js       器件映射表、.uvprojx 正则解析、参数 → 文件内容（buildOutputs）、换行符处理
app/gen/zip.js         零依赖 ZIP（store 模式 + CRC32），时间戳可注入以便复现
app/gen/view.js        页面：绑定/持久化(localStorage)、拖放、预览、写入文件夹 / 打包 / 单文件下载
index.html             #tab-gen 面板（标签按钮 + 侧栏字段 + 预览区）
tools/selftest/gen-parity.mjs   对账自测（无硬件）
tools/selftest/gen-page.test.mjs 真页面验收（CDP）
tools/fixtures/gen/             对账基线与 uvprojx 样例
```

## 7. 不在范围内

`CMakeLists.txt` 生成、SEGGER RTT 源码、TRACE_LOG 源码那几组**没做**（那要把整个 uvprojx→CMake 引擎搬过来）。
需要的话另开一单。

## 8. 已知未验证 / 边角

- **生成的 Makefile 目标本身没被执行过**：内容是逐行读+交叉核对（CMake 确实产出 hex/bin、
  RTT 地址与链接脚本一致、J-Link 程序名存在、`arm-none-eabi-gdb`/`make`/`cmake`/`ninja`/`pyocd` 都在），
  但 `jlink-prog` / `jlink-rtt` / `openocd-rtt` 这些**没有真跑过**（会动板子，要另外约）。
- **真·「写入文件夹」**（真的弹目录选择框、真的落盘）需要真人手势 + 文件系统授权，自动化测不了；
  自动测到的是它**下面那层逻辑**（冲突检测 / 覆盖开关 / 写字节 / 状态行），用假目录句柄驱动真代码路径。
- Firefox / Safari：没有 `showDirectoryPicker`，走 ZIP；这两个浏览器的拖放行为也没测过。
- ZIP 用的是 store 模式（不压缩）：产物本来就小（约 42 KB），而且这样字节可控、便于对账；
  Windows 资源管理器 / 7-Zip / unzip 都能解。
- 大文件不在场景里（产物总共 ~42 KB）。
