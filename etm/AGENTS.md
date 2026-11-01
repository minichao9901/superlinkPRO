# ETM 子目录工作约定

文档与进度说明优先使用中文。

## 工作目录

- 当前集成目录：`E:/superlinkPRO`；本目录为其中的 `etm/` 子树。先阅读根目录 [AGENTS.md](../AGENTS.md)。
- 旧 `E:/web-etm-trace`、旧 F103 worktree 与 coordination 目录属于迁移前记录，不作为当前 session 的必需环境。
- 如任务明确启用并行协作，以本次任务提供的分支、worktree、协调目录和租约为准；先用 `git worktree list` 核对实际位置。

并行协作时各自只修改分配的 worktree，各自提交，不改写其他会话的分支，不对集成目录执行 reset/clean。
存在本次协调目录时，在任务开始、接口修改前、提交后读取约定；跨职责修改先协调。
单会话工作不要求创建已废弃的协调目录，也不因旧文档自动启动并行任务。

## CLI 与问题跟踪

- 统一入口：从仓库根目录运行 `python -X utf8 toolbox.py trace --help`；ETM 直接入口为 `etm/trace.py`，实现位于 `etm/tools/trace_cli.py`。
- Web 全分类入口和完成判定见 [统一 CLI 总指南](../docs/toolbox-cli-ai.md)；ETM 参数/离线/硬件会话见 [Trace CLI 指南](../docs/trace-cli-ai.md)。
- 新问题、复现、修复、验收失败或限制变化必须同步更新 [项目问题跟踪列表](../docs/项目问题跟踪列表.md)。关闭条目需要修复与验证证据。

## 硬件与服务

SWD 探针、烧录、目标复位、Kingst 采集由 Codex 独占验收，另一位 AI 不操作硬件。
硬件使用前确认设备占用和本次任务的租约/约定；临时服务使用自己的端口和输出目录，不停止对方进程。
已验证采集和 `.etmrecord` 作为只读证据；不要覆盖它们。

## 构建与集成

从共同基线提交开始。F103 固件从当前 worktree 的 build.ps1 构建，不使用指向其他 worktree 的固定源路径。
忽略的 ELF/采集结果不会随普通 Git worktree 自动复制；需要时从本次指定的基准目录或 artifacts 取对应记录包，并核对指纹。
代码交付消息必须包含 commit SHA、修改范围、验证结果、已知限制与需要对方处理的事项。
由集成负责人完成合并和实板测试后更新集成状态。

## 固定验收与产物保留

- ETM 主线采用 H743 CDC echo 的 10/200/500/1000 ms 四档，规则见根目录
  `docs/etm-cdc-acceptance.md`；ETMv3、IT未知和异常/损坏负例另作兼容性回归。
- 基准原始记录放 `etm/records/baselines/`，每档绑定匹配ELF、源码、实际深度及质量证据；
  未采集或未录满的档位明确标为待验证。合成放大不作为真机1s证明。
- `docs/validation` 只保留精简报告、脚本、指纹和统计；完整运行包、重复解码/导出、
  源码副本放临时run目录并在交付后按 `docs/storage-maintenance.md` 规则清理。
- 清理先生成逐文件清单、保护回归输入和正式交付物；没有用户授权不清理基准原件。
