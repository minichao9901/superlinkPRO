# 给 AI 使用的 Trace CLI

入口：仓库根目录下运行 `python -X utf8 etm/trace.py`。

完整包 `1.0.14-native.14` 提供 `etm-trace.cmd`，等价于
`etm-bridge.exe --cli`。包内含 Python、Node.js、USB 适配依赖与原生工具，
无需在用户机器安装 Python/Node/npm。将下文 `python -X utf8 etm/trace.py`
替换成 `etm-trace.cmd` 即可；从其他目录调用时使用该 CMD 的完整路径。
例如：`etm-trace.cmd doctor`、`etm-trace.cmd --runtime E:/trace-runs/runtime-001 web status`。

本轮正式包已完成便携启动/退出、离线解码、查询和阅读导出验收；验证环境中没有系统
Python/Node PATH。使用 H743 已采记录离线核验，没有执行新的复位、烧录或采集。
此前 H743 短窗、断点及原镜像烧录有独立报告；不能将它们当作本次完整包的新硬件验收。
F103ZE/H7B0 不纳入本轮验收。最终交付范围见 `docs/etm-reading-release-2026-10-24.md`。

**硬件操作复用本工程网页的实现，不再依赖或加载 pyOCD。** 调试命令使用
`web/app/dbg/cmd.js`，调试会话使用 `DebugSession`，烧录使用网页原有
`FlashView / FlashRunner / algos.js`，Trace 配置和起点使用 `EtmTarget`。
Node USB 适配层只给同一个 `WebUsbDapProbe` 提供 WebUSB 接口，不另写 CMSIS-DAP/SWD 协议。
解码、索引、查询和导出继续默认采用已有的 C++ 优化链路。

本文范围是 **ETM 页面，以及配套的 Cortex-M 调试/烧录功能**，不是串口工具箱中所有
SPI、I2C、ADC、SWO、RISC-V 工具的统一命令行。接口开放不等于每个目标型号均经过本轮真机验收。

## 1. 网页能力对应关系

| 网页能力 | CLI 入口 |
|---|---|
| 载入 ELF、查函数/源文件 | `web load-elf`；离线 `elf inspect` |
| 函数/文件行号/地址断点 | `web command -e "b main"`、`-e "b main.c:199"`、`-e "b *0x08000100"` |
| 查看/删除断点、命中后继续 | `bl`、`bd 编号`、`bd all`、`c`，同一运行时内保留会话 |
| 暂停、复位、指令/源码单步 | `halt`、`reset halt`、`reset run`、`s`、`n`、`si`、`fin` |
| 运行到光标/指定地址 | 调试命令 `rc 文件:行`；Trace 起点 `target run-to --entry ...` |
| 复位到业务起点 | `target reset --elf ... --entry ...` |
| 寄存器、内存、变量、栈帧、观察点 | 原样传入 `r`、`md`、`mw`、`p`、`bt`、`frame`、`info locals`、`wp` 等网页命令 |
| 变量监视 | `w`、`wl`、`wd`；Native 模式在暂停状态下刷新监视值 |
| 镜像加载/内存导出 | `load_image`、`dump_image`，启动时显式提供 `--image-root` |
| 烧录 ELF/HEX/BIN | `web flash --file ... --chip ...`，执行网页烧录实现，强制校验并复位运行 |
| 时钟/ETM 配置、回读/恢复 | `target inspect/configure/restore/stop/recover` |
| 连续窗口、从断点联动采集 | `capture`；附加 `--entry`、`--reset-to-entry` |
| LA 通道、时长、采样率、阈值、相位、滤波 | `capture --clock/--data/--seconds/--rate/--threshold/--fraction/--glitch-ns` |
| 导入/重解/后配源码 | `import-record`、`decode`、`record sources` |
| 执行流、调用栈、调用统计、热点、火焰图数据 | `query --view page/event/functions/hotspots/flame/window/timeline` |
| ETM 包、可信区段、未知/边界导航 | `query --table packets`、`--view integrity/issues/navigate` |
| 查询采集总长度 | `record info`、`query --view summary` 中的 `captureExtent.actualSeconds` |
| 导出执行流、原始数据、ELF、可重解记录包 | `export`，见下文十种格式 |

