# SPI 例程全量 Trace 验证方案与配置分工

日期：2026-10-11。状态：用户已确认实施；工程复制、H7B0/LDO 适配、多个档位构建和业务窗口实采已完成。启动入口暂停尚未通过本板验收。后续实测结论见《SPI例程Trace验收与频率边界》。

## 用户需求

1. 将 `E:\Share\github\STM32H7xx_Develop\App_SPI_H7B0` 复制到独立 ETM 项目，分析复杂程序的执行流。
2. 明确 MIPI16 在 500 MSa/s/ch 下的 trace 频率限制，以及与 CPU 主频的关系。
3. 参考现有 SWO，明确目标固件、Web 和 LA 的配置职责。
4. 本轮仍在 `E:\web-etm-trace` 完善，不接入主 Web 工具。

## 已核实的工程情况

- 用户确认板上 HSE 为 **25 MHz**；LA 为 **MIPI16，500 MSa/s/ch，与 LA5016 同档次**。
- `main.c` 实际启用 MPU、I/D Cache、HAL、SPI2、USART1、RTT 和 SysTick。FMC、I2C4、I2S6、LTDC、OSPI、RTC、SDMMC 的初始化调用目前被注释。
- 业务循环：PC8 拉低 → `HAL_SPI_Transmit` 发送 100 字节 → PC8 拉高 → 100 次空循环。采用轮询发送，目前没有 SPI DMA/业务中断路径；SysTick 是实际存在的异常路径。
- SPI2 引脚：PA12/SCK、PC2/MISO、PC3/MOSI。PC8 在实际循环中用作输出标记；PE2～PE6 没有发现当前启用路径的引脚配置冲突。
- Keil 工程实际选择 `STM32H7B3LIHxQ`，宏为 `STM32H7B3xxQ`，启动文件也是 H7B3，Flash 链接上限是 2 MiB。复制后必须改为实际 H7B0 型号与 128 KiB 上限。
- 现有 `App.axf` 是 ARM Compiler 5.06 构建的小端 ARM ELF，DWARF 3；现有读取器能读取 92 个带大小的函数符号及源码行表。Flash 加载段为 19,232 字节，静态检查未超出 H7B0 的 128 KiB。
- 现有 AXF 的 main 反汇编包含 SPI 发送及空循环，与对应源码结构相符；这不等于整份源代码与 AXF 一致，更不等于与当前目标 Flash 一致。
- AXF 的启动文件 DWARF 路径指向另一个 UART_Printf 工程，必须显式校验和重映射，不能凭同名文件直接认定正确。
- 原 HAL 配置的 HSE_VALUE 是 24 MHz，而 system 文件默认 25 MHz。原 PLL1 为 M=12、N=280、P=2、R=4；按真实 25 MHz 得到 CPU 291.667 MHz、TRACEPORTCK 145.833 MHz、物理 TRACECLK 72.917 MHz。CPU 超过 H7B0 280 MHz 上限，不能照搬运行。

## 拟采用的时钟与构建

保持原业务逻辑，原工程及 AXF 保存为对照副本；建立单独的 H7B0 验证构建，生成匹配 ELF、BIN、源码、编译参数与指纹。

- 全工程统一 `HSE_VALUE=25000000`。
- 正式验证档：PLL1 M=5、N=112、P=2、Q=3、R=4，输入 5 MHz、VCO 560 MHz、CPU 280 MHz、TRACEPORTCK 140 MHz、物理 TRACECLK 70 MHz，PLL 输入范围选择 4～8 MHz。
- PLL2 若保持源码原本的 200 MHz SPI 内核目标，可用 M=5、N=80、P=2、Q=2、R=4，输入范围也选择 4～8 MHz。SPI /4 对应 SCK 50 MHz。这会修正原工程在 25 MHz 板上实际约 52.083 MHz 的 SCK，报告必须注明。
- 诊断档：PLL1 P=8、R=8，其余不变，CPU 70 MHz、物理 TRACECLK 35 MHz。SPI 内核仍由 PLL2 决定。该档用于定位问题，不能替代 280 MHz 原业务频率的验收。
- 当前环境可用 Arm GCC，未在已检查的工具目录找到 ARMCC。新构建默认采用 GCC，保留调试信息并标明优化选项；新 ELF 与旧 ARMCC AXF 不混用。
- 原有未调用的 256 KiB RTT 测速缓冲是带非零初值的数组；GCC 构建必须核实其被链接回收或采用明确的初始化实现，防止形成超出 128 KiB 的 Flash 镜像。
- 新验证固件普通启动不自行开启 ETM，保留 PE2～PE6 供 Web 配置，避免固件与 Web 重复争用 CoreSight。

