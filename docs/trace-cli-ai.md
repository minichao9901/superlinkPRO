# 给 AI 使用的 Trace CLI

入口：在仓库根目录运行 `python -X utf8 etm/trace.py`。这是直接控制本地探针、
Kingst 和解码工具的命令行，不需要网页、浏览器 USB 授权或后台 Web 服务。
目标寄存器配置复用 `etm/web/app/target.js`；解码、查询、导出复用现有原生优化链路。

当前硬件验收范围是 **Windows x64、H743、akaLinkPro CMSIS-DAP、KingstVIS 3.6.6**。
H7B0可选择现有配置路径，但本轮未实板验收；F103等目标的离线ETMv3记录可以解码，
不在这版硬件CLI的目标选择范围。它不会自动烧录、修改PLL或复位目标。

## 1. 环境与第一条命令

需要Python、Node.js、原生工具，以及KingstVIS和设备驱动：

```powershell
python -m pip install -r etm/requirements-cli.txt
python -X utf8 etm/trace.py doctor
python -X utf8 etm/trace.py doctor --hardware
```

`requirements-cli.txt`固定pyOCD 0.45.1、pyserial 3.5。硬件传输层有一处仅作用于当前
pyOCD会话的Funnel发现钩子，避免连接时擅自打开全部输入；升级pyOCD后应先重验。
不修改全局pyOCD安装。当前实测Node.js为24.13.1。

原生工具需位于 `etm/build-native/`：`etm_frontend.exe`、`etm_decode.exe`、
`etm_storebuild.exe`、`etm_export.exe`。缺失时先按照工程原有 `etm/build-decoder.ps1`
构建，不把旧版本或Python回退伪装为默认优化引擎。离线命令不连接探针/LA。

KingstVIS需运行并开启Socket（通常127.0.0.1:23367）；首次设置方式沿用
`web/docs/逻辑分析仪攻略.md`。CLI不会修改全局设置，也不会自动关闭其他调试器或网页。

使用硬件前，关闭占用同一探针/LA/CDC的其他操作。CLI会在Git公共目录创建
`trace-cli-hardware.lock/`，防止不同worktree的CLI同时使用硬件。这个锁不能约束网页、
OpenOCD或另一个调试器。存在旧锁时先核实 `owner.json` 中进程是否已退出，再由使用者
处理；CLI不会自动抢锁。正常退出和异常清理都会释放自己创建的锁。

## 2. AI 调用契约

除 `--help` 外，stdout只有一份JSON；工具日志、pyOCD提示和解码进度写stderr。
例如PowerShell分别保存：

```powershell
python -X utf8 etm/trace.py --json-out E:/trace-runs/doctor-result.json doctor `
  1>E:/trace-runs/doctor-stdout.json 2>E:/trace-runs/doctor-stderr.log
```

`--json-out`是全局选项，放在子命令**前面**，必须使用不存在的新文件。
所有 `--out` 目录/导出文件也必须不存在。失败输出目录保留证据，复测换一个名字，
不能直接反复重跑覆盖它。

结果形状：

```json
{
  "protocol": "trace-cli/1",
  "runId": "20261023-...",
  "command": "capture",
  "ok": true,
  "result": {"recordDir": "...", "engine": "native"},
  "exitCode": 0,
  "elapsedSeconds": 10.0
}
```

| 退出码 | 含义 | AI下一步 |
|---|---|---|
| 0 | 命令成功；仍须查看适用的质量/边界字段 | 按所需工作流继续 |
| 2 | 参数、配置、范围或前置条件不符 | 修正参数，不先操作硬件 |
| 3 | 缺依赖/原生程序 | 修复环境后重试 |
| 4 | 设备、传输、文件IO或配置恢复未确认 | 查stderr及cleanup，停止扩大采样窗 |
| 5 | 实际长度、输入指纹或解码质量未通过 | 保留原始证据，不报告验收通过 |
| 130 | 用户中断 | 查cleanup.json；不要自动复位/重试 |

失败JSON带 `error.message`、可选 `error.details` 和 `error.notes`。
捕获目录内还有 `cli-error.json`、`cleanup.json`。`laIdle=null`表示尚未启动/确认LA，
不是已确认空闲。恢复不成功时不要只凭“命令结束”就声称硬件已释放。

## 3. 推荐端到端：H743短窗真机采集

先给本次操作选一个新输出根目录。下面路径和序列号都是示例，须替换成实际值：

```powershell
$probe = '8F538633E6AEC4CA8ABB6A36FEBB1AC2'
$elf = 'C:/firmware/fw.elf'
$run = 'E:/trace-runs/h743-run-001'

