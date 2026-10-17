# PXView 采集后端技术参考（CLI 主线 / subprocess 用法）

> 生产路径：Python 用 `subprocess` 调 `C:\Program Files\PXView\pxview-cli.cmd` 的子命令，再解析它落盘的 `channel_N.bin`（对标 KingstVIS socket API + 跳变 CSV 路线）。
> 依据：① 源码 `C:\Program Files\PXView\share\pxview\python\pxview_automation\cli.py`（633 行，只读未改；行号随文标注）及其调用链 `highlevel.py`/`client.py`；② `[实测]`＝2026-10-13 在本机对运行中的 PXView(serverInfo **pxview 1.6.5**，MCP 内嵌在 GUI 进程)`pxview-cli.cmd` 的只读调用；③ `[str]`＝PXView.exe 内嵌字符串。无源码/无实测支撑的一律标 **未证实**。`docs/cli-reference.md` 与实现**有出入**，本文以 `cli.py` 为准。

## 0. 结论速览

| 问题 | 结论 |
|---|---|
| CLI 能否当生产采集后端 | 基本可以：`capture`（阻塞至完成）+ `export --format binary` + 自己解包即可，`[实测]` 5 通道 ×1,000,000 样本 → `channel_0..4.bin` 各 125,000 B |
| 位打包 | **8 样本/字节、LSB-first、逐通道**；`channel_N.bin` **无文件头**，与 `.pxc/.pxl` 内部编码一致 |
| 最大缺口 | **没有 stop/取消子命令**；`--timeout` 对多数命令被内部钳到 30 s；单会话共享（两个 subprocess 会互相清数据） |
| 编码雷 | `channels`、`list-decoders` 在 GBK 控制台**直接崩**（exit 1），必须 `PYTHONIOENCODING=utf-8` |
| 触发 | `capture --trigger c:type` 会**静默失效**（服务器不认这个参数）；触发只能走 MCP `configure_trigger` |

## 1. 调用方式与全局约定

```powershell
& "C:\Program Files\PXView\pxview-cli.cmd" [全局选项] <子命令> [子命令选项]   # 全局选项必须在子命令之前
```

| 全局选项 | 类型/默认 | 说明（`cli.py:58-77`） |
|---|---|---|
| `--host` | str / `127.0.0.1` | MCP 服务地址 |
| `--port` | int / `10110` | MCP 端口 |
| `--timeout` | float / `60.0` | HTTP 超时秒数，但**多数请求被内部钳到 min(--timeout, 30)**（§7.3） |
| `--json` | flag | 输出原始 JSON；`list/dict` 返回值**不加 `--json` 也是 JSON**（`_print`，`cli.py:228-235`） |
| `--auto-start` | flag | 3 s 内探测不到服务时启动 `PXViewProcess(exe_path=--exe, port=--port)`（`cli.py:238-267`） |
| `--exe` | str / None | 配合 `--auto-start` 指定可执行文件 |

* CLI 本质是 shim：`"%~dp0python.exe" -m pxview_automation.cli %*`（无需系统 Python），内部仍走 MCP（§附录）。
* 每次调用都会：`wait_for_server(3s)`（不满足则报 `Cannot connect to MCP server at <host>:<port>. Is PXView running with --headless? Use --auto-start to launch it automatically.`）→ `connect()`（MCP 握手）→ 执行 → `disconnect()`。**无跨进程状态**：会话/采集状态全在服务器进程里。
* 子命令与 `cli.py` 注册顺序：`list-devices, scan, channels, capture, decode, results, export, export-table, samples, status, save, load, run, list-decoders`（`cli.py:82-184`）。**`dump-schema` 在 `_COMMAND_MAP`（`cli.py:584`）里但没有注册子解析器** → `[实测]` 调用它得到 argparse 报错、**exit 2**。

## 2. 子命令逐个说明（参数 / 默认 / 取值 → 实际输出）

