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
