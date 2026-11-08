# Windows 工作台 web.7 整合验收

2026-11-08（北京时间）。本次把 Web 审查修复、独立 NRST 复位、RV32 调试和默认工作台 CLI 整合进 `1.1.0-web.7`。源码提交为 `6f33baec982f2c7bf4570411181340a960f9fc7c`。ETM 快照由同一临时工作树的 `etm/` 重新同步并构建，快照清单记录构建前的基线 `11441e5` 和当时的源文件指纹。原生索引器一并重新构建。

| 验收项 | 结论 |
| --- | --- |
| Web 回归 | `make -C web test-offline` 通过，其中 ETM 11/11 组通过。51 项关键 Web 测试和 13 项 Python CLI/打包测试通过。覆盖 SPI NOR 越界拒绝、J-Scope CSV 数值、ARM FPB 断点清理、探针交接、NRST 和 RV32 暂停。Web bundle、ETM 页面副本和 CLI 文档生成检查均通过。 |
| 冻结 EXE | 3,410 项文件指纹、启动、HTTP、NRST 页面资源、原生示例重解、Trace CLI 查询和火焰图导出通过。验收 EXE 的 SHA-256 为 `5510d05f93426d068401c014a0a6d8166492ab6701aa772f2ad88beba39c893b`。 |
| 默认 CLI 队列 | EXE 默认启动队列，未携带令牌返回 403。Chrome 156 加载包内 bundle，`workbench-cli.cmd` 连续完成并回收 205 条请求。另一个三项批次在第 2 项失败时标明第 3 项未提交。 |
| RV32 下载和调试 | 冻结 EXE、Chrome 156 / WebUSB、akaLinkPro、1 MHz JTAG。`original_mcu.cpu` 的 TAP ID 为 `0x00000001`。`debug_demo.elf` 的 7,056 字节加载段与 BSS 回读一致。Nano 的 43,560 字节回读一致并命中 `rt_schedule` (`0x80003cc8`)。源码行断点、跨过断点、删除及断开恢复、`rc/n/si/fin` 通过。 |
| RV32 暂停和 RTT | 已暂停时重复 `halt` 100 次全部成功，观察到 100 次状态查询、0 条 OpenOCD `halt`。运行时重复 `c` 正确提示目标已运行。RTT Viewer 实际导出 65,408 字节，含 1,021 条连续记录，页面丢失与损坏计数均为 0。导出 SHA-256 为 `d58437b6cf267a49ebf66570ac7e83384ec31c90f62695848c69d45c94c1155c`。 |
| 目标恢复 | 原 131,072 字节 BRAM 逐字节恢复，SHA-256 为 `fe23bdacdb982222dd3648212e9b0d86a019f2056614822e2c6d991501748e69`。33 项寄存器回读一致，目标保持测试前的暂停状态，探针已断开。 |

首次扩大离线回归误用了 MinGW Python，缺少 Werkzeug。切回带项目依赖的 Windows Python 后整套通过。真机首轮和原会话复试均在备份前的只读 `poll` 报 `CMD_DAP_JTAG_SEQ failed`。正常断开并重连后，整轮验收通过。重复暂停已避免发送多余的 OpenOCD `halt`，读链路的偶发错误仍在 ISSUE-103 跟踪。

构建和验收均在本次 `/tmp` 专用目录。通过后对账候选包输入与已提交的 Git 文件，将正式源码提交写入包清单。ZIP 中的 EXE、运行时和网页文件逐项核对为验收时的相同字节。

正式 ZIP 为 103,184,777 字节，SHA-256 为 `2c3207450d37d76c0d3f5fd452cdfad565f47e7a5ab112a83b3c3a7e5a183a56`。包内文件身份为 `e1545be7ef9094627e2a8594dc981b7544814a2b4297f0959b3f2a0214df5570`。
