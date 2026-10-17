# SPI 例程 Trace 验收与频率边界

日期：2026-10-11。独立工具仍位于 `E:\web-etm-trace`，没有接入主 Web 工具。

更新：当前已按用户要求固定 **CPU 70 MHz / TRACECLK 35 MHz 稳定基准 v1**，完成新的 1 ms 复验与 10 ms 全地址业务窗口验收，固件及记录封存见 [稳定基准说明](../baselines/spi-70-35-v1/README.md)。频率优化暂停。下文保留之前各频率档的实验结果，不表示目标板当前仍使用 280 MHz 固件。

## 已完成

- 复制原 App_SPI_H7B0 工程并保存原 ARMCC AXF。工作副本修正为 H7B0、25 MHz HSE、LDO、128 KiB Flash 上限，保留 SPI2 轮询业务、缓存、RTT 和 SysTick。
- 构建 CPU 70/280 MHz 的多个 trace 分频档；每份约 20 KiB Flash。新 ELF 与原 AXF 明确分开，采集绑定实际烧录的 ELF。
- Web 增加固件档位、HSE 输入、RCC 时钟识别、CPU/HCLK/TRACECLK 显示、业务入口临时硬件断点、可恢复的 trace 门控/GPIO/CoreSight 配置及配置快照。
- 复位或切换固件前先停止 trace、刷新 FIFO/TPIU，避免在时钟切换时遗留未完成刷新。ETF 按 Arm 规定在启用状态发起 flush/stop，等待 Ready 后再关闭。
- 物理解码按每个实际半周期取样，增加分段时钟与长间隔报告。捕获窗口内不按地址过滤。
- SPI 调用、HAL 内部轮询、返回、SysTick 路径已经进入执行流、指令计数火焰图、函数调用和热点视图；可导出配套记录包和源码执行流。
- 原板 128 KiB Flash 已完整备份。真机测试使用生产版 `target.js` 控制器，经本地 pyOCD 验收适配层连接 SWD；这不是浏览器 USB 设备选择流程的全程自动化验收。

## 真实业务窗口

均使用 MIPI16，500 MSa/s/ch，CH0～4=PE2～PE6，1 ms 窗口，Trace ID=2，未启用返回栈和周期计数。

| 指标 | CPU 70 / Trace 35 MHz | CPU 280 / Trace 35 MHz（当前推荐） |
|---|---:|---:|
| 实测 TRACECLK 平均值 | 35.000060 MHz | 约 35 MHz |
| 指令路径记录 | 47,064 | 190,653 |
| 已证实执行记录 | 41,790 | 168,956 |
| IT 条件执行状态未知 | 37 | 554 |
| 源码行映射记录 | 47,064 | 190,653 |
| 已识别 ETMv4 包 | 2,848 | 11,915 |
| ETM 输入/包覆盖字节 | 3,034 / 3,034 | 12,747 / 12,747 |
| 包覆盖遗漏/重叠 | 0 / 0 | 0 / 0 |
| 解码错误/overflow 包 | 0 / 0 | 0 / 0 |
| SysTick | 1 | 1 |
| 核对的直接分支落点 | 9,273 | 37,895 |
| 观测 SPI 调用 | 13 | 52 |
| 闭合的 SPI 发送 | 12 | 51 |
| 每次闭合发送的 TXDR 字节写入 | 100 | 100 |

末尾还有未闭合发送：70 MHz CPU 样本已看到 85 次字节写入，280 MHz CPU 样本已看到 3 次；不能将其当作完整发送。两个样本都有 12 字节尾部不完整 TPIU 帧。解码器初始化边界保留，未构造捕获前的调用栈。

对应记录：

- `records/capture-470ede2c5ebb4a809825e33c440bf938/`
- `records/capture-0d80d61586bf4f868225dcd908dfa5d7/`（280 MHz CPU / 35 MHz trace）
- `records/capture-a346eb62e7ff4fe289ef01942ae34c98/`（46.667 MHz 边界样本）

`spi-validation.json` 核对此构建的所有相邻直接分支落点，并统计闭合 HAL_SPI_Transmit 中实际恢复出的 TXDR 写入指令。它验证控制流和 100 字节发送结构，不表示获知了每个内存值。

## 频率与采样相位结论

