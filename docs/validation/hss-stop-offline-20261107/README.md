# HSS 停止收尾与原生 CLI 拦截复核

日期：2026-11-07（Asia/Shanghai）。源码基线 `cf5e6fb`；本报告与修复同提交。
只做软件模型、源码构建和本地日志分析，未连接探针、复位、烧录、测速或重新发布工作台。

## ISSUE-043：HSS 停止收尾

旧版有两处独立损失：探针停止调度时不回填最后一笔 posted read，也不提交半满 DATA；
网页把在飞读取结果按旧轮次丢弃。即使包序号没有缺口，最终接收样本数也可能少于探针计数。

HPM EP 0x83 使用[协商的 STOP 尾部协议](../../hss-tick-protocol.md#stop-tail-on-hpm-ep-0x83)：
探针主循环补读、发送有效尾包及结束标记，浏览器按提交顺序交付，并让结束标记之后的空读逐笔退场。
如果最后一笔读失败，探针剔除该槽并单独计数，网页将 `收到 + 无效` 和 `produced` 对账。
新固件仍接受旧客户端的无参数 STOP；旧固件无法提供半包，网页可报告可观测的尾差。
Zynq EP 0x87 保留原有握手，ETM 未改。

| 验证 | 范围 |
| --- | --- |
| `python -X utf8 script_test/scope_host_test.py` | 编译实际 sampler C：SWD/JTAG 流水线、IRQ 内请求 STOP、全满队列、半包及整包边界读失败、八变量、精确数量的 ZLP、重复 STOP、旧 STOP、总线复位中断 |
| `python -X utf8 tools/test_workbench_offline.py --output <独立目录> --with-c-models` | 138 个文件、260 项通过。覆盖有序交付、读边界、空读退场与重开、STOP 失败不重发、最终计数及两个 C wire 模型 |
| `node web/tools/dev/build-web.mjs --check` | 生产 bundle 与 169 个源码模块同步 |
| HPM5301EVKLite，HPM SDK 1.11.0 / RISC-V GCC 13.2.0 | 独立目录完成 CMake/Ninja 编译、链接和镜像打包；未烧录 |

本机候选 `tmp/hss-stop-offline-20261107/firmware-build/output/akaLinkPro_App_pack.bin`：
194,232 字节，SHA-256 `197f2203c348f6108c9e4c7ba5c55521c1ade31a9054df91559e47b17c39524e`。
这是离线构建产物，不能据此认定实物板、USB 驱动或采样速率已验收。

待真机：用匹配探针固件和此版 Web 在 probe + F103ZE 上测单字 500/400 kSa/s 和八变量 50 k 组/s；
覆盖手动 STOP、定时 STOP、采集未满包/恰好落在 USB 读边界、连续重开与共享 CDC/SPI。
要求无注入故障时 `收到 == produced`、无效样本/USB 丢样/读错/包缺口均为 0，且成功停止不复位 USB。
另测拔插/复位时的失败提示与恢复。速率复用[历史正式窗口和相同条件](../perf-pages-20261107/README.md)，
不把暖机或停止收尾纳入稳态速率，也不预填本次真机结果。

## ISSUE-038：GCC 模型补测

此前 GCC 16.1 的退出 1、无输出，连最小 C 程序也发生；本轮未复现。
用户重启后的环境里，未替换的 GCC 及其 `cc1`、`cc1plus`、`collect2` 都能启动，
最小 C 程序使用原工具链和历史私有前缀分别编译成功。两个 C wire 模型使用原工具链、任务内 TEMP/TMP 实际编译并通过。
这补上了离线验证缺口；现有证据无法判定历史故障是哪个子进程、依赖或环境状态所致。
保留[历史复现与 C++ 规避记录](../perf-pages-20261107/runtime-sync.json)；若再现，保留子进程退出码、GCC `-v` 输出和当时环境。

## ISSUE-044：数字开头的探针序列号触发 PowerShell 策略误判

对照[上轮配置核查](../perf-pages-20261107/startup-settings.json)、重启前后的原始工具记录和
[Codex 0.166.0-alpha.6 源码](https://github.com/openai/codex/tree/rust-v0.166.0-alpha.6)，
在本地只把旧命令当文本分析。用同版本 PowerShell grammar 0.26.4 重建语法树，并按源码的词项校验及匹配条件复算。
两个独立的绑定启动记录都能识别为一个命令，语法树无错误；问题出在后续的参数校验和保守匹配：

1. 探针序列号是未加引号、以数字开头且含字母的参数。Codex 的
   [PowerShell 词项检查](https://github.com/openai/codex/blob/rust-v0.166.0-alpha.6/codex-rs/shell-command/src/command_safety/powershell_tree_sitter.rs#L462-L476)
   将这类非标准十进制词项留给保守路径，整条命令失去「这是 Python 及其参数」的识别结果。
2. [Windows 命令判定](https://github.com/openai/codex/blob/rust-v0.166.0-alpha.6/codex-rs/shell-command/src/command_safety/windows_dangerous_commands.rs#L104-L125)
   在保守路径中接受任意位置的 `start`，并与同一命令里的 HTTP URL 组合。这里实际是 Python CLI 的
   `web start` 子命令和 `--web-url` 的 loopback 参数，满足误判条件。
3. [执行策略](https://github.com/openai/codex/blob/rust-v0.166.0-alpha.6/codex-rs/core/src/exec_policy.rs#L800-L814)
   在 `approval_policy=never` 时直接拒绝这种命令；
   [通用拒绝分支](https://github.com/openai/codex/blob/rust-v0.166.0-alpha.6/codex-rs/core/src/exec_policy.rs#L1129-L1137)
   不带具体规则。因此 Full Access 不改变这个判定，`reviewer=user` 也没有收到可确认的弹窗。

| 原始结果时间（北京时间） | 观察与纯文本复算 |
| --- | --- |
| 11-07 07:00:22 | 绑定启动在 CreateProcess 前被拒；语法树无错误，数字开头的序列号令参数校验回退，`start + URL` 匹配 |
| 11-07 07:09:08 | 只读检查命令也带着原启动参数；其外层语法未被识别为普通命令，保守路径同样匹配，检查器未启动 |
| 11-07 08:36:03 | 重启后，换成随桌面版提供的 PowerShell，独立绑定启动仍在 CreateProcess 前被拒；复算命中同一条件 |

对两条独立启动，只在**文本模型**中去掉绑定参数后，语法仍正确，剩余参数可被当作独立
Python 调用识别，`start` 就不会被当作 Windows 命令。此对照未执行任何启动命令。
这些证据定位了与记录吻合的具体误判链，但桌面端未提供私有规则 ID 或服务侧决策日志。
没有修改设置、重试被拒动作或更换入口规避。问题留待 Codex 平台修正并以真实启动复核；
本地保留调用时间、调用 ID 和命令 SHA-256 供平台对照，不把完整命令及序列号入库。
