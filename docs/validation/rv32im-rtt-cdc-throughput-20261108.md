# RV32IM 探针 RTT 转发吞吐与 CDC 收尾验收

2026-11-08（北京时间）。探针源码基于 `f0bbf64` 加本轮 `usb_composite.c` 修改。目标为现接的 Zynq RV32IM，CPU 100 MHz，`rtt.elf` 的 `_SEGGER_RTT` 位于 `0x80000878`。测试数据是 32 KiB 阻塞模式环缓冲中的连续 64 B 序号记录。探针为 akaLinkPro A.0（序列号末八位 `0AE800E0`），路径为探针原生 JTAG/SBA → USB CDC → Windows COM52。OpenOCD Wasm 负责实验前后的 BRAM 装载和恢复。原生 JTAG 位时序为探针默认值，未实测 TCK 波形。

测速从收到有效记录后开始。停转发后继续读串口，逐条校验序号和固定载荷，并将主机累计实收与探针搬运计数对账。下表为每档约 3 秒的计时窗口，单位 KiB/s。

| 读块大小 | 修复后实收速度 | 收尾对账 |
| ---: | ---: | --- |
| 64 B | 1,141.08 | 一致 |
| 128 B | 1,271.70 | 一致 |
| 256 B | 1,337.46 | 一致 |
| 512 B | 1,382.17 | 一致 |
| 768 B | 1,393.49 | 一致 |
| 1,024 B | 1,396.96 | 一致 |
| 1,536 B | 1,403.27 | 一致 |
| 2,048 B | 1,406.36 | 一致 |
| 2,112 B | 1,406.05 | 一致 |
| 3,072 B | 1,404.05 | 一致 |
| 4,096 B | 1,414.56 | 一致 |

4,096 B 档长测 30.008 秒，实收 43,560,960 B，平均 **1,417.62 KiB/s**。含预热和收尾共实收 43,632,640 B，与探针搬运计数完全一致。681,760 条记录通过序号和载荷校验，探针读错、`RdOff` 写错均为 0。

2,048 B 档长测 20.006 秒，实收 29,020,160 B，平均 **1,416.56 KiB/s**。含预热和收尾的主机与探针计数均为 29,110,272 B，454,848 条记录连续，两档长测吞吐接近。