python -X utf8 etm/trace.py target inspect --probe $probe --profile stm32h743 --hse 25

python -X utf8 etm/trace.py capture `
  --probe $probe --profile stm32h743 --hse 25 --elf $elf `
  --out $run --seconds 0.02 --rate 500000000 `
  --expect-core-hz 60000000 --expect-trace-hz 30000000 `
  --pin-speed 2 --trace-id 2 --return-stack --cycles `
  --clock 0 --data 1 2 3 4 --fraction 0.25 --glitch-ns 4 --threshold 1.65 `
  --echo-port COM33 --echo-interval 0.004 `
  --source-root E:/superlinkPRO

python -X utf8 etm/trace.py verify --record $run --require-decoded
python -X utf8 etm/trace.py query --record $run
python -X utf8 etm/trace.py export --record $run --format html --out E:/trace-runs/h743-run-001.html
python -X utf8 etm/trace.py export --record $run --format record --out E:/trace-runs/h743-run-001.etmrecord
```

实际顺序为：独占租约 → 识别 → Flash逐字节核对ELF → 配置并回读 → 保存恢复状态 →
运行目标及负载预热 → LA采样/回传 → 保存原始KVDAT → **核对实际深度** →
恢复采集前Trace配置和CPU运行/暂停状态 → 原生解码/完整索引 → 指纹与质量检查。
不用CSV中转，不拼接，不在后台偷偷分窗。`--no-decode`可只采集并保存，之后离线解码。

无触发的连续窗口是这版CLI默认模式：预先运行目标，避免短窗全落在CPU暂停的时间里。
不提供未经本轮验收的“自动复位到断点再开始”路径。若要测初始化启动入口，请继续使用
项目已经验证的网页起点工作流，而不是把连续运行窗口误称为启动过程采集。

**源目录不能随意猜。** `--source-root`只读取ELF记录的、确实落在该根目录内的路径，
不按同名文件名猜配对。ELF在另一台机器构建、路径不一致时，可用
`--sources-json sources.json`提供明确的“ELF路径→源码正文”映射。ELF自身不含源码正文；
无配套源码时，源码类导出会明确拒绝，CSV/原始数据等仍可导出。

### 业务负载

`--echo-port`可选，不提供就不打开串口。提供时使用带序号/CRC的完整行持续发送并回收，
记录到 `echo-rx.bin`、`echo-events.jsonl`、`echo-result.json`。不能把探针COM口当成目标，
CLI会拒绝VID=0D28的探针串口。H743 CDC当前为COM33、COM34是探针，但端口号可能变化，
以本次 `doctor --hardware` 为准。

周期banner由固件实现，不是CLI或LA产生。固件准备可参照：

```powershell
# 只构建，不自动烧录；SdkRoot按本机实际目录替换
pwsh -File etm/firmware/stm32h743_cdc_trace/build.ps1 `
  -CoreMHz 60 -TraceR 8 -BannerEveryMs 20 -SdkRoot C:/sdk/h743 `
  -OutputDir C:/firmware/run-001 -NoPublish
```

