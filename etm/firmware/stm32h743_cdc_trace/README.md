# STM32H743 阿波罗板 CDC + 网页 ETMv4

板级输入：HSE 25 MHz，USB_SLAVE 接 PA11/PA12，外部供给 VDD33USB。CH0=PE2/TRACECLK，CH1～4=PE3～PE6/TRACED0～3，CH5 不使用，LA 必须共地。

## 构建与烧录

```powershell
# 在本目录，工具链须提供 arm-none-eabi-gcc，或显式传 -Gcc
pwsh -File ./build.ps1 -CoreMHz 60 -TraceR 8
# 从工程根目录烧录明确镜像
pwsh -File ./tools/h743/h743-flash.ps1 -Elf ./firmware/stm32h743_cdc_trace/build/fw.elf
```

此档 CPU/HCLK=60 MHz，PLL1 VCO=480 MHz，PLL1R=60 MHz，物理 TRACECLK=30 MHz。USB 独立使用 PLL3Q=48 MHz。`-CoreMHz 480 -TraceR 16` 得 CPU480/HCLK240/TRACECLK30，需相应芯片修订版本支持。

产物 `build/fw.{elf,bin,hex}`，默认复制到根目录；`-OutputDir <目录> -NoPublish` 可独立验证其他档位而不覆盖已发布镜像。`-AhbDiv 0` 使用该档默认分频，1/2/4/8/16 为显式覆盖。`-UsbSupply External` 为本板默认；其他板按供电原理图选择，不能盲目打开内部 USB 稳压器。

SDK 不随本例提交。可准备本目录的 `sdk/`，或用 `-SdkRoot <目录>` 指向包含
`Drivers/`、`Core/`、`RTT_TRACE_LOG/` 的兼容 STM32H743 SDK；缺失 H743 头文件时构建会明确报错。

## 持续负载验收

`-BannerEveryMs 20` 让主循环每 20 ms 尝试发送一次 banner；默认 `0` 保留一次 banner。
周期模式会在 CDC IN 端点忙时保留待处理 echo。验收脚本可每 4 ms 发送一包带序号及 CRC 的文本，
同时读取回显；不能只发送而不接收，也不能把探针串口当作目标 CDC。

本例在 RTT 初始化后明确选择 `SEGGER_RTT_MODE_NO_BLOCK_SKIP`：没有 RTT 读取器、日志缓冲区满时，
可丢弃调试日志，CDC 主循环继续运行。这不丢弃 CDC echo 或 ETM 数据。阻塞型 SDK 默认模式曾在
累计约 1.37 万次 echo 后令主循环卡在 `_WriteBlocking`；2026-10-23 已通过将 RTT 缓冲区置满、
695 包 echo 全部回收的真机测试验证修复。

本次接线下，CPU60 / TRACECLK30 MHz 的 200 ms 波形未通过严格 TPIU 帧检查；降为
`-CoreMHz 60 -TraceR 12 -BannerEveryMs 20`（物理 TRACECLK20 MHz）、GPIO 速度 2 后，
完成 200 / 500 / 1000 ms 各三次持续负载采集。该结果仅限定本次硬件与设置，不能替代 30 MHz 验收。
上述历史测试的 1 s 完整处理约 14 s，未达当时 ≤10 s 目标。当前 native.13 起已按用户决定开放
最长 1 s / 5亿样本；允许设置1s不表示每种接线/时钟都能录满，实际深度和质量仍须通过verify。
今后的主线验收统一按 [CDC四档验收规范](../../../docs/etm-cdc-acceptance.md) 执行10/200/500/1000ms。

## 网页操作

1. 启动工具箱 ETM 本地桥，打开“更多功能 → ETM 执行轨迹”。
2. 浏览载入**与烧录镜像一致**的 ELF；需要时选择源码目录。不要把旧版本 ELF 绑定到新固件。
3. 连接探针，搜索并添加 `trace_hook_entry`，选为采集起点。它在时钟初始化之后、CDC 初始化之前。
4. 时钟页填 HSE=25 MHz，核对 CPU60/HCLK60/TRACECLK30；网页只计算和核对，不改 PLL。
5. 500 MSa/s、门限1.65 V、CH0～4、短窗2～10 ms。先关闭分支广播、返回栈和周期计数；这些选项可通过网页配置并回读。
6. 若原始采样有窄脉冲，可在“通道、相位与滤波”选择**解码前软件滤波4 ns**做明确对照；页面 1.0.6 起默认 4 ns；真机 200 ms 验收中 0 ns 会失败、≥2 ns 可行（2/4 ns 解码结果逐字节相同）。它不会改写原始CSV/KVDAT，也不代表模拟信号质量已改善。KingstVIS 的滤波配置快照不能保证 Socket 导出的CSV已经滤波。
7. 联动采集会先校验 Flash、复位运行到断点、暂停本页断点，配置 Trace、武装 LA，再继续 CPU；结束后恢复本页断点。可直接再次采集；是否先复位由调试页选项控制。
8. 查看执行流、源码、调用统计、热点和火焰图；横轴为执行序号，不是函数耗时。保存 `.etmrecord` 可在另一台电脑重解码。

当前完整桥包默认采用原生 KVDAT 前端、OpenCSD 和 TraceStore 分页，正常新路径不经 CSV、JSONL 或 SQLite 中转；旧记录仍有兼容读取路径。记录大小仍影响波形输出缓冲区及派生索引的内存。旧版巨型 decoded.json 要重新解码。不可把字节对账、指令数或源码映射率当作采集无损证明。

## 职责与诊断

固件负责 HSE、供电/VOS、Flash 等待周期、PLL1/PLL3、总线、SysTick 和 USB 外设。网页负责 PE2～PE6 的 Trace 复用、调试时钟门控、ETM/Funnel/ETF/TPIU、断点及采集；不会自动替固件修复 USB 时钟。

`g_trace` 位于0x20003000，魔数0x34524354；`ready=1` 仅表示 USB 初始化函数成功返回，不代表 Windows 已枚举。`g_boot_stage` 地址以本次 ELF 符号表为准。Windows 目标串口 VID/PID=F055:7431；0D28:0204 的COM34是探针，不是目标。

必须保留真实 `_init/_fini`，禁止再用 `PROVIDE(_init=0)`。DWC2 选速须正确处理专用 FS PHY；TRDT 按 HCLK计算。初始化失败保持 ready=0 并报告，不无限等待模式切换。软件复位不会完整重置 CoreSight，每次采集仍需重新配置。

本例默认对普通文本原样回显；`hello world!\n` 应收到相同13字节，不附加错误行。测试“初始化→枚举→回显”的完整过程需要足够长的**同一次**采集；分时窗导出不能冒充全程无缺口记录。

详细审查及本轮验收见 `../../docs/h743-web-review-2026-10-17.md`。
