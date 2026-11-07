# superlinkPRO 工程整理与 Windows web.6 验收

2026-11-08（北京时间）。接手基线为已部署的 `a71d2a5`；前序会话完成 OpenOCD Wasm 共享目标、F103ZE/H743/HPM6800EVK/RP2350 真机回归及 H743 自动解码改造。本轮为离线整理，没有复位、烧录、采集或接管已有服务。

## 工作台

- `toolbox.py target import / use / info` 等待工作台 CFG 导入和沿用完成，直接回报错误、核心、Flash 能力与原版配置指纹。Edge 离线以原版 RP2350 CFG 验证 `rp2350.cm1`、不存在核心的拒绝、烧录和 RTT Viewer 沿用、重复沿用返回未变更。既有真机报告只覆盖其中已测的 `cm0`。
- 删除无运行时入口的旧桥 Base64 模块、旧 RTT 原生 RISC-V 适配层和旧烧录页复位辅助模块。仍被探针高速、复位或离线测试使用的模块保留。15 个页面的静态 CLI 索引重新生成。
- RP2350 [验收夹具](../../script_test/rp2350_workbench/README.md)从临时目录迁入源码树。Pico SDK 2.2.0、GNU Arm 10.3.1 重新配置和编译通过；未烧录。其它已跟踪的 C 工程和与固定输入配套的固件未删除。

## ETM 记录

| 型号 | 保留的常用结果 | 理由 |
| --- | --- | --- |
| STM32H743 | `capture-477f0c8526af4c8c9de9b6538566cfce`（200 ms）、`capture-83af7022ef054155ac5bb9889e381f02`（1000 ms） | 默认与完整长窗各一份；2026-11-07 工作台原生自动解码，两份都录满 |
| STM32F103ZE | `capture-2d6da5829a8c4811b7c5ed2ca9e08cc3`（10 ms） | 唯一找到的可严格验证的真实 ETMv3.4 记录；保留协议兼容回归 |
| STM32H7B0 | `capture-926a4f2b899542f5b566e68a1d1eb631`（1 ms）及 [10 ms SPI 封存包](../../etm/baselines/spi-70-35-v1/README.md) | 短窗复验与完整业务窗；10 ms 包在独立目录导入后重新解码 |

五份记录均以 `toolbox.py trace verify --require-decoded` 离线通过。工作台记录放在 `etm/records/`。早期两份 H743 固定测试输入转入 `etm/records/baselines/legacy-h743/`，三段合成输入和 Python 对照 Store 转入 `etm/records/baselines/regression/`；它们与原有 steady/boundary、绑定探针及四份 TCP/UDP 原件一起保留为独立回归夹具。H743 已完成的四档真机报告和输入指纹继续保留。归档轮换不改变未来四档验收要求。

## 源码、产物与验证

Windows `1.1.0-web.6` 的源码提交为 `bb435e6918cd9b3e6d5ce60527a641081958ff8d`，ETM 运行时来源为 `c1a565739231fb0a564ad9aedd35f77ec44dd284`。候选包通过 EXE 启动、HTTP、3436 项包文件校验、原生演示记录解码、冻结 Trace CLI 查询及导出后安装到 `dist/superlinkPRO/`。发布 ZIP 为 103,378,160 字节，SHA-256 `26eb754ff888f4274548ef4c2ec19b964a19d7765e1f09bfef8472801e8adf36`。历史 ETM 独立下载保持原样。

Web 与 CLI 定向回归通过（Node 40 项、Python CLI/打包 12 项），Web 合并脚本、ETM 页面副本与两个 CLI 参考生成检查通过。迁移后的 10 ms 原生前端及合成片段测试、固定输入五份核验、存储策略五项测试通过。证据见 [机器摘要与指纹](repository-maintenance-20261108.json)。

## 本机清理

