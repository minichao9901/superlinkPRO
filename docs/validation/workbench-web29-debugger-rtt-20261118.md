# SuperLinkPRO web.29 调试器 RTT 终端验收

2026-11-18。功能提交 `090c472`，Windows 打包源码 `d2d290f`，分发提交 `1b6311f`，标签 `workbench-v1.1.0-web.29`。关联 ISSUE-172。

调试器右侧 RTT 支持点击输入、退格、回车和粘贴，使用目标的下行通道 0。上行由独立轮询驱动。勾选「连接后自动开始」时，可以先连接目标，待 ELF 符号可用、固件初始化控制块后自动接入。只有上行的固件继续显示日志。 [使用说明](../../web/docs/dbg-page.md#rtt-交互终端)

## 验证

- 源码、Windows 冻结候选和本机固定安装均测试未合并模块及合并包。Pages 测试公开合并包。浏览器回归覆盖控制块晚初始化、自动刷新、键盘字节、粘贴、跨环尾、满环提示、清屏、停止取消待发数据、重新定位以及断开禁用输入。 [回归入口](../../web/tools/selftest/dbg-rtt-terminal.test.mjs)
- 调试器整页浏览器回归 156 项通过，CLI 13 项通过。RTT 协议、调试互斥和 OpenOCD 会话定向测试通过。冻结 Windows 候选核对 3,570 个文件、CLI、HTTP 和离线 Trace 解码及导出。
- 首次探测正常枚举了 TAP，但 Hart 0 无法停机。用户重新烧录后，固定 EXE 在 Chrome / WebUSB 接入 akaLinkPro CMSIS-DAP 2.1.2 和 Zynq Hazard3，JTAG 请求时钟 1 MHz，TAP ID `0x00000001`。先连接运行中的目标，再加载 `rtthread_nano.elf`（SHA-256 `1510d56bfbddcabde0d9651ca7d5928dd053c5f937b80f5ed3f39e83be9bfe7a`），页面自动接入 `0x800177a4`。整个真机验收无需点「定位并开始」。
- 从真实页面终端投递键盘事件，目标实际收到 `help\r` 和 `version\r`，下行通道写入及读取游标均从 0 经 5 到 13。回读上行缓冲核对 `RT-Thread shell commands`、`4.1.1` 和 `msh >`。执行 `version` 后输出增加 141 B，读写游标均由 1,106 变为 1,247。真机依据为目标 SRAM 回读，界面绘制通过浏览器模拟目标验证。收尾释放探针，板上程序继续运行。
- 本轮 D 组核对建链和运行状态，V 组定向核对调试器终端的 ELF 定位及上下行。S 组未执行 SRAM 专项，F、R、J、E 组未执行，各组性能项目未测。

## 分发

- 固定 EXE 从 `dist/superlinkPRO` 启动并报告 web.29，SHA-256 为 `2564fd03302048985009720d61e2b8b4baabc0e93dd90615fd01e8f484d83257`。安装时保留 `data/` 原目录，核对其中 754 个文件。4 个被占用的文件按文件标识、长度和修改时间核对，其余文件按 SHA-256 核对。
- Windows ZIP 为 104,134,935 B，SHA-256 为 `3c437406e2a3244a2e786d588f8ec33a3a8f31a92d05de84f5e65f16d950a79e`。Pages 的清单、缓存版本和 bundle 指纹一致。Pages 和 GitHub Release 分别匿名完整下载，大小和 SHA-256 均与本机清单相同。 [线上工作台](https://minichao9901.github.io/superlinkPRO/#dbg) · [Pages 工作流](https://github.com/minichao9901/superlinkPRO/actions/runs/44221129126) · [GitHub Release](https://github.com/minichao9901/superlinkPRO/releases/tag/workbench-v1.1.0-web.29)

完整本机证据保存在 `tmp/workbench-web29-release-20261118/`。