修复前探针应用构建 `2026/11/07 14:01:44`，512、1,024、2,048、4,096 B 档停转发后均比探针搬运计数少 2,048 B。2,112 B 档可产生非整包尾部，主机与探针计数一致，约 1,404 KiB/s。修复在 CDC IN 最后一次发送填满 USB 包、环已排空并切回 UART 时提交零长度包，结束主机尚未完成的读取。RTT/SPI 持续转发中的暂时空隙不加零长度包。对照结果与 Windows bulk IN 读取由短包结束的规则相符。[Windows USB 传输文档](https://learn.microsoft.com/en-us/windows-hardware/drivers/usbcon/usb-bandwidth-allocation)

探针候选应用构建 `2026/11/08 16:19:57`，长度 194,312 B，SHA-256 `e57cc5a86e09faa153bb83d5ad2c76aaac90dd16ccee3b992c320e80c6667901`。DFU 仅写应用区 `0x80020000`，读回逐字节一致，重启后 HID 构建时间和运行时探针配置核对一致。旧应用备份 SHA-256 `5d32f353f91dc8b38074179d5aed0dc0cc2c5bd7644567d9c092c5a406aac3ed`。主机模型覆盖 STOP 前后最后一个 IN 完成、已是短包、提交失败重试以及避免持续产流时插包，CDC 所有权交接模型也通过。[CDC IN 专项](../../script_test/cdc_in_tail_host_test.py)

测速结束后恢复目标原来的 131,072 B BRAM，逐字节校验一致，SHA-256 为 `fe23bdacdb982222dd3648212e9b0d86a019f2056614822e2c6d991501748e69`。33 项寄存器回读一致，目标保持原暂停状态。测速记录、候选应用及回退备份位于本次 `tmp/rtt-cdc-rv32-20261108/`。

## DMI idle 与本机 OpenOCD 补测

同一块板、探针和修复后的固件。目标的 `DTMCS[14:12]` 是调试器读取的 idle 提示。本机 OpenOCD 和探针均读到 `DTMCS=0x00028071`，其 idle 字段为 0。转发使用的实际等待由探针 HID `0x33` 的 `CONFIG`（action 7）设置，范围 0–255；可执行 `python -X utf8 script_test/hpm6800_riscv.py delay 8`，`status` 可回读。遇到 DMI BUSY，固件每次自动加 2 拍。因此只将设定值与最终回读值一致的样本视为该固定档位的成绩。[RISC-V DTM 规范](https://docs.riscv.org/reference/debug/v1.0/dtm.html)

RTT 使用 4,096 B 块和同一份带序号的饱和产流 ELF。以下各档计时约 4 秒，停止后主机计数均与探针搬运计数相等，序号与载荷检查通过。

| 设定 idle | 停止后回读 | KiB/s |
| ---: | ---: | ---: |
| 0 | 2 | 1,554.80 |
| 1 | 1 | 1,573.20 |
| 2 | 2 | 1,555.35 |
| 4 | 4 | 1,508.19 |
| 6 | 6 | 1,457.71 |
| 8 | 8 | 1,415.22 |
| 12 | 12 | 1,326.12 |
| 16 | 16 | 1,263.15 |
| 24 | 24 | 1,136.44 |
| 32 | 32 | 1,041.96 |

`idle=1` 长测 20.009 秒为 **1,581.30 KiB/s**，校验 507,392 条连续记录。默认 `idle=8` 长测 15.012 秒为 **1,417.22 KiB/s**，校验 341,472 条连续记录。`idle=1` 提高 **11.6%**，两轮没有探针读错、`RdOff` 写错或重扫。测试结束后回读确认 idle 恢复为 8，RTT 已停。再次核对 128 KiB BRAM 和 33 项寄存器，目标保持原暂停状态。

本机 OpenOCD 使用 `0.12.0+dev-gcb0c224`、CMSIS-DAP v2 USB Bulk、RISC-V `sysbus`，目标暂停。在 `0x80008000` 用不同二进制图案做 `load_image` 写入和 `dump_image` 回读。计时为命令往返，逐字节比对放在计时窗外。以下为每档两轮 32 KiB 的平均值。

| OpenOCD 请求 JTAG 时钟 | 读 KiB/s | 写 KiB/s |
| ---: | ---: | ---: |
| 1 MHz | 29.99 | 380.02 |
| 4 MHz | 29.79 | 377.27 |
| 8 MHz | 29.80 | 370.57 |
| 10 MHz | 29.96 | 371.32 |
| 15 MHz | 28.45 | 365.94 |
| 20 MHz | 29.64 | 379.07 |

10 MHz 请求值加测三轮 64 KiB，平均读 **29.65 KiB/s**，写 **397.94 KiB/s**，所有图案回读一致。探针固件的 CMSIS-DAP `JTAG_Sequence` 固定调用汇编时序，请求档位不等于物理 TCK 实测值。RTT 转发则由探针直接跑 DMI/SBA 搬运并经 CDC 输出。[JTAG 固件入口](../../firmware/application_5301/src/dap/JTAG_DP/JTAG_DP.c#L40)

## 本机 OpenOCD SRAM 读写瓶颈

2026-11-08，同板同探针和同版原生 OpenOCD `0.12.0+dev-gcb0c224`。目标保持暂停，从 `0x80008000` 开始做 64 KiB 二进制读写，三轮命令往返耗时中位数为读 **2.2233 s（28.79 KiB/s）**、写 **0.1561 s（409.92 KiB/s）**，写约快 14.2 倍。另用同一地址的 4 KiB 窗口记录 OpenOCD `debug_level 4` 和 libusb 日志。调试日志影响耗时，以下仅用它数包和扫描。

| 4 KiB 操作 | `DAP_JTAG_Sequence` 请求 | WinUSB Bulk OUT / IN | 41 位 DMI DR 扫描 | 请求内 JTAG 时钟总数 |
| --- | ---: | ---: | ---: | ---: |
| `dump_image` | 1,032 | 1,032 / 1,032 | 2,065 | 101,185 |
| `load_image` | 61 | 61 / 61 | 1,059 | 51,891 |

读路径已打开 SBA 自动递增和 `sbreadondata`，但每次读取一个 `SBDATA0` 就执行一个批次。批次末尾的 DMI `NOP` 取回响应，因而每字有两个扫描，并触发一次同步 USB OUT/IN。1,032 对中有 1,031 对只有 50 B 请求和 14 B 响应。写路径的 8 个主要批次每个包含 128 次写扫描和 1 次末尾扫描，由 CMSIS-DAP 拆成较大的请求。读写的 USB 往返数相差 **16.9 倍**，JTAG 时钟数相差 **1.95 倍**。额外的 JTAG 扫描有代价，主要瓶颈是 SBA 读的拆批导致主机逐字等 USB 应答。跟踪窗口内没有传输告警或失败。物理 TCK 未用仪器测量。[OpenOCD 读循环](https://github.com/openocd-org/openocd/blob/cb0c224/src/target/riscv/riscv-013.c#L3494) · [批次尾部 NOP](https://github.com/openocd-org/openocd/blob/cb0c224/src/target/riscv/riscv-013.c#L2600) · [写批次](https://github.com/openocd-org/openocd/blob/cb0c224/src/target/riscv/riscv-013.c#L4780) · [CMSIS-DAP 同步传输](https://github.com/openocd-org/openocd/blob/cb0c224/src/jtag/drivers/cmsis_dap.c#L348)

同地址对照用探针的 `rbench` 以默认 `idle=8` 连读 1,024 B × 1,024，探针计时 675.7 ms，读 **1,515.4 KiB/s**。这项只计探针本地搬运。探针 RTT 转发复用同一 DMI 读流水，下一次读扫描同时取回前一字，然后把数据成块送入 CDC。默认档实收 1,417.22 KiB/s 与探针本地读速率接近。工作台 Wasm 已有 32 位 SBA 读合批实现，其 HPM6800 真机证据单独记录。本轮诊断对象为本机原生 OpenOCD。[探针 DMI 流水](../../firmware/application_5301/src/riscv/riscv_jtag.c#L741) · [RTT 读取路径](../../firmware/application_5301/src/rtt/rtt_bridge.c#L358) · [Wasm 验证](hpm6800-wasm-sba-tail-20261107.md)

测速图案均逐字节回读一致，计时和跟踪阶段分别恢复并校验原字节及 34 项寄存器。探针内部只读对照结束后又复核了原 64 KiB，目标仍暂停在 `0x80000000`，探针空闲且 idle 仍为 8。诊断未修改工作台、探针或 OpenOCD 的代码。4 KiB 跟踪日志 SHA-256 为 `3fe1d5204bf3f08490fd026194a5632ec3c4fc449bda974bbd6a0f4d6680aa3b`。
