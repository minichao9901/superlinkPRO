# 三板验收移植与离线核对（2026-11-15）

基于 SuperLinkPRO `a08ef1a` 工作区，按 ZynqLinkPRO `0076566a` 及三板验收范围适配。上游参考范围从 `4884a93a` 的精简清单和 `54150b31` 的验收执行器延伸至 `404d12a8` 的证据收尾。本报告记离线移植，H743 实测另留原始证据。本轮范围调整后，工作台仅启用 ARM/SWD 和 RISC-V/JTAG，ARM/JTAG 用原生 CLI。

## 验收范围

| 目标板 | 通用工具 | ETM |
| --- | --- | --- |
| F103ZE | Web SWD、原生 ARM/JTAG，覆盖 SRAM、烧录、调试、RTT Viewer、RTT 转发、J-Scope。ARM/JTAG 高速通道需要含 F103ZE 双 TAP 识别的 APP | Kingst、ETMv3，1 MHz SWD 控制 |
| H743 | Web SWD、原生 ARM/JTAG，通用工具同上。用 CPU480／HCLK240 夹具核对吞吐 | Kingst、ETMv4，1 MHz SWD 控制，整板最低 1 秒，CDC 专项 10/200/500/1000 ms |
| HPM6800EVK | RISC-V JTAG，通用工具同上。烧录使用 SDK CFG，XIP 独立回读走映射区 | 不适用 |

两仓主清单都是七组各五项。每条工作台链路三轮跨页与断线压力、单路和八路的 Web 与原生 J-Scope 60 秒证据，以及逐轮性能、恢复与交付均列入本仓口径。SuperLinkPRO 的主测档、稳定上限和计数证据按本机配对建立。具体条件见 [主清单](../板卡验收清单.md)。

## 源码适配

- 原生 ARM/JTAG 使用上游官方 STM32 target CFG，启动前通过 CMSIS-DAP 选择 ARM SWJ，校验 CPU 和边界 TAP。
- HPM5301 APP 识别 F103ZE 和 H743 的 CPU／边界双 TAP，沿用现有 ADIv5 MEM-AP、RTT 指针回读与 EP83 采样后端。J-Scope 显示实得及标称采样率，单路用 Sa/s，多路用组/s。上游参考包括 `3e29af39`、`2bbbd33b`、`6df21416`、`74b7e65e`、`f4b3a552`。
- Viewer 的 ARM/SWD 与 RISC-V/JTAG 继续通过 OpenOCD Wasm，原生 ARM/JTAG Viewer 通过 OpenOCD RTT TCP。Web／原生 Scope 结束阶段使用本仓 EP `0x83` 的 STOP ZLP、读请求退休和最终计数。
- 三板脚本从本仓 `board-matrix.json` 绑定目标 ELF。默认 `toolbox.py board-acceptance` 只展开计划。执行和复核按板卡、链路及用例区分记录，证据目录不可覆盖。Flash 脚本先核对现场镜像和备份，回读实际 Flash／映射区，必要时从烧录器写回备份。
- 三轮跨页压力与实际物理断线仍需独立实测。RTT 原始下行、环回绕、边界样本、原生 SRAM／烧录对比、J-Scope 的同条件历史固定速率和 ETM 专项仍有手工或分项入口。没有计数、恢复或配对证据的项目标记待验。

## 离线实施与检查

