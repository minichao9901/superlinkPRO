# H743 移植后的双链路验收（2026-11-15）

SuperLinkPRO 源码工作台，H743 与 akaLinkPro 一对一连接。探针 APP 0.1，编译时间 `2026/11/14 15:00:07`，序列号 `B4444F2110DDDAFE44800BBB0AE800E0`，COM52。Web 复用独立 Chrome 配置中的该设备授权，设备选择器调用为 0。SWD 主测 60 MHz，ARM/JTAG 请求 10 MHz，未外测 ARM/JTAG TCK。ETM 按本轮范围跳过。

这份报告保留调整前的双链路结果。当前 ARM/JTAG 只有原生 CLI 入口，见 [范围调整后的 H743 复测](#armjtag-原生入口复测)。

## 功能与恢复

- SWD DPIDR `0x6ba02477`，JTAG CPU TAP `0x6ba00477`、边界 TAP `0x06450041`。CPUID `0x411fc271`，DEV_ID `0x450`，Flash 2048 KiB。
- 同一原 RTT ELF 的装载内容为 1192 B，SWD 和 ARM/JTAG 均用 Web 按钮烧录、校验、复位。独立回读完整 2 MiB，目标恢复运行且心跳连续推进。Web 总耗时分别约 1.79 s 和 5.27 s。
- 两条链路均执行 20 KiB AXI SRAM 图案写入及独立回读，跨 1 KiB TAR、1/2/4 B 和非对齐访问通过，前后 64 B 哨兵与原始 SRAM 全部恢复。Web 队列单次读/写分别约为 SWD 0.148/0.181 MB/s、ARM/JTAG 0.094/0.083 MB/s。这是功能测量，历史流水线对照待验。
- 两条链路均确认调试建链、停机、单步 PC 变化和继续。SWD 临时断点下发现页面停机状态滞后，已加状态轮询回归，实板命中和源码步进单独跟踪于 ISSUE-156。

## RTT 窗口

同一 RTT ELF，CPU480/HCLK240，AXI SRAM 控制块 `0x24000014`，连续固定载荷 `hello world!\n`。速率按约 8 秒的稳定窗口，十进制 MB/s。原生 Viewer 为 OpenOCD RTT TCP，Web 由工作台正常循环读取，目标环与轮询请求固定。转发用同探针、同 2048 B 搬运块，串口 Web Serial 与原生 CLI 分别读取。

| 用例 | SWD Web／CLI MB/s | ARM/JTAG Web／CLI MB/s |
| --- | ---: | ---: |
| RTT Viewer | 0.702 / 0.066 | 0.163 / 0.071 |
| RTT 转发 | 2.998 / 3.004 | 0.400 / 0.399 |

转发 Web 和原生的原始有效载荷一致，探针搬运与接收尾差、读错和写指针错误均为零。Viewer 的 ARM/JTAG 首轮原始导出带着上一会话的显示历史。清空再测后，512 B 版本逐字节一致，约 0.086 MB/s。恢复 H743 原轮询读量后，一次未对齐，随后清空并预热的复测逐字节一致，约 0.163 MB/s。表中记录的是后一次有效窗口，显示在高流量时自动暂停。固定文本验证了接收字节，连续序号、下行和环尾边界另验。

## J-Scope 60 秒

同一 H743 专用 ELF，单路为 `0x24001028`，八路为从 `0x24001014` 起的八个连续 DWORD，FAST_BATCH 与 CDC_OFF 开启。Web 与原生分别运行，探针 24 MHz 时基独立计算稳定窗组率。原生用 EP83 有序异步读、ZLP 和读退休，同窗口原始 DEF、包序和最终计数对账。

| 链路 | 变量 | 周期 | Web | 原生 CLI |
| --- | ---: | ---: | ---: | ---: |
| SWD | 1 | 10 µs | 100.000 kSa/s | 100.000 kSa/s |
| SWD | 8 | 35 µs | 28.571 k组/s | 28.571 k组/s |
| ARM/JTAG | 1 | 80 µs | 12.500 kSa/s | 12.500 kSa/s |
| ARM/JTAG | 8 | 180 µs | 5.556 k组/s | 5.556 k组/s |

八个有效窗口的 USB 丢样、序号缺口、读错、无效组和 STOP 尾差均为 0。SWD 单路窗口跳拍为 Web 1 组、CLI 3 组，其余为 0。以上是本轮稳定点，采样上限待按相邻周期继续摸高。

| 链路和通道 | Web STOP 后探针／原始实收 | CLI STOP 后探针／原始实收 |
| --- | ---: | ---: |
| SWD 单路 | 6,089,924 / 6,089,924 | 6,081,120 / 6,081,120 |
| SWD 八路 | 1,739,979 / 1,739,979 | 1,737,464 / 1,737,464 |
| ARM/JTAG 单路 | 761,453 / 761,453 | 760,154 / 760,154 |
| ARM/JTAG 八路 | 338,979 / 338,979 | 337,846 / 337,846 |

ARM/JTAG 首次原生长窗因 HID 读失败中断。重新打开同一探针，停止后独立核对终态计数停止变化，原设置已恢复。随后在 Web 新窗口的 JSP 中定位到 DEF 前的旧 DATA，修正 JSP 导出起点。重跑时原生定期读取状态，单、八路均跑满 60 秒。八路 Web 原始 JSP 和 CSV 共 338,979 行，逐值及时间戳比对无差异。首次中断、异常窗口及重跑各保留证据，关联 ISSUE-155、157、158。

结束时原 2 MiB Flash SHA-256 为 `7b6a4f9e7b83de0b9cf81236fe38508b5bcd519b7dc85fd9b811befdb77e666b`，与两份开测备份相同。H743 以 480 MHz 运行，恢复后心跳从 330987 递增至 331170。探针采样和转发均空闲，引脚配置及目标类型回到开测值。

原始证据与独立恢复备份在 `tmp/zynq-acceptance-port/h743-hardware/`。

## ARM/JTAG 原生入口复测

范围调整后使用 `toolbox.py arm-jtag`，同一 H743、探针和 RTT 固件，10 MHz 请求档。原生 HPM SDK OpenOCD `45e78b300` 使用官方双 bank CFG。CPU TAP `0x6ba00477` 和边界 TAP `0x06450041` 均读回。AP2 和 CPU0 检查通过，CPUID `0x411fc271`。暂停、单步和继续均完成，终态为 `running`。本次未烧录 Flash。

RTT TCP 读到 `0x24000014` 控制块，接收 11,264 B。对齐后的 865 条完整 `hello world!\n` 记录逐字节一致。停止后 OpenOCD 退出。这个短窗用于验证新入口和退出，完整速率和 STOP 证据见上文的历史窗口。本次 F103ZE 只做原生 CFG 离线加载。

| 证据 | 本地路径 | SHA-256 |
| --- | --- | --- |
| 建链和单步 | `tmp/arm-jtag-cli-h743-3c824a10/native-commands.log` | `b5ab951326149c7681c07ef82406a01c92887c77164332c306c75e45ae6119c7` |
| RTT 原始上行 | `tmp/arm-jtag-cli-rtt-h743-4e5a46c5/rtt.bin` | `c8d52b8b3d032a46095d810ce9fd353754a0a8ac5ca61b364cc50651dd865ac4` |
| RTT 和 CPUID | `tmp/arm-jtag-cli-rtt-h743-4e5a46c5/native-commands.log` | `7b53082511bb6afcb3d0750ae02e48bbcc19b6eb911da6e61046f40624d6d27d` |
