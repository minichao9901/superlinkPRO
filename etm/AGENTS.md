# 多 AI 协作约定

文档与进度说明优先使用中文。

## 工作目录

- 集成目录：`E:\web-etm-trace`，由 Codex 负责合并与整体验收。
- Codex 目标适配：`E:\web-etm-trace-worktrees\codex-f103-target`。
- 另一位 AI：`E:\web-etm-trace-worktrees\other-ai-f103-ui`。
- 共享协调目录：`E:\web-etm-trace-coordination`，请先阅读 `plan.md` 和发给自己的消息。

各自只修改自己的 worktree。各自提交，不改写另一位 AI 的分支，不对集成目录执行 reset/clean。
在任务开始、接口修改前、提交后读取协调目录；仅更新自己的任务状态，消息每条独立 JSON 文件。
文件职责与接口以协调目录 `plan.md` 为准。跨职责修改先协调。

## 硬件与服务

SWD 探针、烧录、目标复位、Kingst 采集由 Codex 独占验收，另一位 AI 不操作硬件。
硬件使用前确认协调目录的租约；临时服务使用自己的端口和输出目录，不停止对方进程。
已验证采集和 `.etmrecord` 作为只读证据；不要覆盖它们。

## 构建与集成

从共同基线提交开始。F103 固件从当前 worktree 的 build.ps1 构建，不使用指向其他 worktree 的固定源路径。
忽略的 ELF/采集结果不会随普通 Git worktree 自动复制；需要时从协调目录 artifacts 取对应记录包。
代码交付消息必须包含 commit SHA、修改范围、验证结果、已知限制与需要对方处理的事项。
由集成负责人完成合并和实板测试后更新集成状态。