CLI 额外提供批量脚本、等待指定 PC、JSON 结果、退出码、输入指纹、实际采集深度门禁、
不覆盖已有输出，以及独立的 CDC echo 负载。`web help` 返回网页当前真实命令帮助；
不要在 AI 脚本里假造网页未实现的 GDB/Tcl 命令。

## 2. 安装与启动

需要 Python 3.11+、Node.js 22+、四个原生解码工具；本轮实测 Python 3.13 / Node 24.13.1。
首次在仓库根目录安装：

```powershell
python -m pip install -r etm/requirements-cli.txt
npm ci --prefix etm/tools/node-transport
python -X utf8 etm/trace.py doctor
```

Node 依赖固定在 lockfile：`usb 3.2.1`、`linkedom 0.18.12`。后者只给网页烧录实现提供其
已有选项/结果控件的本地适配，不加载浏览器页面。没有浏览器 USB 首次授权或调试端口要求。
当前 Windows 真机通道使用 akaLinkPro 的现有 WinUSB 驱动。

原生工具位于 `etm/build-native/`：`etm_frontend.exe`、`etm_decode.exe`、
`etm_storebuild.exe`、`etm_export.exe`。缺失时沿用项目的 `etm/build-decoder.ps1` 构建。
`doctor` 检查解码依赖；`doctor --hardware` 还检查运行时探针枚举与 Kingst。

启动一个新的本地运行时目录。目录必须不存在，端口必须空闲：

```powershell
python -X utf8 etm/trace.py --web-url http://127.0.0.1:8878 web start `
  --runtime-dir E:/trace-runs/runtime-001 `
  --probe 8F538633E6AEC4CA8ABB6A36FEBB1AC2 `
  --image-root E:/trace-runs/images
```

`--image-root` 是可选的、已存在的目录，只影响 `load_image/dump_image`。
启动不烧录、不复位、不自动连接目标；硬件动作由后续明确命令执行。

随后每次调用都指定同一运行时目录：

```powershell
python -X utf8 etm/trace.py --runtime E:/trace-runs/runtime-001 web status
python -X utf8 etm/trace.py --runtime E:/trace-runs/runtime-001 web connect
python -X utf8 etm/trace.py --runtime E:/trace-runs/runtime-001 doctor --hardware
```

停止运行时：

```powershell
python -X utf8 etm/trace.py web stop --runtime-dir E:/trace-runs/runtime-001
```

运行时保存 `runtime.json`、`host.log` 和本机令牌。停止请求绑定该目录的实例标识；不会按
一个可能复用的旧 PID 强杀进程。正在写 Flash 时不会强行断电/杀 worker；清理超时会明确报错，
继续查看 `runtime.json/host.log`。不要将令牌文件提交、打进记录包或传给远端。

### 已有本地网页服务的接法

也可以显式启动带队列的同源工作台服务，再运行 Native worker：

```powershell
python -X utf8 web/tools/dev/serve-etm.py --port 8878 --cli-token-file E:/trace-runs/cli-token
node etm/tools/node-transport/worker.mjs --web-url http://127.0.0.1:8878 `
  --token-file E:/trace-runs/cli-token --probe 8F538633E6AEC4CA8ABB6A36FEBB1AC2
python -X utf8 etm/trace.py --web-url http://127.0.0.1:8878 `
  --token-file E:/trace-runs/cli-token web status
