# HPM6800EVK SRAM、RTT Viewer 与 J-Scope 收尾复测

2026-11-07（北京时间）。使用同一台 akaLinkPro 和 HPM6800EVK。基线为 `73e4b82` 加迁移工作区改动，Wasm 核心源自 OpenOCD `9f2d8762b3a75b4d7b2155affcf3d8bb8eb8849a`。工作台真机使用本机 8911 会话。原生 OpenOCD 与探针内部 SBA 的测试用于复现历史 SRAM 计时口径。

| SRAM 测量 | 实测结果 |
| --- | --- |
| 历史 HPM 脚本，本机 SDK OpenOCD，64 KiB `load_image` / `dump_image` | CPU 暂停，ILM 和非缓存 AXI SRAM 各请求 8、20、25 MHz。六组均逐字节一致，OpenOCD 自计时最高写 86.8 KiB/s、读 83.7 KiB/s。计时和数据图案沿用原脚本，测试区移到空闲地址并在结束后恢复、读回。 |
| 历史探针脚本，探针内部 SBA | 非缓存 AXI SRAM，1,024 B × 50。读 1,550,641 B/s、写 1,563,145 B/s，计时采用探针计数器。结束后恢复测试区并读回。 |
| 原 Wasm，16 KiB，运行态 `sysbus` | 8、25 MHz 同板对照。实际 `dump_image` 18.1、18.2 kB/s，`read_memory 32 phys` 16.7、17.8 kB/s。`load_image` 213、217 kB/s。 |
| 批量读 Wasm，同探针旧固件、同条件 | `dump_image` 198.5、231.8 kB/s，`read_memory 32 phys` 151.3、182.4 kB/s。相同数据和区间均读回一致。 |

原 OpenOCD SBA 读循环为每个 32 位字单独提交一批 DMI 扫描，写入路径已有合批。Wasm 构建把连续 32 位 SBA 读合到最多 128 个字的批次，保留尾字处理与忙状态恢复。工作台对 HPM6800 的 ILM 和非缓存 AXI SRAM 大块访问使用 `sysbus` 与 Wasm 文件系统的二进制 `load_image` / `dump_image`，小块寄存器及其它地址沿用原路径。更新后的 16 KiB 二进制读仍需 280 对 USB OUT/IN，应与探针内计时区分。转发到烧录的复测还发现上一会话残留 `0x14` 回复，Wasm 建链现可在已发出的 Info 请求后有界跳过这些回复。 [历史 HPM SRAM 脚本](../../script_test/sram_speed_hpm6800.py) · [探针基准](../../script_test/hpm6800_riscv.py) · [Wasm 构建](../../web/tools/dev/link-openocd-core-wasm.py)

探针实机初始构建时间为 `2026/10/18 21:41:39`，早于尾包协议修复。旧版本停采不提交剩余半包，且缺少补齐最后一次 posted read 的握手。升级到仓库 `ee00aa3` 已验收的 `2026/11/07 14:01:44` 应用，DFU 读回 SHA-256 为 `5d32f353f91dc8b38074179d5aed0dc0cc2c5bd7644567d9c092c5a406aac3ed`。原应用区域已备份并对账。 [探针侧历史验收](hss-spicdc-hw-20261107/README.md)

升级后从浏览器重复 1、3、8 通道 13 档。累计实收 3,527,424 个样本，每档均与 `produced` 相等，无效样本、寄存器读错误和包序缺口均为 0。高档仍出现探针调度跳拍，该计数在采样前发生。ISSUE-043 的 HPM 停采尾差由升级实机固件解决。

最终全驱动配置的 Wasm 二进制路径按历史 64 KiB 块长复测。请求 8、20、25 MHz 时，运行态 `load_image` 为 205.29–206.68 KiB/s，`dump_image` 为 206.44–209.68 KiB/s，结束后读回确认恢复。一次 64 KiB `dump_image` 仍需 1,120 对 USB OUT/IN。浏览器工作台的先行复测含命令调度，暂停态峰值读 143,294 B/s、写 148,029 B/s。 [机器证据](hpm6800-wasm-sba-tail-20261107.json)

RTT flood 持续运行时，原 32 KiB 大块轮询在修订 Wasm 上仍可复现 USB 传输取消。使用相同 Wasm 核心和 RTT 协议代码，主机直接运行 8 KiB 与 16 KiB 分块，分别实收 189,104 与 182,525 B/s，错位读和覆盖计数均为 0。浏览器 Viewer 对 HPM6800EVK 采用 8 KiB 分块，使每轮可以更快提交读游标。 [Viewer 路径](../../web/app/rtt/view.js)

8911 前台确认加载 8 KiB 分块后，首轮边收边渲染仅有 4,541 B/s。另一次在连接前手动暂停渲染，8 MHz 下连续实收 2,678,784 B，测得 137,327 B/s，覆盖和错位读计数为 0。该轮累计字节数超过原始导出缓冲的 2 MiB 容量，导出快照无法核对开头发出的回显。Viewer 现按大块实收和环缓冲积压提前暂停渲染，保留原始接收和文件记录。刷新后以默认显示状态开启 Viewer，首个 8 KiB 数据块即触发暂停；7.18 秒窗口实收速率 155,618 B/s（152.0 KiB/s），覆盖和错位读计数均为 0。通过 Viewer 下发唯一标记，并在同一次接收的原始导出中逐字节核对回显。 [机器证据](hpm6800-wasm-sba-tail-20261107.json)

此前一次跨页复测中，探针 CDC 转发为 1,462,378 B/s，模式对齐后无不匹配。首次切回烧录在 DAP Info 建链阶段超时，未开始写入，随后重试通过。该失败仍保留在 ISSUE-067 的证据中。

刷新后的 `final4` 从 Wasm 烧录 RTT flood 开始，经 Wasm RTT Viewer、探针 CDC 转发、Wasm 烧录 Scope、J-Scope，再切回 Wasm 烧录 dbgstress。三个 Flash 镜像均在第一次尝试烧录成功、读回一致并复位。CDC 接收 5,898,240 B，实测 1,464,561 B/s；从样本模式对齐起无字节不匹配或读取错误。J-Scope 六档累计实收 1,992,536 个样本，逐档停采尾差、USB 丢样、包序缺口和读取错误均为 0。最快实收为单通道约 306 kS/s、八通道约 31.4 kS/s；高负载仍有探针采样前跳拍。

同一流程在 8 MHz 下完成运行态 64 KiB SRAM CLI 读写与原数据恢复。两次测量的最高读、写分别为 143,477 和 151,316 B/s，包含工作台 CLI 调度时间。最后的调试压力测试在一次 OpenOCD Wasm / WebUSB 连接中通过 13/13；源码步进后和连续 40 轮停走清除用户断点后，OpenOCD 均报告空断点列表。八个流程阶段均成功。结束时目标保留 dbgstress，调试和 RTT Viewer 已断开，探针占用锁已释放。临时证据与 DFU 原应用备份保存在 `tmp/hpm-wasm-sba-tail-20261107/`。