### 2.1 无参命令
| 子命令 | stdout（人类可读模式） | `--json` | 实测 |
|---|---|---|---|
| `list-devices` | 表头 `ID / Driver / Name / Type` + 每设备一行，Type∈`demo`\|`hardware`\|`file`；空→`No devices found.`（`cli.py:274-286`） | JSON 数组，字段 `id,driver_name,display_name,path,is_hardware,is_demo,is_file,is_active,is_compat_device,is_dsl_device,is_hardware_dso,is_hardware_logic,is_virtual,usb_speed` | exit 0 |
| `scan` | **恒定 JSON 数组**（`refresh_device_list`，`cli.py:289-291`；服务端超时 120 s） | 同左 | exit 0；实测此路径 `driver_name` 会补全（`demo`） |
| `channels` | 表 `Index/Name/Type/Enabled`，Enabled 是 `✓`/`✗`；空→`No channels (device not connected?).`（`cli.py:294-310`） | JSON 数组，字段 `index,name,type(0=logic),enabled,enabled_default,vdiv,vfactor` | **exit 1（GBK 崩，§7.2）**；加 `PYTHONIOENCODING=utf-8` 或 `--json` 才正常 |
| `status` | **恒定 JSON 对象**（`dict` 被 `_print` 转 JSON，`cli.py:451-453`） | 同左 | 字段 `state,state_code,progress,triggered,have_hardware_data,have_view_data,have_decoded_result,is_saving,is_instant,is_glitch_filter_active,is_signal_invert_active`；实测 `completed/4/1.0` |
| `list-decoders` | 表 `Name/Long Name/Channels`（443 条，`cli.py:466-480`） | JSON 数组（`id,name,long_name,channels,channel_info`） | **exit 1（GBK 崩，§7.2）** |