已删除 33,440 个清单内文件，逻辑体积 68,590,133,205 字节（约 63.9 GiB），包括旧 Trace、临时脚本和文档、验收解压副本及可重建编译中间件。另删除了原始四份 TCP/UDP 记录目录中的两份重复临时 SVG；四份火焰图 CLI/HTTP、几何和逐条路径复验通过。主仓五条支线均已合入 `main`，删除分支、拆除已验证干净的旧性能工作树并清理另一条失效工作树登记。

旧 H743 自动解码运行目录的日志在预检期间变动，和仍在运行的工作台一起保留（ISSUE-081）。Windows 仍占用旧 H743 10 ms 的单个 Store 文件及 500 ms 记录的 Store，整份 500 ms 暂留（ISSUE-082）。未停止服务，未动正式样本、独立回归夹具、用户数据或原有未提交的探针固件输出。核对后尝试删除重复的 OpenOCD checkout，执行工具在启动前以 `blocked by policy` 拒绝，副本仍保留（ISSUE-083）。

续查时，两份 Store 的删除权限探测均返回 Windows 错误 32。Restart Manager 将两处占用都归到监听本机 8911 端口的 Python 工作台，ETM 健康检查返回 HTTP 200。待该会话退出后，可以复核并删除两个遗留项。另已核对本轮验收目录下的生成文件全部清空，删除剩余空目录的命令也被执行工具以 `blocked by policy` 拒绝。

用户随后授权关闭 8911。退出前，CLI 执行页已断开，8911 只剩监听连接，服务没有解码子进程，记录目录没有解码标记。验收日志中五次采集均已保存。在独立控制台发送 Ctrl+C，进程正常退出，运行状态写为 `stopped`，8911 监听消失。另一处 8899 工作台仍在监听。

退出后，两份待清理 H743 记录的剩余 18 个文件与原清单逐项一致。删除这两份记录、已停止的 H743 验收运行目录及发布验收残留空目录，共核对 52 个文件和 302 个目录，文件逻辑体积 350,588,383 字节。19 项复现脚本、Kingst 配置备份和精简验收数据归档在本机 `docs/maintenance/h743-20261107-local-evidence.json`，每项都有 SHA-256。采集根目录中的 H743 记录复查只剩 200 ms 和完整 1 s。

针对 `docs/` 和 `etm/records/` 再次盘点，找到四份签名已失效的 `browse-v3.sqlite`。当前代码已判定它们需要重建。用无旧索引的隔离副本跑 `toolbox.py trace query --view summary`，200 ms steady 流式记录与 10 ms USB 初始化内联记录分别在 56.26 s 和 6.38 s 内完成查询和索引重建。逐项预检后只删除这四个索引，逻辑体积 5,205,561,344 字节。复核五份固定输入清单、五份长期保留结果的文件指纹和全部删除日志均通过。

该轮合计移除约 5.17 GiB 的文件。清理后按文件逻辑大小盘点，整个工作区约 17.29 GiB，`docs/` 约 7.23 GiB，`etm/records/` 约 4.25 GiB。主要剩余资产是固定回归输入的原始 CSV、KVDAT、匹配 ELF、旧版事件流与查询加速边车，以及当前保留的 TraceStore。精确对照仍会读这些数据。原先因政策保留的重复 OpenOCD checkout 仍在。

## 发布

发布提交 `ef1227606b8a6b7bedc00be517e1a464920bac8a` 推送时一并删除了远程已合入的 `codex/hss-stop-offline`，远端分支只剩 `main`。[Pages 工作流 42397232085](https://github.com/minichao9901/superlinkPRO/actions/runs/42397232085) 部署成功。[线上工作台](https://minichao9901.github.io/superlinkPRO/)在 Edge 启动后有 15 个工具页，调试、烧录和 RTT Viewer 的共享目标入口均在。线上页面入口、合并脚本、CLI 模块、Wasm 和 ETM 页面副本与本地发布文件一致；[线上 Windows ZIP](https://minichao9901.github.io/superlinkPRO/downloads/superlinkPRO-win64.zip)完整读取后，大小和 SHA-256 与发布清单一致。
