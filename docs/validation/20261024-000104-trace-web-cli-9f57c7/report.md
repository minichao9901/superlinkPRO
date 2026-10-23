# Trace CLI 复用网页能力：实现与 H743 验证

- Run-ID：`20261024-000104-trace-web-cli-9f57c7`
- 生成时间：2026-10-24T04:50:44.089008+08:00
- 工作目录：`C:/Users/Administrator/.codex/worktrees/etm-kvdat-fastpath/superlinkPRO`
- Tested-Revision：`pre-commit-worktree`
- Tested-Base：`216e60e53c8983431980243839d99ed6df0568f5`
- Tested-Dirty：`true`
- **Validation-Result：partial；Hardware-Test：partial。**

实现已补齐网页 ETM 及配套 Cortex-M 调试/烧录入口，主要硬件路径已经通过。
**最终 worker 的重新启动/一键运行时启动和退出没有完成实际验收**：工具自动审批拒绝了启动命令。
已运行的早期 Native worker 完成了下表中的真实操作。之后对会话交接、待恢复配置、队列回执、
文件导出与退出清理的改进完成软件回归；不能把早期 worker 的实测当成最终 worker 全部改动均已板测。

## 1. 改动和复用边界

- 删除 CLI 对 pyOCD 的依赖和会话初始化钩子。没有修改全局 pyOCD 安装。
- 调试命令原样调用网页 `runCmd`；使用同一 `DebugSession`，覆盖断点、寄存器、内存、变量、
  源码单步、栈帧、DWT、镜像命令。Native 会话跨 CLI 调用保留符号、断点和监视。
- 烧录调用网页现有 `FlashView._flashWebusb`、`FlashRunner` 和算法表，强制校验/复位运行。
  BIN 要求基地址，Native 在接触目标前先按现有网页范围规则预检镜像。
- `EtmTarget` 负责 Trace 配置、回读、临时起点断点、复位到起点和调试口恢复。
  `capture --entry` 先启动 LA，再放行 CPU；沿用网页 D0 下降沿触发顺序。
- Node 的 `usb@3.2.1` 只适配标准 WebUSB 接口；没有另写 CMSIS-DAP/SWD 协议。
  `linkedom` 为现有烧录代码提供其选项/结果控件，不运行浏览器页面。
- 同一命令适配器也可由显式授权的本地网页执行。命令队列默认关闭、回环监听、令牌校验；
  固定动作，不提供 eval。Native 与浏览器 worker 不能同时绑定一个服务。
- 查询补齐函数、热点、火焰图/可信区段数据、实际采集时长；增加 ELF 导出、本地记录管理、
  后配源码备份、旧内联/JSONL 历史读取。默认原生解码/索引/导出策略保持开启。
- 修复 PC 导航与底层接口的映射；修复网页命令 `info` 的 SWD MHz 换算，补齐无 ELF 时的裸地址命令。
- 本轮未修改 OpenCSD、原生解码算法、目标固件源码或 Flash 镜像内容。

## 2. 软件验证

| 检查 | 结果 |
|---|---|
| CLI / 记录 / 运行时单元测试 | 24 项通过 |
| 队列准入、令牌、回执、取消、单 worker 测试 | 5 项通过 |
| Web CLI、USB 生命周期、会话交接、文件输出适配 | 最终补充 18 项通过 |
| 网页调试器既有核心回归 | 326 通过、0 失败 |
| 网页镜像、RAM 填充、Flash 镜像/ABI/数据时钟回归 | 全部通过，逐命令日志见 results.json |
| ETM 目标、F103/H743 适配和断点回归 | 27 项通过 |
| 共享 WebUSB 控制写、调试口恢复与复位回归 | 8 项通过 |
| 生成网页脚本、ETM 页面副本、Python/Node 语法检查 | 通过 |
| 真实 CLI 子进程端到端 | **66 条全部符合预期** |

66 条检查包含七种导航的双向定位、十种导出、五种区间导出、拒绝覆盖、记录包导入、
原始 KVDAT 重解和记录重解、源码后配与备份。stdout JSON 与另存 JSON 完全一致。
导入/重解后 summary 和 quality 与原记录一致。源码类导出使用既有、带七份配套源码的
独立历史记录；没有用下文无 DWARF 的快照伪造源码映射。

