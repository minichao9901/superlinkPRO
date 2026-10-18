# Git 日志与验证证据全局约定

版本：1.0｜日期：2026-10-14

本文件可作为一个完整章节复制到全局 AGENTS.md 或 CLAUDE.md。用于要求 AI 将版本说明、验证摘要与证据索引保存在 Git 历史中。规则适用于单 AI 与双 AI 开发，不依赖配套工具已经存在。

## 1. 基本要求

1. 用户最新要求优先；遵循运行环境的上级限制。按用户既有授权进行本地提交，不重复确认已授权动作；本规则不授予推送、部署、发布或修改全局设置的权限。
2. 修改前查看 Git 状态、相关提交正文及必要 notes，确认已有改动归属。不覆盖他人未提交修改，不把无关文件混入本次提交。
3. 有实质变更的提交写清问题、改动、验证与限制；不要只写一句泛泛标题。小改动适当简化，不为了填模板虚构内容。
4. Git 历史记录版本事实；任务在线状态、分工、硬件锁仍留在协调机制中。
5. 完整测试报告与原始数据单独保存；提交正文保存摘要、脚本路径、报告 fullpath 和版本关联。避免重复维护内容相同的进度 MD。
6. 项目已有协议、构建和使用文档继续维护；提交记录不能替代长期使用说明。

## 2. 提交标题与正文

标题建议采用 `<type>(<scope>): <具体变化>`，正文优先中文。类型可用 `feat`、`fix`、`perf`、`refactor`、`test`、`docs`、`build`、`chore`；无适用 scope 时省略。

Git 没有 50／72 字符这种消息硬上限；短标题是阅读建议。正文以充分且易查为准，不粘贴海量原始输出。长消息使用编辑器或文件输入，避免命令行长度与转义问题。

正文按实际需要包括：

- 问题：触发条件、原行为、影响。
- 修改：主要变化、原因和有必要解释的取舍。
- 验证：测试入口、条件、关键结果和未执行项。
- 限制与后续：尚未覆盖的场景、依赖和具体下一步。

末尾使用固定 `Key: Value` 字段作为 trailers，字段块前空一行。单个值尽量一行，多次验证重复对应字段；同一组用稳定 run-id 关联，不靠字段位置猜对应关系。

## 3. 元数据与验证索引

有任务 ID 时记录 `Task-ID`；记录执行 AI 的 `Agent`，多会话协作再记录 `Session-ID`。Git author 按用户／仓库现有身份配置，不伪造他人身份。

| 字段 | 含义与要求 |
| --- | --- |
| Task-ID | 项目任务编号；无任务系统时可省略 |
| Agent | 实际执行工具／模型标识，如 Codex、DeepSeek |
| Session-ID | 多会话时的唯一会话标识 |
| Stage | 提交时阶段，如 experimental、ready-for-review、ready-for-integration |
| Validation-Result | passed、failed、partial、not-run；与正文限制一致 |
| Validation-Script | 对应代码版本中的仓库相对脚本路径 |
| Validation-Script-Fullpath | 当次实际执行脚本的绝对路径 |
| Validation-Command | 实际运行命令及关键参数；不写凭据 |
| Validation-Cwd | 实际运行时的绝对工作目录 |
| Validation-Run-ID | 唯一验证编号；多次验证由正文／报告列明各组字段 |
| Validation-Report | 当次生成报告的绝对路径，优先 MD |
| Validation-Report-Location | 可选：相对稳定存储根的路径，便于迁移 |
| Validation-Report-SHA256 | 重要报告的内容校验值 |
| Evidence-Fullpath | 可重复：原始日志、截图、波形、结果 JSON 等路径 |
| Tested-Revision | 已存在的完整提交 SHA，或明确写 pre-commit-worktree |
| Tested-Base | 提交前验证时所基于的完整 SHA |
| Tested-Dirty | tested 输入是否含未提交改动：true／false |
| Related-Revision | 多仓库／固件与网页等配套版本，写仓库ID=完整SHA |
| Hardware-Test | passed、failed、partial、not-run、not-applicable |
| Source-Commit | 集成提交可重复：来源完整 SHA；不同于当前提交 |

