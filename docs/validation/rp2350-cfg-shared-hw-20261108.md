# RP2350 原版 OpenOCD CFG 三页共享验收

源码基线 `73e4b82` 加工作区改动，OpenOCD Wasm 核心 `9f2d876`。使用 8911 工作台 CLI、akaLinkPro 和 RP2350 的 Cortex-M33 0 号核。加载 `9f2d876` 的原版 `target/rp2350.cfg`，不修改文件。Wasm 在 USB 关闭时加载配置，返回 `rp2350.cm0` 和 `rp2350.cm1`、SWD 1 MHz、Flash bank `0x10000000`。从调试页选择 0 号核，烧录与 RTT Viewer 点击各自的「沿用目标」，验证三页使用同一份 CFG。

原版脚本默认启用 SMP 组。初轮烧录和回读通过，随后调试继续时另一个核心状态引起 `resume of a SMP target failed`。现在工作台通过选中的 Cortex-M 核操作，连接后关闭 SMP 组联动，原脚本内容和 Flash 配置仍生效。脚本目录依赖、原版多核心的离线识别、USB 不开放和同址虚拟 bank 优先级有回归覆盖。

测试镜像基于 Pico SDK 2.2.0，链接地址 `0x10000000`，提供 SEGGER RTT 回显、32 KiB 上行环、饱和固定文本模式和专门预留的 64 KiB SRAM 测试区。烧录前对 RP2350 身份和原 Flash 首 16 KiB 做了两次读回对账。验收过程中只写这四个 Flash 扇区和测试固件预留的 SRAM。[夹具源码与构建方法](../../script_test/rp2350_workbench/README.md)

Flash 页由原版 CFG 烧录 14,748 B、四扇区，逐字节读回通过。软件复位后运行心跳增长。调试在同一会话通过源码跳过、进入、跳出、八步指令单步、寄存器、SRAM 读写及 40 轮停走；最后读回 FPB，无启用的比较器。RTT Viewer 使用与 Flash 相同的 CFG 内容标识和所选核，1 MHz 实收 435 条上行心跳，下行唯一标记逐字节回显，覆盖和错位读均为 0。

SRAM 使用页面的 Wasm `memRead`/`memWrite`，计时含工作台队列和字节序列化。暂停所选核，在 8/32/64 KiB 三种块长上写入确定性字节图案并逐字节读回。成功窗口的实测峰值如下，速率使用十进制 kB/s。

| 块长 | 最高读速率（请求档） | 最高写速率（请求档） |
| --- | ---: | ---: |
| 8 KiB | 73.0 kB/s（25 MHz） | 223.8 kB/s（20 MHz） |
| 32 KiB | 278.2 kB/s（10 MHz） | 287.9 kB/s（10 MHz，重测） |
| 64 KiB | 272.1 kB/s（40 MHz） | 455.5 kB/s（40 MHz） |

30、45 MHz 在首轮健康检查退回 1 MHz；40 MHz 首轮完成两个 64 KiB 窗口，重测健康检查退档。50 MHz 的 64 KiB 重测达到 269.4/448.3 kB/s。60 MHz 失去会话，未记录为可用速率。复位重新初始化测试固件后，复测预先备份 64 KiB 并在安全档恢复、独立读回一致。这些数字是本机和当前探针的工作台路径实测，不是裸 SWD 线速。

探针固件 RTT 转发单独验收。Wasm 先确认目标 `_SEGGER_RTT` 位于 `0x2000077c`，32 KiB 上行环已占用 32,755 B，环尾含饱和负载的固定文本。释放 Wasm 后，探针 CDC 转发使用相同 ELF 地址，遍历 20/30/36/45/60 MHz 请求档。复测每档等待约 4 秒，均未读到控制块，探针分别报告 75/75/74/75/77 次读取错误，主机实收 0 B。60 MHz 请求最终显示降为 45 MHz。探针的 `swd_host` 使用旧 MEM-AP 寄存器和 APSEL 寻址，与 RP2350 的 ADIv6 MEM-AP 不匹配；没有可用的转发速度可报告。 [探针寻址](../../firmware/application_5301/src/swd_host/swd_host.c)

验收结束用原版 CFG 恢复 Flash 首 16 KiB，软件复位后独立读回与接入时的 SHA-256 一致。调试、Viewer 和探针转发均已释放。 [机器验收数据](rp2350-cfg-shared-hw-20261108.json)
