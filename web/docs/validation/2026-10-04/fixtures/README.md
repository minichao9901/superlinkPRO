# 实机补测夹具快照

此目录保留 2026-10-04 后续架构/外设补测的最终脚本和原生传输适配器，不是浏览器原生 API 测试。证据及限制见 [验收报告](../../2026-10-04-f103cb.md)。文件摘要保存在上一级 `followup-sha256.json`。

脚本原位置为网页仓库根目录下的 `tmp/patch-verification/`，相对导入仍按该位置保存。需要复现时将所需快照复制回该目录，从仓库根目录运行；Python 路径与 akaLinkPro `script_test` 路径是本机固定路径，需要按执行环境修改。依赖 hidapi、PyUSB、libusb 以及在线的本轮 probe 固件、F103CB RTT seq 例程、AT24C02（0x50）和 W25Q64（JEDEC ef4017）。W25Q64 原有地址 0 前 256 字节为 00..ff 图案；脚本不会创建这个图案，也不发送外设存储写入/擦除命令。

- `peripheral-handoff-real.mjs`：生产 DebugSession、I2cSession、SpiSession 和统一管理器的共存、交接、忙保护及失能回复故障恢复。正常结束退出码为 0。
- `engine-handoff-real.mjs`：生产 Scope/RTT 转发核心与 DebugSession 的引擎交接，保持 I2C 会话；UI 渲染使用替身。正常结束退出码为 0。
- `nor-read-diag.mjs`：单线不同分片、SPI 时钟、DAP 共存的读回诊断。输出 bad/iterations；退出码 0 只说明诊断完成，仍须检查 bad 字段。
- `nor-modes-read-real.mjs`：接线尚未明确时的模式诊断快照，含四线命令。当前 IO2/IO3 未接，不适合作为本接线的验收入口；保留它用于解释历史日志。
- `native-adapter.mjs` / `native-rpc.py`：生产 WebHID/WebUSB 接口形状到本机 HID/libusb 的适配；同端点 OUT 在 JS 侧排队，避免 Python 工作线程改变同步写入提交顺序。它没有验证浏览器授权、原生并发提交、页面节流或 Web Locks。