字段按适用情况使用，不要求每个提交都有硬件测试或生成新报告。若实际执行了验证脚本并产出报告，必须记录脚本相对路径、fullpath、命令／工作目录、报告 fullpath 和验证结果。

没有脚本的手工验证说明操作和证据；没运行写 not-run 及原因。没生成报告就明确写未生成，不得填一个看似存在的路径。简单低影响改动可用正文说明检查结果，不为凑字段增加无效测试和报告。

路径使用绝对路径和正斜杠，例如 `E:/demo/...` 或 `/home/user/demo/...`，保留空格，不对字段值做 URL 编码。命令中的带空格路径按实际 shell 正确引用。

fullpath 用于本机定位，不保证跨机器可用。脚本由“对应提交 + 仓库相对路径”恢复；报告由稳定证据位置与指纹核对。脚本在仓库外时记录 fullpath 和内容指纹，不能虚构仓库相对路径。

## 4. 测试版本与结果真实性

1. 测试结果必须来自真实执行，不能把预计结果、旧结果或对方摘要当成新测试通过。
2. 记录软件／工具版本、目标／板型、配置、输入和关键指标；根据任务选择必要项。
3. 不只写“全部通过”。说明测试入口、数量或覆盖场景；性能数字注明单位、时长、次数和测量条件。
4. 软件回归、模拟验证、实板验证、浏览器人工操作分别描述；一种通过不能替代另一种。
5. 测试已存在提交时记录完整 SHA 和工作树是否含修改。
6. 提交前测试写 `Tested-Revision: pre-commit-worktree`、Tested-Base 和 Tested-Dirty，说明输入快照。重要验证在证据中保存相关 diff／输入清单和构建产物指纹。
7. 无法预先知道包含这段提交消息的最终 SHA，不把父提交填成当前最终版本。若测试后又改动受测输入，重新验证受影响范围。
8. 集成、冲突解决或配套版本变化后，对最终组合重新执行必要验证；不要直接沿用来源分支的“通过”。

## 5. 报告与原始证据

证据优先保存到持久位置，例如 `<coordination>/artifacts/<task-id>/<run-id>/`；单 AI 可使用项目指定的验证目录。不要只放在会被删除的临时目录或 worktree 中。

- 一次验证一个唯一 run-id，报告发布后不覆盖；复测生成新目录／新文件。
- 报告包含任务、时间、命令、目录、环境、输入版本、关键结果、失败详情和未覆盖项。
- 保存有复核价值的原始输出、输入清单和必要数据；大波形与二进制不粘贴进 commit message。
- 重要证据记录 SHA256。相同文件名不代表内容相同；打开后先核对指纹和版本。
- 被 Git 忽略的证据不会因代码 push 自动备份，交付时明确保存／备份位置与可访问性。
- 报告使用 MD 便于阅读；机器结果可使用 JSON／CSV，两者互相引用同一次 run-id。

## 6. 提交后补充测试：Git notes

提交后的实板测试、独立审查和验收可使用 notes，避免为补说明改写原提交。

仅在项目已启用并约定同步方式时使用；建议明确引用 `refs/notes/validation` 和唯一写入负责人。未启用时保存验证报告及协调消息，必要时用正常报告提交记录，不默认创建空代码提交。

```powershell
git notes --ref=validation append -F "<验证附注UTF8文件>" <受测完整SHA>
git show -s --format=full --notes=validation <受测完整SHA>
```

附注写测试时间、执行方、受测 SHA、配套版本、结果、报告 fullpath 与指纹。同一引用的写入串行执行；不以 `-f` 覆盖已有结果。

同仓库的本地 worktree 共享 notes 引用；独立 clone 不共享。notes 不是普通分支提交正文，远端同步需单独配置。得到相应远端写入授权后，示例为：

```powershell
git push origin refs/notes/validation:refs/notes/validation
git fetch origin refs/notes/validation:refs/notes/validation
```

这些命令不保证解决分叉；出现冲突先检查并合并 notes 历史，不能强制覆盖。cherry-pick 后 SHA 改变，不假定附注自动迁移；来源 notes 保留来源测试对象，集成提交记录来源关联并追加自己的最终验证结果。

## 7. 安全、可审查地提交