- 端到端记录：`E:\superlinkPRO\docs\validation\20261024-000104-trace-web-cli-9f57c7\acceptance-042254/checks.json`
- 端到端数据：`C:\Users\Administrator\.codex\validation\20261024-000104-trace-web-cli-9f57c7\acceptance-042254`
- 分组回归：`E:\superlinkPRO\docs\validation\20261024-000104-trace-web-cli-9f57c7\checks-final-044026/results.json`
- 最后补充：`E:/superlinkPRO/docs/validation/20261024-000104-trace-web-cli-9f57c7/adapters-final-supplement.log`

发现并纠正过两个实际问题：PC 导航原先向底层传了不支持的 mode，已修复；旧 RecordStore
分页会把越界 offset 夹回最后一页，旧格式逐事件导出需按 total 结束，已补停止条件和回归。
首次失败检查保留在 `accept-web-cli-first.log` 等文件中，没有改成通过或覆盖。

## 3. H743 真机条件

- Windows x64，Python 3.13.5 / Node 24.13.1 / KingstVIS 3.6.6.0。
- akaLinkPro：`8F538633E6AEC4CA8ABB6A36FEBB1AC2`；CPUID `0x411fc271`，DEV_ID `0x450`。
- 实际 CPU 60MHz，物理 TRACECLK 30MHz；500MSa/s，CH0 时钟、CH1～4 数据，1.65V，
  相位 0.25，软件滤波 4ns，Trace ID 2，return stack/cycle count 开，branch broadcast 关。
- 本轮硬件 RPC 未加载 pyOCD，访问由网页原有 WebUSB 驱动执行。

板上程序与上一轮样例 ELF 不匹配（首个差异 `0x08000002`），已正确拒绝配置。
未将不匹配的源文件作为板上程序的源码。先对整片 2MiB Flash 独立读取两次，结果一致；
从真实 Flash 内容建立无原工程 DWARF 的 ELF 容器，用于机器码匹配和 Trace 解码。
仅为两个观察地址加了人工标签；`sourceMapped=0`，不声称这些标签来自原程序符号表。

## 4. 真机结果

| 操作 | 观察与结果 |
|---|---|
| 连接、识别、回读时钟 | 通过；CPU 60 / TRACECLK 30MHz |
| 跨命令保留/命中断点 | 通过；停在 `0x0800b44c` |
| 单步、删除断点、继续 | 通过；PC 从 `0x0800b44c` 前进到 `0x0800b44e` |
| 复位后运行到指定入口 | 通过；一次命中，临时断点移除；在初始化阶段读回 HSI 64/32MHz，继续后恢复 60/30MHz |
| 独立 configure → inspect → restore | 通过；inspect 未提前恢复配置，状态文件完成后标为已用 |
| 断点联动采集第一份 | 20ms / 10,000,000 样本；1,050,208 条指令；packetIssues=0 |
| 断点联动采集最后一份 | 20ms / 10,000,000 样本；1,046,360 条指令 |
| 最后一份的解码/索引/核验 | 0.948s；含硬件准备的采集阶段 10.234s；非同负载性能对比 |
| 最后一份边界 | 保留 1 个明确位于输入末尾的 I_INCOMPLETE_EOT；其余质量门禁通过，不把尾部包悄悄抹掉 |
| 独立 Python 前端对照 | TPIU 字节、ETM 字节、framing 全部与原生结果一致 |
| 错误 20MHz 预期、实际 30MHz | 拒绝，退出 2；未启动采样，没有导出上次 LA 缓存；恢复确认 |
| 网页实现烧录接口 | 回写原镜像的 316,880 字节，网页算法校验成功并复位运行 |
| 烧录后独立整片回读 | 2MiB 与操作前一致，SHA256 见下；保留原程序内容 |
| 最后独立回读 | 26 组受控掩码 0 差异，GPIO 时钟恢复，断点为空，CPU 继续运行 |
| 收尾 | 探针已断开；自建测试服务/worker 已关闭，KingstVIS 保留运行 |

