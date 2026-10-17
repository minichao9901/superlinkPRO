# ADC 复用 SPI/QSPI Bulk

ADC 使用已有 SPI 接口的 OUT 0x0B / IN 0x8B，不新增接口、端点或大型缓冲。
OUT 的 16 KiB 缓冲作为 ADC 4096 个 32-bit DMA 槽的环形区；IN 的 8 KiB
缓冲分为两个 4096-byte 发送块。SPI/QSPI 与 ADC 独占使用这些资源。
自定义 HID 保持 64 bytes、原报告 ID；HS bInterval=6（4 ms），FS 为 4 ms。
RTT/JScope 数据接口和搬运路径保持原样。

## 硬件

当前高速模式支持 HPM5301 EVKLite：PB14 / ADC0_IN6，J3[10]，与 QSPI IO2 共脚。
输入为 0–VREFH 的单端电压，共地；使用低阻信号源，切换前断开外部 QSPI 驱动。
参考电压默认 3.3 V，网页校准换算不改变硬件允许电压。
带 VREF/LED 共用 ADC、且无 SPI bridge 的 akaLinkPro 板型不开放此高速模式；
其原低速 HID ADC ABI 保留。

GPTMR1 通道 2 → TRGM0 → ADC0 硬件序列触发 → ADC 自带 DMA 环形缓冲。
不使用逐样本定时中断，也不在主循环逐点启动转换。通道 0/1 保留给 LED/周期总线。
仅调整 ADC/模拟时钟，不调用改变 CPU 时钟的板级 ADC 初始化。
可设置 8/10/12/16-bit 硬件转换分辨率，采样周期 3 个 ADC 时钟；请求速率为整数
1–2,000,000 Sa/s，实际速率按定时器整数周期向下量化，并在数据中返回。
2 MSa/s 是请求上限。2026-10-06 在 HPM5301 EVKLite 上，8/10/12/16 位在 1 MSa/s
和 2 MSa/s 下均各持续采集 30 秒并通过；压力条件和记录见 Web 仓库
`docs/validation/2026-10-05-hardware-test-results.md`。该结果验证当前探针、Chrome 与页面负载下的
传输和计数，不代表长期运行保证，也不验证模拟精度、线性度或 ENOB。
100/200 ns（10/5 MSa/s）不在本实现范围；500 ns 为 2 MSa/s。

## HID 0x38 控制

状态为 res[4..7] u32 LE；数据从 res[8] 开始。常规动作 req[1]=2；
OPEN 的 req[1]=12，req[4] 位宽、req[5..8] 速率、req[9..12] 数量（0 连续）。

| Action | 返回数据 | 用途 |
| --- | --- | --- |
| 9 | ADB2、EP:u8=0x8B、version:u8=2、bits mask:u8=15、channel:u8=6、max rate:u32、DMA words:u16=4096、block bytes:u16=4096、flags:u8、supported:u8、reference mV:u16 | 能力和占用查询 |
| 10 | token:u32 | OPEN，仅占用资源，尚未采集 |
| 11 | 无 | END，请求停止并排空数据；未 START 也能结束 |
| 12 | 无 | CLOSE，END 传输及硬件清理完成后才释放共享资源；BUSY 可重试 |
| 13 | 无 | START，主循环配置 DMA/触发并启动 |
| 14 | token/rate/received/sent/fault 各 u32，owned/running/ended/prepared 各 u8 | 恢复丢失 OPEN 应答及诊断 |

CAPS flags bit0 表示尚有 SPI OUT DMA，bit1 表示其他占用/未完成工作。
网页先协调停止 SPI、周期总线和目标采集，要求 bit1=0；若 bit0=1，向 OUT 0x0B
发送零长度传输，让旧 OUT 完成。OPEN 仅在两个缓冲和端点均空闲时成功，
不取消仍在使用共享内存的 USB DMA。先挂起 IN 读取，再 START。

## ADS2 数据

每次 WebUSB transferIn(11, 4096)。包头 32 bytes，全体多字节整数为 LE：