```

以上服务/worker 是前台命令，应放在独立终端或受管理的进程中。
服务默认不开放命令队列，只有提供 `--cli-token-file` 才启用，且只监听本机回环地址。

可选的浏览器 worker 供控制**当前网页会话**时使用：打开同一个本地服务的
`/?trace-cli=<令牌内容>#etm`。它也调用同一适配器，由网页的 ProbeManager 仲裁。
一个服务只能绑定一个 worker；Native worker 和浏览器 worker 不能同时绑定这个队列。
首次 WebUSB 授权仍由浏览器负责。普通打开 `/#etm` 不会自动成为 CLI worker。

Native 模式持有的是自己的探针句柄，不能与另一个浏览器/调试器同时操作同一探针。
CLI 还使用 Git 公共目录中的 `trace-cli-hardware.lock/`，防止本仓库不同 worktree 的
CLI 并行操作硬件；锁不会约束仓库以外的调试工具，也不会自动抢占旧租约。
便携 CLI 不需要 Git；租约放在完整包的 `data/trace-cli-hardware.lock/`。
同一设备不能被不同完整包实例并行操作，系统 USB 占用错误也不会触发抢占。

## 3. 输出和失败契约

除 `--help` 外，stdout 只有一份 JSON，进度/日志写 stderr。
全局参数 `--runtime`、`--web-url`、`--token-file`、`--json-out` 放在子命令前面。

```powershell
python -X utf8 etm/trace.py --runtime E:/trace-runs/runtime-001 `
  --json-out E:/trace-runs/result-001.json web command -e "info" `
  1>E:/trace-runs/stdout-001.json 2>E:/trace-runs/stderr-001.log
```

结果有 `protocol: trace-cli/1`、`runId`、`command`、`ok`、`exitCode`、`elapsedSeconds` 和
`result` 或 `error`。`--json-out`、导出文件和新记录目录均拒绝覆盖。批量命令在首个错误/取消
处停止，已经执行的步骤保存在错误详情，不能把后续未执行命令当成成功。

| 退出码 | 含义 |
|---|---|
| 0 | 命令成功；仍要检查适用的质量、实际时长与验收范围 |
| 2 | 参数、前置条件、范围不符 |
| 3 | 缺依赖/原生程序 |
| 4 | 传输、设备、IO、恢复不确定或运行时错误 |
| 5 | 实际窗口、输入指纹或解码质量不合格 |
| 130 | 用户中断 |

硬件等待支持 `web cancel`，取消可越过正在执行的 CLI 硬件租约。取消作用于调试等待和
Trace 起点等待，不承诺中断已经开始的 Flash 擦写。超时后不要自动重试写入操作。
采集目录保留 `session.json`、`target-state.json`、`cleanup.json`、失败时的 `cli-error.json`。
`laIdle: null` 表示尚未启动或尚未确认，不能说已经确认空闲。

## 4. 调试与烧录

```powershell
python -X utf8 etm/trace.py --runtime E:/trace-runs/runtime-001 web load-elf --file E:/firmware/fw.elf
python -X utf8 etm/trace.py --runtime E:/trace-runs/runtime-001 web command `
  -e "halt" -e "b main" -e "bl" -e "c"
python -X utf8 etm/trace.py --runtime E:/trace-runs/runtime-001 web wait-halt --timeout 15
python -X utf8 etm/trace.py --runtime E:/trace-runs/runtime-001 web command `
  -e "s" -e "r pc" -e "bd all" -e "c"
```

会话、符号、断点和监视保存在运行时里，后续 CLI 调用不会重新建立调试器并清空它们。
`web command --script commands.txt` 逐行执行，空行和以 `#` 开始的注释行跳过；
可以与重复的 `-e/--execute` 混用。先执行 `-e`，再执行文件里的命令。

源码级 `n/si/fin`、局部变量和行号断点依赖匹配的 ELF/DWARF；优化掉的行/变量会明确报错，
不猜相邻行、不编造变量值。裸地址的读内存和硬件断点可以不载入 ELF。
`load_image` 是网页的内存加载命令，不替代 Flash 擦写算法；烧录必须用 `web flash`。