- 按一个可审查目的组织提交，不把无关修复混入同一提交。
- 提交前检查暂存区 diff，确认实际内容与消息相符。
- 不无差别使用 `git add .` 吸收他人改动；明确选择本任务文件。
- 长正文先保存为 UTF-8 文件，再用 `git commit -F "<消息文件>"`；不要用字符串拼接执行消息内容。
- 已交付或共享的历史不因补结果而 amend／rebase；先使用 notes 或新的正常提交。
- 分支、端口、在线状态和硬件租约属于动态信息，不为每次状态更新制造空提交。
- 若需要 checkpoint，明确标识 experimental、未完成项和验证范围，不包装成已验收版本。
- 不执行用户未授权的 push、强推、删除分支或发布操作。

## 8. 提交消息模板

以下占位符必须替换；没有执行的项写明 not-run。任务和测试简单时可删减不适用部分。

```text
fix(module): <具体变化>

问题：
<触发条件、原行为和影响>

修改：
<主要变化与关键原因>

验证：
<实际条件、关键结果、证据与未执行项；多次运行按run-id分组>

限制与后续：
<边界、遗留问题和下一步；无则写无已知新增限制>

Task-ID: <任务ID>
Agent: <实际执行AI>
Stage: ready-for-review
Tested-Revision: pre-commit-worktree
Tested-Base: <完整SHA>
Tested-Dirty: true
Validation-Run-ID: <唯一运行ID>
Validation-Result: <passed|failed|partial|not-run>
Validation-Script: <仓库相对路径>
Validation-Script-Fullpath: <实际脚本绝对路径>
Validation-Command: <实际命令和参数>
Validation-Cwd: <实际工作目录绝对路径>
Validation-Report: <已存在报告绝对路径>
Hardware-Test: <passed|failed|partial|not-run|not-applicable>
```

## 9. AI 查询方式

先确认当前仓库、分支和任务，再搜索；不要把其他实验分支的验证当作主线已通过。

```powershell
# 近期完整正文和验证附注
git log -8 --format=full --notes=validation

# 查询提交正文中的任务ID；--all只包含本地已有引用，不会主动下载远端
git log --all --fixed-strings --grep="Task-ID: T001" --format=full --notes=validation

# 找含报告索引的提交
git log --all --fixed-strings --grep="Validation-Report:" --format=full

# 先搜索输出，再按找到的SHA读取完整消息和附注
git log --all --format="Commit: %H%n%B" --notes=validation | rg -n "Validation-|T001|关键词"
git show -s --format=full --notes=validation <完整SHA>
```

`rg` 搜索工作目录不会直接搜索 Git 对象。`git log --grep` 搜索提交正文；仅在 notes 被展示时也会匹配展示的附注，因此搜索验证附注必须显式带 `--notes=validation`。管道中的 rg 行号只属于当次输出，不是持久索引；结果附近不一定包含提交头，应读取完整消息核对关联。

按需生成可再生的文本索引，索引覆盖文件自身即可，不覆盖任何验证证据：

```powershell
# 替换占位路径；协调目录须已存在，不同写入者使用不同输出文件
git -C "<仓库绝对路径>" log --all --format="Commit: %H%n%B" --notes=validation |
  Set-Content -LiteralPath "<协调目录绝对路径>/git-log-index-codex.txt" -Encoding utf8
rg -n "Validation-Report:|关键词" "<协调目录绝对路径>/git-log-index-codex.txt"
```

上述导出命令用 `git -C` 指定来源仓库，不要求协调目录自身是 Git 仓库。索引是派生缓存，注明来源仓库和生成时间；使用前重新生成，不作为新的权威记录。

查到路径后验证文件存在、指纹和受测版本；路径失效时报告证据缺失，不能根据摘要声称已复核。

## 10. 官方参考

- [Git commit](https://git-scm.com/docs/git-commit)：正文、文件输入、trailers 和 amend。
- [Git log](https://git-scm.com/docs/git-log)：消息搜索、自定义输出与 notes 展示。
- [Git notes](https://git-scm.com/docs/git-notes)：附注、独立引用及历史合并。

字段名、状态枚举与证据目录是本约定的项目规则，不是 Git 内置校验。项目未部署检查工具时，AI 必须自行检查，不能声称已自动强制执行。