| 偏移 | 字段 |
| --- | --- |
| 0 | ADS2 ASCII |
| 4–7 | version=2 / type(DATA=1, END=2) / bits / flags=0 |
| 8 | token:u32 |
| 12 | block sequence:u32，从 0 递增 |
| 16 | first sample index:u32，按样本数递增并允许回绕 |
| 20 | actual rate:u32 |
| 24–27 | sample count:u16 / channel=6 / stride=2 |
| 28 | fault:u32 |
| 32 | 右对齐 u16 样本数组，最多 2031 点 |

最大传输 4094 bytes，以短包结束，不需要额外 ZLP。END 无样本。
主循环按块复制/量化，USB 完成回调只归还发送槽；回调不逐点采集。
DMA stop position 保留一个保护槽，消费者完成复制后才推进可写边界。
主机过慢时硬件停止，报 fault=5；触发冲突/FIFO 错误报 6。禁止覆盖未读样本。
有限采集按目标点数停止，随后排空 DATA 和 END。USB reset/disconnect 作废代数、
立即关触发/DMA，主循环恢复引脚/时钟后才允许 SPI 重新使用缓冲。

网页优先用 Dedicated Worker 持有 WebUSB 接口，在 START 前挂起 1–32 笔原生 IN 读取，
按提交顺序处理并及时补充请求；Worker 将完成数据复制到可转移缓冲区，页面线程负责
代数、序号、样本索引、速率和位宽校验及采集缓存。固件按协商的读取数发送对应数量的
END，关闭前等待所有原生读取退场。浏览器不支持 Worker WebUSB 时保留页面线程读取路径。
解析失败也尝试停止并排空 END。停止/关闭失败保留占用以便重试，不复位整台 USB 设备。
RTT/JScope、周期任务及 SPI 新启动在 ADC 占用期间拒绝，避免同时争用。

## 验证

`make test-host` 和 `make test-usb-descriptors`（需配置 HPM SDK）覆盖有限计数、环形回绕、
溢出保护、单个 IN DMA、
复位/关闭以及共享端点描述符。官方 SDK 头文件的主机语法检查不等于目标链接。
2026-10-06 完成 HPM5301 EVKLite 目标编译、真实 USB 页面采集与 1/2 MSa/s 四种位宽
连续测试；每档 30 秒，并在页面线程每 100 ms 人为阻塞 7 ms。固件主机测试和 Web
离线全套回归也通过。此处仍未覆盖校准信号源下的电压精度、线性度/ENOB、长时间运行、
实体拔插/复位以及 ADC 停止后 RTT/J-Scope 旧→新→旧性能对比。

复审增加两项生产 C 回归：ADC 初始化失败时不读取旧 DMA 指针、不发送残留样本；
USB reset 与 START 的最终启动检查置于同一短临界区，防止复位后重新开启触发。

## 缓冲分层与 STM32 HT/TC 对照

STM32 常见的循环 DMA 半传输/全传输（HT/TC）方案本身很合适：DMA 填充一半时通知
消费者处理另一半；消费者必须在 DMA 回到该半区前完成，否则仍会读到被覆盖的数据。

本实现解决的是从 ADC DMA 到浏览器的端到端交付，缓冲分为三层：HPM ADC 的 4096 个
32-bit DMA 槽组成环形区并带保护边界，防止消费者落后时静默覆盖；探针使用两个 4096 字节发送槽交替搬运；
浏览器 Worker 最多挂起 32 笔 USB IN 读取，吸收浏览器调度抖动。也就是说，设备侧仍有
双缓冲，主机侧再用有界读取队列；它不是用 32 个缓冲取代 MCU 的 ping-pong。

若数据只在 MCU 内做短小、确定时限的处理，HT/TC 双半区通常更省内存且简单。跨 USB
送到浏览器时，只用两个 MCU 缓冲无法覆盖主机调度延迟；应保留 DMA 写入边界保护和
足够的 USB/主机队列。队列只能吸收短时抖动，不能弥补长期平均消费速度低于采样数据率。