1. CPU 280 MHz / Trace 70 MHz：帧校验失败。扫描 0.1～0.9 半周期位置，没有得到完整有效 TPIU 解复用。时钟出现 2 ns、4 ns 短间隔，当前连接不能据此做可信执行流验收。
2. CPU 280 MHz / Trace 56 MHz：默认 0.25 位置的帧校验失败。
3. CPU 280 MHz / Trace 46.667 MHz：0.25 与 0.30 位置输出完全相同的 12,747 字节 ETM 数据，且完成上述解码与 SPI 结构核对；但 0.10、0.20 位置生成不同数据流，它们同样可能没有 OpenCSD 错误。**这直接证明“零解码错误”不能作为无损保证。** 0.25 的最小数字跳变距离为 0，0.30 为约 0.4 ns；这档对相位仍然敏感，需要保留此限制。
4. **CPU 280 MHz / Trace 35 MHz：已完成实测并作为当前推荐档。** 0.10～0.40 的七个位置输出完全相同的 12,747 字节 ETM 数据，默认 0.25 的最小数字跳变距离约 2.5 ns。0.75、0.85 靠近数据转换区，产生不同数据；0.90 恢复同一字节流。因此必须选择并保存稳定采样位置，不能认为半周期任意位置都有效。
5. 35 MHz trace 下的 280 MHz CPU 样本与 46.667 MHz 边界样本在选定位置得到完全相同的 ETM 字节流。重复采集又恢复 191,663 条路径记录，未报告解码错误；配置恢复通过。结束时目标保持运行，SWD 诊断读到 CPU 280 MHz、HCLK 140 MHz、SPI 内核 200 MHz、trace 35 MHz、ready=1、error=0、SPI 状态=HAL_OK。

这些结论是当前目标板、连线、阈值与采样参数的测试结果，不能写成 MIPI16 的绝对硬件上限。改善接地、缩短连线、检查输入阈值和验证 GPIO 驱动速度后，可能得到不同边界。

用户确认 MIPI16 与 LA5016 同档次；官方 LA5016 的 80 MHz 最快数字信号、6.25 ns 最小脉宽可以作规划参照，不能替代 MIPI16 自身额定规格。500 MSa/s 的 2 ns 采样间隔也不能换算成保证支持 250 MHz DDR trace。

PLL1 的 P 与 R 独立：CPU=VCO/P/CDCPRE，物理 TRACECLK=VCO/R/2。本项目 VCO=560 MHz、P=2，可以维持 CPU 280 MHz，通过 R=4/5/6/8 得到 70/56/46.667/35 MHz trace。降低 trace 频率会减小输出带宽；是否 overflow 仍应按实际业务检查。

## 尚未通过与精度范围

- **从复位入口开始的启动全程捕获尚未通过。** 实验中暂停在复位入口后出现 SWD 内存访问故障，调试端口重连与 debug reset 没有恢复正常内存访问。页面暂时禁用该选项，保留已验收的业务入口采集。
- 此项目已经分析的是不按地址过滤的真实业务窗口，不是所有启动指令或无限时长的全运行历史。
- IT 非分支条件指令的执行状态仍有未知项。GCC 即使关闭普通 if-conversion，也会产生条件比较与 ITE 赋值。未知项主要在 HAL_SPI_Transmit 和 SPI_WaitOnFlagUntilTimeout；不以当前 RAM 值推断其历史执行。
- 源码映射率不是程序覆盖率。火焰图按路径指令记录数加权，尚不是时间火焰图；不将调用占比标成调用/秒。
- 包字节覆盖、分支落点、发送次数和相位检查共同提供证据，但不能证明所有电气采样绝对无损，也不能恢复历史局部变量值。

## 配置分工

| 负责方 | 内容 |
|---|---|
| 目标固件 | HSE、LDO/VOS、Flash 延迟、PLL P/R、CPU/HCLK、SPI/UART/SysTick 时钟、MPU/缓存、业务 GPIO和 trace 引脚预留 |
| Web/SWD | 读取 RCC 与硬件能力、验证 ELF、CoreSight 解锁/门控/路由、trace GPIO 复用、ETM Trace ID/选项、可靠暂停/继续/业务复位、保存及恢复配置 |
| Web + 本地服务 + LA | 采样率、阈值、触发、深度、开始/停止、原始 CSV/KVData、配置与固件绑定、解码、分析和导出 |

沿用 SWO 的原则：普通固件不自行开启 trace；网页填写 HSE 只用于计算，不改写共享 PLL、CPU 或外设时钟。

## 文件

- 固件说明：`firmware/stm32h7b0_spi_trace/README.md`
- 静态检查：`docs/validation/2026-10-11-spi-static.json`
- 烧录校验：`docs/validation/2026-10-11-spi-flash-*.json`
- 采样相位检查：对应记录内的 `phase-validation.json`（推荐档稳定窗约 4.2 ns，边界档约 0.5 ns）
- 真实分支/发送核对：记录内的 `spi-validation.json`
- 原板备份：`docs/validation/2026-10-11-before-spi-flash-128k.bin`
- 火焰图截图：`docs/validation/2026-10-11-spi-flame.jpg`

软件验证：25 项 Python 测试与 3 项 JavaScript 测试通过，覆盖真实解码、调用/异常、未知 IT 状态、记录导入导出、分段时钟、PLL P/R 区分、配置掩码恢复、FIFO 关闭顺序与相位余量判断。

参考：Arm CoreSight TMC TRM（DDI0461B）https://documentation-service.arm.com/static/5f1060dd0daa596235e7ab7a ，Kingst 官方规格 https://www.qdkingst.com/en 。
