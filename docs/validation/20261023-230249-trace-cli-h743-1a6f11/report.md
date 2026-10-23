# Trace CLI 完整交付与H743真机验收（2026-10-23）

结论：CLI配置/跨进程恢复、真实短窗采集、原生解码、完整性校验、查询、九种导出、记录包往返和网页导入已通过本轮验证。入口为`python -X utf8 etm/trace.py`，完整中文方法见`docs/trace-cli-ai.md`。

- Run-ID: `20261023-230249-trace-cli-h743-1a6f11`
- Tested-Revision: pre-commit-worktree
- Tested-Base: `3875bfd8f79bee462e898d8c0446b25a1cc7246e`
- Tested-Dirty: true（CLI、文档、依赖清单、CLI测试）
- Validation-Result: passed（下述已覆盖范围）
- Hardware-Test: passed（H743短窗与明确故障路径）
- 工作目录：`C:/Users/Administrator/.codex/worktrees/etm-kvdat-fastpath/superlinkPRO`
- 原始证据：`E:/superlinkPRO/docs/validation/20261023-230249-trace-cli-h743-1a6f11`
- 数据：`C:/Users/Administrator/.codex/validation/20261023-230249-trace-cli-h743-1a6f11`
- 代码没有改动已发布Web/EXE的运行逻辑；这套CLI为源码入口，本轮不打包/推送线上EXE。

## 实现

Python统一CLI，stdout一份JSON、stderr日志；退出码0/2/3/4/5/130区分成功、参数、依赖、IO/硬件、验证失败和中断。`--json-out`在操作前独占预留；新记录/导出拒绝覆盖已有文件。软件引擎默认强制native、完整索引、原生投影/筛选和ELF准备缓存，未静默回退。

Node子进程复用生产`target.js`配置、ELF校验与恢复，不复制一套寄存器配置。通过本地stdio由Python对指定pyOCD探针执行实际SWD读写，不开HTTP端口，不伪造探针，不自动烧录/复位/修改PLL。独占锁在Git公共目录覆盖各worktree的CLI。

首轮实际configure后restore被Funnel配置变化拒绝；定位为pyOCD 0.45.1连接发现阶段默认启用全部Funnel输入。已增加**当前会话实例**的发现钩子跳过Funnel自动配置，CPU/DWT/FPB仍按pyOCD正常初始化；无全局库修改。保持目标身份、状态SHA和寄存器期望值检查，未通过忽略全部变化来规避保护。修复后跨进程配置/恢复再次成功，最终独立读回26组受控寄存器掩码0差异。

## 真机条件与四次新采集

H743 DEV_ID0x450、Cortex-M7；akaLinkPro序列号`8F538633E6AEC4CA8ABB6A36FEBB1AC2`；COM33目标CDC；KingstVIS3.6.6。
CPU60MHz、物理TRACECLK30MHz、HSE25MHz、500MSa/s、20ms、CH0时钟/CH1～4数据、threshold1.65V、fraction0.25、软件滤波4ns、GPIO速度2；Trace ID2，返回栈/周期计数开、分支广播关。使用当前板上已烧录匹配ELF，31856字节Flash回读核对；本轮不重新烧录或复位。

持续负载来自固件20ms周期banner和CLI约4ms序号/CRC echo。echo总包数覆盖预热/设置/回传时间，不冒充物理窗口内调用数；最后一列从实际trace恢复的main调用边取得。

| 样本 | 实际采样点 | 恢复指令 | 包问题 | echo发送/收到 | 窗口内banner/echo调用 |
|---|---:|---:|---:|---|---|
| a | 10000000 | 363262 | 0 | 833/833 | 1/5 |
| b | 10000000 | 366263 | 0 | 829/829 | 1/5 |
| c | 10000000 | 360816 | 0 | 827/827 | 1/4 |
| final | 10000000 | 365843 | 0 | 825/825 | 1/5 |

