# superlinkPRO：新 session 工作指引

本仓是探针固件、Web 工作台与 ETM 工具的合并工程。默认使用中文沟通、编写工作记录。
当前集成目录为 `E:/superlinkPRO`；其他机器以实际仓库根目录为准。以下命令均从仓库根目录执行。

## 1. 接手顺序

1. 先执行 `git status --short --branch`、`git log -10 --oneline`；需要跨分支工作时再检查 `git worktree list`。保留用户未提交修改及未跟踪数据，不执行清空工作区的操作。
2. 阅读 [项目问题跟踪列表](docs/项目问题跟踪列表.md)，找到本次任务相关的未关闭问题和历史回归入口。
3. 阅读 [CLI 总指南](docs/toolbox-cli-ai.md) 和相关模块文档，再查看实际代码。进入子目录前阅读适用的子目录 `AGENTS.md`。
4. 根据最新源码、版本清单和对应验收报告判断当前能力。历史交接文档中的分支、绝对路径、性能数字及限制只代表当时版本，不应直接当成现状。
5. 先完成用户授权范围内的工作；需要硬件时确认设备、目标和正在运行的任务。纯离线任务不复位、烧录或抢占设备。

## 2. 目录与事实来源

| 路径 | 用途 |
|---|---|
| `toolbox.py` | **所有工具分类的统一 CLI 总入口**，AI 操作首选入口 |
| `web/app/`、`web/index.html` | Web 工作台源代码与主页面 |
| `etm/server.py`、`etm/tools/`、`etm/native/` | ETM 服务、Python 编排与查询、C++ 性能热点/OpenCSD 包装 |
| `etm/web/` | ETM 页面权威源代码；`web/etm/` 是生成的线上副本 |
| `web/bridge/etm/backend/` | 便携包运行时快照，由同步工具维护 |
| `firmware/`、`script_test/` | 探针固件、主机模型与硬件验证脚本 |
| `docs/` | 总体文档、问题跟踪、发布结论、精简验收证据；**统一使用此目录，不另建 `doc/`** |
| `web/docs/`、`etm/docs/` | 对应子系统说明和历史记录 |
| `etm/records/baselines/` | 原始基准记录及配套 ELF/配置/源码；按基线规范保留 |
| `tmp/` | 每次任务独立的可再生构建、导出、解压、运行目录 |

正式 Windows 下载包及版本事实来源：`web/bridge/etm/downloads/manifest.json`、同目录 `etm-bridge-win64.zip`。
运行时来源及文件指纹：`web/bridge/etm/provenance.json`。报告必须说明源码提交、包版本和验证对象。
当前还存在 `.formal-trace-20261024/` 等未跟踪历史验收输入，不能将“未跟踪”直接解释为可删除。

## 3. CLI 路径与使用方法（必读）

### 统一入口和文档

- 入口脚本：**`E:/superlinkPRO/toolbox.py`**（仓库相对路径 `toolbox.py`）。
- [统一 CLI 总指南](docs/toolbox-cli-ai.md)：分类、浏览器会话、操作和完成判定。
- [全部命令与参数参考](docs/toolbox-cli-options.md)：方法、参数、选项、默认值及约束。
- [机器可读接口清单](docs/toolbox-cli-reference.json)：静态分类/控件；动态控件与公开方法以运行时 `describe` 为准。
- [ETM Trace CLI 指南](docs/trace-cli-ai.md)：离线记录、解码、查询、导出以及目标/采集操作。
- [火焰图 SVG/HTML 相关说明](docs/etm-flame-svg.md)、[阅读导出说明](docs/trace-reading.md)。

```powershell
python -X utf8 toolbox.py --help
python -X utf8 toolbox.py catalog
python -X utf8 toolbox.py trace --help
python -X utf8 toolbox.py trace export --help
```

源码环境按需安装 `etm/requirements-cli.txt`；Web/原生 USB 运行环境及 Node 依赖见相应指南。
不要为执行离线查询而自动启动硬件会话。

### 操作正在运行的 Web 页面

`page` 命令通过本机授权队列操作真实网页，需要保持浏览器执行页面打开。
下例运行时目录必须是一个**尚不存在的新目录**；将 `session-NEW` 换成本次 session 的唯一名称。

```powershell
python -X utf8 toolbox.py --runtime E:/superlinkPRO/tmp/session-NEW serve --port 8896
# 按启动输出打开 browserUrl；初次 USB/HID/串口选择由用户完成。
# 另开终端，继续使用同一个运行时路径：
python -X utf8 toolbox.py --runtime E:/superlinkPRO/tmp/session-NEW ping
python -X utf8 toolbox.py --runtime E:/superlinkPRO/tmp/session-NEW page etm describe
python -X utf8 toolbox.py --runtime E:/superlinkPRO/tmp/session-NEW page etm state
```

先 `catalog`，再 `describe/state`；依返回结果使用 `set/click/upload/invoke/event`，不猜方法名、控件 ID 或参数。
全局参数放在子命令前。`ok:true` 仅代表请求处理成功，`click` 投递成功不等于采集/烧录完成；
必须继续核对 job、页面日志、质量、计数或落盘文件。超时后保留并查询 job ID，避免重复执行硬件写操作。
统一入口通常在 JSON 的 `value` 字段返回载荷（如 `catalog` 的 `value.pages`）；`trace` 沿用其自身的 `result` 信封，解析前核对实际输出，勿混用两者字段。
运行时令牌不得写入公开报告或 Git。

### ETM 离线与原生会话

