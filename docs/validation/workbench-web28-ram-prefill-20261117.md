# SuperLinkPRO web.28 RAM 范围预填验收

2026-11-17。功能提交 `0c70569`，Windows 打包源码 `17e12bf`。

在调试器「镜像文件」选择 ELF 后，连接了可声明 RAM 的自定义目标会显示建议的起址和字节数。Cortex-M0+ 使用向量表和初始 MSP。通用 RV32 覆盖 `PT_LOAD` 的加载地址、运行地址及 BSS。可修改两框。点击「声明 RAM」后，工作台才将范围用于当前连接。 [操作说明](../../web/docs/dbg-page.md#下载到-ram-并调试)

- 在 8880 源码工作台和 Windows 冻结候选中导入实际 `rtthread_nano.elf`，浏览器模拟 Cortex-M0+ 会话预填 `0x20000000` 和 `65536`。模型显示导入后范围待声明，普通界面刷新保留手动修改的长度。两处各验源码模块和合并包。
- 调试器与 Wasm 定向软件测试 35 项通过，含独立 LMA、BSS、无效范围与回读保护。工作台 CLI 13 项通过。冻结 Windows 候选验证文件清单、CLI、HTTP、离线 Trace 解码和导出。
- 本轮范围是软件及分发验证。未重新上板运行 S、F、D、V、R、J、E 组，Zynq 上的硬件 RAM 下载和启动证据见前次记录。 [Zynq 实板记录](zynq-cm0p-ram-debug-20261117.md)

本机固定安装保持 `data/` 原位，104 个可读文件逐一核对 SHA-256。另有 3 个临时文件仍被占用，核对其原路径、文件标识、大小和修改时间。固定 EXE SHA-256 为 `7ff9270b27772a954d525bc74d03a172c56114bd76ffef6c7070304f57c320e9`。

Windows ZIP 大小 104,129,132 字节，SHA-256 为 `6dd2b7797d3ba136c6d8efa37c5ce30d6fc9231115c797f9e71d54717dca8828`。本机验收证据位于 `tmp/workbench-web28-release-20261117/`。