```powershell
python -X utf8 etm/trace.py --runtime E:/trace-runs/runtime-001 web flash `
  --file E:/firmware/fw.elf --chip stm32h7
# BIN 必须明确给基地址：
python -X utf8 etm/trace.py --runtime E:/trace-runs/runtime-001 web flash `
  --file E:/firmware/fw.bin --chip stm32h7 --base 0x08000000
```

`--chip` 使用网页烧录器的选项值，例如 `stm32h7`、`stm32h7b0`、`stm32f103`。
不要把 Trace profile `stm32h743` 当成烧录器 chip 值。Native 只接出已有的 Cortex-M
WebUSB 算法；需要 OpenOCD/J-Link 或 RISC-V 烧录后端的型号不在本 CLI 的这条烧录路径中。
带空洞的 HEX 在无人值守时会拒绝需要交互确认的情况，优先使用完整 ELF/BIN。

## 5. Trace 配置、起点与采集

离线列函数和解析精确断点，不连接硬件：

```powershell
python -X utf8 etm/trace.py elf inspect --elf E:/firmware/fw.elf --search trace_hook
python -X utf8 etm/trace.py elf resolve --elf E:/firmware/fw.elf --spec "main.c:199"
```

Trace 起点解析复用网页的严格解析器。遇到多个候选会返回候选列表，使用 `--address 0x...`
明确选择。Trace 起点只接受匹配 Flash ELF 中可确认的指令，不猜相邻源码行。

```powershell
python -X utf8 etm/trace.py --runtime E:/trace-runs/runtime-001 target inspect --hse 25
python -X utf8 etm/trace.py --runtime E:/trace-runs/runtime-001 target run-to `
  --elf E:/firmware/fw.elf --entry trace_hook_entry
python -X utf8 etm/trace.py --runtime E:/trace-runs/runtime-001 target reset `
  --elf E:/firmware/fw.elf --entry trace_hook_entry --timeout 15
```

`target reset` 是“复位后停到指定入口”，`web command -e "reset halt"` 是普通调试器的
“复位并停核”，两者不同。业务采集起点应位于时钟初始化之后；停在初始化早期时，实际时钟
可能仍是 HSI 64MHz。HSE 参数只参与计算，不会替你改 PLL。

Trace 控制沿用网页固定的 1MHz SWD 路径，拒绝其它 `--swd-hz` 值。
调试会话可在 `web connect --swd-hz ...` 时选择其它档位；已经连接时变更档位应先断开再连接。

普通连续运行窗口：

```powershell
python -X utf8 etm/trace.py --runtime E:/trace-runs/runtime-001 capture `
  --elf E:/firmware/fw.elf --out E:/trace-runs/capture-001 `
  --seconds 0.2 --rate 500000000 --expect-core-hz 60000000 --expect-trace-hz 30000000
```

从业务断点开始：

```powershell
python -X utf8 etm/trace.py --runtime E:/trace-runs/runtime-001 capture `
  --elf E:/firmware/fw.elf --out E:/trace-runs/capture-002 `
  --entry trace_hook_entry --reset-to-entry --entry-timeout 15 `
  --seconds 1 --rate 500000000 --clock 0 --data 1 2 3 4 `
  --threshold 1.65 --fraction 0.25 --glitch-ns 4 `
  --expect-core-hz 60000000 --expect-trace-hz 30000000 `
  --source-root E:/superlinkPRO
```

有 `--entry` 时先停到起点，配置 Trace，**先发 LA start，再放行 CPU**；使用 D0 下降沿触发，
同网页顺序。不加 `--reset-to-entry` 则等待下一次到达这个入口。无 `--entry` 时预先运行目标，
采集持续运行的窗口。`--no-decode` 只采集保存。采集实际 KVDAT 深度必须与请求和设备回读一致，
不能把 LA 的 set-time ACK 当成已经录满。CLI 上限 1s / 5亿样本 / 500MSa/s，未录满返回 5，
原始波形保留。完整短窗验收不等于所有负载的 1s 都一定录满。

