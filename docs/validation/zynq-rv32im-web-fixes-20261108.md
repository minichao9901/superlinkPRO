# Zynq RV32IM 调试工作台修复验收

2026-11-08。源码基于 `11441e50514f6b83358c0c332fe6f7486159a670`，含本轮和已有本地修改。OpenOCD Wasm `0.12.0+dev-g9f2d876-dirty`，Wasm SHA-256 `5ee31e1e56f89bb9376e120d950de1a769bf3a84a158e9cd0aa6b346d6607b9e`。目标为 Zynq 上的 `original_mcu.cpu`，akaLinkPro 2.1.2，JTAG 1 MHz，TAP `0x00000001`、`misa=0x40001100`，可写 BRAM `0x80000000` 起 128 KiB。

| 验收点 | 结果 |
| --- | --- |
| 通用 CFG RAM 下载 | `debug_demo.elf` 入口 `0x80000000`、`main=0x80000888` 两种停法通过；7,056 字节含 BSS 回读一致。RT-Thread `nano.elf` 的数据段 LMA、VMA 不同；一键入口停机通过，40,728 字节加载数据与 ELF 一致，2,832 字节 BSS 清零。 |
| 软件断点 | 探测为 0 个硬件触发器，数据观察点报告不支持。命中 `b rt_schedule` 的 `0x80003cc8`、`b debug_step_site` 的 `0x800008c0` 和源码行 `hw_debug_demo.c:15` 的 `0x800008ec`。从 `0x800008c0` 继续先单步跨过至 `0x800008c4`，再命中第二断点。清除、断开再连接后均回读到原指令。 |
| 源码步进 | `rc` 到第 14 行 `0x800008cc`；`n` 跳过调用到 `0x800008e0`；`si` 进入 `debug_add` 的 `0x80000820`；`fin` 回到 `0x800008dc`。用 OpenOCD 断点列表确认无残留。 |
| 调试与 RTT | 已运行时重复 `c` 返回「目标已在运行」。复位后装载 RTT 例程，通过 RTT Viewer 使用的协议与 Wasm 内存通路，从 `0x80000878` 读到 32,704 字节，含启动横幅与 510 条连续编号记录，读取未报丢失或损坏。 |
| 恢复 | 原始 131,072 字节 BRAM 逐字节回写核对；恢复 GPR 和 PC，目标保持原先暂停状态。探针模式和其他 HID 配置读回与开始一致，模式为 SWD + JTAG。未烧录 Flash。 |

实板使用仓库的 Node 原生 USB 适配器、相同的 OpenOCD Wasm、调试会话、源码行断点方法和 RTT 协议。测试脚本为 WinUSB 的标准设备描述符读取临时先占用接口 0，随后释放；该适配层兼容问题记录在 ISSUE-106。初次实板跳出还复现了 `AUIPC ra` / `JALR ra,-180(ra)` 被错误判为未知返回地址；中止该轮步出后完整恢复 BRAM，修正立即数与调用识别后实板复验通过。切换 Nano 到 RTT 测试程序时按 CFG 复位并停机，消除前一个程序的运行状态影响；通用 RAM 下载本身不自动复位。

浏览器用 Chrome 156 验证真实页面执行队列：依次完成并回收 205 项；在三项批次的第 2 项注入无效调试命令，确认返回失败、第 3 项标为未提交。启动授权放在 URL 片段，浏览器不随 HTTP 请求发送令牌；页面加载后从地址栏移除令牌。

用户完成浏览器授权后，在同一台板上补测 8927 源码工作台的 Chrome / WebUSB 路径。浏览器显示镜像目录读写已授权；`dump_image` 生成的 131,072 字节文件与 CLI 分块内存回读一致，`load_image images/debug_demo.elf` 的 2,932 字节回读与 ELF 加载段一致。随后刷新到最新源码，重新导入同一 CFG：`debug_demo` 停在入口和 `main`，`nano.elf` 的 43,560 字节加载数据及 BSS 逐块核对通过。命令断点、点击源码行号、继续跨过断点、清除及断开恢复、`rc/n/si/fin` 和 `b rt_schedule` 均在真实页面验证。RTT Viewer 下载 98,112 字节，核对启动横幅及 1,532 条连续编号记录；页面报告无丢失、无损坏。两轮各回写并核对 131,072 字节 BRAM 和 33 项寄存器（含 `mstatus` 和 `pc`），保持原暂停状态，最后释放调试接口。

