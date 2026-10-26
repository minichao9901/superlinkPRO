# superlinkPRO：AI 使用的统一 CLI 总指南

总入口是仓库根目录 **`toolbox.py`**。本文件是给 AI 的第一阅读入口；
全部页面的分类、静态控件和方法源码位置集中在 **`docs/toolbox-cli-reference.json`**。
逐条命令、全部参数、默认值、可选值和必填/互斥规则见
**[CLI 命令与参数完整参考](toolbox-cli-options.md)**（从真实解析器生成，覆盖63个命令/分类节点）。
运行时的 `page <分类> describe` 返回当前页面真实控件、当前选项、公开方法和可用子对象，优先于静态清单。

这里提供两种执行方式：`trace` 原样复用成熟的 ETM CLI；`page` 通过本机授权队列调用
正在运行的网页。后者覆盖全部 15 个工具页面的控件及公开方法，共用网页协议、会话和资源协调。
它需要浏览器，不能等同于所有功能均已实现独立的无浏览器驱动。此次没有新增真机验收。

## 1. AI 的执行规则

1. 先 `catalog`，再对所需页面 `describe` / `state`；依据返回结果选择控件和参数，不猜控件 ID、方法签名、硬件型号。
2. 修改参数用 `set`，文件用 `upload` 或 `invoke` 的 `$file`，普通操作用 `click`；可用的方法优先 `invoke`，它会等待原方法返回。
3. **`ok:true` 表示 CLI 请求处理成功，不保证硬件任务成功。** `click/set/upload/event` 返回 `completed:false`，仅表示事件已投递。
   `invoke` 返回 `returned:true`，原方法仍可能自行捕获错误，或只启动后台采集。必须继续检查页面状态、日志、计数、质量和导出结果。
4. 超时后不能重试烧录、写寄存器、发总线事务、复位或采集。使用返回错误中的 `job ID` 查询；任务可能仍在执行。
5. 只有用户已授权的任务才执行硬件写入。页面会话、资源互斥和原有错误规则保持有效；不要用两个工作台/原生 worker 同时抢同一探针。
6. 正在记录或烧录时保持页面打开。退出服务/关闭页面不是可靠的停止记录方式；先正常停止并确认数据落盘。

## 2. 启动与最短路径

在仓库根目录执行（Python 3.11+；Web 运行需要桌面 Chrome / Edge）：

```powershell
python -m pip install -r etm/requirements-cli.txt
python -X utf8 toolbox.py catalog
python -X utf8 toolbox.py --runtime E:/trace-runs/toolbox-001 serve --port 8896
```

`serve` 前台运行，复用现有完整工作台服务和 ETM 后端。运行时目录必须不存在；不要把
`trace web start` 的原生运行时目录填到这里。启动输出一个 `browserUrl`，在 Chrome / Edge
打开并保持此页面；它带一次本机会话令牌，不要复制到公共报告。初次连接 USB/HID/串口，
须在网页完成浏览器设备授权。网页可再次连接已授权设备，CLI 不能制造浏览器所要求的真实用户手势。
一套服务只允许一个执行页面。原标签页刷新复用身份；新开/复制标签页视为另一个执行页面，
更换执行页面需重启服务并使用新运行时目录。刷新前必须先停止任务、保存下载，页面内的硬件事务不会因身份复用而自动恢复。

另开终端：

```powershell
python -X utf8 toolbox.py --runtime E:/trace-runs/toolbox-001 ping
python -X utf8 toolbox.py --runtime E:/trace-runs/toolbox-001 page serial describe
python -X utf8 toolbox.py --runtime E:/trace-runs/toolbox-001 page serial state
```

`ping` 不枚举或操作设备；`status` 沿用 Trace 工作台摘要，可能枚举已授权 USB 设备。
服务只绑定 `127.0.0.1`，队列要求本机地址、同源请求及 64 位十六进制令牌。
运行时应放在仓库外或忽略的临时目录，不能公开其 `cli-token`。

## 3. 全部功能分类

以下分类名在 `page <分类>` 中使用。控件覆盖参数、动作、分析、导入导出和页面上的分项工具；
动态表格、接入后新增选项和生成的控件，应在生成后重新 `describe`。

