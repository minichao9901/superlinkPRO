# SuperLinkPRO web.28 RAM 范围预填验收

2026-11-17。功能提交 `0c70569`，Windows 打包源码 `17e12bf`。

在调试器「镜像文件」选择 ELF 后，连接了可声明 RAM 的自定义目标会显示建议的起址和字节数。Cortex-M0+ 使用向量表和初始 MSP。通用 RV32 覆盖 `PT_LOAD` 的加载地址、运行地址及 BSS。可修改两框。点击「声明 RAM」后，工作台才将范围用于当前连接。 [操作说明](../../web/docs/dbg-page.md#下载到-ram-并调试)

- 在 8880 源码工作台和 Windows 冻结候选中导入实际 `rtthread_nano.elf`，浏览器模拟 Cortex-M0+ 会话预填 `0x20000000` 和 `65536`。模型显示导入后范围待声明，普通界面刷新保留手动修改的长度。两处各验源码模块和合并包。
- 调试器与 Wasm 定向软件测试 35 项通过，含独立 LMA、BSS、无效范围与回读保护。工作台 CLI 13 项通过。冻结 Windows 候选验证文件清单、CLI、HTTP、离线 Trace 解码和导出。
- 本轮范围是软件及分发验证。未重新上板运行 S、F、D、V、R、J、E 组，Zynq 上的硬件 RAM 下载和启动证据见前次记录。 [Zynq 实板记录](zynq-cm0p-ram-debug-20261117.md)

本机固定安装保持 `data/` 原位，104 个可读文件逐一核对 SHA-256。另有 3 个临时文件仍被占用，核对其原路径、文件标识、大小和修改时间。固定 EXE SHA-256 为 `7ff9270b27772a954d525bc74d03a172c56114bd76ffef6c7070304f57c320e9`。

固定 EXE 从实际目录启动后，8878 报告 web.28，浏览器执行队列已就绪。该服务的源码模块和合并包均通过 Nano ELF 预填及手动值保留测试。

Windows ZIP 大小 104,129,132 字节，SHA-256 为 `6dd2b7797d3ba136c6d8efa37c5ce30d6fc9231115c797f9e71d54717dca8828`。本机验收证据位于 `tmp/workbench-web28-release-20261117/`。

分发提交 `72a6abbc7f5d4c14467585cbc6a98975589bbb7d`，标签 `workbench-v1.1.0-web.28`。Pages 合并包复验实际 Nano ELF 预填和手动值保留。Pages ZIP 通过 Edge 匿名完整下载并重新计算 SHA-256，与本机清单一致。 [Pages 工作流](https://github.com/minichao9901/superlinkPRO/actions/runs/44053692746) · [公开工作台](https://minichao9901.github.io/superlinkPRO/#dbg)

Release ZIP 上传初次遇到 TLS 握手失败或超时，GitHub 返回尚未完成的草稿资产。核对草稿后用流式 HTTPS 重新上传同一个 ZIP，GitHub 资产大小及 SHA-256 与本机清单一致，Release 已公开。 [GitHub Release](https://github.com/minichao9901/superlinkPRO/releases/tag/workbench-v1.1.0-web.28)

从 Release 匿名完整下载 ZIP，重新计算大小和 SHA-256。Release、Pages 和本机 ZIP 均为 104,129,132 字节，三者 SHA-256 一致。
