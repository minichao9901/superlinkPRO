# HPM6800EVK 的 Wasm RTT 与 J-Scope 传输验证

2026-11-07（北京时间）。源码基线 `73e4b82`。Wasm 原型走线上 `f2fa8ca` 的 OpenOCD Worker、akaLinkPro WebUSB、HPM6800EVK 和原版 HPM SDK 1.11.0 CFG，JTAG 时钟 8 MHz。原生 J-Scope 使用本地工作台 8911。板上沿用 `hpm6800evk_dbgstress/fw.elf`，两条路径串行使用探针，测试未擦写 Flash、未复位目标。 [机器证据](hpm6800-wasm-streams-20261107.json)

当前调试器的 Wasm 配置仅核验 HPM6800EVK 的 SDK 文件。工作台有十个 HPM 板卡预设，其他九个还需要各自的目标配置及 RAM、Flash、复位和调试真机验收。 [板卡预设](../../web/app/targets/hpm/porting.js) · [Wasm 配置入口](../../web/app/core/openocd-hpm-profile.js)

目标运行时，现有 Wasm 调试会话以默认模式读内存会报 `Failed to read priv register`。原型将 RISC-V 内存访问切到 `sysbus`，发出 `phys` 的 32 位读写，并恢复原来的模式。连续读取 `0x01240000` 起五个字，160 次耗时 495.3 ms，约 323 批/秒，间隔中位数 2.9 ms、P95 4.1 ms。应用计数增加，目标保持运行。 [OpenOCD RISC-V 内存访问](https://openocd.org/doc/html/Architecture-and-Core-Commands.html)

RTT 原型让现有 `Rtt` 协议类走同一个 Wasm 内存会话。在已运行的目标上，备份 `0x00018000` 起 2,304 字节 ILM，构造一个上行和一个下行通道。768 字节上行绕回读取耗时 64.2 ms，23 字节下行绕回写入，内容和两个游标逐项对账通过。测试后原区间逐字节恢复。该结果验证运行态真实目标 RAM 与工作台协议类的互通，缓冲区由验收脚本预置，持续产流和吞吐仍需用 RTT flood 固件测量。RTT 转发页目前由探针固件搬运数据，通过 CDC 发给浏览器。 [RTT 协议](../../web/app/rtt/protocol.js) · [CDC 转发](../../web/app/hid/stream.js)

J-Scope 原生对照读取同一块板的 `g_ticks`、`g_loops`、`g_stage` 和 `g_checksum`，由探针固件定时读、WebUSB `0x83` 收包。

| 采样周期 | 实测速率 | 主机收到 | 探针生成 | 停采计数差 |
|---|---:|---:|---:|---:|
| 100 µs | 10,000 Hz | 22,940 | 22,958 | −18 |
| 100 µs 复跑 | 10,000 Hz | 16,089 | 16,119 | −30 |
| 1,000 µs | 1,000 Hz | 2,170 | 2,172 | −2 |

三轮的中间包缺口、跳拍和读取错误计数均为零，停止时的样本数仍未对齐，已回填 ISSUE-043，原因待复核。以本次实测，RTT Viewer 可以继续验证 Wasm 内存后端，J-Scope 的低频轮询和微秒级探针采样适合分别验收。上层可以共用目标选择和探针占用管理。Wasm 与 J-Scope 的实际测试均已断开并释放硬件协调锁。 [J-Scope 探针传输](../../web/app/scope/transport.js)