| 分类 | 网页 | 功能范围 / 推荐入口 |
|---|---|---|
| `serial` | 串口助手 | 端口和参数、ASCII/HEX/ANSI、时间戳、定时/快捷发送、DTR/RTS、接收/记录；`view.sendText`、`session.write` |
| `terminal` | 终端 | 交互输入、ANSI、回显、键盘映射、粘贴、清空/保存；`event` 和共享 `session` |
| `rtt` | RTT Viewer | 探针/本地桥/模拟目标、RTT 搜索和通道、上下行、ELF、接收记录 |
| `rttcdc` | RTT 转发 | `view` 管 CDC 数据；`hid` 管目标/时钟、转发启停/状态；共享串口 `session` |
| `spicdc` | SPI 转发 | `session` 管从机 SPI/DMA 配置与启停；`stream` 管接收显示/记录；`serial` 管 CDC |
| `scope` | J-Scope | ELF/DWARF 变量、标量/数组/成员、1–8 通道、速率标定、采样、触发/游标、分道/缩放、CSV/原始包和质量报告 |
| `dbg` | 调试器 | ARM/RISC-V、源码/指令断点、暂停/继续/单步、寄存器/内存、监视/局部变量、调用栈、SVD、异常诊断；Cortex-M 也可直接使用 `trace web command` |
| `flash` | 烧录器 | ELF/HEX/BIN、芯片/算法、基地址、擦除/编程/校验、复位、IDCODE |
| `swo` | SWO PC 采样 | CPU/Trace 时钟、双端速率和带宽规划、起点/采样、`.swopc`、ELF/源码、离线分析、热点/区间/回放、CSV/C/HTML 等当前页面支持的导出 |
| `etm` | ETM 执行轨迹 | 目标 ELF/断点/时钟、LA 采样、原始/记录包导入、解码、执行流/调用/火焰/热点/包/质量、源码、全部导出；批处理优先 `trace` |
| `spi` | USB SPI/QSPI | 连接/参数/使能、单/双/四线帧、命令表、寄存器、DSL/周期脚本、实时值、NOR ID/SFDP/读写擦除/速度、回环 |
| `panel` | SPI/QSPI 屏 | 与 `spi` 共用 `session`；面板档、初始化表解析/重放/导出、复位/背光/电源、图案/图片/动画/视频/局部更新、寄存器与 GRAM 读回 |
| `i2c` | USB I2C | 扫描、地址/速率、读写事务/长读写、命令表、寄存器、DSL/周期任务、实时值 |
| `analog` | USB ADC/DAC | ADC 配置/单次或连续采集、时压游标/测量/触发、CSV；DAC 预览、波形参数、启停/状态、波形 CSV；协议操作用 `session` |
| `gen` | 工程生成 | `.uvprojx`、路径/参数、生成 Makefile/脚本、预览、单文件/ZIP、本地桥包/ETM 桥包下载 |

控制按钮、图形视图并不都对应一个可单独调用的方法。闭包内动作通过控件接口执行，
画布/终端交互通过 `event`；方法需要 DOM、回调或浏览器设备对象时也应采用控件接口。
全屏、剪贴板、首次设备选择、系统文件夹选择等浏览器特权操作仍需用户手势；不要把这类限制当成目标板故障。

## 4. 通用接口完整说明

全局参数须放在子命令前：`--runtime <目录>`、`--timeout <秒>`（默认 60）、
`--out <新文件>`（JSON 结果，拒绝覆盖）。返回退出码 0 / 1 分别代表请求处理成功 / 失败；
命令参数解析错误为 2。`trace` 保持原 Trace CLI 的退出码。