该参数组合得到CPU60 / 物理TRACECLK30MHz；`TraceR 12`为20MHz。
固件需要使用本项目的非阻塞RTT日志修复，避免没有RTT读取器时写满后卡死主循环。
烧录属于独立、显式的操作；CLI采集只校验，不会替换板上程序。

## 4. 独立配置与恢复

自动采集已经负责配置和恢复。只有需要把目标保持在已配置状态，供外部工具采样时，
才单独调用configure：

```powershell
python -X utf8 etm/trace.py target configure --probe $probe --elf $elf `
  --state E:/trace-runs/target-state-001.json --resume

# 外部工具采样完成后
python -X utf8 etm/trace.py target restore --probe $probe `
  --state E:/trace-runs/target-state-001.json
```

configure不带 `--resume` 时保持暂停；restore恢复到**configure前**的运行/暂停状态。
状态包含探针、目标身份、所改寄存器及掩码、期望配置和ELF指纹；恢复时拒绝目标不符、
状态文件被改动、状态已用过或目标配置发生变化的情况。不把旧状态强压回新的配置。
状态文件恢复后标为已用，保留作为证据。

如果丢失状态或目标已被其他工具改动，可显式停止Trace：

```powershell
python -X utf8 etm/trace.py target stop --probe $probe
# 需要保持CPU暂停时才加 --halt
```

stop关闭Trace并默认继续CPU；它不是“恢复丢失的原始引脚/寄存器快照”。
本CLI不管理外部调试器的断点，请勿与网页、OpenOCD、pyOCD gdbserver并行操作同一目标。

## 5. 离线解码与记录包

所有离线命令都不要求连接板子。重解码写入新目录，原始记录保持不变：

```powershell
python -X utf8 etm/trace.py decode --record E:/trace-runs/h743-run-001 `
  --out E:/trace-runs/h743-run-001-redecoded

python -X utf8 etm/trace.py decode --input E:/capture/capture.kvdat `
  --elf E:/capture/firmware.elf --config E:/capture/session.json `
  --sources-json E:/capture/sources.json --out E:/trace-runs/offline-001

python -X utf8 etm/trace.py import-record --input E:/capture/test.etmrecord `
  --out E:/trace-runs/import-001
```

`decode --input`也支持跳变CSV和已解复用ETM BIN；BIN不是TPIU物理流。
必须提供采集时的完整配置，不能只给traceid然后猜默认ETMv4寄存器。
`--config`也可使用target configure生成的状态文件，此时会保留目标/ELF绑定。
CSV/BIN缺少KVDAT采样头时，CLI不会用请求参数冒充实际采样时长。

记录包保留原始波形、匹配ELF、配置、源码和存在的TraceStore/绑定输入；可由CLI导入，
也可在网页“导入记录”中打开。导入不自动重解，旧包没有store时再显式decode。
拒绝重复成员、越界路径、符号链接，限制单成员1GiB、解压总量2GiB。
CLI目前不做历史schema-3自动分窗记录的重建；这类记录使用原有兼容工具。

## 6. 查询

query默认返回摘要/质量。其他视图直接使用现有TraceStore接口：

```powershell
python -X utf8 etm/trace.py query --record $run --view overview
python -X utf8 etm/trace.py query --record $run --view page --offset 0 --limit 150
python -X utf8 etm/trace.py query --record $run --view page --search main --hide-skipped
python -X utf8 etm/trace.py query --record $run --view event --seq 10000
python -X utf8 etm/trace.py query --record $run --view navigate --seq 10000 --mode call --direction 1
python -X utf8 etm/trace.py query --record $run --view window --from-seq 10000 --to-seq 30000
python -X utf8 etm/trace.py query --record $run --view timeline --search main
python -X utf8 etm/trace.py query --record $run --view issues --offset 0 --limit 100
python -X utf8 etm/trace.py query --record $run --view source --file 'ELF中记录的完整路径'
```