`toolbox.py trace ...` 转交 `etm/trace.py`，实现位于 `etm/tools/trace_cli.py`。
离线操作优先使用 `query/verify/export/record`；参数先查 `--help`。例如：

```powershell
python -X utf8 toolbox.py trace query --record E:/records/example --view summary
python -X utf8 toolbox.py trace verify --record E:/records/example --require-decoded
python -X utf8 toolbox.py trace export --record E:/records/example --format flame-svg --from-seq 100 --to-seq 300 --out E:/exports/new-flame.svg
```

`--out` 使用新文件，不覆盖验收输入。原生硬件会话由 `trace web start` 管理，不能与统一 `serve` 的运行时目录混用。
硬件辅助实现见 `etm/tools/trace_cli_hardware.py`、`trace_cli_runtime.py`、`trace_target_cli.mjs`。
完整离线包中使用 `etm-trace.cmd ...` 或 `etm-bridge.exe --cli ...`，参数对应 `trace` 后面的部分，详见 Trace CLI 指南。

## 4. 问题跟踪必须同步维护

**以后遇到问题、Bug、验收失败或新增限制，必须同步更新 `docs/项目问题跟踪列表.md`。**

- 先检索现有条目，复现相同问题时更新原 ID，避免重复登记；新问题分配递增 ID。
- 记录触发条件、影响范围、优先级、状态、日期、证据及下一步；尚未确认时标记“待复核”，不要直接写成已确认缺陷。
- 修复与状态更新放在同一提交；验收失败、问题重开、临时规避、延后处理也要更新原条目。
- 关闭问题必须引用修复提交或变更、验证方法/报告，并区分软件回归、保留记录离线验证和真机验收。仅有历史报告时注明其日期和范围。
- 保留已解决条目及其证据链接，不删除历史、不把“未复现/旧路径停用/打算修复”当成已解决。
- 大量日志和波形只保存引用/指纹/必要摘要；完整证据依存储策略管理。交付时简述本次涉及的问题 ID。

## 5. 修改、同步与验证

- 优先修改权威源代码；不要仅改 `web/etm/`、运行时快照或解压 EXE 目录来完成修复。
- 修改 ETM 页面后运行 `node web/tools/dev/sync-etm-page.mjs`，检查用 `--check`。
- 修改 Web 合并脚本涉及的输入后，运行 `node web/tools/dev/build-web.mjs`，检查用 `--check`。
- 修改 CLI 参数/页面入口后，更新 `python -X utf8 tools/build_cli_options.py`、`node tools/build_toolbox_reference.mjs`；两者均支持 `--check`。
- 需要发布便携包时，先提交/明确源代码版本，再用 `python -X utf8 web/tools/dev/sync-etm-runtime.py --etm-root etm` 同步运行时；
  按需更新 `node web/tools/dev/embed-etm-kit.mjs` 和 Web 构建，再运行 `python -X utf8 web/tools/dev/build-etm-package.py --output <独立候选目录>`。
  原生同步需要本地 C++ 工具链；构建异常应先记录和诊断，禁止静默复用不匹配的旧 EXE。
- 按改动选择必要验证。CLI 可用 `python -X utf8 -m unittest tools.tests.test_toolbox_cli -v`；
  ETM 综合离线入口为 `python -X utf8 web/tools/dev/test-etm.py --output <本次独立目录>`，`--portable` 另测完整 Windows 包。
  纯文档修改检查链接、命令、状态证据与 diff，不要求重复跑全量硬件或解码测试。
- 原生进程无法写系统 TEMP 时，可以给**本次子进程**指定工程内独立 TEMP/TMP；记录原失败及重测结果，不全局改系统设置。
- 验收候选包后再替换正式下载：核对 manifest、ZIP 大小/SHA256、来源、包内资源及冻结 CLI/HTTP 实际行为。
  推送后如涉及发布，核对 Pages 结果和线上清单；源码、本机包、线上包的验证范围分别说明。

## 6. ETM 正确性与硬件约束

- OpenCSD 解码、原始 TraceStore 执行事实和阅读/火焰图展示分层处理；展示折叠不能删除原始执行事实。
- 保留 unknown、not-taken、初始未同步、尾部截断、未知祖先等证据；执行序号不冒充毫秒。
- 采集请求时长与实际采样深度/时长分别报告。性能说明写清采集、回传、解码、索引、首屏和导出的计时边界。
- 主线采用 H743 CDC echo 的 10/200/500/1000 ms 四档，按 [CDC 验收规范](docs/etm-cdc-acceptance.md) 执行；
  ETMv3、未知 IT、异常/损坏负例另外保留。旧记录、合成放大或未录满窗口不作为新的真机完整 1 s 证明。
- SWD 探针、目标板、Kingst LA 采集/烧录/复位操作不得与其他会话并发争用。不要停止用户或其他会话的服务。

## 7. 存储与 Git 交付

遵循 [存储维护](docs/storage-maintenance.md)：`docs/validation/` 留精简报告、脚本、指纹和统计；
大波形、源码副本、解压运行包放独立临时目录或正式基准位置。清理先列清单并核对保护资产，
不能用 `git clean/reset` 清掉未知数据，不能因路径含 `tmp` 就删除基准、嵌套仓库或其他 session 的资产。

提交前检查 diff、必要验证和问题表，只暂存本次相关文件；不改写他人历史。需要新分支时默认前缀 `codex/`。
按用户要求执行 commit/push/tag/发布，不能把仅 commit 描述成已上线。最终报告包含提交、变更、验证、仍未关闭的问题。