### 2.2 带参命令
| 子命令 | 参数（类型 / 默认 / 取值） | stdout | `--json` |
|---|---|---|---|
| `capture` | `--device`(str/**必填**)；`--channels`(str,None,`0,1,2-4` 逗号+区间，`_utils.py:104-125`)；`--analog-channels`(同)；`--rate`(str,None,`1M`/`100K`/裸 Hz，后缀 K/M/G 大小写均可，`cli.py:193-203`)；`--time`(str,None,`500ms`/`1s`/`2m`/`1h`/裸秒，`_utils.py:60-101`)；`--samples`(int,None)；`--threshold`(float,None,伏)；`--trigger`(str,None,`通道:类型` 如 `0:rising`)；`--no-wait`(flag) | JSON 状态对象（同 `status`），`--no-wait` 时多为 `capturing` | 恒 JSON（无需 `--json`） |
| `export` | `--format`(str,默认 `csv`,∈`csv`\|`binary`\|`vcd`\|`hex`\|`bits`)；`--dir`(str/**必填**)；`--digital-channels`(str,None)；`--analog-channels`(str,None) | 服务端文本 `exported`（`cli.py:383-392`） | `--json` 时输出带引号字符串 `"exported"` |
| `samples` | `--channel`(int/**必填**)；`--start`(int,0)；`--count`(int,None=到末尾)；`--type`(str,`logic`,∈`logic`\|`analog`\|`dso`)；`--format`(str,`hex`,∈`hex`\|`bin`\|`dec`，仅对 logic 生效) | 逐行文本，见 §4 | 逻辑→**单个 JSON 对象** `{"data":"<hex>","count":<字节数>}` |
| `decode` | `--protocol`(str/**必填**)；`--device`(str,None)；`--channel-map`(可多次，`名称=索引`)；`--option`(可多次，`键=值`) | analyzer 实例 ID **裸串**（如 `1:1`） | `--json` 时是 JSON 字符串（带引号） |
| `results` | `--analyzer-id`(str/**必填**)；`--max`(int,1000) | 每行 `[start-end] text1 \| text2`；空→`No annotations.`（`cli.py:361-380`） | JSON 数组（`default=str`） |
| `export-table` | `--analyzer-id`(str,None)；`--out`(str/**必填**) | `Table export complete.` 或服务端文本 | 同左 |
| `save` | `--out`(str/**必填**) | 服务端文本或 `Save complete.`（写 `.pxl` 归档） | 同左 |
| `load` | `--file`(str/**必填**) | 服务端文本或 `Load complete.` | 同左 |
| `run` | `--device`(必填)；`--channels`；`--rate`；`--time`；`--samples`(int)；`--protocol`；`--channel-map`；`--option`；`--export`(可多次，`格式:路径`) | **人类文本**：`Added decoder: …`/`Starting capture on …`/`Capture complete: completed`/`Exporting csv → <path>`/`Decoded N annotations:`（`cli.py:501-562`） | **被忽略**，仍是文本（`run` 直接 `print`） |

* `decode`/`results`/`export-table` 依赖**采集前**已 `add_analyzer`（`decode` 建议在 `capture` 之前调用，才会自动解码）。
* `export-table` **不给 `--analyzer-id`** 会走 `ValueError("Provide either 'analyzers' or 'analyzer_id'")`（`cli.py:396`→`client.py:874-880`）→ `[实测]` stderr `Unexpected error: Provide either 'analyzers' or 'analyzer_id'`，**exit 1**。

## 3. `capture` 内部到底做了什么、怎么校验

调用链 `cmd_capture`（`cli.py:313-343`）→ `PXView.capture`（`highlevel.py:176-267`）→ `McpClient.start_capture`（`client.py:564-615`）：

1. **不切工作模式、不逐个使能通道、不单独设阈值**。通道/采样率/阈值全部作为扁平参数塞进 `start_capture`：`deviceId, digitalChannels, analogChannels, digitalSampleRate, digitalThresholdVolts`（`highlevel.py:218-227`）；服务端在一次 `configure_and_start` 里落配置（`[str]` `SessionService::configure_and_start(...)`）。→ 若设备不在 Logic 模式，先手工切模式（CLI 没暴露 `switch_work_mode`）。
2. **时长 vs 样本数**：`--time` → `{"timedCaptureMode":{"durationSeconds":X}}`；否则 `--samples` → `{"manualCaptureMode":{"sampleCount":N}}`；**两者同时给时 `--time` 胜**（`elif`，`highlevel.py:231-234`）。
3. **触发**：只在 `--trigger` 解析出两段时才加 `digitalCaptureMode{triggerChannelIndex,triggerType}`（`highlevel.py:237-241`）；但 `client.start_capture` 只识别 `timedCaptureMode/manualCaptureMode/streamCaptureMode`，其余键原样透传（`client.py:598-613`），而服务器 schema **没有** `digitalCaptureMode` → **触发静默失效**（`--trigger` 不报错、也不生效）。正解见 §9。
4. **`captureMode` 值已过期**：定时分支会写 `captureMode="timed"`（`client.py:600`），而服务器 schema 明确 `'timed'/'stream' are NOT valid here`（合法值 `manual/single/repeat/loop`）。是否仍生效**未证实**（`durationSeconds` 是独立参数）；要规避可只用 `--samples`（走 `manual` 合法分支），或绕开 CLI 直接调 MCP。
5. **等待**：不带 `--no-wait` → `wait_capture(timeout_seconds=300, HTTP 超时 310)`（`highlevel.py:254-257`，300 s **硬编码、CLI 不暴露**）→ 再取 `get_capture_status()`；若 `state=="error"` 则抛 `McpError("Capture failed: <error_message>")`（`highlevel.py:258-265`）。带 `--no-wait` → 立即打印状态（多半 `capturing`）。
6. **参数校验规则**（源码事实）：
   * `--rate`：`_parse_rate` 只认末尾 `K/M/G`（`1M`→1000000）或裸整数；`1e6`/`1.5M` 之外的畸形串抛 `ValueError` → `Unexpected error: …`，exit 1。
   * `--time`：单位 `ms/s/m/min/h/hr` 或裸秒；其他单位抛 `ValueError`。
   * `--trigger`：只按 `:` 切两段，格式不对**静默忽略**（`cli.py:324-328`），不报错。
   * `--channels`：逗号/区间解析，**不校验范围**；越界或未使能通道要到 `samples`/解码阶段才以 `Failed to read logic samples` 暴露。
   * **非法采样率不会在 CLI 报错**：由驱动自行调整/钳制（`[str]` `pxlogic: adjust_samplerate`、`pxlogic: %s: setting samplerate to %llu`），真要确认只能 `status`/`get_session_status` 回读实际值。USB 带宽不足时 `[str]`：`USB bandwidth can not support current sample rate! Please reduce the sample rate!`。
   * **CLI 每次调用都重新握手**，但**不新建/不隔离会话**：`capture` 直接作用于服务器"当前活动会话"。

## 4. `export` 落盘行为（实测）

| format | 生成文件 | 大小/结构（1 通道 ×1,000,000 样本实测） |
|---|---|---|
| `binary` | `channel_<idx>.bin` / `analog_<idx>.bin` | **125,000 B＝ceil(N/8)，无头无尾**，8 样本/字节 LSB-first |
| `csv` | `channel_<idx>.csv` | libsigrok output/csv：4 行 `;` 注释头（`; CSV generated by libsigrok 0.6.0`/`; from PXLogic on <日期>`/`; Channels (1/32): 0`/`; Samplerate: 10 MHz`）＋**每样本一行**（无列头、无时间列），1M 样本＝3,000,127 B |
| `hex` | `channel_<idx>.hex` | libsigrok 文本：`libsigrok 0.6.0`/`Acquisition with 1/32 channels at 10 MHz`/`0:00 00 …`，每行 24 字节 |
| `bits` | `channel_<idx>.bits` | 同上，每行 8 字节二进制字符（`0:00000000 …`） |
| `vcd` | `channel_<idx>.vcd` | **跳变式**：`$timescale 100 ns`(＝1/采样率)、`$var wire 1 ! 0`、`#0  0!`、`#1000000`；1M 全 0 样本只有 261 B。**这是最接近 Kingst"跳变 CSV"的形式** |

* **文件名固定** `channel_<idx>.<ext>`（数字通道）/`analog_<idx>.<ext>`（模拟），取**通道索引**而非名字；`[str]` 模板 `/channel_%1.bin`、`/channel_`、`/analog_`。**每通道一个文件**，无时间戳/会话名。
* **目录**：不必预先存在，会**递归创建**（失败分支文案 `Failed to create output directory`）。但 `--dir` 用**相对路径会被服务器进程的 CWD 解析**（客户端只把 `/c/…`、`/tmp/…` 转成 Windows 路径，`_utils.py:13-37`）→ **一律传绝对路径**。
* **同名覆盖**：同目录同格式重复导出直接覆盖、无提示（实测 mtime 变化、内容随会话数据变化）。
* **`--digital-channels` 不传的后果**（实测，与 schema 描述一致）：
  * `binary` → 默认导出**全部已使能通道**：`[实测]` 未传参数时得到 `channel_0..4.bin` 各 125,000 B（正好是 5 通道 ETM 形态）；
  * `csv`/`vcd`/`hex`/`bits` → **一个文件都不生成**，但 stdout 仍是 `exported`、**exit 0**（`[实测]` 传 `--dir` 到一个空目录，文件数 0）。→ 必须显式传 `--digital-channels`，且导出后**校验文件存在与大小**。
* 无采集数据时同样"成功"：`[实测]` 生成 **0 字节** `channel_0.bin` 且 stdout `exported`。
* 非法 `--format` 由 argparse 拦截（choices），exit 2；服务端侧文案 `Unsupported export format: xyz (supported: csv, binary, vcd, hex, bits)`。

## 5. `samples` 输出格式与批量可用性

| 模式 | 输出（`cli.py:404-448`） | 实测 |
|---|---|---|
| logic + `--format hex`（默认） | 逐行 `[i] 0xNN`，**i 是字节序号，一行＝8 个样本** | `--count 16` → 3 行 `[0] 0x00` `[1] 0x00` `[2] 0x00` |
| logic + `--format bin` | 逐行 `[i] 00000000`（每字节 8 bit） | `--count 8` → 2 行 |
| logic + `--format dec` | 逐行 `[i] 0`（字节十进制） | 同上 |
| logic + `--json` | **一个 JSON 对象**：`{"data": "<打包字节的 hex>", "count": <字节数>}` | `--count 16` → `{"data": "000000", "count": 3}`（count 是**字节数**不是样本数） |
| analog / dso | 逐样本一行 `[start+i] <float>`；`--json` → JSON 数组 | – |

* `--count N` 实际取 **N+1** 个样本：`end = start + count` 且服务端 `endSample` **含端点**（`cli.py:406`），打印行数＝`ceil((N+1)/8)`（逻辑）。
* **不适合大批量**：① 文本逐行、每行仅 8 样本（1M 样本≈12.5 万行≈1 MB 文本）；② 逻辑通道给的是位打包字节，**还得自己解包**；③ 无 `--count` 上限但有 **30 s HTTP 超时钳制**（§7.3），大范围读取会超时→重试→`Connection error`。
* 批量取数请用 `export --format binary`（一次落盘、无文本膨胀），或直接调 MCP `get_samples`（单次可返回整段）。`samples` 适合点查/抽检。

## 6. 退出码与失败表现

| 退出码 | 场景 | 实测样例 |
|---|---|---|
| 0 | 成功；**也包括**"无数据也算成功"的情况（空导出、`No devices found.`、`No channels (device not connected?).`）；无子命令时打印帮助 | `export`→`exported` |
| 1 | `Unknown command`（`cli.py:605-607`）；`McpConnectionError`→stderr `Connection error: …`；`McpError`→`MCP error: <text>`；`PxvError`→`Error: …`；其它异常→`Unexpected error: <exc>`（`cli.py:609-628`） | `MCP error: Failed to read logic samples`（`samples --channel 31`）；`Unexpected error: Provide either 'analyzers' or 'analyzer_id'`（`export-table` 缺 id）；GBK 编码崩 |
| 2 | argparse 层错误：未知子命令/未知选项/缺必填/非法 choices | `dump-schema`、`nope` |
| 130 | `KeyboardInterrupt`（`cli.py:623-625`） | Ctrl+C |

* 服务器把工具级错误放在 `result.isError=true` + 文本（自带 `[…]` 类别前缀，如 `[MethodNotFound]`），CLI 统一转 `MCP error: …`；**stdout 可能有半截内容**（如 `channels` 先打了表头再崩），解析时不要只看 stdout。

## 7. 已知坑与限制（CLI 生产路径重点）

1. **并发不安全（最重要）**：所有 CLI 调用共享**服务器里同一个会话/同一份采集数据**。`[实测]` 期间另一客户端改了会话（`sample_limit` 1e6→1e8、`sample_rate` 10M→500M、清空数据），导致同一路径两次导出的 `channel_0.bin` 内容不同、并出现 0 字节文件。→ 生产端必须**自己串行化**（文件锁/单 worker 队列），`capture` 与 `export` 之间不要插入别的采集/加载动作；`export` 后校验文件大小 `== ceil(N/8)`。
2. **GBK 控制台崩溃**：`channels`（`✓`/`✗`）与 `list-decoders`（`I²C` 的 `²`）在中文 Windows 默认 GBK 下抛 `'gbk' codec can't encode character …`，**exit 1 且输出残缺**。修复：`PYTHONIOENCODING=utf-8`（实测 exit 0 且正常打印）或 `chcp 65001`，或改用 `--json`。subprocess 调用建议固定带上该环境变量并按 utf-8 解码。
3. **`--timeout` 基本无效**：普通请求实际用 `min(self.timeout, 30.0)`（`client.py:194`），所以 `--timeout 300` 仍只有 30 s；仅 `scan`（120 s，`client.py:1055`）与 `capture` 的等待（310 s，`highlevel.py:254-257`）例外。超时后会自动重试 3 次（退避 0.5/1.0 s，且会先补一次握手，`client.py:199-241`），最终 stderr `Connection error: Cannot connect to MCP server at …`、exit 1。**大导出/大范围 samples 因此有超时风险**，需要分片或改用 MCP。
4. **没有取消/停止命令**：子命令表里**没有 stop**（`cli.py:82-184`），`docs/cli-reference.md` 也没有。正在跑的采集只能用 MCP `stop_capture`、GUI，或杀掉 PXView 进程（丢数据）。**这是当前 CLI 路径的硬缺口**，生产脚本要么改用 MCP，要么保证每次采集都在设定时长内自然结束。
5. **GUI 正在运行时可以直接调**：`[实测]` GUI 在跑时上述命令全部可用——MCP 服务就内嵌在 GUI 进程里（10110/10430 由它监听）。**不要**在同一台机器再起 `pxviewd.exe`/`PXView.exe --headless` 去抢同一台 LA：PXLogic 走 libusb 独占（`[str]` `pxlogic: Failed to open device`），第二个实例打不开设备；`--auto-start` 恰是最容易触发这条路径的分支（仅在 3 s 探测失败时才会启动）。**未证实**：GUI+headless 双实例能否共存。
6. **与 Kingst 等其他 LA 的关系**：PXView 只访问自己的 PXLogic 设备，不会碰 Kingst 的 USB；但两个软件都独占各自的设备句柄，"抢设备"只会发生在**同一台 PXLogic 被两个 PXView 实例打开**时（见上）。同时使用两套 LA 时无冲突证据。
7. **`--auto-start` 起的 daemon 生命周期不可靠**：`_connect_client` 只 `proc.start()`、从不 `proc.stop()`；若 pywin32 可用，子进程被加入 Job Object（`KILL_ON_JOB_CLOSE`，`process.py:228-277`），会**随 CLI 退出一起被杀**；pywin32 缺失时可能残留。**未证实**，别依赖它常驻。
8. **设备 ID 是数字字符串**：`[实测]` `"1"`=Demo device(`is_demo`)，`"2"`=`PX_Tool PX-Logic U3 channel 32`(`driver_name=pxlogic`,`is_hardware=true`,`is_hardware_logic=true`,`usb_speed=4`)。文档/示例里的 `"demo"`、`fx2lafw:usb:0x0925:0x3881` 都不是本机 ID；ID 随热插拔变化，**每次先 `--json list-devices` 取**，选 `is_hardware=true` 那条。
9. **`run` 的 `--export` 会踩空导出**：`run` 调 `pxv.export(format, directory)` **不传 digital_channels**（`cli.py:539-544`），所以 `--export csv:./out` 等格式**什么都不生成**（binary 可用）；另外 `run` 忽略 `--json`、混打人类文本。生产请拆成 `capture` + `export` 两条命令。
10. **其它**：`save`/`load` 的 `.pxl` 是会话 zip 归档（`[str]` 4 GiB/条目限制文案），不是裸样本；`--channels` 支持区间写法 `0-4`；`decode` 需在 `capture` 前加解码器才会自动解码，`results` 前建议等解码完成。

## 8. 位打包格式（已确认，务必照此解包）

* **8 样本/字节、LSB-first、逐通道**；`channel_N.bin` **无任何头**，长度＝`ceil(样本数/8)`；与 `.pxc/.pxl` 内部编码、MCP `get_samples` 负载**同一套**（服务器说明原文 *"the same encoding as export_raw_data ('binary') and the .pxl/.pxc files"*，`[str]`）。
* 取值：`bit(n) = (data[n>>3] >> (n & 7)) & 1`；MCP 侧另有 `first_sample`（起点向下 8 对齐）/`sample_count`/`byte_count`/`truncated` 元数据（`client.py:1509-1546`）。
* 实测交叉验证：同一次采集的 `export_raw_data(binary)` 文件与 MCP `get_samples` 的 base64 解码结果 **125,000 B 逐字节相同**；`[实测]` 5 通道导出得到 `channel_0..4.bin` 各 125,000 B。
* 端序/尾字节：末字节只保证低 `sample_count & 7` 位有效（`sample_count=11` 时 `byte_count=2`），解析后按样本数截断。

## 9. 生产用法示例（subprocess + 解包，可直接抄）

```python
"""PXLogic 抓 4-bit ETM（CH0=TRACECLK, CH1..4=D0..D3）→ 位矩阵。只用 CLI。"""
import json, os, pathlib, subprocess
import numpy as np

CLI = r"C:\Program Files\PXView\pxview-cli.cmd"
ENV = {**os.environ, "PYTHONIOENCODING": "utf-8"}      # 必须：否则 channels/list-decoders 在 GBK 下崩

def cli(*args, timeout=400):                            # capture 内部等待上限 300s，外层给足
    p = subprocess.run([CLI, *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=ENV, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(f"pxview-cli {' '.join(args)} -> {p.returncode}: "
                           f"{(p.stderr or p.stdout).strip()}")
    return p.stdout

devs = json.loads(cli("--json", "list-devices"))                      # JSON 数组
hw = next(d for d in devs if d.get("is_hardware") and d.get("is_hardware_logic"))
print("device:", hw["id"], hw["display_name"])

# 1) 采集：0-4 通道、10 MHz、500 ms、3.3V 逻辑阈值（不带 --no-wait → 阻塞至完成）
cli("capture", "--device", hw["id"], "--channels", "0-4",
    "--rate", "10M", "--time", "500ms", "--threshold", "1.8")
print(json.loads(cli("status"))["state"])                             # completed / error

# 2) 导出裸数据（必须显式给通道，否则 csv 等格式静默无文件）
out = pathlib.Path(r"E:\web-etm-trace-worktrees\other-ai-f103-ui\tmp\pxv-cli"); out.mkdir(parents=True, exist_ok=True)
cli("export", "--format", "binary", "--dir", str(out), "--digital-channels", "0-4")
for f in sorted(out.glob("channel_*.bin")):
    print(f.name, f.stat().st_size)

# 3) 解包成 (N,5) 位矩阵；N 未知时用最小文件长度推样本数
raws  = [np.frombuffer((out / f"channel_{c}.bin").read_bytes(), dtype=np.uint8) for c in range(5)]
n     = min(len(r) for r in raws) * 8
bits  = np.stack([np.unpackbits(r, bitorder="little")[:n] for r in raws], axis=1)  # 列序 CH0..CH4
np.save(out / "etm_bits.npy", bits)
print("samples:", n, "shape:", bits.shape)                            # -> (N, 5)

# 4) 校验：文件名/大小自检（无数据时 CLI 会生成 0 字节文件但仍 exit 0）
assert all((out / f"channel_{c}.bin").stat().st_size > 0 for c in range(5)), "导出为空，检查会话是否被抢占"
```
* 需要触发/取消/长超时/大批量样本时，CLI 不够用 → 退回 MCP（附录）或 GUI；`samples` 仅用于抽检。
* 若 `status` 出现 `error`，先用 MCP `configure_error_state(action="get")` 取 `error_message`（CLI 无此命令）。

## 10. 附录：CLI 背后的 MCP（背景，细节从略）
* CLI 是 `pxview_automation` 包（`pxview-cli.cmd` → `python.exe -m pxview_automation.cli`）的薄封装；该包再通过 **JSON-RPC 2.0 over HTTP** 调 `PXView.exe`/`pxviewd.exe` 内嵌的 MCP 服务（默认 `http://127.0.0.1:10110/mcp`，`initialize` 协议版本 `2025-03-26`，共 **45 个工具** `[实测]`）。
* 关键等价关系：`capture`→`start_capture`+`wait_capture`；`export`→`export_raw_data`；`samples`→`get_samples`；`status`→`get_capture_status`；`save/load`→`save_capture/load_capture`；`decode/results`→`add_analyzer/get_analyzer_results`。
* CLI **缺失但 MCP 有**的能力：`stop_capture`（取消）、`configure_trigger`（真实触发，含 `configJson`）、`switch_work_mode`、`connect_device`、`set_sample_config`、`get_session_status`、`configure_error_state`、`create_session/set_active_session`。
* 直接调 MCP 的收益：`get_samples` 单次可返回整段（`[实测]` 1,000,000 样本→125,000 B/41 ms）；`export_raw_data` 参数更全（`iso8601Timestamp`、`analogDownsampleRatio`）；可用 `initialize`+`tools/list` 自取权威 schema。
* 依赖最小示例：`urllib.request` POST `{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"get_capture_status","arguments":{}}}`，响应在 `result.content[0].text`（JSON 字符串）；错误为 `result.isError=true` + 文本（如 `[MethodNotFound] …`），畸形请求才是 JSON-RPC `error`(`-32700`)。