| 命令 | 参数 / 返回 |
|---|---|
| `catalog [--full]` | 无需服务；返回全部分类和功能范围；--full 输出包含静态控件/选项/方法源码位置的总清单 |
| `serve --port 8896` | 创建运行时并在前台运行本机服务；正常停止硬件后按 Ctrl+C 退出 |
| `ping` / `status` | 执行页面就绪 / 工作台摘要 |
| `page P describe [--target view]` | 当前控件 `selector/type/value/options/disabled/label`，当前公开方法及 `targets` |
| `page P state` | 摘要、最近全局异常和页面文字；页面自己捕获的错误可能只出现在文字/日志中 |
| `page P show` | 切换显示页面；仅投递，不保证异步 `onShow` 已完成 |
| `page P read SELECTOR` | 唯一控件/结果区域的当前值与文本，最多 100000 字符 |
| `page P set SELECTOR JSON_VALUE` | input/textarea/select 参数；checkbox 必须为 JSON `true/false`；触发 input 和 change |
| `page P set SELECTOR FILE --value-file` | 从 JSON 文件读取值，避免 PowerShell 字符串转义 |
| `page P click SELECTOR` | 点击唯一且未禁用的控件；不等待异步处理器完成。radio/分段选项用 click |
| `page P interrupt SELECTOR` | 长任务期间请求停止，只允许 describe.interrupts 中的停止按钮；不强制中断烧录/擦写 |
| `page P upload FILE_INPUT FILE...` | 本地文件放入真实 file input，并触发 change；`--directory` 接一个源码目录，保留相对路径 |
| `page P invoke METHOD [--target T] [--args JSON_ARRAY / --args-file FILE]` | 调用 describe 公开的原方法，等待其 Promise，返回 JSON 化结果；默认 target=view |
| `page P wait SELECTOR --contains TEXT` | 轮询结果区域文字直到匹配或全局 timeout；不是采集质量判据 |
| `page P event SELECTOR TYPE [--data JSON_OBJECT]` | 键盘、鼠标、pointer、wheel、input/change；x/y 为控件左上角相对像素 |
| `page P downloads` | 本次 CLI 工作台拦截并暂存的 Blob 下载清单 |
| `page P download ID DESTINATION` | 每块 1 MiB 保存，完成输出字节数和 SHA256；先写 `.partial`，成功后改名，拒绝覆盖 |
| `page P discard ID` | 释放浏览器中一个暂存 Blob；下载成功后再释放 |
| `request FILE.json` | 提交完整请求对象，既可用 workbench 动作，也可用已有 Trace 浏览器动作 |
| `batch FILE.json` | 顺序执行 1～1000 条请求，首个 CLI 错误停止，保留已完成结果；不是回滚事务 |
| `job ID` | 查询 queued/running/done 和结果，不重做操作 |
| `ack ID` | 清除已经完成的 job，释放队列容量 |
| `trace ...` | 原样转交现有 ETM CLI，详见第 7 节；必须紧接 toolbox.py |

`event` 支持：keydown/keyup、mousedown/mousemove/mouseup、pointerdown/pointermove/pointerup、
wheel、click/dblclick、input/change。例如键盘参数 `{"key":"Enter","code":"Enter"}`，
拖动应依次提交 down/move/up；元素需先显示、布局就绪，坐标来自实际页面尺寸。

默认 `target=view`。`session` 在存在时指向原页面会话；额外有上表中的 hid/stream/serial、
SWO 的 capture/sources、ETM 的 target、存在时的 store。以 `describe` 返回的 targets 为准，
禁止任意对象路径、构造函数和私有方法调用，不执行 eval。方法签名只是原函数提示，不是完整类型模式；
涉及复杂参数时先看清单所指源码，不能直接猜参数。

`describe.globalControls` 还列出全局复位、NRST 模式、全屏、帮助等公共控件，可在任一分类中
按其 selector 使用；浏览器全屏仍受用户手势限制。部分页面把文件 input 放在 body 而非本页内，
会以 `@file:view._fileInput`、`@file:hid._elfInput` 等虚拟 selector 出现在 controls 中，
使用 describe 返回的原字符串传给 upload；这些是受控文件入口，不是任意对象访问。

## 5. 示例：参数、文件、导出

### 无硬件预览 ADC/DAC 波形

```powershell
python -X utf8 toolbox.py --runtime E:/trace-runs/toolbox-001 page analog describe
python -X utf8 toolbox.py --runtime E:/trace-runs/toolbox-001 page analog invoke preview
python -X utf8 toolbox.py --runtime E:/trace-runs/toolbox-001 page analog state
```

### 串口发送

