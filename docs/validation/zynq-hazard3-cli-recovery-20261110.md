# Zynq Hazard3：工作台 CLI 失效会话与恢复复核

2026-11-10，关联 ISSUE-103。源码仓库 HEAD `044af4a`；验证对象为本机8878的 Windows 工作台 `1.1.0-web.11`，包来源 `8086f961317148089c81bd4ae26f1d5744698e76`，packageId `f4959eff07c5d62eee1d48256aeed29207f37ac908085c00ec7ff215bdc87bb1`。未修改工作台或探针固件，未发布新包。

目标为 XC7Z020CLG400-2 PL 内的80MHz无PMP Hazard3，`original_mcu.cpu`，akaLinkPro 序列号尾段DAFE对应的同一探针。

## 真机复现与边界

- 用户运行 `python mcu.py release workbench`，停在首次 `memRead(0x10003000,4)`，尚未下载本轮例程。
- `ping`、页面 `summary` 能快速返回，缓存显示 `connected:true`、目标暂停。但只读 timer 请求 job `dc7f86c7c9bdfbe995270162cc058672` 返回 `Polling failed`、`error writing USB data`、`CMD_DAP_JTAG_SEQ failed`、`dtmcs scan failed` 及 `Examination failed`。
- 调试页执行一次 disconnect/connect 后，同一 timer 读取成功，job `946f82bff91df2a52f3187d9d124e5c2`。此恢复步骤没有重放下载、复位或 Flash 写入。
- 这里只能确认“缓存连接状态不能证明当前USB/JTAG链路可用”和重连可恢复；USB错误触发原因仍待复核，不能将重连规避记作底层修复。

## 调用方修正与已验证结果

Hazard3 工程的验收脚本增加实际timer读取预检；仅已明确完成且失败的首次只读请求允许重连一次。请求超时、仍可能执行时不重连、不重放写入。新增只读doctor命令和逐请求耗时/阶段提示。四项离线恢复策略测试通过。

原调用方每个读写请求和ACK均启动一次冻结EXE。当前本机用官方 `toolbox.py` 在Python进程内调用相同浏览器队列，减少进程启动开销；没有复制USB协议或绕开网页。强制EXE方式的一次ping加ACK实测1.189秒；新路径的普通timer读取请求约0.03–0.15秒，二者不是探针原始吞吐量测试。

单例selftest下载、回读校验和运行约6秒通过。PS初始化通过板载Digilent/XSCT执行，读到 `OFFICIAL_PS_INIT_PASS ARM_HALTED=1`。加速后还修正了调用方原先依赖慢请求掩盖的断点等待竞态：等待实际暂停状态后再读取寄存器。

详细脚本及本轮结果保存在本机 `E:/zynq_harzard3_mcu/scripts/`、`build/cli_diagnosis/` 与 `build/workbench_e2e/`；正式版既有bit、ELF和探针固件未更换。

后续分阶段复测14/14例程通过，SHA256逐项对应原发布ELF。QSPI4KiB备份、擦写、恢复及边界校验130.59秒；RTT本轮导出114688字节，1791条连续记录加banner，载荷和序号错误为零。普通读取请求中位58.6毫秒。RTT调用方另补了保存旧缓冲、清空后独立采集，避免历史数据占满2MiB被误当作本轮导出失败。