默认 Trace ID 2、GPIO speed 2、return-stack 和 cycles 开启、branch-broadcast 关闭。
与网页相同的 profiles 全部可选：`auto/stm32h743/stm32h7b0/stm32f103ze/stm32f1/stm32f4`；
目标不支持 Trace/选项时由网页目标逻辑拒绝。例如 F103ZE 应显式加
`--no-return-stack --no-cycles --hse 8`，本轮没有扩称它已经完成 CLI 真机验收。

仅在需要将目标留在配置状态供外部采样时：

```powershell
python -X utf8 etm/trace.py --runtime E:/trace-runs/runtime-001 target configure `
  --elf E:/firmware/fw.elf --state E:/trace-runs/target-001.json --resume
python -X utf8 etm/trace.py --runtime E:/trace-runs/runtime-001 target restore --state E:/trace-runs/target-001.json
```

状态绑定探针、目标、ELF 指纹和期望寄存器；目标寄存器已变化时拒绝旧状态覆盖。configure 不带
`--resume` 保持暂停。恢复成功后状态标为已用。新的 configure/capture/起点操作拒绝覆盖尚待恢复的
配置；可以 restore，或用 `target stop` 明确停止 Trace 并放弃这份原始快照。
`target stop --halt` 同时保持 CPU 暂停；`target recover` 恢复调试口并保持暂停。
起点等待失败时，保留暂停并清理本次临时断点，不自动把失败位置当成采集起点。

可选 `--echo-port COMxx --echo-interval 0.004` 使用带序号/CRC 的完整行发送、回收和保存。
必须选目标 CDC，不是探针 COM 口；CLI 拒绝 VID 0D28 的探针串口。周期 banner 由固件实现。
如果 reset 后尚未初始化 CDC，不要在该早期入口要求 echo 预热；端口不存在会明确失败。

## 6. 离线、记录、时长、源码

这些命令不要求运行时、探针或 LA：

```powershell
python -X utf8 etm/trace.py record list --root E:/trace-runs
python -X utf8 etm/trace.py record info --record E:/trace-runs/capture-001
python -X utf8 etm/trace.py record config --record E:/trace-runs/capture-001 --out E:/trace-runs/config-001.json
python -X utf8 etm/trace.py decode --record E:/trace-runs/capture-001 --out E:/trace-runs/redecoded-001
python -X utf8 etm/trace.py decode --input E:/capture.kvdat --elf E:/firmware/fw.elf `
  --config E:/trace-runs/config-001.json --out E:/trace-runs/imported-001
python -X utf8 etm/trace.py verify --record E:/trace-runs/capture-001 --require-decoded
python -X utf8 etm/trace.py import-record --input E:/record.etmrecord --out E:/trace-runs/imported-002
```

`captureExtent.actualSeconds × 1000` 是实际毫秒数，`requestedSeconds` 是请求时长。
`known: false` 表示旧数据缺足够信息，不根据指令数量或火焰图宽度猜毫秒数。
Trace 中的指令序号、调用权重都不是时间。旧内联/JSONL 历史通过网页原有 RecordStore 查询；
会重建派生索引，原始 decoded/JSONL 保留。旧记录没有当前 TraceStore 绑定时会标注未核验，
可以重解到新目录获取默认优化格式；不把旧格式冒充当前已绑定原生记录。

后配源码：

```powershell
python -X utf8 etm/trace.py record sources --record E:/trace-runs/capture-001 --source-root E:/superlinkPRO
# ELF来自另一台机器时，显式给“ELF文件路径 → 源码正文”的字典：
python -X utf8 etm/trace.py record sources --record E:/trace-runs/capture-001 --sources-json E:/sources.json
```

`--source-root` 只读取该根目录内、ELF 记录的实际路径，不按同名文件猜配对。
后配源码保存旧 sources 的独立备份，不修改采集波形、固件或执行流。无配套源码时源码类
导出明确拒绝；CSV、逐事件 JSONL、机器码和原始记录仍可查询/导出。