四份KVDAT都实际录满20ms（10000000点），输入指纹/绑定验证通过；溢出、重叠、遗漏、未处理、帧内FSYNC、丢帧/idle resync均0。每次cleanup均确认LA空闲并恢复采集前目标配置。末尾若有不完整包仅在确实位于输入末端时单列，本轮表中包问题数据不篡改。初始化未同步、条件未知和采集前调用栈未知继续保留。

## 离线及真实失败路径

- 单元测试12项通过：JSON错误与结果独占预留、不覆盖/失败不发布半成品、租约归属/异常释放、完整配置要求、实际927ms拒绝冒充1s、尾包与错误/gap判据、zip路径/重复成员拒绝、输入指纹、pyOCD实例钩子不影响其他组件等。
- 最终端到端CLI进程检查 **50项** 全通过：三份verify、summary/overview、首中末页/事件、六模式双向导航、window/timeline/issues/source、九种导出、记录包导入、记录/原始KVDAT两种重解及字段对照、覆盖/参数/配置/输入损坏等拒绝。每条命令记录完整参数、返回码、stdout JSON、stderr和耗时；见`E:/superlinkPRO/docs/validation/20261023-230249-trace-cli-h743-1a6f11/workflow-18c481/acceptance.json`。
- 九种输出：simplified C、完整source C、HTML、CSV、摘要JSON、events-jsonl、原始KVDAT、ETM BIN、.etmrecord。支持区间的格式核验10000～10100序号，共101条；原始KVDAT/ETM导出逐字节一致。包带原始输入/ELF/配置/源码/store及绑定输入，不靠巨大events数组中转。
- CLI导出的.etmrecord实际被源Web后端导入，summary/quality/analysis均一致。CLI导入往返、原始KVDAT重解、已有记录重解同样逐字段一致。
- 最终20ms原始波形再由独立Python有界Frontend重跑，TPIU/ETM字节流与C++完整一致、framing字段一致；原始SHA256 `35429a3469f5255fbbba3a37b7640f64b54cd3e2fceaec0951792fb5ffffde14`。
- 真机不匹配时钟：故意要求20MHz而目标30MHz，配置后/采样前返回2，cleanup确认恢复；未自动改PLL。
- 真机不匹配ELF：仅复制文件后修改其Flash映像1字节，不改板上Flash；4次回读仍不符时返回4，cleanup确认恢复，不继续采集。
- 真实历史30MHz“请求1s但只采926.76555ms”记录以CLI verify读取，实际返回5并保留原始数据；不是只对合成计数进行测试。见`verify-real-truncated.json`。
- 最终独立pyOCD读回确认CPU运行、g_trace循环增长、ETM/ETF关闭、TPIU状态2，26组受控寄存器掩码恢复0差异，Flash两段全部与ELF一致。见`final-independent-health.json`。

## 限制

本轮只验收H743和Windows x64。H7B0只复用配置路径、未本轮板测；没有扩称F103等型号已真机CLI验收。离线ETMv3完整配置有原引擎支持，但本轮端到端样本为ETMv4。未验收自动复位/断点起点模式，CLI没有提供该未验证流程；外部调试器的断点不归CLI管理。pyOCD实例钩子固定0.45.1，需要升级时重验。

没有新增1s采集成绩，不宣称30MHz完整1s已通过。没有测试拔插探针、强制杀进程、所有Ctrl+C时序；这类异常必须查看cleanup/notes并处理残留租约，不能假定已恢复。记录目录和导出文件不覆盖；配置/manifest元数据2MiB、成员1GiB、解压总量2GiB预算保留。

依赖、命令和错误处理详见中文方法文档。验证脚本`accept-workflow.py`、`final-independent-health.py`、`final-reference-frontend.py`位于上述证据目录；前者每次生成独立workflow子目录，证据不覆盖。结果计时只说明本机功能运行，不作为独占性能基准。