浏览器仅有 WebUSB 授权时，短暂把探针置为 SWD + VCOM，连接失败并提示「授权 JTAG 探针」。用户完成 Chrome HID 授权后，先回读确认探针处于 SWD + VCOM，再由同一页面连接。页面自动切到 SWD + JTAG 并连上目标，探针配置回读与测试前逐字节一致。再次重连时目标仍暂停在 `0x80000000`，核对后断开探针。

一次在已暂停时重复执行 `halt` 曾返回 `CMD_DAP_JTAG_SEQ failed`。随后只读轮询和两次 `reset halt` 均成功。历史浏览器结果见 [上轮 Zynq 记录](zynq-rv32im-wasm-hw-20261108.md)。

ISSUE-103 补测定位到工作台的 RV32 调试会话继承了无条件发送 OpenOCD `halt` 的实现。OpenOCD 单 hart 路径即使已知核心停住，也会在执行 `halt` 时通过 JTAG 读 hart 状态。CMSIS-DAP 驱动在 USB 交换失败或响应状态非 `DAP_OK` 时都报告同一个 `CMD_DAP_JTAG_SEQ failed`。历史日志未区分两种情况，本轮修复前也未复现该偶发传输错误。依据为 Wasm 所用版本的 [RISC-V halt 实现](https://github.com/openocd-org/openocd/blob/9f2d8762b3a75b4d7b2155affcf3d8bb8eb8849a/src/target/riscv/riscv.c#L2784-L2882) 和 [CMSIS-DAP 队列实现](https://github.com/openocd-org/openocd/blob/9f2d8762b3a75b4d7b2155affcf3d8bb8eb8849a/src/jtag/drivers/cmsis_dap.c#L1648-L1674)。

修复后先查询实时状态。已暂停时更新寄存器，无需再次发送 `halt`。运行时仍发送 `halt` 并回读确认，真正暂停失败仍向页面返回原始错误。8927 的 Chrome 156 / WebUSB、同一块 RV32 板和 1 MHz JTAG 上，修复前 50 次重复暂停发出 50 条 OpenOCD `halt`，修复后 100 次全部成功并发出 0 条。使用备份过的 BRAM 字放置一条自跳转指令，确认目标运行后只发送一次 `halt` 即停在预期地址。随后读回确认 BRAM 字和 PC 已恢复，目标暂停在 `0x80000000`，浏览器已断开。原始命令记录在 `tmp/issue-103-halt-20261108/before-fix.json` 和 `after-fix.json`。20 项离线测试通过，其中故障注入确认真实暂停失败会返回原始错误。Web bundle 同步与指纹检查通过。

Windows 候选为 `1.1.0-web.7-rc.2`，ZIP 103,182,649 字节，SHA-256 `f4270342e88849067575fa4b718390744cbf329486475b20448672d1206be2f5`；ETM 运行时快照来自 `c1a565739231fb0a564ad9aedd35f77ec44dd284`。首轮冻结构建误取快照里的旧队列，授权健康端点为 404；改为显式携带本轮工作台队列源码后复验。最终候选通过 3,410 个包文件及来源指纹核对、HTTP 下载校验、原生解码与 Trace CLI 回归。队列默认开启，未授权请求返回 403，冻结客户端成功提交并回收结果；另用 Chrome 156 打开同一冻结服务页面，`--workbench-cli ping` 与 `page dbg state` 均通过。冻结包浏览器检查未访问硬件。

本地原始证据在 `tmp/zynq-rv32-web-fixes-20261108/` 下的 `native-hw-06/`、`browser-directory-02/`、`browser-full-01/`、`browser-jtag-01.json` 和 `browser-jtag-02.json`。备份与恢复的 BRAM SHA-256 均为 `fe23bdacdb982222dd3648212e9b0d86a019f2056614822e2c6d991501748e69`。原生 USB 和浏览器 RTT 导出 SHA-256 分别为 `47fa856b789fc46ca11893042117b370da3ca4f6604eba68012c9f7dfdc19b29` 和 `2eae7613632fa0ba2b32b2375f9ce7d92560e97c760b79e953a8eae562f5056f`。本轮不替换 8878 已安装包或线上发布。
