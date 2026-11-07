# HSS 停止与 SPI→CDC 并发验收

2026-11-07（Asia/Shanghai）。同一台 HPM5301EVKLite 探针与额定 72 MHz 的 STM32F103ZE。
F103 SPI1 发送仓库 `stm32f103_spi_tx` 夹具的 64 B SPIC 帧，探针 SPI2 经 DMA 接收并通过 COM43 转发。
主机检查连续序号和每帧 56 B 已知载荷。HSS 读夹具的 `g_pack.u_hi`，SWD 请求档 60 MHz，flags `0x81`，保留 CDC 服务。

## Windows 接口认领

从设备读回 650 B MS OS 2.0 描述符。原生 USB 在 MI_04、MI_05 均无法认领，Windows 两个子实例已绑定 WINUSB，但都缺 `DeviceInterfaceGUIDs`。配置描述符中的真实接口分工如下。

| 接口 | 作用 | 固件声明的 GUID |
| --- | --- | --- |
| MI_04 | SPI/QSPI 数据面，端点 0x0B 和 0x8B | `{3E7B1C48-9D2A-4F61-B5E8-7C04A9D3F210}` |
| MI_05 | WebUSB 标识接口，无 bulk 端点 | `{92CE6462-9C77-46FE-933B-31CB9C5AA3B9}` |

备份当前登记后，只给本探针这两个设备实例补写对应的 REG_MULTI_SZ，再逐个重启子接口。
libusb 认领 MI_00/04/05 通过，生产 `WebUsbSpiTransport` 经 Node USB 认领 MI_04 通过。
测试结束恢复原探针应用后，认领复核仍通过。Windows 最初遗漏属性的形成原因待查。
接口 GUID 的用途见 [Microsoft WinUSB 安装说明](https://learn.microsoft.com/en-us/windows-hardware/drivers/usbcon/winusb-installation)。

## 丢帧复现与修复

基线 `4983a5c` 在 4.5 MHz SPI 负载下，12 轮 HSS 停止都收齐已生成样本。
但每次启动 HSS，探针 SPI DMA 环都丢约 2.7 KB，累计 32,678 B，主机记录 523 个缺帧和 23 次坏帧或失步。
SWD 建链与逐档换频多次经过 `Set_Clock_Delay`，同步向 115200 波特率的调试 UART 打印。
在打印期间，SPI DMA 继续接收，主循环来不及搬走积压。去掉该行打印，诊断仍可从原有标定应答回读时钟代码和延时。

新探针应用构建时间 `2026/11/07 14:01:44`，194,088 B，DFU 读回 SHA-256
`5d32f353f91dc8b38074179d5aed0dc0cc2c5bd7644567d9c092c5a406aac3ed`。
F103 夹具 BIN SHA-256 为 `fbfb19065185988397bca46686d6afc7cc1257ccb8401a4841c9170b97188b38`。
首次并行构建中一个 SDK ADC 编译子任务静默退出，保留日志，限制为 4 个并行任务后完成实际编译、链接和打包。

## 通过条件与结果

每档保持 SPI 源与 CDC 接收连续运行，HSS 交替测试 100/50/1000/20 µs 周期和 512/4096/8192 B 原生读取，
逐轮验证尾包数、0 B 结束读、剩余空读退场、序号和已生成样本数。SPI SCK 来自 F103 RCC 与分频回读。

| SPI SCK | CDC 带载实收 | HSS 停止并重开 | HSS 收到 / 探针生成 |
| --- | ---: | ---: | ---: |
| 4.5 MHz | 0.563 MB/s | 12 轮 | 69,483 / 69,483 |
| 18 MHz | 2.254 MB/s | 12 轮 | 69,454 / 69,454 |
| 18 MHz，生产 JS 停止握手 | 54,943 帧随测试连续通过 | 4 轮 | 356 / 356 |

24 轮有序异步 libusb 测试均交付最后的半包，6 轮恰好在读取边界以 0 B 结束。
额外 4 轮运行生产 `ScopeView._stopData` 和 `VendorEpTransport`，经 Node USB 适配器在同一连接中实测。
所有轮次的 HSS USB 丢样、SWD 读错、无效值、包序异常和未退场读取均为 0。
SPI 两档全程接收 11,150,532 B，与探针转发计数一致。缺帧、坏帧、逆序、FIFO 溢出、DMA 错误、探针丢弃与发送端补缓冲迟到均为 0。
测试覆盖真机和生产 JS 的原生适配器路径，浏览器权限选择属于另一入口。

探针原应用区域与 F103ZE 全 Flash 均已读回验证恢复。目标回到 48 MHz，故障寄存器为 0。
原探针应用重启后 Windows 仍可认领 MI_04/05，F103 原 CDC 的 PING/PONG 通过。
Scope、SPI CDC 主机模型、Web bundle 与 ETM 副本检查通过。
逐轮计数和镜像指纹见 [summary.json](summary.json)，脚本、失败和通过的原始记录及备份在
`E:/superlinkPRO-backups/20261107-hss-spicdc-hardware/`。
