# H743 CLI 便携候选包验收

版本：1.0.14-native.14-rc.1；Run-ID：20261024-0803-h743-cli-release。
源码提交：7ac104879311a94b4ee4a791175e0b0b50a5b292；源码 sourceDirty=false。
Tested-Revision：7ac104879311a94b4ee4a791175e0b0b50a5b292；Tested-Dirty：true（仅更新来源清单至已提交源码，未改受测程序）。
Validation-Result：partial；本报告中的离线/便携项 passed，Hardware-Test：not-run。

## 完整包

- ZIP：117729048 bytes。
- SHA256：`7013b00bd4f37f1260c49abff9a200a6cd19af467a951cc70b59bcf7bd3fa240`。
- Package-ID：`7eeec0657b5c1df93afbf17c8b87be3a95eef91ec9bcd4af762df3fa391969af`。
- 文件：3249 项逐个大小/SHA256 通过，另重算 packageId 与下载清单一致。
- 构建：web/tools/dev/build-etm-package.py；实际输出 C:/Users/Administrator/.codex/validation/20261024-0803-h743-cli-release/package-final。
- 版本由 GitHub Release 候选页单独分发，正式 web/bridge/etm/downloads 仍保留原 native.13 ZIP 和清单；候选包不放入 Git 的大文件历史。

## 本轮实际执行

1. validate-package.py：解压至中文空格目录，PATH 仅 System32/Windows，没有系统 Python/Node/npm/Git；EXE doctor、CMD doctor、CLI help、桥 --check 通过。Node/USB/DOM 和网页调试/Flash/ETM 模块成功载入，未打开硬件。
2. 冻结 CLI 的 65 条真实子进程端到端全部符合预期。十种导出、五种区间导出、七种导航、包导入/重解、后配源码及不覆盖已有文件；stdout JSON 与 --json-out 完全一致。KVDAT/记录两种重解后的 summary/quality 与既有原记录一致。源码 CLI 同样 65 项通过。
3. validate-long-record.py：导入既有物理 H743 1s 记录，确认实际采集1s/5亿采样点；用本包原生链路完整重解。18356923 条指令，处理 14.904s，子进程墙钟17.462s（含启动/校验，非独占性能基准）。summary、quality、analysis 与导入的旧解码结果完全一致；首/中/尾150行分页及101条CSV区间导出成功。保留原记录的未知执行状态，不把质量通过说成所有指令均已证实执行。
4. 真实1s输入包 SHA256：`03d2cbbf455868ce94ecec713834fc4cc5acf93ec7fd9bde4979c9a884796523`。路径见 long-record-validation.json。
5. 源码回归与启动失败修复的证据见同目录 source-report.md；29项Python CLI、5项队列、6项快照同步回归及网页既有回归通过。

## 仍未完成的验收

本轮 web start 命令在 exec_command 创建进程之前遭策略拒绝，只给出 blocked by policy。没有执行替代启动来绕过限制。不是应用运行失败，也没有证据表明是 auto_review 审批；当时实际配置 never/user。
最终版的运行时启动/退出、H743 新采集200/500/1000ms、调试/烧录硬件回归、异常拔插和匹配DWARF的源码调试仍未完成。以前 6043900 中早期 worker 的真实断点/短窗/原镜像烧录与整片回读证据保留，但不替代本包的新板测。
本轮不验收 F103ZE/H7B0。候选版应按上述边界试用，不能称全部硬件验收完成。

## 证据与发布

脚本/结果持久目录：E:/superlinkPRO/docs/validation/20261024-0803-h743-cli-release。
运行包/原始数据目录：C:/Users/Administrator/.codex/validation/20261024-0803-h743-cli-release。
portable-validation.json、portable-acceptance-pointer.json、long-record-validation.json 为本轮实际结果。
发布后另外保存 release-receipt.json、public-download-verification.json；本报告不提前宣称远端发布成功。