[DeepSeek Harness 会话](http://127.0.0.1:62811) 实施 runner 和文档初版。Codex 独立审查并修正了链路复核碰撞、真实队列回包、Flash 与 SRAM 恢复、J-Scope 定义格式和原生 STOP 路径。Windows `npx.cmd` 的启动失败通过已安装 DSH 的 Node 入口恢复。主会话继续执行，子代理额度为零。

- HPM5301EVKLite APP 以 HPM SDK 1.11.0 在独立目录构建成功，输出保存在 `tmp/zynq-acceptance-port/app-build/output/`。
- 复用独立 Chrome 的无硬件 Scope 渲染测试通过 14 项。Node 回归包括官方 CFG、SWJ 报文和失败清理、H743 调试时钟、RTT／Scope 生命周期和会话绑定。
- Python 验收测试包括计划、假队列执行、时钟与安全门、按链路复核、SYS/Flash 恢复和 JSP 终态计数。模拟 SRAM 备份放在 `tmp/board-acceptance-test-artifacts-20261115/`。

## RTT 转发执行与复核续作

[DeepSeek Harness 续作会话](http://127.0.0.1:59353) 实现 Web Serial 窗口。独立审查补上了 STOP 前结束测速、STOP 后等待迟到数据、实收大于 `moved` 时判失败和报告启动状态的回归。固件 STOP 会清除 `cbAddr`，执行器用 START 状态绑定实际通道和控制块。

- R 组在同一已授权探针和 COM 上执行两次 Web 及一次原生窗口。独立核对目标 `RdOff` 进位、各窗原始字节、实际搬运块和终态计数。
- 511/512/513 B 的有限载荷通过三个 SRAM 上行环逐字节对账。执行器在写入前验证 ELF 占用、保存测试区与哨兵，结束后读回恢复，并检查原目标运行状态。
- 离线复核核对结果摘要与独立 JSONL。RTT 通过项的原始文件 SHA-256、跨窗序号及 STOP 计数会重新计算。跨页压力调度可按链路独立复现，复核保留尚无执行证据的轮次。

该阶段通过 87 项 Python 和 32 项 Node 检查，并核对三板计划、Web bundle 和 CLI 清单。F103ZE 可以匹配仓库现有序号 ELF，H743 和 HPM6800EVK 的已登记连续载荷为固定文本，R01 的长窗序号仍需配套夹具。

## RTT Viewer 环尾验收

按 ZynqLinkPRO 的 Viewer 对账条件构造有限 SRAM 夹具。配套 ELF 先与实际 Flash 对齐，检查夹具地址避开 ELF 装载区和当前栈。H743 要求 D-cache 已关闭，HPM6800EVK 使用非缓存区域。V01、V02、V04 共用一次窗口，覆盖上行的 32 条连续序号和十进制序号回卷，以及 8 字节二进制下行。上下行读写各经过一个环尾。Viewer 从实际页面发送，释放后调试器独立读回环、游标和前后哨兵，再核对 SRAM 恢复。离线复核重算期望内容和游标，并测试即使更新文件哈希也能发现被改动的游标。

三个板卡的夹具地址已按实际 ELF 核对，并用生产 JS RTT 协议在模拟内存中逐字节读写通过。同时通过 132 项 Python 测试、相关 Node RTT 与工作台回归、Web 构建及 CLI 静态清单检查。本节范围为离线，实测结果待接板。

## H743 AXI SRAM 流水测速

按上游 `f4b3a552` 的交替读写窗口移植。Web 和原生 OpenOCD 的普通块测速继续按各自计时口径记录。流水阶段独占同序列号的 SuperLinkPRO Bulk 接口。DAP Info 回报 1024 B 包和 4 槽时使用 128 字、深度 4。每个复合请求含 TAR 和 TransferBlock，测试覆盖连续四段 1 KiB，实际测 64 块一轮、三轮和至少 10 秒交替读写。记录的是 SWD 请求值和 RCC 回读的目标 CPU/HCLK。恢复阶段用原生 OpenOCD 比对整段 AXI SRAM 和哨兵，再检查工作台重连后的夹具心跳。

离线模拟覆盖包容量、正确回包、损坏回包后恢复、D-cache 门控、队列收尾未确认和恢复失败。完整模拟配置运行 10.008 秒、795 个交替轮次，最多四个请求在途，恢复字节一致。137 项 Python 测试与 Web 构建、CLI 清单检查通过。首个同条件实测速率与 2% 复跑所需的对照基线待建立。

## H743 和 HPM6800EVK 的 RTT 长窗夹具

在现有固件工程中增加 `-Sequence` 构建。输出为独立的 `fw-seq.elf`，保留现有 `fw.elf` 的固定文本测速用途。新夹具输出 13 字节递增记录，每毫秒最多 32 条，一个 60 秒主窗的名义载荷约 25 MB。验收入口按现场 Flash 识别夹具，同页跑 60 秒和重开短窗，随后以原生 COM 跑 60 秒。回放从三个原始文件检查连续序号，另外核对两条路径的最终 STOP 搬运计数和目标 `RdOff`。统一验收命令默认预留 180 秒等单个队列任务，保留显式 `--timeout`。

| 目标 | 控制块／上行环 | 序号 ELF SHA-256 |
| --- | --- | --- |
| H743 | `0x24000014`／`0x24000174` | `449c54330429570dfd781975f760daa54967bc402e2b6b798e18f7467e9f210f` |
| HPM6800EVK | `0x01240000`／`0x012400a8`，非缓存 AXI SRAM | `ba68a2a9554b66789eb1cf633b3ba8acfe1b4730e64b8b418e886cc45468d638` |

两份 ELF 编译成功，与清单、Flash 装载段和 RTT 符号交叉核对通过。F103ZE 的既有序号夹具继续使用 8 秒窗。139 项 Python 和 13 项 Web 及矩阵离线检查通过，覆盖 60 秒定时、过短窗口拒绝、跨窗序号、STOP 计数和证据合并。新长窗仍待实机运行。

## 2026-11-15 收尾交接

交付范围为本地源码、验收脚本、两份序号 ELF 和验证记录。三板整板目标已暂停。最终一轮通过 141 项 Python、32 项 Node、Web bundle 和 CLI 清单检查。本次收尾为离线。

| 用例 | 本次交接的证据与边界 |
| --- | --- |
| S · SRAM | 三板脚本通过离线回归，H743/SWD 流水完整模拟跑过 10 秒。正式流水吞吐基线待实机。 |
| F · 烧录 | H743 早先双链路烧录、校验和原 Flash 恢复有记录。三板使用新 runner 的整组实机待验。 |
| D · 调试 | H743 原生 ARM/JTAG 新入口通过识别、暂停、单步和继续。临时断点与源码步进的待复验项仍按 ISSUE-156 跟踪。 |
| V · RTT Viewer | 三板环尾夹具离线回放通过，H743 原生 TCP 做过实机冒烟。新 runner 的三板完整 V 组待验。 |
| R · RTT 转发 | 序号 ELF、长窗计时、跨窗序号和 STOP 复核通过离线检查。新 60 秒序号路径待实机。 |
| J · J-Scope | 用 24 MHz 硬件快照判定组率和跳拍率。Web 长窗和原生固定窗复核原始 JSP、DEF、包序、STOP 整组数和字节数。回放 H743 两份历史原生 60 秒数据（6,081,120／1,737,464 组）通过完整计数复核。HPM6800EVK 两份历史 Web JSP（14,783,888／1,917,885 组）通过 DEF、包序与 STOP 对账。本次新增判据和复跑流程待实机。 |
| E · ETM | 按本轮范围暂缓；HPM6800EVK 为不适用。 |
| 跨页断线 | 模拟测试核对切页发生在停止前，并要求重连前后保存顶栏、HID、USB 与串口空闲字段。三轮实机压力及物理拔插待验。 |

H743 最后一次实机恢复记录显示 CPU 480 MHz、心跳推进、原 2 MiB Flash 哈希一致，详见同日 [H743 报告](h743-board-acceptance-port-20261115.md)。收尾检查未发现活动的本轮 OpenOCD，已结束四个本轮临时辅助进程。原始数据、失败样本、ELF、Flash／SRAM 备份以及引用链保留在 `tmp/zynq-acceptance-port/`、`tmp/issue150-20261114/` 和 H743 报告所列目录。临时浏览器目录及队列令牌的删除命令被自动审批拦截，文件留在本机。

| 交付位置 | 状态 |
| --- | --- |
| 本地 Git | 本报告和对应源码、夹具纳入本轮交接提交。 |
| GitHub `main` | 本轮交付仅本地，远端跟踪基线为 `a08ef1a`。 |
| 固定 Windows 工作台 | 本轮范围为源码，固定 EXE 及用户数据原样保留。 |
| GitHub Pages／Windows Release | 本轮交付范围为本地源码与证据。 |
| QSPI 冷启动 | Zynq 专用步骤，本轮不适用。 |