## MIPI16 频率判断

**500 MSa/s 是采样率，不能直接换算成“支持 250 MHz trace”。** 采样间隔为 2 ns；并行 trace 是 DDR，每个半周期都有一个数据符号。

| 物理 TRACECLK | 半周期 | 500 MSa/s 下每半周期样点数 | 4 位 DDR 原始端口容量 |
|---|---:|---:|---:|
| 35 MHz | 14.286 ns | 7.14 | 35 MB/s |
| 50 MHz | 10 ns | 5 | 50 MB/s |
| 62.5 MHz | 8 ns | 4 | 62.5 MB/s |
| 70 MHz | 7.143 ns | 3.57 | 70 MB/s |
| 80 MHz | 6.25 ns | 3.125 | 80 MB/s |

这里的 MB/s 是十进制端口容量，尚未扣除 TPIU 帧、同步与其他开销，不是 USB 导出速度，也不是保证无损的 ETM 指令吞吐率。

将至少 4～5 个样点/半周期作为初始工程余量，对应 50～62.5 MHz；这是验证策略，**不是 MIPI16 硬件额定上限**。70 MHz 可以尝试，但需要检查每通道相位、稳定采样区与丢包。

Kingst 官网 LA5016 的规格为 500 MSa/s、最快数字信号 80 MHz、最小脉宽 6.25 ns。用户说明 MIPI16 同档次，可将其作为规划参照；目前没有找到 MIPI16 独立的官方输入带宽/最小脉宽规格，不能将 LA5016 数值直接写成 MIPI16 的保证值。已有 MIPI16 实测通过的是约 4.008 MHz trace，仍需高频阶梯验证。

H7B0 在 PLL1 档下：

```
CPU = VCO / P / CDCPRE
TRACEPORTCK = VCO / R
物理 TRACECLK = TRACEPORTCK / 2
CPU / TRACECLK = 2R / (P × CDCPRE)
```

所以 CPU 不存在“LA 80 MHz → CPU 只能 160 MHz”的固定关系。P=2、R=4 可得到 CPU 280 MHz / trace 70 MHz；但 ETM 编码量取决于执行路径，可能因输出带宽不足发生 overflow，仍需独立验证。降低 trace 频率而不降低 CPU，可能改善电气采样而恶化 trace 带宽。

ST AN5307 对该系列给出的 trace 引脚时钟能力为最高 133 MHz；实际系统还受 MCU 数据手册条件、LA 输入能力和连线共同限制。

## 配置职责

| 设置 | 目标固件 | Web / SWD | LA |
|---|---|---|---|
| HSE、供电、VOS、Flash 等待周期 | 初始化并负责正确性 | 填写实际 HSE 用于计算，回读验证 | — |
| PLL1 P/R、CPU/HCLK、SPI/UART/SysTick 时钟 | 初始化；提供经过验证的档位 | 读取与显示；不在运行中任意改共享 PLL | — |
| 应用 GPIO、缓存、MPU、DMA、业务行为 | 负责；预留 trace 引脚 | 检查引脚冲突 | — |
| DEMCR、调试时钟门控、CoreSight 解锁 | 普通启动不主动启用 | 保存原状态、配置、回读、退出恢复 | — |
| PE2～PE6 的 trace 复用与驱动设置 | 预留且不在业务中覆盖 | 配置并按掩码恢复原状态 | 按实际通道映射连接 |
| ETM 指令追踪、Trace ID、地址过滤、返回栈、周期计数 | 普通启动不主动配置 | 能力检查后配置；保存实际寄存器值用于解码 | — |
| Funnel / ETF / TPIU、4 位并行输出 | 普通启动不主动配置 | 负责串联、启停、flush 与状态检查 | — |
| 复位、暂停、继续、捕获起止位置 | 正常启动；可提供明确业务入口 | SWD 负责，并与 LA 武装状态联动 | 触发、采集、停止 |
| 采样率、阈值、深度、触发、采样相位 | — | 通过现有本地服务设置并读取结果 | 硬件执行；保存原始采样 |
| ELF/源码绑定、包解码、执行流与统计 | 提供匹配的构建产物 | 负责解码、质量报告、导出 | — |

