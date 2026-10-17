# SPI/I²C 周期任务改用 probe 定时器

真实探针上，现有 `loop` / `every` 的周期部分一次上传到 HID 0x37/BPT1 引擎，
网页只轮询有界结果队列。一次性命令继续使用原 SPI bulk / I²C XFER 接口。
假探针保留浏览器模拟周期。旧固件会明确报“不支持”，不会静默退回主机定时。

最多 8 组，每组 16 条/512 B，周期 1–60000 ms。组内顺序执行，组间不交错。
SPI 周期组支持 XFER/CS/GPIO/PING/AUX_IN/延时，单条完整帧≤128 B、读取≤54 B；
I²C 周期组支持普通读写/延时，写≤51 B、读≤54 B。初始化 STEP/RESET/CFG、扫描、
长读和大块操作仍可在一次性部分执行。暂不将任意脚本编译为 MCU 程序。

延时由 probe 非阻塞处理。迟拍时跳过并计数；32 条结果队列满后停止并报错，
因此页面被系统冻结很久仍会停止采集，不是无限离线记录器。
实时值的实测频率使用 probe 时间戳，避免批量到包造成假频率。
STOP 必须收到确认；失败保留会话占用，可重试停止/断开。

固件协议、执行契约和上板门禁见配套固件仓库 `docs/probe-periodic-bus.md`。
当前仅通过离线验证，尚未确认实板周期抖动或 RTT/J-Scope 性能不退化。

跨仓库 C/JS 协议测试（需要 Python 和主机 GCC）：

```powershell
$env:PROBE_FIRMWARE_REPO='E:\Share\github\akaLinkPro'
$env:PYTHON='python'
node tools/selftest/bus-periodic.test.mjs
```

用配套固件生产 C 调度器及 SPI/I²C 参数校验函数测试真实 HID 组包，
不依赖在线板子。虚拟总线仅验证协议与调度，不模拟真实电气时序。
