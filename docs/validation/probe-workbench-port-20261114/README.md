# SuperLinkPRO 顶栏、流收尾与 Windows 候选验收

2026-11-14，北京时间。源码以 `4ce5e62` 为基线，包含本次工作区改动。Web 和冻结 Windows 候选均为 `1.1.0-web.18`，Web bundle 构建于 `2026-11-13 17:14:00.417 UTC`。以下是本次现场结果。

## 实板

Windows 同时枚举两只 `0d28:0204`。本轮只操作 `akaLinkPro CMSIS-DAP`，USB 与 HID 命令序列号均为 `B4444F2110DDDAFE44800BBB0AE800E0`，CDC 为 COM34，固件 `0.1`，编译于 `2026/11/09 07:08:33`。另一只是 `zynqlinkPRO HID`，序列号 `12345678`。当前目标经 Wasm/JTAG 识别为 HPM6800EVK，TAP `0x1000563d`。变量地址、标记及主要加载段与仓库 `dbgstress` 一致，启动区一个加载段有差异。

| 项目 | 实测结果 |
| --- | --- |
| RTT 转发 | Edge 源码和 Chrome 145 合并页面分别完成五轮。每轮发出不同的 15,744 字节递增序列，浏览器 Web Serial 逐字节一致，探针 `moved=15744`、读错 0，五个目标读指针都是 15,744。前四轮停止后按钮立即可用，CDC 生产者回到 UART，COM34 保持打开。每次测试后写回并逐字节核对临时 RTT 环所占的 83,200 B SRAM。 |
| RTT → J-Scope | 第五轮转发会话保持运行约 10 秒，然后直接从 J-Scope 点顶栏连接。资源管理器停止 RTT，COM34 仍打开，J-Scope 接管 EP `0x83`。该轮使用一次性环数据，用于验证运行态交接。 |
| J-Scope | 现有 HPM6800 JTAG 协议，变量 `g_ticks`、`g_stage`，1,000 µs。Edge 连续三轮收到 519／499／495 组，Chrome 收到 528／508／504 组。Chrome 原始 DATA 负载为 4,224／4,064／4,032 B，原始包逐帧核对 DEF 首包、长度、包序和样本计数。各轮尾差、读错、残包、USB 丢样及调度跳拍均为 0。DEF 没有报告物理 TCK。400/500 kSa/s SWD 与 SPI→CDC 实负载另需匹配板卡及接线。 |
| 顶栏与能力 | Chrome 真机从顶栏连接调试器、RTT 转发、J-Scope、SPI/QSPI、共用 SPI 的面板、SPI 转发、I²C、模拟采集。烧录页使用顶栏选定设备读取 HPM TAP。SPI/QSPI 和面板均打开 HID 与 USB 数据端点。SPI 转发读到空闲，I²C 读到桥已连接，ADC 读到 16 位、最高 2 MSa/s，实机回复 DAC 不支持。对应释放后占用、等待和失败列表均为空。NRST 控件通过 `0x3A` 能力回读启用，实际目标 NRST 未接。 |
| HPM 目标烧录 | 冻结候选的真实烧录页使用 HPM SDK 官方 CFG。两次独立读取 XPI 前 64 KiB，发现仓库 ELF 的 `0x80000400` 加载段与板上现有版本不同，因此用双读一致的原始 BIN 作验收输入。16 个扇区全部写入并校验，重新连接独立读取全部 65,536 B，与原备份逐字节一致，SHA-256 `a0f998fdfa7d3cde6bc2439fb46ab5a0d5f11a2258b50ec63211d3c0c92ca0db`。软件复位后 PC 在 XIP，`g_ticks` 从 667,123 增至 668,156。 |
| HID 权限和描述符 | Windows HIDAPI 与 Chrome 均显示 `akaLinkPro CMSIS-DAP`。Windows USB/HIDAPI 和 HID `0x11` 返回同一序列号，WebHID 对象自身未公开序列号。仅把已有 USB/HID/CDC 授权复制到隔离的测试配置。Chrome 完全退出后，通过探针 HID 软件重启并观测到设备离开和同序号重新枚举。重开同一 Chrome 配置和站点，三类授权仍可直接枚举，帮助页读回同一型号和编译时间。新配置的无头设备选择测试未获得选择事件，首次可见选择框和物理断电复验留待现场。CherryUSB 描述符与 EP0 沿用现有实现。 |
| 探针 DFU | 冻结候选的 `--workbench-cli probe-flash` 从运行态由 HID 自动进入 DFU，按序列号和端口 `1-2.1` 匹配。备份完整 901,120 B 应用区，再重写仓库中同一个 192,848 B APP。备份 APP 前缀、请求文件与写后读回的 SHA-256 均为 `e0e0f3e74c1bdb19bb7b30db3b9b7cc97f03a185abfe690d8dbd6d694018f0ac`。离开 DFU 时状态应答中断，同一探针已重新进入运行态，冻结 Web 帮助再读版本和时间一致。 |

冻结候选另在 HPM JTAG 上各采约 1 秒。单变量请求 400、500 kSa/s，实得 312.741、311.187 kSa/s，生成 315,194、313,672 样本，跳拍 87,945、190,323。八变量请求 50 k 组/s，实得 23.485 k 组/s，生成 23,666 组，跳拍 26,732。三轮已经生成的样本全部收到，USB 丢样、读错、包序缺口和尾差为 0。各档物理 TCK 未测，数据点可用于比较同一 HPM 配置的收尾。完整结果见本机 `tmp/port-20261114/hpm-scope-rates.json`。

## H743 与候选包

- 官方 H7 CFG 仍由 Wasm 解析。对同 DAP 的 AP2 装入 `examine-start`，先确认 DBGMCU 调试时钟，再运行原事件。实际 Jim Tcl 验证了原版 CFG 的 AP2 事件注册，用寄存器模型验证掩码、读回和原事件执行。注入初始化失败后返回擦写前的明确错误。现场 HPM6800EVK 的结果不代替 H743 的 SDRAM 初态、独立 Flash 读回和复位验收。
- Windows 候选的冻结 CLI、HTTP、演示解码、匿名完整下载及 SHA-256 验证通过。帮助页同时显示 Web 和 Windows 版本，浏览器加载合并入口。交付候选 ZIP 共 103,467,400 B，SHA-256 `65c1d5adfd82e7c97c34343099f82a3bf858c4d83b9d6f415e04012d37bc7ccb`。冻结服务按其清单另行重打的 HTTP ZIP 共 104,025,774 B，SHA-256 `37260eb065c072e56aa455dd376c7b95ed8625083f997dfe2ab54becb2db28ae`。EXE SHA-256 `260a4765be43043ea034410903e57c7cc12bcb8204bb5c7024dca2575956c9e6`。包清单 `sourceDirty=true`，ETM 运行时来源 `044af4aee4b6f1ad6423976edacb47e0e4870f7e`。
- 145 个离线 Web 测试文件、13 项 CLI 和打包单测、H7/RTT 定向回归，以及 CLI 参考、Web bundle 与 ETM 线上副本一致性检查通过。Windows 正式安装与 Pages 部署继续使用现有发布版本。

本机原始包、临时 SRAM 备份、DFU 读回和诊断位于 `tmp/port-20261114/`。Windows 候选及冻结验证位于 `tmp/workbench-package-1368fcdb/`。浏览器配置和 CLI 令牌留在临时目录。问题编号见[跟踪列表](../../项目问题跟踪列表.md)。