完成网页串口授权/连接后，准备 `send.json`，内容为 ` ["hello\r\n"] `：

```powershell
python -X utf8 toolbox.py --runtime E:/trace-runs/toolbox-001 page serial invoke sendText --args-file send.json
```

HEX 模式仍按页面现有规则解释；精确字节可用 session 的 `write` 加 `$file kind=bytes`。
不要假定 `sendText` 会覆盖网页 HEX/EOL 设置，先读页面控件。

### 导入文件 / 源码目录

先 `describe` 查当前文件 input 的 selector，然后用 `upload`。也可以直接调用
支持 File/ArrayBuffer/Uint8Array 的原方法：

```json
[{"$file":"E:/my-project/firmware.elf","kind":"buffer"},"firmware.elf"]
```

将这段存为 `elf-args.json`：

```powershell
python -X utf8 toolbox.py --runtime E:/trace-runs/toolbox-001 page swo invoke loadElf --args-file elf-args.json
```

`$file` 由 Python 读取，`kind=file/buffer/bytes` 分别生成浏览器 File/ArrayBuffer/Uint8Array。
`upload --directory` 保留目录名和下级相对路径；不要上传整个 SDK/构建目录。单文件限制 24 MiB，
总请求限制 47 MiB，ETM 大 KVDAT、记录包应使用 `trace import-record/decode`，不会绕 Base64。

### 导出下载

进入某页面的 CLI 操作后，该页通过 Blob 链接产生的导出会改为 CLI 暂存，而不会自动下载到浏览器默认目录。
例如串口已有接收文本：

```powershell
python -X utf8 toolbox.py --runtime E:/trace-runs/toolbox-001 page serial invoke save
python -X utf8 toolbox.py --runtime E:/trace-runs/toolbox-001 page serial downloads
python -X utf8 toolbox.py --runtime E:/trace-runs/toolbox-001 page serial download 1 E:/trace-runs/serial.txt
python -X utf8 toolbox.py --runtime E:/trace-runs/toolbox-001 page serial discard 1
```

每个页面 window 最多保留 32 个 Blob，URL revoke 后仍能下载，刷新页面会丢失暂存。
直接 HTTP 链接下载的桥包仍走浏览器原有下载；系统文件选择式的持续记录仍需网页用户选择位置，
不能把 downloads 清单当作已落盘的持续记录。大 ETM 导出优先 `trace export --out`。

## 6. 批处理、完成判定与故障恢复

请求文件是分类清晰的 JSON，可供其他 AI 直接生成：

```json
[
  {"action":"workbench","page":"analog","op":"describe"},
  {"action":"workbench","page":"analog","op":"invoke","method":"preview","args":[]},
  {"action":"workbench","page":"analog","op":"state"}
]
```

```powershell
python -X utf8 toolbox.py --runtime E:/trace-runs/toolbox-001 --out E:/trace-runs/batch-result.json batch actions.json
```

`batch` 会先展开/检查本地文件引用，然后顺序提交，不能撤销已经完成的硬件动作。
事件投递不等于完成，不能在 click 后立即假定可导出。需要等待结果时拆成独立的 wait/state，
或使用可等待的原方法，再核对成功文本、计数和质量。原方法自行显示错误时，batch 不会自动理解所有页面日志。

长任务期间允许 `read/state/downloads/interrupt`，普通设置和硬件命令仍串行互斥。
`interrupt` 只投递原页面的停止操作，仍需确认原任务和数据写入已结束；已有 Trace 目标事务
优先使用原 CLI 的取消/恢复流程，不允许借页面操作抢占。Flash 擦写没有通用强制终止接口。

返回示例：

```json
{"job":"32位十六进制ID","ok":true,"value":{"dispatched":true,"completed":false}}
```

队列最多保存 200 个 job，成功的单条命令默认保留结果以便追查；AI 应在结果保存后 `ack ID`。
batch 内成功项、wait 中间轮询和下载分块会及时 ack。已完成但未 ack 的结果最多保留一小时，
新提交时清除过期项。排队容量为 20；worker 缺失时拒绝提交，避免之后意外执行。

