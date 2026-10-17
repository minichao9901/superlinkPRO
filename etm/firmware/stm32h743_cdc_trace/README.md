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

## 网页操作

1. 启动工具箱 ETM 本地桥，打开“更多功能 → ETM 执行轨迹”。
2. 浏览载入**与烧录镜像一致**的 ELF；需要时选择源码目录。不要把旧版本 ELF 绑定到新固件。
3. 连接探针，搜索并添加 `trace_hook_entry`，选为采集起点。它在时钟初始化之后、CDC 初始化之前。
4. 时钟页填 HSE=25 MHz，核对 CPU60/HCLK60/TRACECLK30；网页只计算和核对，不改 PLL。
5. 500 MSa/s、门限1.65 V、CH0～4、短窗2～10 ms。先关闭分支广播、返回栈和周期计数；这些选项可通过网页配置并回读。
6. 若原始采样有窄脉冲，可在“通道、相位与滤波”选择**解码前软件滤波4 ns**做明确对照；默认0关闭。它不会改写原始CSV/KVDAT，也不代表模拟信号质量已改善。KingstVIS 的滤波配置快照不能保证 Socket 导出的CSV已经滤波。
7. 联动采集会先校验 Flash、复位运行到断点、暂停本页断点，配置 Trace、武装 LA，再继续 CPU；结束后恢复本页断点。可直接再次采集；是否先复位由调试页选项控制。
8. 查看执行流、源码、调用统计、热点和火焰图；横轴为执行序号，不是函数耗时。保存 `.etmrecord` 可在另一台电脑重解码。

长记录采用 JSONL 和 SQLite 分页；原始 CSV 取样仍需要与采集大小相关的内存。旧版巨型 decoded.json 要重新解码。不可把字节对账、指令数或源码映射率当作采集无损证明。

## 职责与诊断

固件负责 HSE、供电/VOS、Flash 等待周期、PLL1/PLL3、总线、SysTick 和 USB 外设。网页负责 PE2～PE6 的 Trace 复用、调试时钟门控、ETM/Funnel/ETF/TPIU、断点及采集；不会自动替固件修复 USB 时钟。

`g_trace` 位于0x20003000，魔数0x34524354；`ready=1` 仅表示 USB 初始化函数成功返回，不代表 Windows 已枚举。`g_boot_stage` 地址以本次 ELF 符号表为准。Windows 目标串口 VID/PID=F055:7431；0D28:0204 的COM34是探针，不是目标。

必须保留真实 `_init/_fini`，禁止再用 `PROVIDE(_init=0)`。DWC2 选速须正确处理专用 FS PHY；TRDT 按 HCLK计算。初始化失败保持 ready=0 并报告，不无限等待模式切换。软件复位不会完整重置 CoreSight，每次采集仍需重新配置。

本例默认对普通文本原样回显；`hello world!\n` 应收到相同13字节，不附加错误行。测试“初始化→枚举→回显”的完整过程需要足够长的**同一次**采集；分时窗导出不能冒充全程无缺口记录。

详细审查及本轮验收见 `../../docs/h743-web-review-2026-10-17.md`。