现有 SWO 示例已经采用“固件负责主时钟，Web 通过 SWD 开启 trace”的方式；SWO 的 `app/swo/route.js` 还有可恢复的门控和 GPIO 配置。本轮 ETM 需要补齐同样的状态保存/恢复、完整 RCC 时钟识别和配置记录。

ETM 页面目前 reset 等待固定 250 ms，依赖旧示例自行配置；复杂程序必须改为明确的暂停位置/状态判断，不能等待固定时间后猜测程序走到哪里。

## 实施顺序与验收

1. 确认后复制工程，保存原始 AXF/源码与指纹，建立 H7B0 验证构建；静态核对 Flash、RAM、向量表、供电、时钟、机器码及源码映射。
2. 完善独立 Web：RCC 自动识别、HSE 输入、CoreSight 状态恢复、可靠复位暂停、LA 武装/启停握手，保存每次实际配置及 ELF 校验结果。
3. 烧录匹配验证固件，读取实际 RCC 与 Flash 比较。先诊断档，再尝试 280 MHz CPU / 70 MHz TRACECLK；按实际失败性质调整采样相位或验证档位。
4. 采集启动阶段和 SPI 稳态阶段的不按地址过滤的 trace；启动阶段有 HSI→PLL 切频，需要物理解码支持分段时钟与相位分析。进一步尝试连续启动→SPI 捕获，明确最早/最晚可靠恢复位置。
5. 复核包字节覆盖、同步区、overflow、错误、无法恢复区、未确认条件指令、Flash 匹配和源码对应。不能仅凭“零解码报错”判断无丢失。
6. 在已验证连续区内重建 main→GPIO→HAL_SPI_Transmit→轮询/返回及 SysTick 嵌套。生成调用统计、指令计数火焰图、热点及源码执行流，并保留原始 CSV/KVData 与自包含记录。
7. 可选连接 CH5=PC8、CH6=PA12、CH7=PC3，核对发送窗口、SCK 和 MOSI。这需要用户接线；现有 CH0～4 的 trace 接线保持有效。外部波形与 ETM 存在输出缓冲延迟，不能把波形时间直接当作每条指令时间。

“全量”定义为指定捕获窗口内不按地址筛掉代码，尽可能保留所有控制流。它不表示无限时长、不表示运行过所有分支，也不表示纯 ETM 提供历史变量值或每条指令准确耗时。只有经过完整性检查的连续区段才能给出可靠的执行顺序；缺口必须展示，不能补猜。

## 参考

- Kingst 官方产品规格：https://www.qdkingst.com/en
- Kingst 官方更新日志（明确存在 MIPI16）：https://www.qdkingst.com/vis/ulog/zh-CN
- ST AN5307：https://www.st.com/resource/en/application_note/an5307-getting-started-with-stm32h7a37b3-line-and-stm32h7b0-value-line-microcontroller-hardware-development-stmicroelectronics.pdf
- 本地 SWO 例程：`E:\web-serial-rtt-tools\tools\target-firmware\stm32f103cb_swo_hse\README.md`
- 本地 SWO 可恢复配置：`E:\web-serial-rtt-tools\app\swo\route.js`
- 本地 SWO 时钟识别：`E:\web-serial-rtt-tools\app\swo\ports.js`