超时错误带 job ID；使用 `job ID` 读取，等待原任务正常结束。不能通过“再点一次”解决超时。
下载失败保留 `.partial` 便于判断；新下载须换新路径，不覆盖已有证据。
重启服务不恢复内存队列和 Blob，硬件是否停止需单独确认。

## 7. 完整 ETM 原生 CLI

所有现有 Trace 子命令保持可用：

```powershell
python -X utf8 toolbox.py trace --help
python -X utf8 toolbox.py trace capabilities
python -X utf8 toolbox.py trace doctor
```

详细参数和工作流见 **`docs/trace-cli-ai.md`**；`trace --help` 和各子命令 `--help` 为准。
包含 `web` 运行时/ELF/调试命令/烧录、`target` 配置/读回/恢复/起点、`capture`、
`import-record`、`decode`、`record`、`query`、`export`、`verify`、`elf` 等现有能力。
JSON 字段筛选、紧凑查询、阅读折叠/宏、证据映射、各格式导出全部继续复用原实现。

注意两个运行时：`toolbox.py --runtime ... serve` 为**浏览器执行工作台**；
`toolbox.py trace --runtime ... web ...` 为原 **Node/WebUSB 原生执行工作台**。
目录和 worker 不能混用，两者不能同时占用同一探针。
当前已发布 `1.0.14-native.14` EXE 提供原 Trace CLI；本次新增 `toolbox.py` 是源码入口，
尚未声称旧 EXE 已包含本次新入口，也不自动替换线上 ZIP。

## 8. 接口维护和验证

总清单由真实页面 HTML 和源码生成，避免重复手工维护数百控件：

```powershell
node tools/build_toolbox_reference.mjs
node tools/build_toolbox_reference.mjs --check
python -X utf8 tools/build_cli_options.py --check
node --test web/tools/selftest/workbench-cli.test.mjs web/tools/selftest/trace-cli-automation.test.mjs
python -m unittest discover -s tools/tests -p test_toolbox_cli.py
python -m unittest discover -s etm/tools/tests -p test_web_cli_queue.py
```

静态清单覆盖全部 15 页；动态控件、方法子对象、实际可用性始终由 describe 补全。
新增网页页面时必须同步 `toolbox.py` / `workbench.js` 分类，检查会发现遗漏。
现有浏览器权限和真实设备条件仍适用；离线/模拟测试不能替代各硬件接口的真机验收。

### 2026-10-24 本轮验收

- 26 项 JavaScript 测试、8 项统一 CLI / HTTP 传输测试、5 项既有队列测试通过。
- 真实浏览器中全部 15 个页面 describe 成功；动态控件也能枚举。
- 真实 `.uvprojx` 经文件 input 导入并解析成功。
- 烧录器挂在页面外的文件 input 可经虚拟 selector 载入小镜像，仅载入、未烧录。
- 无设备连接时修改 DAC 预览频率、生成波形、导出 CSV，经 CLI 分块落盘后与浏览器 Blob 字节一致。
  本次 CSV 为 1131 字节 / 41 行，SHA256：`5b0143184851b64b3a684daf42e149334e025432d277b5696a13ab3962d3cb97`。
- 修复并验证原标签页刷新后仍可执行 ping；新页面独占和长任务停止白名单有回归测试。
- Web 构建检查、ETM 线上子页一致性检查通过。未连接/复位/烧录目标，也没有更新线上 ZIP。

浏览器验收可复现：启动服务并打开其 browserUrl 后，执行
`python -X utf8 tools/test_toolbox_browser.py --runtime <运行时目录> --out <新的结果JSON>`。
该脚本只枚举页面、导入仓库测试工程、修改/恢复波形预览频率并导出，不操作设备。

2026-10-25 补充：命令参数参考覆盖63个节点，增加文档与真实解析器一致性回归；统一 CLI / HTTP 测试现为9项，全部通过。

## 火焰图 SVG

`trace export --format flame-svg` 支持当前序号区间的完整执行顺序图、聚合图及完整函数名。源码使用 `python toolbox.py trace export`，离线包使用 `etm-trace.cmd export`。模式、权重、区间和 HTTP 接口见 [火焰图 SVG 导出](etm-flame-svg.md)。
