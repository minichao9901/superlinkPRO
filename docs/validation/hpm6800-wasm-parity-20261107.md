# HPM6800EVK 调试后端切换验收

2026-11-07（北京时间）。源码基线 `ee00aa3cce6ba02cdba4e12a360eba2dc0d34b3c` 加工作区改动。真机为 HPM6800EVK 和 akaLinkPro，使用 Chrome 156 的已授权本地调试页，HPM SDK 1.11.0 原版 `hpm6800_all_in_one.cfg`、8 MHz JTAG。沿用已烧录并回读的 `dbgstress/fw.elf`，镜像 SHA-256 为 `f76305dbb023612d114585fb6152a95949a5768988014084a051341a238124da`。

切换入口前，Wasm 在一次连接中通过全部 13 组。19 步指令单步、19 步源码跳过、源码进入和五次逐层跳出后，直接进入 40 轮停走。每轮均命中 `0x80005b1e`。压力前、每 10 轮和清空后都读回 OpenOCD 的断点列表并与页面对账。重连次数、靶子异常计数和页面错误均为零。常规复位并停、复位并运行分别完成两轮断点重命中。XIP 常量从当前 ELF 的完整只读段取值，页面标明数据来源。结束后断开探针并释放协调锁。 [机器证据](hpm6800-wasm-parity-20261107.json)

验收后移除 HPM6800EVK 在内置 JTAG 板型列表里的入口。旧 HPM6800EVK 设置和从其他工具沿用的 HPM6800EVK 目标进入 Wasm。其它 RISC-V 板型继续有 JTAG 入口。浏览器检查了旧设置迁移、复位按钮及烧录和 RTT 板型列表。 [断点、复位和 XIP 实现](../../web/app/dbg/openocd-session.js) · [入口迁移](../../web/app/dbg/view.js)

加载测试使用独立 Chrome 156。GitHub Pages 现站冷加载三次，页面就绪中位数为 559 ms。现站的候选 Wasm 路径返回 404，因此候选增量用本机同源 A/B 测量，未部署候选。基线是 `ee00aa3` 的 HTML 和合并包，两组资源使用同一静态服务。限制为 8 Mbps 和 150 ms 延迟，每组冷加载 `#dbg` 五次，交替顺序。从导航到启动遮罩消失，切换前的中位数为 1,087.1 与 1,131.9 ms，最终版本为 1,114.2 与 1,111.4 ms。所有首屏样本的 Wasm Worker 与 `.wasm` 请求均为零。两轮中位数差处于几十毫秒的量级。

首次启用 Wasm 时，另测 Worker 创建到引擎初始化完成。静态服务器对模块和二进制按同样的 8 Mbps、150 ms 条件发送，每种三次。gzip 的 Wasm 响应为 1,802,655 字节，中位数 2.19 秒。未压缩的响应为 5,615,519 字节，中位数 5.98 秒。这段时间发生在用户连接探针时，测量未计入 USB 授权和目标连接。上线后的响应压缩、网络和缓存会决定实际耗时。 [现站](https://minichao9901.github.io/superlinkPRO/)

随后已上线并完成 Pages 冷首屏、gzip 和 HPM6800EVK 连接复测。 [上线后实测](hpm6800-wasm-pages-20261107.md)

复现使用已烧录 `dbgstress` 固件的板卡、当前本地授权执行页及 SDK 配置目录。 [压测脚本](../../web/tools/selftest/dbg-hpm6800-dual-stress.py)

```powershell
python -X utf8 web/tools/selftest/dbg-hpm6800-dual-stress.py <运行时目录> <SDK的boards/openocd目录> <新结果JSON> --backend openocd
```
