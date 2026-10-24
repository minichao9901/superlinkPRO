# H743 CLI 候选版：源码收尾与软件验证

Run-ID：20261024-0803-h743-cli-release。范围：H743 的 ETM、调试与烧录；不验收 F103ZE/H7B0。
Tested-Revision：pre-commit-worktree；Tested-Base：604390050b1c368ceb115142d96791f3d6788883；Tested-Dirty：true。
Validation-Result：partial；Hardware-Test：not-run（本轮）。

## 修改

- 便携 EXE 新增 --cli，CMD 入口复用同一解析器；随包 Node、固定 USB/DOM 依赖与网页原有实现，不依赖系统 Python/Node/npm/Git，也不加载 pyOCD。
- 分开冻结的后端目录与外置网页/Node 资源目录；锁放到包内 data，原生解码器仍默认开启。
- 修复启动失败遗留 starting 状态、启动超时未请求退出、握手失败遗留 worker、内部 ping 队列结果未回执的问题。
- 保留正常退出清理，不强杀可能正在写 Flash 的进程。候选构建默认输出到独立临时发布目录，不替换稳定下载包。
- 增补 Node 许可证、资源与退出失败回归；来源清单记录 sourceDirty。

## 本轮实际验证

- 29 项 CLI Python 单元测试通过，另 5 项命令队列测试通过；最终握手失败清理回归在 runtime-cleanup-tests.log。
- 18 组回归命令执行完成；其中 portable-source 的 8 项因未指定便携包验证目录全部跳过，不当作实际 EXE 通过。
- 网页调试核心 326 项、Node CLI/USB/镜像适配 18 项、ETM 目标 27 项、共享控制/复位 8 项及 Flash/镜像等现有回归通过。具体数量以各日志为准。
- 本轮实际运行 65 条源码 CLI 离线端到端：十种导出、查询、导航、输入校验、包往返、KVDAT 与记录重解；重解后的 summary/quality 与原记录一致。读取的是既有 H743 原始记录，不是新硬件采集。
- 运行时快照同步测试通过。首次本机 GCC 内部程序缺 DLL，失败日志保留；用本次验证目录内独立编译程序/DLL重新构建成功，没有修改全局工具链或系统设置。
- 便携候选包的验证另立报告；本报告不提前声称其成功。

## 硬件阻塞与历史证据

08:03 启动命令在 exec_command 创建进程之前被拒绝，仅返回 blocked by policy，无具体理由；未生成启动结果 JSON。本次没有重试其它启动入口来绕过拦截，也没有新操作 SWD/Flash。
上次报告的“自动审批拒绝”应更正为“执行工具策略拒绝”；已核实该会话实际 approval_policy=never、approvals_reviewer=user，并未启用 auto_review。

此前 6043900 的早期 worker 有 H743 20ms 断点联动、地址断点/单步、配置恢复、原镜像烧录与整片回读一致证据：
E:/superlinkPRO/docs/validation/20261024-000104-trace-web-cli-9f57c7/report.md。
它不代替最终候选包的一键启动/退出、200/500/1000ms、异常拔插、DWARF 源码调试等新验收。
因此只发布候选版，不替换 native.13 正式下载，不宣称用户要求的最终硬件验收全部完成。

## 证据入口

持久证据目录：E:/superlinkPRO/docs/validation/20261024-0803-h743-cli-release。
原始记录/运行包目录：C:/Users/Administrator/.codex/validation/20261024-0803-h743-cli-release。
执行脚本：run-checks.py、accept-offline.py；入口索引：checks-pointer.json、acceptance-pointer.json。
完整日志与生成数据保留在持久目录；本提交仅保存报告、脚本、检查清单，不提交用户记录、令牌或 SDK。