`mode`支持instruction/call/return/exception/issue/source/pc；PC模式另传
`--pc 0x08001234`。方向只能-1或1。包页使用 `--table packets`。
先看overview的eventCount，再计算中页/末页，不能把“指令数”误当成包含边界事件的总序号。
`--from-seq/--to-seq`是执行序号，**不是ms或µs**。调用栈来自历史重建，不是当前寄存器快照。

## 7. 导出格式

```powershell
python -X utf8 etm/trace.py export --record $run --format csv `
  --from-seq 10000 --to-seq 30000 --out E:/trace-runs/selection.csv
```

| format | 输出内容 | 执行序号裁剪 |
|---|---|---|
| simplified | 合并连续相同源码位置的C执行摘录 | 支持 |
| source | 完整源码执行流C摘录 | 支持 |
| html | 可离线浏览的简化执行流/配套源码HTML | 支持 |
| csv | 每条执行事件/指令的CSV | 支持 |
| events-jsonl | 完整逐事件JSON行，可能很大/较慢 | 支持 |
| json | 摘要、质量、分析和源码JSON，**不含全量事件数组** | 不支持 |
| raw | 原始KVDAT/CSV/BIN逐字节副本 | 不支持 |
| etm | 已解复用ETM源字节流 | 不支持 |
| record | 含原始数据的可重解码.etmrecord包 | 不支持 |

两种C均是恢复执行顺序的可读摘录，不是可编译程序。JSON摘要与events-jsonl明确分开，
避免误把“小型decoded.json”当成完整执行历史。四种常用执行流导出默认走原生投影；
已有准备索引的很小区间可能按现有策略使用更快的准备读路径，不改变结果口径。

## 8. AI 验收判据与停止条件

建议先20ms，重复确认后才扩大到200/500/1000ms。CLI默认上限跟随当前已发布产品为1s，
**但 CLI 对实际窗口长度的验收比网页警告更严格**：未录满时返回5，原始记录保留，
不自动把它继续算作合格1s解码任务。网页允许继续浏览短于请求的实际窗口，二者判据不同。

必须同时检查：

1. `ok=true`、exitCode=0；实际KVDAT深度/采样率与请求匹配，允许1个采样点舍入。
2. `verification.passed=true`，输入/配置SHA256和store绑定核验通过。
3. 结构错误、帧内FSYNC、丢弃partial frame、idle resync、溢出、字节重叠/遗漏/未处理为0。
4. `I_INCOMPLETE_EOT`只有确实落在输入末端时才作为边界单列，其他包问题不放行。
5. 初始化未同步、条件执行未知、采集前调用栈未知继续保留，不据此声称全部路径已知。
6. 有业务负载时，echo-result需passed且没有缺包/CRC错误；检查trace中实际业务调用，
   不仅看宿主机总发包数。宿主机负载可能跨越LA的设置/回传时间，总数不等于窗口内调用数。
7. `cleanup.targetRestored=true`、LA空闲已确认；失败时检查notes和保存的现场，不自动重试/复位。

当前30MHz连续信号在500MSa/s请求1s时曾只录到约927ms，可能受LA压缩存储限制；
200MSa/s对照也未解决，并有包问题。降低采样率、滤掉数据或放宽解析规则不能冒充通过。
20MHz的1s验收不能直接代替30MHz条件。

记录中核心文件：`capture.kvdat`、`firmware.elf`、`session.json`、`sources.json`、
`inputs.json`、`physical.json`、`framing.json`、`etm.bin`、`native.bin`、
`store/manifest.json`、`decoded.json`、`decode-performance.json`、`cleanup.json`。
`target-state.json`是当次采集现场；出现异常还会保留 `cli-error.json`。

计时分开看：`acquisitionSeconds`包含连接、核对、配置、预热、采样/回传、保存和恢复；
`wallSeconds`是解码/索引路径耗时；外层elapsedSeconds为CLI整次调用。父阶段已经包含子阶段，
不能把所有计时字段直接相加。单次短窗冒烟耗时不等于独占机器性能基准。
