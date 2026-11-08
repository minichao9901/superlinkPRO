# STM32H743 工作台网页 ETM 原生自动解码验收

2026-11-07（北京时间）。源码基线 `73e4b82` 加工作区的 OpenOCD Wasm 及本次改动。源码工作台两个启动入口调用 Trace CLI 的同一原生引擎设置，默认用二进制 OpenCSD 输出和紧凑 TraceStore，关闭静默回退。原生临时文件放在本次运行目录，避免 ISSUE-029 的系统 TEMP 写入失败。便携工作台显式选择 Python 对照引擎的入口照旧。

沿用[上一轮 H743 真机验收](h743-wasm-hw-20261107.md)的 CDC ELF，SHA-256 `3d3fda8c6202ed63763e10ea0532cf7aa81aa2021a7f2f2d2720a3ef9f1c42a9`。Wasm 烧录回读并软件复位后，读取 CPU/HCLK 60 MHz、物理 TRACECLK 20 MHz。使用 akaLinkPro、Kingst 500 MSa/s、CH0 时钟、CH1～4 数据、阈值 1.65 V、相位 0.25、软件滤波 4 ns，关闭前五通道的硬件滤波。ETM CONFIGR `0x1`，Trace ID `2`。

四档从同一工作台会话分别发出「联动采集」，勾选「保存后自动解码」。总耗时从 CLI 投递点击开始，到页面装载新记录、首批指令可读且联动清理完成为止。随后独立运行 `toolbox trace verify --require-decoded`，校验时间单列在机器证据中。网页直接保存的记录使用 TraceStore 清单绑定输入，复验还逐项核对原始 KVDAT、ELF 和 ETM 配置的指纹。未对这组记录另行解码。

| 请求 / 实录 | 实际样本 | 恢复指令 | 从点击到可用 | 其中服务端自动解码和索引 | 原记录 ID |
| --- | ---: | ---: | ---: | ---: | --- |
| 10 ms | 5,000,000 | 174,978 | 9.58 s | 0.80 s | `capture-262cf98377ab4365a51d8e1cf273fe05` |
| 200 ms | 100,000,000 | 3,493,514 | 14.09 s | 4.27 s | `capture-477f0c8526af4c8c9de9b6538566cfce` |
| 500 ms | 250,000,000 | 8,733,330 | 18.66 s | 6.80 s | `capture-c0c7d432292f432a9b9c252bd1a352e1` |
| 1000 ms | 500,000,000 | 17,464,159 | 30.48 s | 15.81 s | `capture-83af7022ef054155ac5bb9889e381f02` |

四档都生成 `native.bin`、原生 TraceStore 和紧凑查询索引。清单引擎版本 `2ca380ab6cf9`，回退为 `false`，没有旧 SQLite 索引。四份 KVDAT 实际深度均与请求一致，严格质量校验通过，包问题均为 0。整轮 CDC 14,296 发、14,296 收，无序号缺口、CRC 错误、重复或串口错误。四份轨迹均含 `handle_command`、`cdc_write` 和 USB 中断路径。原始 KVDAT、ELF 和解码结果保存在工作台记录目录 `etm/records/`，按表中 ID 查找。

2026-11-08 存储轮换后长期保留表中 200 ms 和 1000 ms 两份完整实录，目录分别为 `etm/records/capture-stm32h743-cdc-200ms` 和 `etm/records/capture-stm32h743-cdc-1s`。10 ms 与 500 ms 保留本报告、机器证据和输入指纹。见 [整理报告](repository-maintenance-20261108.md)。

上次同板的旧自动链路在 200/500 ms 分别约 82.60 / 191.25 s。旧 500 ms 的 CDC 线程曾退出，所以耗时只作流程对照。此次 1 s 的 30.48 s 是采集联动、保存、解码和页面装载的总耗时，15.81 s 是其中服务端自动解码和索引，和历史仅计紧凑离线处理的数字分开。

原 Flash 首个 128 KiB 扇区已恢复，独立读回 SHA-256 与开始前相同，软件复位后原固件 CDC 72 发、72 收。Kingst 配置按备份恢复并校验，ETM 已断开，探针占用锁已释放。统一 CLI、源码工作台挂载及便携引擎选项的 22 项离线测试通过。 [机器验收数据](h743-etm-auto-decode-20261107.json)