## 7. 查询和导出

源码学习可用 `export --format simplified|html --readability learning`，同时生成正文、序号映射和
指纹清单；C/HTML统一进行重复块折叠、无源码片段摘要、条件未知归组及可选源码/宏注释。
默认旧模式保持兼容。规则、参数、离线HTML边界见 [Trace阅读说明](trace-reading.md)。
`capabilities` 查看实际工具协议与分页限制；`query --compact` 精简摘要，`--fields` 选择点分字段。
阅读能力已纳入 native.14 正式完整包，旧 rc.1 包不含这些参数；先检查
`capabilities` 的 `softwareCapabilities.readingProjection.available` 和实际下载版本。
简化 C 不逐行插入序号/文件行号注释；精确定位在配套映射中。函数宏按需用
`--macro-view expanded` 显示展开注释，默认仍保持紧凑。

```powershell
python -X utf8 etm/trace.py query --record E:/trace-runs/capture-001 --view summary
python -X utf8 etm/trace.py query --record E:/trace-runs/capture-001 --view page --offset 0 --limit 100
python -X utf8 etm/trace.py query --record E:/trace-runs/capture-001 --view page --table packets
python -X utf8 etm/trace.py query --record E:/trace-runs/capture-001 --view event --seq 100
python -X utf8 etm/trace.py query --record E:/trace-runs/capture-001 --view navigate --seq 100 --mode call --direction 1
python -X utf8 etm/trace.py query --record E:/trace-runs/capture-001 --view hotspots
python -X utf8 etm/trace.py query --record E:/trace-runs/capture-001 --view window --from-seq 100 --to-seq 2000
```

可用视图：`summary/overview/page/event/navigate/window/timeline/issues/source/functions/hotspots/flame/integrity`。
导航模式 `instruction/call/return/exception/issue/source/pc`；PC 导航加 `--pc 0x...`。
`--search`、`--hide-skipped`、`--from-seq/--to-seq` 对应网页筛选、隐藏未成立分支和局部区间。
`flame` 返回调用权重树；执行顺序图使用 `timeline`。这些 JSON 供 AI 分析，不另开 UI。

```powershell
python -X utf8 etm/trace.py export --record E:/trace-runs/capture-001 --format simplified --out E:/trace-runs/simplified.c
python -X utf8 etm/trace.py export --record E:/trace-runs/capture-001 --format html --out E:/trace-runs/flow.html
python -X utf8 etm/trace.py export --record E:/trace-runs/capture-001 --format record --out E:/trace-runs/capture.etmrecord
```

十种格式：`simplified/source/html/csv/json/events-jsonl/raw/etm/elf/record`。
前四种与网页导出同义；简化 C 按连续源码位置合并，完整 C 按执行指令保留重复源码行，均不是可编译程序。
`json` 是摘要/分析/源码 JSON；逐事件输出用 `events-jsonl`。
C/HTML/CSV/JSONL 可按完整序号区间导出，其余格式拒绝执行序号裁剪。
`.etmrecord` 保存原始输入、匹配固件、配置、源码和已有解码存储，可再次离线重解，也可由网页导入。

## 8. 验证边界

原生路径默认开启，仍逐份核验输入指纹、store 绑定和质量。overflow、解码错误、未核算字节、
中途失同步、framing/时钟间隙不合格；仅确认为输入尾部的 `I_INCOMPLETE_EOT` 单列保留。
未知执行状态、采集前祖先不会改成已证实执行。

本轮 H743 真机详细结果、实际命令、原始波形、Flash 回读指纹、错误案例和未覆盖项，见
`docs/validation/20261024-000104-trace-web-cli-9f57c7/report.md`。
API 开放范围与真机验收范围在报告中分别列出，不能把软件模拟/离线测试当成每个型号、
每个 DWARF 优化场景均已板测通过。