整片 Flash 的操作前两次读取及操作后读取均为：

`de6e880669a9fb5204a06d04ae9652fdb83da0860c62aef1af34db3bd6574399`

最后原始 KVDAT SHA256：
`a2ccff3712311fe29a57c8afe40a0581226fbf3610440f0b236c2ba14d1c9f31`

第一版独立寄存器检查在 GPIO 时钟关闭时读到了 `0xffffffff`，不能据此认定 GPIO 未恢复。
第二版在 CPU 暂停时临时打开 GPIOE 时钟，读回后恢复原时钟位，并在 finally 继续 CPU；
26 掩码均一致。两版原始输出均保留，最终以 `independent-final-health-v2.json` 为准。

## 5. 未完成的实际验收与范围

1. **一键 web start/stop 未实际运行。** 自动审批两次拒绝，返回仅为 `blocked by policy`。
   后一次启动已改成只做软件 ping、不枚举/打开探针，仍被拒绝。没有生成该次启动的结果 JSON，
   不伪造运行时 PID、端口或成功结果。生命周期代码和拒绝覆盖/占用端口等路径通过软件测试。
2. 真实操作由本轮早期 `native2` worker 完成。最终 worker 新增的挂起断点/DWT 恢复、
   待恢复配置所有权、防误恢复、队列回执及退出回收等已软件测试，实际重启被拦截，不能扩称均已板测。
3. 本轮只做 H743 短窗；没有用新硬件通道重复 500ms/1s、F103/H7B0、拔插及强杀全时序。
   本轮目标没有可用目标 CDC 端口，未重新验收 echo/bannner 负载。
4. 无匹配 DWARF，未在当前程序上验收源码行断点、源码级 n/si/fin、局部变量。
   接口复用网页原实现，这些功能有软件回归，不能说当前程序已经完成源码调试验收。
5. Native 烧录接出 Cortex-M WebUSB 算法；OpenOCD/J-Link/RISC-V 后端未接入此 CLI 路径。
   大于包预算或缺完整原始输入的历史分片记录不能当作完整合格包导出。
6. 本次本地交付，不推送、不部署，不把源码 CLI 冒称为已发布 native.13 EXE 的新能力。

## 6. 证据入口

- 本报告：`E:/superlinkPRO/docs/validation/20261024-000104-trace-web-cli-9f57c7/report.md`
- 最终软件输入快照：`E:/superlinkPRO/docs/validation/20261024-000104-trace-web-cli-9f57c7/tested-inputs.json`
- 原始证据指纹索引：`E:/superlinkPRO/docs/validation/20261024-000104-trace-web-cli-9f57c7/evidence-index.json`
- 波形、Flash 备份、记录和导出：`C:/Users/Administrator/.codex/validation/20261024-000104-trace-web-cli-9f57c7`
- 生产中文方法：`docs/trace-cli-ai.md`

实际执行脚本：

- `E:/superlinkPRO/docs/validation/20261024-000104-trace-web-cli-9f57c7/run-final-checks.py`
- `E:/superlinkPRO/docs/validation/20261024-000104-trace-web-cli-9f57c7/accept-web-cli.py`
- `E:/superlinkPRO/docs/validation/20261024-000104-trace-web-cli-9f57c7/backup-flash.py`
- `E:/superlinkPRO/docs/validation/20261024-000104-trace-web-cli-9f57c7/verify-flash-after.py`
- `E:/superlinkPRO/docs/validation/20261024-000104-trace-web-cli-9f57c7/make-snapshot-elf.py`
- `E:/superlinkPRO/docs/validation/20261024-000104-trace-web-cli-9f57c7/native-reference-frontend.py`
- `E:/superlinkPRO/docs/validation/20261024-000104-trace-web-cli-9f57c7/independent-final-health-v2.py`

代码与报告可随 Git 保存；大体积波形、导出和备份留在上述持久目录。
令牌和含令牌 URL 的服务原始日志不随本提交分发。
