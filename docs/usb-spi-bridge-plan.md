# USB → SPI/QSPI 转发功能方案（v1，待评审）

> 分支：`feature/usb-spi-bridge`　目标板：HPM5301EVKLite（探针）
> 状态：**方案已评审**（2026-09-28 用户确认端点/引脚/SCLK 三项），代码未写、未上板（用户当前在用探针）。按 §8 分阶段实施。

---

## 0. 结论速览

| 议题 | 结论 | 依据 |
|---|---|---|
| **端点**（已确认） | **新开一对 bulk**：IN `0x8B` / OUT `0x0B`（物理 EP11，双向同名），挂在新增的 vendor-specific 接口上；**不复用 CDC** | §3 |
| 外设 | `SPI1`；引脚 PA26/27/28/29 + **PA30/PA31（quad IO2/IO3）**，全部在 J3 排针上 | §2.1 §2.3 |
| 传输形态 | 单线 / 双线 / **四线**；硬件自带 cmd / addr / dummy / token 相位，一次 CS 内完成 | §2.2 |
| SCLK（已确认） | 默认 20 MHz；`sclk_hz` 由 HID 配置，**面板协议直接指定 40 / 60 / 80 MHz**，逐档上板实测 | §4.5 §6.3 |
| **目标屏** | 天马 2P01 / **AXS15352**（4 线 SPI + DC，240×296，RGB565）与 **ST77916**（**QSPI 四线**，360×360，RGB565 @40 MHz）——两套初始化序列都由网页逐条下发，探针逐条执行 | §2.10 |
| TX 策略 | `len < 100` 轮询；`len ≥ 100` DMA；**阈值可配**，上板实测后再定 | §6 |
| RX 策略 | 一律轮询（CPU 收 FIFO），与 TX 策略解耦 | §6 |
| 缓冲 | 32 KB AHB SRAM（`0xF0400000`，当前完全空闲）：OUT 环 16 KB + IN 环 8 KB + 暂存 2 KB + 预留 6 KB；DLM 只多花约 0.3 KB 元数据 | §5.2 |
| 控制面 | HID 新增 `CMD 0x35`（配置 / 状态 / 使能 / 复位 / 中止 / 面板档），沿用 0x31/0x32/0x34 的报文约定 | §4.4 |
| 数据面 | bulk OUT = **有序帧流**（数据 + CS/DC/延时等线序动作统一走这条）；bulk IN = 读数据 + 应答 | §4.2 §4.3 |
| 面板友好 | **面板档（profile）+ 紧凑步帧 `STEP`**：网页把 `{cmd, data, n, delay_ms}` 表原样下发，探针按档自动处理 DC / 0x02+24 bit 地址 / 四线数据 | §4.7 |
| 与现有功能 | **引脚零冲突**；主循环挂一个 `spi_bridge_poll()`，未使能时一条分支直接返回 | §2.8 §5.1 |
| 预期吞吐 | 单线 20 MHz ≈ 2.5 MB/s；四线 20 MHz ≈ 10 MB/s；**四线 40 MHz ≈ 20 MB/s**（USB HS 侧余量远大于此） | §6.3 |

---

## 1. 需求与边界

**需求**（用户 2026-09-28）

1. 新分支、新功能：USB → SPI/QSPI 转发。
2. 协议自定，上位机（web）按此实现。
3. 目标用途：驱动 LCD / QSPI 屏，以及各类 SPI 模块。
4. `len < 100` 轮询发送；`len > 100` DMA 发送；接收用轮询。
5. 端点：新开一对 bulk 还是复用 CDC —— 要给出结论。
6. 缓冲建议放 32 KB AHB SRAM。
7. 探针暂时不能用，先写代码，后上板验证。

**本方案不做**

- 网页侧实现（只给协议契约，后续单独出 handoff 文档）。
- 具体某块屏的初始化序列：由主机下发，固件保持通用（不内置面板型号表）。
- 专用命令集（NOR 的 sector erase 等）：用通用帧即可拼出。
- 与 JTAG/DAP/RTT/Scope 的功能耦合：SPI 桥是独立通道，互不干扰。

---

## 2. 硬件调研结论（附证据）

### 2.1 J3 排针完整引脚表

来源：`docs/HPM5301EVKLite_UG_V1.1.pdf` 表 2（官方用户手册，已本地提取核对）。

| 奇数脚 | 信号 | | 偶数脚 | 信号 |
|---|---|---|---|---|
| 1 | 3.3V | | 2 | 5.0V |
| 3 | PB09 | | 4 | 5.0V |
| 5 | PB08 | | 6 | GND |
| 7 | PA02 | | 8 | **PB15**（UART3.TXD，探针 VCOM 占用） |
| 9 | GND | | 10 | **PB14**（UART3.RXD，探针 VCOM 占用） |
| 11 | **PA31** | | 12 | NC |
| 13 | PB11 | | 14 | GND |
| 15 | NC | | 16 | NC |
| 17 | 3.3V | | 18 | NC |
| 19 | **PA29**（SPI1.MOSI） | | 20 | GND |
| 21 | **PA28**（SPI1.MISO） | | 22 | NC |
| 23 | **PA27**（SPI1.SCLK） | | 24 | **PA26**（SPI1.CS0） |
| 25 | GND | | 26 | PB10（板上标注 SPI_CS1，GPIO） |
| 27 | PB12 | | 28 | PB13 |
| 29 | PY00 | | 30 | GND |
| 31 | PY01 | | 32 | PA09 |
| 33 | PA10（板载 LED2） | | 34 | GND |
| 35 | NC | | 36 | PA00（UART0.TXD / log） |
| 37 | **PA30** | | 38 | PA01（UART0.RXD / log） |
| 39 | GND | | 40 | NC |

**SPI1 六线全部在排针上**（这是本功能成立的前提）：

| 功能 | 引脚 | J3 脚 | 说明 |
|---|---|---|---|
| CS0 | PA26 | 24 | 硬件 CS，`IOC_PA26_FUNC_CTL_SPI1_CS_0` |
| SCLK | PA27 | 23 | `IOC_PA27_FUNC_CTL_SPI1_SCLK` |
| MOSI / IO0 | PA29 | 19 | 单线时 MOSI；双/四线时 DAT0 |
| MISO / IO1 | PA28 | 21 | 单线时 MISO；双/四线时 DAT1 |
| **IO2** | **PA30** | **37** | `IOC_PA30_FUNC_CTL_SPI1_DAT2` |
| **IO3** | **PA31** | **11** | `IOC_PA31_FUNC_CTL_SPI1_DAT3` |

> SDK 的 `boards/hpm5301evklite/pinmux.c` 只初始化 PA26~PA29（`init_spi1_pins()`），**没有 quad 的 DAT2/DAT3**——需要我们在应用侧补一份引脚初始化。

### 2.2 SPI1 的硬件能力（够用且富余）

来源：`soc/HPM5300/HPM5301/hpm_soc_ip_feature.h`、`hpm_soc_feature.h`、`soc/HPM5300/ip/hpm_spi_regs.h`。

| 能力 | 值 | 对我们的意义 |
|---|---|---|
| `SPI_SOC_TRANSFER_COUNT_MAX` | `0xFFFFFFFF` | 单次传输长度不受 512 限制，**不需要联动描述符**（SDK 例程里那套 `dma_linked_descriptor` 在 HPM5301 上被 `#if` 排除） |
| `SPI_SOC_FIFO_DEPTH` | 8 项 | 轮询路径的粒度；配合 DataMerge 一次可灌 32 B |
| `HPM_IP_FEATURE_SPI_NEW_TRANS_COUNT` | 1 | 32 位传输计数 |
| `HPM_IP_FEATURE_SPI_CS_SELECT` | 1 | 硬件 CS0~CS3 可选（本板只有 CS0 引到排针） |
| `HPM_IP_FEATURE_SPI_SUPPORT_DIRECTIO` | 1 | 可用 DIRECTIO 寄存器直接驱动 CS/SCLK/IO（备用手动时序手段） |
| 数据相位格式 | `single / dual / quad` | **QSPI 屏的核心** |
| 地址相位格式 | `single / dualquad` | QSPI 屏的地址相位 |
| 相位 | cmd / addr / dummy(1~4) / token(0x00 或 0x69) | 覆盖 QSPI 屏与 SPI NOR/传感器的常规时序 |
| 传输模式 | write / read / write_read / dummy_read / … | 覆盖双向场景 |
| 时序旋钮 | `cpol/cpha`、采样沿、`cs2sclk`、`csht` | CS 与时钟相对时序可调（屏类器件敏感） |

驱动 API（`drivers/inc/hpm_spi_drv.h`，全部可直接用）：

- `spi_master_timing_init()` / `spi_format_init()` / `spi_control_init()`
- **轮询整帧**：`spi_transfer(ptr, cfg, &cmd, &addr, wbuf, wlen, rbuf, rlen)` —— 一次调用完成 cmd+addr+数据相位
- **DMA**：`spi_setup_dma_transfer(ptr, cfg, &cmd, &addr, wcount, rcount)` —— 先 `spi_control_init` 配好相位，再由 DMA 喂数据相位（cmd/addr 由驱动写寄存器完成，**同一次 CS 窗口内**）
- `spi_wait_for_idle_status()` 收尾、`spi_directio_*()` 兜底

参考例程：`samples/drivers/spi/master_trans_large_amount_of_data`（DMA + cmd/addr + GPIO CS 的完整范式）、`samples/drivers/spi/polling/master`、`samples/spi_components/*`。

### 2.3 QSPI（四线）可行性 —— 关键结论

- HPM5301 的 SPI1 有 `DAT2 = PA30`、`DAT3 = PA31` 两处引脚功能（`soc/HPM5300/HPM5301/hpm_iomux.h`），**且这两个脚都在 J3 排针上**（J3[37] / J3[11]）。
- 驱动层 `spi_data_phase_format_t { single, dual, quad }`、`spi_addr_phase_format_t { single, dualquad }` 齐全。
- 结论：**能驱动真正的 QSPI 屏**（cmd 单线 + addr 四线 + data 四线，或 cmd/addr/data 全四线，按屏的时序配）。这正好对上「LCD/QSPI 屏」的需求。

### 2.4 板载 QSPI NOR 不占排针（不会打架）

- 板载 1 MB NOR 走 `XPI0`（`BOARD_APP_XPI_NOR_XPI_BASE = HPM_XPI0`），HPM5301 只有 XPI0 一个 XPI 控制器。
- XPI0 的 CA 总线可映射到两处：`PX00~PX07`（XPI 专用 pad）与 `PA24~PA31`。**板载 NOR 在 PX 上**——否则 SDK 自带的 SPI1 例程（用 PA26~PA29）会破坏 flash 连接，而它在 EVKLite 上是能跑通的；且 I2S-over-SPI 例程把 PA31 当普通 CS 用（`BOARD_I2S_SPI_CS_GPIO_PAD = IOC_PAD_PA31`）。
- 因此结论：**排针上的 PA26~PA31 可以放心当 SPI1 用**，与启动 flash 无冲突。XPI0 无法再拿来做第二路 QSPI 主机（唯一实例），所以「QSPI 屏」只能走 SPI1 的 quad 模式。

### 2.5 AHB SRAM：32 KB，当前完全空闲

| 项 | 值 | 证据 |
|---|---|---|
| 基址 / 大小 | `0xF0400000` / 32 KB | `soc/HPM5300/HPM5301/toolchains/gcc/flash.ld` 的 `MEMORY` |
| 应用 linker | `.ahb_sram (NOLOAD) : { KEEP(*(.ahb_sram)) } > AHB_SRAM` | `firmware/application_5301/linker/flash_dfu_app.ld:216-219` |
| 当前占用 | **0**（无任何 `.ahb_sram` 变量） | 全仓库 grep |
| 时钟/门控 | 无 sysctl 资源位，恒可访问 | `soc/HPM5300/HPM5301/hpm_sysctl_drv.h` 无 ahb_sram 资源 |
| NOLOAD 的性质 | 不进固件镜像、不参与 CRC/长度校验 | linker 段属性 |

**无 D-Cache**：HPM5301 没有 L1C 实例（`soc/HPM5300/ip/` 下无 `hpm_l1c_regs.h`，SoC 头文件里无 `HPM_L1C`）。所以：

- DMA 缓冲**不需要 cache 维护**（对比 SDK 例程里 `ATTR_ALIGN(HPM_L1C_CACHELINE_SIZE)` 那套是为带 cache 的型号准备的）；
- AHB SRAM 直接就是 DMA 可达内存（无需 `core_local_mem_to_sys_address()` 地址转换，那是 DLM/ILM 才需要的）。

### 2.6 USB 端点预算：够，而且加端点**不涨 RAM**

| 项 | 值 | 证据 |
|---|---|---|
| 硬件端点总数 | EP0~EP15（`ENDPTCTRL[16]`） | `soc/HPM5300/ip/hpm_usb_regs.h:49` |
| DCD 上限 | `USB_SOC_DCD_MAX_ENDPOINT_COUNT = 16` | `hpm_soc_feature.h` |
| 现用 EP | IN：1(DAP) 3(SWO/scope) 4(CDC) 6(CDC-INT) 7(HID) 9(MSC)；OUT：2(DAP) 5(CDC) 8(HID) 10(MSC) | `usb_composite.h` + `usb_composite.c` |
| 空闲 EP 索引 | **11 ~ 15** | 同上 |
| cherryusb 端点表 | `tx_msg[16]` / `rx_msg[16]` / `intf[16]` | `core/usbd_core.c:82-87` |
| DCD 数据竞技场 | `_dcd_data` 4096 B，**按 16 EP × 双向静态分配** | 上一轮 DLM A 档瘦身的实测 |
| → 新增一对 EP 的 RAM 代价 | **0 B**（QHD/QTD 已按最大端点数为每对端点预留） | 同上 |

蓝本：J-Scope 采样器用的 bulk IN `0x83`（`swo_in_ep` + `scope_sampler_tx_complete()` 的「在飞包账本」流控），新通道照抄这套模式即可。

### 2.7 DMA：`dma_mgr` 自动分配，不与现有功能抢通道

- 当前只用 2 个通道：CDC UART 的 RX/TX（`HPM_DMA_SRC_UART3_RX/TX`），由 `dma_mgr_request_resource()` 动态申请（`src/usb/cdc_interface.c`）。
- SPI1 的请求源已就绪：`HPM_DMA_SRC_SPI1_RX = 0x2A`、`HPM_DMA_SRC_SPI1_TX = 0x2B`（SDK `board.h` 里就有 `BOARD_APP_SPI_TX_DMA`）。
- 通道数充裕（`DMA_SOC_CHANNEL_NUM = 32`），且用 `dma_mgr` 申请可避免硬编码冲突。

### 2.8 引脚占用与冲突检查

| 资源 | 探针现用 | 与 SPI 桥 |
|---|---|---|
| DAP（J5 调试口） | PA04 TDO / PA05 TDI / PA06 TCK / PA07 TMS / PA08 nRESET | 无冲突 |
| VCOM（UART3） | PB15 TXD / PB14 RXD（J3[8]/[10]） | 无冲突 |
| 板载 LED | PA10（J3[33]，低有效） | 无冲突（**但别选它当辅助 GPIO**） |
| USER KEY | PA03（不在排针） | 无冲突 |
| VREF ADC | PB10 **仅 akaLinkPro 有**；EVKLite `BOARD_HAS_VREF_ADC = 0` → **PB10 空闲** | 可用作辅助 GPIO（板上标注 SPI_CS1） |
| SPI1 | 未使用 | 本功能占用 PA26~PA31 |

**辅助 GPIO 候选（J3 上、EVKLite 构建下空闲）**：PB10(J3[26])、PB11(J3[13])、PB12(J3[27])、PB13(J3[28])、PA02(J3[7])、PA09(J3[32])、PA00/PA01(J3[36]/[38])、PY00/PY01(J3[29]/[31])。

**默认建议接线**（QSPI 屏，全在 J3）：

```
屏 SCLK  ← J3[23] PA27       屏 CS   ← J3[24] PA26
屏 D0    ← J3[19] PA29       屏 D1   ← J3[21] PA28
屏 D2    ← J3[37] PA30       屏 D3   ← J3[11] PA31
屏 DC    ← J3[13] PB11       屏 RESET← J3[27] PB12
屏 BL    ← J3[28] PB13       GND ← J3[6/9/14/20/25/30/34/39]，3V3 ← J3[1/17]
```

这几个辅助脚**不做死**：协议里由主机按「pad 表索引」配置，固件校验合法性（例如开了 quad 就不允许把 PA30/PA31 再配成辅助 GPIO）。

---

### 2.9 两块目标屏的实际接口需求（协议据此定型）

参照物是用户已有的 ESP32-P4 工程（`E:\esp-idf-wsh\projects\`），它们把两块屏都跑通过：

| 屏 | 接口 | DC/RS | 数据线 | 命令相位 | 像素相位 | 其他 |
|---|---|---|---|---|---|---|
| 天马 2P01 / **AXS15352**（240×296，RGB565） | 4 线 SPI | **有（高=数据）** | 1（只有 MOSI，**没接 MISO**） | 8 bit 命令 | 8 bit 参数（同一 CS 窗口内翻 DC） | RST、BL、**TE 输入** |
| **ST77916**（圆屏 360×360，RGB565 @40 MHz） | **QSPI** | **无** | **4** | **32 bit = opcode `0x02` + 24 bit 地址**（地址高位放面板命令字） | 参数 1 线；**像素用 opcode `0x32` + 4 线** | RST、BL（20 kHz PWM 调光）、**TE 输入** |

出处：

- `spi_lcd_axs15352\main\app_main.c`：`dc_gpio_num = PIN_LCD_DC`、`lcd_cmd_bits = 8`、`lcd_param_bits = 8`、`quad_mode = 0`、`reset_gpio_num`、TE 作为输入 + ISR；`axs15352_init_cmds.h` = 30 条 `{cmd, data, nbytes, delay_ms}`。
- `qspi_lcd_st77916\main\app_main.c` 注释与 `espressif__esp_lcd_st77916\include\esp_lcd_st77916.h`：`ST77916_PANEL_IO_QSPI_CONFIG` → `dc_gpio_num = -1`、`lcd_cmd_bits = 32`、`lcd_param_bits = 8`、`quad_mode = true`；工程注释写明「**0x02 命令帧 + 0x02/1 线参数 + 0x32/4 线像素**」；`st77916_init_cmds_ch32.h` = 192 条命令 / 215 参数字节 / 累计 120 ms 延时。
- 初始化序列数据：`E:\esp-idf-wsh\资料\panel_init\`（`extra_parts\TianMa2p01+AXS15352_SPI_565_*.txt`、`dumps\st77916.json`、`parts\*.json`、`SPEC.md`），步骤类型为 `cmd / delay / delay_us / reset / comment`。

**这三条结论直接决定了协议形状**（原方案已覆盖，此处做一次对账）：

| 屏的需求 | 协议里的对应项 | 结论 |
|---|---|---|
| QSPI：8 bit opcode + 24 bit 地址 + 可变线数数据 | `XFER` 的 `cmd` + `addr_len/addr` + `tcfg.lines` | ✅ 原生支持，无需改 |
| SPI+DC：同一 CS 窗口内「命令(8bit) → 翻 DC → 参数」 | `tcfg.dc_en/dc_level` + `CS_HOLD` + 面板档自动展开 | ✅ 用 `STEP` 步帧自动完成（§4.7） |
| 参数 1 线 / 像素 4 线（同一屏两种相位） | 每帧独立 `tcfg.lines` | ✅ |
| 192 + 30 条初始化步骤，网页逐条下发 | 紧凑 `STEP` 帧（12 B + 参数） | ✅ 新增，见 §4.7 |
| 累计 120 ms 延时（含单条 100/120 ms） | `DELAY` 帧 + **非阻塞**调度 | ✅ 见 §5.3 |
| 复位（低有效脉冲 + 上电等待） | `RESET` 帧（用辅助 RST 脚） | ✅ 新增，见 §4.2 |
| TE 撕裂信号（输入） | `AUX_IN` 帧（带 RSP 回读电平） | ✅ 新增，见 §4.2 |
| 背光（GPIO / 20 kHz PWM 调光） | v1：BL 脚开关；PWM 调光列为后续可选 | ⚠️ 见 §9.2 |
| RGB565 高字节在前 | 与探针无关：探针做**字节透明**转发，字节序由主机负责 | ✅ |

> 结论：**方案不需要推翻任何设计**，只需补两个便利帧（`STEP` / `RESET`）和一个输入帧（`AUX_IN`），以及面板档概念。

---

## 3. 端点决策：新开一对 bulk（推荐），不复用 CDC

| 维度 | 新开 bulk 对（0x8B/0x0B） | 复用 CDC（0x84/0x05） |
|---|---|---|
| 隔离性 | **独占**，与 COM 口 / RTT-over-USB 互不影响 | 与 VCOM、RTT 桥**共用同一条字节流**，需要额外复用/分帧 |
| 与 0x34 桥开关的耦合 | 无 | CDC 桥被 `CMD_BRIDGE` 关掉时 SPI 桥一起断 |
| 报文边界 | 512 B 整包，天然有边界 | 纯字节流，帧边界靠协议自己找 |
| 主循环影响 | 独立回调 + 独立环 | 要和 UART 环、RTT 抢同一条 IN 队列 |
| RAM 代价 | **0 B**（§2.6） | 0 B |
| 描述符改动 | 新增 1 个接口 + 2 个端点（`INTF_NUM`/`USB_CONFIG_SIZE` 是宏，改一处即可） | 无 |
| 主机侧复杂度 | 打开一个接口、两条管道，语义清晰 | 需要与 RTT/VCOM 复用同一条管道，网页侧已经有一套时序假设 |
| 风险 | 配置描述符改动需要回归一次枚举（VID/PID 不变，接口号后移） | 可能干扰现有 RTT/COM 的稳定性 |

**结论：新开一对 bulk。** 代价（一次描述符改动 + 一次枚举回归）远小于复用 CDC 带来的协议耦合与回归面。CDC 端的 VCOM/RTT 是探针现有主力功能，不该被新功能共享管道。

**接口与端点分配**

| 项 | 取值 |
|---|---|
| 新接口号 | HID 之后插入（`SPI_INTF_NUM = HID_INTF_NUM + CONFIG_CHERRYDAP_USE_CUSTOM_HID`），WebUSB/DFU 接口号宏随之顺移 |
| bInterfaceClass | `0xFF`（vendor specific），`bNumEndpoints = 2` |
| OUT 端点 | `0x0B`（EP11 OUT），bulk，512 B（HS） |
| IN 端点 | `0x8B`（EP11 IN），bulk，512 B（HS） |
| 字符串 | 复用现有 iInterface 习惯，可留 0 |

---

## 4. 协议设计

### 4.1 分层

```
       ┌───────────── HID 0x35（控制面，64 B 报文）─────────────┐
       │ 配置（SCLK/模式/线数/CS 策略/DMA 阈值/辅助脚）、状态与统计、  │
       │ 使能/失能、复位、中止                                    │
       └───────────────────────────────────────────────────────┘
       ┌──────── bulk OUT 0x0B（有序帧流）┬ bulk IN 0x8B（应答流）┐
       │ 数据 + 一切与**线序**有关的动作：  │ 读数据 + 每个 RSP 帧的   │
       │ 传输帧、CS 拉低/释放、DC/RESET、  │ 应答（含 seq / 状态）；   │
       │ 延时、保活                       │ 另可异步上报错误事件      │
       └─────────────────────────────────┴───────────────────────┘
```

**为什么线序动作必须走 bulk**：USB 两条管道之间没有顺序保证。如果「CS 拉低」走 HID、「数据」走 bulk，两者的到达顺序无法界定。把参与时序的动作全部放进同一条 bulk OUT 流，探针按 FIFO 顺序执行，顺序性由协议本身保证。

**为什么数据不塞进 HID**：HID 报文只有 64 B，且与 RTT/Scope/RISCV 共用；大块像素数据必须走 bulk。

### 4.2 bulk OUT 帧格式（主机 → 探针）

约束：**一个 USB 包（≤512 B）内可含多帧，但一帧不跨包**。这样每帧的 payload 在物理上连续，DMA 可以直接从环里取，不需要额外搬运。

```
帧头（8 B，小端）
  0: u16 magic = 0x4253      ('S','B')
  2: u8  type                帧类型
  3: u8  flags               标志
  4: u16 seq                 主机分配，应答原样带回（用于请求/应答配对）
  6: u16 len                 本帧 payload 字节数
```

**flags**

| 位 | 名称 | 含义 |
|---|---|---|
| 0 | `RSP` | 要求回一个应答包（bulk IN） |
| 1 | `CS_HOLD` | 本帧结束后**保持 CS 有效**（跨越多次传输的器件） |
| 2 | `CS_OFF` | 本帧结束后释放 CS |
| 3 | `CS_AUX` | 本次用**辅助 CS 线**（默认用硬件 CS0） |
| 4 | `NO_DMA` | 本帧强制走轮询（对照/调试） |
| 5 | `FORCE_DMA` | 本帧强制走 DMA |
| 6-7 | 保留 | 必须为 0 |

**type**

| 值 | 名称 | payload |
|---|---|---|
| 0x01 | `XFER` | 12 B 传输头（见下）+ TX 数据；**通用帧**，一切时序都能表达 |
| 0x02 | `CS` | `u8`：0 = 释放，1 = 拉低（手动 CS，配合 `CS_HOLD`） |
| 0x03 | `GPIO` | `u8 line, u8 level`；line：0=DC，1=RESET，2=AUX_CS，3=BL |
| 0x04 | `DELAY` | `u32` 微秒（**非阻塞**，见 §5.3） |
| 0x05 | `PING` | 空；用来测往返延迟 / 保活（配 `RSP`） |
| 0x06 | `CFG` | 数据面内改运行参数（可选，v1 只支持改 `tx_dma_threshold`） |
| **0x07** | **`STEP`** | **面板初始化步**：`u8 cmd, u8 nparams, u16 delay_ms, params[nparams]`；按当前**面板档**展开成实际时序（§4.7）——网页把 `{cmd,data,n,delay_ms}` 表原样下发 |
| **0x08** | **`RESET`** | `u16 low_ms, u16 post_ms`：把辅助 RST 脚拉到有效电平并保持 `low_ms`，释放后再等 `post_ms`（典型 `10 / 120`）；两步都不阻塞主循环 |
| **0x09** | **`AUX_IN`** | 空（配 `RSP`）：回读辅助**输入**脚（TE 等）电平，应答 payload = `u8` 位图 |

> `STEP` / `RESET` / `AUX_IN` 三个是**便利帧**：用 `XFER`/`GPIO`/`DELAY` 组合也能拼出同样效果，但面板初始化序列动辄 200 条，紧凑帧能把网页侧的代码量和报文量都压下来（§4.7）。

**XFER 的 12 B 传输头**

```
 0: u8  cmd          命令字节（tcfg.cmd_en = 1 时有效）
 1: u8  tcfg         位域：
                       bit1:0 数据相位线数  0=1线 1=2线 2=4线
                       bit2   cmd_en        发送命令相位
                       bit3   addr_en       发送地址相位
                       bit4   addr_quad     地址相位用 2/4 线（否则单线）
                       bit5   dc_en         传输前先设 DC 线
                       bit6   dc_level      DC 电平（1 = 数据，0 = 命令）
                       bit7   token_en      发送 0x69 token（部分屏需要）
 2: u8  addr_len     地址字节数 0~4
 3: u8  dummy        dummy 周期 0=无，1~4
 4: u16 tx_len       数据相位发送字节数
 6: u16 rx_len       数据相位接收字节数
 8: u32 addr         地址（小端；addr_len 决定实际发几个字节，MSB 优先）
12: ...              tx_len 字节的发送数据
```

一次 `XFER` = **一次硬件 SPI 事务 = 一次 CS 窗口**，时序为：

```
CS↓ ─ [cmd] ─ [addr] ─ [dummy] ─ [data(tx/rx, 1/2/4 线)] ─ CS↑
```

（`CS_HOLD` 时末尾不抬 CS，留给后续帧。）

`tx_len` / `rx_len` 的分工与 SDK 的 `spi_trans_mode_t` 对应：都非 0 → `write_read`；只有 tx → `write_only`；只有 rx → `read_only`。

**长度上限**：`len ≤ 512 - 8 = 504`；`tx_len ≤ len - 12`，`rx_len ≤ 504`（收到 IN 包时按 `len` 回读）。

### 4.3 bulk IN 应答格式（探针 → 主机）

```
 0: u16 magic = 0x4253
 2: u8  type     0x81 = RSP（对 RSP 帧的应答）；0x82 = EVT（异步事件/错误）
 3: u8  status   0=OK 1=未使能 2=帧格式错 3=参数越界 4=SPI 超时
                 5=IN 环满（丢弃） 6=DMA 错误 7=设备忙
 4: u16 seq      对应帧的 seq（EVT 时为最后处理的帧）
 6: u16 len      后面跟的读数据字节数（≤ 504）
 8: ...          len 字节读数据
```

- 只有带 `RSP` 的帧才产生应答 → 纯写入流不产生任何 IN 流量，主机可以全速灌数据。
- 帧本身不带 `RSP` 但执行出错时，探针发 `EVT`（同时计数进 HID 统计），主机在空闲时读取即可。

### 4.4 HID 控制面：新增 `CMD 0x35`（SPI 桥）

沿用现有约定（`src/api/api_param.c`）：

- 请求：`req[2] = 0x35`，`req[3] = action`，`req[4..]` = 参数
- 响应：`res[2] = 0x35`，`res[3] = action` 回显，`res[4..7]` = 32 位状态字，`res[8..]` = 附加数据

| action | 名称 | 请求 | 响应 |
|---|---|---|---|
| 0 | `STATUS` | — | `res[4..7]` 状态字（见下）+ `res[8..]` 计数器（见下） |
| 1 | `ENABLE` | `req[4]`：0 关 / 1 开 | `res[4..7]` = 当前开关状态 |
| 2 | `RESET` | — | 清环、复位状态机、清计数器（不动配置） |
| 3 | `SET_CFG` | `req[4..]` = 配置块（≤56 B） | `res[4..7]` = 状态；非法字段被夹取并在 `res[8]` 列出 |
| 4 | `GET_CFG` | — | `res[4..]` = 配置块 |
| 5 | `PIN_CFG` | `req[4]`=line，`req[5]`=pad 索引，`req[6]`=有效电平 | 非法映射返回错误码（例如要 quad 却把 PA30 配成 GPIO） |
| 6 | `ABORT` | — | 丢弃未处理帧与 IN 队列，回到干净状态 |
| 7 | `SET_PROFILE` | `req[4..]` = 面板档块（§4.7） | `res[4..7]` = 状态；非法值夹取 |
| 8 | `GET_PROFILE` | — | `res[4..]` = 面板档块 |

> 说明：CS/GPIO 的**直接**操作**不放** HID，避免与 bulk 流产生顺序歧义；HID 只做配置与状态（§4.1）。

**状态字 `res[4..7]`（u32 小端）**

| 位 | 含义 |
|---|---|
| 0 | 已使能 |
| 1 | 正在处理帧 |
| 2 | CS 当前有效 |
| 3 | IN 侧被流控暂停（等主机取走数据） |
| 4 | OUT 环接近满 |
| 8-15 | 最近一次错误码 |
| 16-31 | 预留 |

**计数器 `res[8..]`**（8 × u32，小端）：`frames_ok`、`frames_err`、`bytes_tx`、`bytes_rx`、`tx_poll_cnt`、`tx_dma_cnt`、`out_ring_overrun`、`in_ring_drop`。

### 4.5 配置块（SET_CFG / GET_CFG）

```
 0: u32 sclk_hz          期望 SCLK（0 = 板级默认 20 MHz）
 4: u8  mode             SPI 模式 0~3（CPOL/CPHA）
 5: u8  bits             数据位宽（v1 固定 8）
 6: u8  cs_policy        0 = GPIO CS（PA26，默认）；1 = 辅助 GPIO CS；2 = 手动（CS 帧控制）；3 = 硬件 CS0
 7: u8  tx_dma_threshold 默认 100；0 = 全轮询；0xFF = 全 DMA
 8: u8  pad_dc           辅助脚 pad 索引（0 = 不用）
 9: u8  pad_rst
10: u8  pad_cs_aux
11: u8  pad_bl
12: u8  pad_active_low   位图：bit0 DC、bit1 RST、bit2 CS、bit3 BL
13: u8  pad_te           辅助**输入**脚（TE 撕裂信号，0 = 不用）
14: u8  flags            bit0 = 随 ENABLE 一起打开；bit1 = 使能时自动清环
15: u8  reserved0
16: u16 out_ring_kb      请求的 OUT 环大小（受 32 KB 总预算夹取）
18: u16 in_ring_kb
20: u16 max_frame_bytes  v1 固定 504
22: ...                  预留（配置块总长 32 B，留出余量）
```

**pad 索引表**（协议内固定，避免主机猜 IOC 编号）：

| 索引 | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 | 13 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 引脚 | 无 | PB11 | PB12 | PB13 | PB10 | PA02 | PA09 | PA00 | PA01 | PY00 | PY01 | PA10 | PA30 | PA31 |
| J3 脚 | — | 13 | 27 | 28 | 26 | 7 | 32 | 36 | 38 | 29 | 31 | 33 | 37 | 11 |

> - PA30/PA31 只有在**没开 quad**时才允许当辅助脚；固件在 `PIN_CFG`/`SET_CFG` 里校验并拒绝。
> - PA09 是板载 TinyUF2 按键脚（按下 + 复位进 DFU），PA10 是板载 LED——两者列在表里只为完整性，**默认不选**。
> - PA00/PA01 是 UART0（ROM ISP / log）——ISP 场景下会被外部串口驱动，作为辅助脚时需自行留意。
> - `pad_te` 是**输入**（上拉/下拉按需配置），用同一张 pad 表；ST77916 与 2P01 都把 TE 引到了排针。

### 4.5.1 面板档块（SET_PROFILE / GET_PROFILE）

```
 0: u8  profile             0 = raw；1 = spi_dcx；2 = qspi（§4.7）
 1: u8  def_lines           档 0 数据相位线数（1/2/4）
 2: u8  dc_active_high      档 1：DC 高 = 数据（AXS15352 为 1）
 3: u8  cs_hold_in_step     档 1：翻 DC 时保持 CS（1 = 复刻 ESP-IDF 行为）
 4: u8  qspi_wr_opcode      档 2：命令写 opcode（默认 0x02）
 5: u8  qspi_color_opcode   像素写 opcode（默认 0x32）
 6: u8  qspi_addr_bytes     地址相位字节数（默认 3）
 7: u8  flags               预留
 8: ...                     预留（面板档块总长 16 B）
```

两块屏的推荐档位（网页侧照此配置即可）：

| 屏 | profile | 关键参数 |
|---|---|---|
| AXS15352 / 天马 2P01 | 1 | `dc_active_high = 1`、`cs_hold_in_step = 1`、`sclk_hz = 20~40 MHz`、`pad_dc = PB11`、`pad_rst = PB12`、`pad_bl = PB13`、`pad_te = PB10` |
| ST77916 | 2 | `qspi_wr_opcode = 0x02`、`qspi_color_opcode = 0x32`、`qspi_addr_bytes = 3`、`sclk_hz = 40 MHz`、`pad_rst`/`pad_bl`/`pad_te` 同上 |


### 4.6 典型时序示例

**A. QSPI 屏写命令（cmd 单线 + 24 位地址 + 四线数据）**

```
OUT: [XFER RSP] seq=1 tcfg{cmd_en,addr_en,lines=4} cmd=0x02 addr_len=3 addr=0x00002C tx_len=2 rx_len=0
     payload: 0x12 0x34            ← 屏侧：CS↓ 0x02 A[23:0] 四线 2 B CS↑
IN : [RSP] seq=1 status=0
```

**B. QSPI 屏读（带 dummy）**

```
OUT: [XFER RSP] seq=2 tcfg{cmd_en,addr_en,lines=4,dummy} cmd=0x03 addr_len=3 addr=0x000004 dummy=2 rx_len=4
IN : [RSP] seq=2 status=0 payload: 4 B 读值
```

**C. 经典 SPI 屏（ST7789 类，DC 控制）**

```
OUT: [XFER] seq=3 flags=RSP tcfg{dc_en, dc_level=0} cmd=0 tx_len=1  payload: 0x2A   ← DC=0 写命令
OUT: [XFER] seq=4 flags=RSP tcfg{dc_en, dc_level=1} cmd=0 tx_len=4  payload: 00 00 01 3F
```

**D. 批量刷图（150 KB 像素，主机切片）**

```
每片：XFER seq++ tcfg{cmd_en=1,addr_en=1,lines=4} cmd=0x02 addr=RAMWR tx_len=480 payload=480 B
（只有最后一片带 RSP，用来确认整批完成；中间片不带 RSP，不产生 IN 流量）
```

**E. SPI 传感器读寄存器（BMI270 类）**

```
OUT: [XFER RSP] tcfg{cmd_en=1} cmd=0x80|reg tx_len=0 rx_len=1 dummy=1
```

**F. SPI NOR（W25Q 类）**

```
读： cmd=0x03 addr_len=3 addr=offset rx_len=N
写： cmd=0x02 addr_len=3 addr=offset tx_len=N
```

---

### 4.7 面板档（profile）与紧凑步帧 `STEP`

网页侧要下发的是**面板初始化表**（形如 `{cmd, data[n], n, delay_ms}`，见 §2.9），探针要「一条一条执行」。为了避免每条都手写 20 字节的 `XFER` 头，引入**面板档**：HID 配置里选一个档，`STEP` 帧就按该档自动展开。

| 档 | 名称 | `STEP` 展开成什么 | 适用 |
|---|---|---|---|
| 0 | `raw` | 一次 `XFER`：`cmd` + `params`，均按 `def_lines`；DC 未用 | 普通 SPI 器件（传感器、NOR） |
| 1 | `spi_dcx` | **同一个 CS 窗口内**：CS↓ → DC=命令电平 → 发 8 bit `cmd` → **翻 DC=数据电平** → 发 `params` → CS↑（`nparams = 0` 时只发命令） | **AXS15352 / 天马 2P01**（4 线 SPI + RS） |
| 2 | `qspi` | 一次 `XFER`：`cmd = qspi_wr_opcode`（默认 `0x02`，1 线）→ 地址相位 24 bit = **`面板命令字 << 8`（命令字在 bits[23:16]，09-30 订正）** → 数据相位 `params`（1 线） | **ST77916** 等 QSPI 屏的命令阶段 |

档位参数（HID 配置里给）：

| 参数 | 默认 | 说明 |
|---|---|---|
| `profile` | 0 | 0/1/2 见上 |
| `def_lines` | 1 | 档 0 的数据相位线数（1/2/4） |
| `dc_line` | 见 §2.8 默认表 | 档 1 用的 DC 辅助脚（0 = 未配，则档 1 退化为档 0） |
| `dc_active_high` | 1 | DC 高=数据（AXS15352 就是如此） |
| `qspi_wr_opcode` | `0x02` | 档 2 的命令写 opcode |
| `qspi_color_opcode` | `0x32` | 像素写 opcode（主机写像素时直接用它发 `XFER`） |
| `qspi_addr_bytes` | 3 | 地址相位字节数（QSPI 屏惯例 3） |
| `cs_hold_in_step` | 1 | 档 1 是否在翻转 DC 时保持 CS（ESP-IDF 行为 = 保持） |

**两屏的对应关系（验证协议够用）**

```
AXS15352（档 1）:
  表行 {0xCE, [0x5A,0xA5], 2, 0}
    → STEP{ cmd=0xCE, nparams=2, delay_ms=0, params=[5A A5] }
    → 线上: CS↓ DC=0 发 CE DC=1 发 5A A5 CS↑                （一个 CS 窗口）
  表行 {0x11, [], 0, 100}
    → STEP{ cmd=0x11, nparams=0, delay_ms=100 }             （命令 + 100 ms 非阻塞延时）

ST77916（档 2）:
  表行 0xF0 + [0x28]
    → STEP{ cmd=0xF0, nparams=1, delay_ms=0, params=[28] }
    → 线上: CS↓ 02 | 00 F0 00 | 28 | CS↑                    （opcode + 24 bit 地址 + 1 线参数；09-30 订正：命令字在第二字节）
  像素（整屏 360×360×2 = 259200 B，主机切片）:
    → XFER{ cmd=0x32, addr_len=3, addr=0x002C00, tcfg.lines=4, tx_len=480 } × 540 片
```

**报文量核算**：AXS15352 的 30 条 ≈ 30×(8+4+参数) ≈ 0.8 KB；ST77916 的 192 条 ≈ 3 KB；两者合计不到 10 个 512 B 包，毫秒级下发完毕。

---

## 5. 固件架构

### 5.1 模块与挂载点

新增文件（`firmware/application_5301/src/spi_bridge/`）：

| 文件 | 职责 |
|---|---|
| `spi_bridge_proto.h` | **协议唯一真源**：帧头、type、flags、传输头、HID action、pad 表、错误码 |
| `spi_bridge.c/.h` | 状态机、帧解析、传输执行（轮询/DMA）、统计；导出 `spi_bridge_init/poll/enable/...` |
| `spi_bridge_usb.c` | bulk IN/OUT 端点回调 + 环读写 + 流控（照抄 scope 的「在飞账本」模式） |

改动的既有文件：

| 文件 | 改动 |
|---|---|
| `src/usb/usb_composite.h` | 新增 `SPI_IN_EP 0x8B` / `SPI_OUT_EP 0x0B` 宏 |
| `src/usb/usb_composite.c` | 描述符加接口 + 2 端点；`INTF_NUM`/`USB_CONFIG_SIZE`/接口号宏顺移；`usbd_add_endpoint`；IN/OUT 回调；复位时清环 |
| `src/api/api_param.c` | 新增 `CMD_SPI 0x35` 分支（动作见 §4.4） |
| `src/main.c` | 主循环加 `spi_bridge_poll();` |
| `boards/hpm5301evklite/pinmux.c` | 新增 `init_spi1_pins_quad()`：PA26~PA29 + 可选 PA30/PA31；**不要**照抄 SDK 的 `LOOP_BACK_MASK` 位（那是回环测试用的） |
| `boards/hpm5301evklite/board.h` | `BOARD_SPI_BRIDGE_*` 默认参数（SCLK、辅助脚、阈值） |
| `firmware/application_5301/CMakeLists.txt` | 新增源文件 |

主循环（`src/main.c`）：

```c
while (1) {
    chry_dap_handle();
    if (usb2uart_bridge_enabled) chry_dap_usb2uart_handle();
    api_param_poll();
    dfu_key_poll();
    rtt_bridge_poll();
    scope_sampler_poll();
    riscv_svc_poll();
    spi_bridge_poll();      /* 未使能 → 一条分支返回；使能 → 处理若干帧后返回 */
}
```

**延迟预算**：`spi_bridge_poll()` 每次最多处理有限的帧/字节（例如「≤ 4 帧或 ≤ 2 KB，先到为准」），避免单次占用主循环过久影响 DAP 与 Scope。剩余帧下一轮继续——OUT 环足够大，不会因此丢数据。

**未使能时的代价**：一个 `if (!enabled) return;`。bulk OUT 端点也**不再重新 arm**（主机侧写入会 NAK，天然背压），所以完全闲置时不产生任何中断负载。

### 5.2 AHB SRAM 分区

| 区域 | 大小 | 用途 |
|---|---|---|
| OUT 环 | 16 KB | 32 × 512 B 槽；帧按包存放，payload 天然连续 → DMA 源可直接指进环 |
| IN 环 | 8 KB | 16 × 512 B 槽；应答包排队 |
| 线性暂存 | 2 KB | 预留（跨槽拼接、非连续 DMA 源、需要字节重排时用；v1 一般不用） |
| 预留 | 6 KB | 以后放大帧 / 双缓冲 / 第二设备 |
| **合计** | **32 KB** | |

放在 `.ahb_sram`（NOLOAD，不进镜像、不影响 DFU 长度与 CRC）。DLM 侧只花元数据：`len[32] + state[32]`（OUT）、`len[16] + state[16]`（IN）、配置与统计 ≈ **300 B 上下**（现 DLM 106592/130304 B = 81.8%，余量充足）。

### 5.3 关键实现要点

1. **引脚复用只在使能时做**：默认（未使能）固件行为完全不变，不会影响 DAP/VCOM/RTT/Scope。
2. **每帧一次事务**：轮询用 `spi_transfer()`；DMA 用 `spi_setup_dma_transfer()` + `dma_mgr` 申请的 `HPM_DMA_SRC_SPI1_TX` 通道，等 `dma_check_transfer_status()` 的 TC 后 `spi_wait_for_idle_status()` 再收尾 CS。
3. **CS 策略**（实现时定稿）：默认 **`cs_policy = 0`，PA26 作 GPIO CS**，由固件按帧拉低/释放；
   `CS_HOLD`/`CS_OFF`/`CS` 帧支持手动跨帧保持；`cs_policy = 1` 改用辅助 GPIO 当 CS（多器件）；
   `cs_policy = 2` 完全手动；`cs_policy = 3` 才是硬件 CS0（每帧自动、时序由 TIMING 寄存器管，
   但**不支持**面板档 1 的「一个 CS 窗口内翻 DC」，那种情况下会返回 `RANGE`）。
   为什么默认不用硬件 CS：面板初始化必须「CS↓ → 命令 → 翻 DC → 参数 → CS↑」在**同一个 CS 窗口**
   里完成，硬件 CS 每次 `spi_transfer()` 都会自己收尾。
4. **RX 轮询**：`spi_transfer()` 内部逐次读 RX FIFO（8 深）；全双工时驱动已按「写一次、收一次」交错，不会溢出（`drivers/src/hpm_spi_drv.c:211`）。
5. **DMA 阈值判定**：看 `tx_len`（数据相位发送字节数），不看整帧长度。
6. **`data_merge`**：v1 用 0（与 SDK 阻塞 API 语义一致，字节精确）；若上板实测轮询路径成瓶颈，再考虑改用 DataMerge（8-bit 下每次 DATA 读写 4 字节，FIFO 一项抵 4 字节）配合自写 FIFO 循环。
7. **超时**：每帧都有硬超时（`spi_wait_for_idle_status` 重试上限），超时 → 状态回 4、计数器 +1、必要时复位 SPI 控制器（`spi_reset_all`），绝不挂死主循环。
8. **不做 cache 维护**（无 D-Cache），也不需要地址转换（AHB SRAM 是系统地址）。
9. **延时非阻塞**：`DELAY` / `STEP.delay_ms` / `RESET` 都只登记「下一个允许执行时刻」（`mchtmr` 计时），`spi_bridge_poll()` 到点才继续处理后续帧。面板序列里有 100/120 ms 的等待，**绝不能在主循环里忙等**（那会让 DAP/RTT/Scope 停摆 100 ms）。等待期间 OUT 环继续被 USB 回调填充，不丢数据。
10. **面板档执行**（§4.7）：档 1 的一次 `STEP` 内部是「同一个 CS 窗口里的两次 SPI 传输 + 一次 DC 翻转」，用 GPIO CS + `board_write_spi_cs` 风格的软件 CS 实现最稳（硬件 CS 无法在一次事务中间翻 DC）；档 2 直接映射到一次 `XFER`。
11. **SCLK 配置**：`clock_set_source_divider(clock_spi1, src, div)` 显式选源（PLL0 三路 720/600/400 MHz 可用），再由 `spi_master_timing_init()` 分频到目标 `sclk_hz`；改频前先 `spi_wait_for_idle_status()`，改频后校验实际频率并回读给主机。

### 5.4 P1 实施记录（2026-09-28，已提交 06c0928）

**资源占用（`build_dfu_evklite`，与改动前对比）**

| 区域 | 改动前 | 改动后 | 说明 |
|---|---|---|---|
| FLASH | 110080 B (12.11%) | **122312 B (13.45%)** | 代码 + 描述符 +12.2 KB |
| ILM | 31352 B (23.92%) | 31352 B | 不变 |
| DLM | 106592 B (81.80%) | **106592 B (81.80%)** | **零增长**（见下） |
| AHB_SRAM | 0 B | **24784 B (75.63%)** | OUT 环 16 KB + IN 环 8 KB + 状态 208 B |

**踩坑：把状态放 `.bss` 会让 DLM 涨整整 2048 B**
第一版把桥的状态（环游标、配置、统计，一共才 ~144 B）放在普通 `.bss`，DLM 立刻从 106592 变成
108640。原因不是状态本身，而是 `.noncacheable` 段带 **2 KB 对齐**：往它前面的 `.bss` 里加一百多
字节，就把这 2 KB 对齐空洞一起算进了 DLM 用量。把全部状态搬进 `.ahb_sram`（连同两个环）之后
DLM 精确回到 106592 B。**这条对以后所有往 DLM 加小变量的人都适用。**

**另外两处实现决策**
- `init_spi1_bridge_pins()` 不复制 SDK `init_spi1_pins()` 里的 `LOOP_BACK` 位（那是 SPI 自环
  测试用的，正常通信会把自己的输出环回进输入），并给 SCLK/IO 脚配了 fast slew + 最大驱动。
- 桥的模块门控：`BOARD_HAS_SPI_BRIDGE`（EVKLite = 1，akaLinkPro = 0）。置 0 时整个模块编成
  空实现、描述符里也不挂这个接口（实测：FLASH 110144 B、DLM 106592 B、AHB_SRAM 0），
  所以 akaLinkPro 构建的枚举与资源占用与改动前一致。

**SCLK 的现实边界（P2 上板实测）**：SPI 的 SCLK = 模块时钟 / N，N 必须整除且为偶数。PLL0 的
720/600/400 MHz 能给出 20/40/60/75/100 MHz，但 **80 MHz 需要模块时钟落在 160/320/480 MHz**，
这三个都用整数分频出不来 ⇒ 固件会挑最接近的档并把**实际值**回给主机（`GET_CFG`/`STATUS` 的
`s_actual_sclk`）。若 PLL1 恰好是 480 MHz 且未被 USB 占用，80 MHz 就有解。

> ⚠️ **上面这两段已被 §5.5 的实测推翻/修正**：80 MHz 现在能配出来（模块时钟来自 PLL1
> 的分频），但用一根跳线做回环跑不动；而且**绝不能拿 PLL0 原频 720 MHz 当模块时钟**。

---

### 5.5 P1 上板验证（2026-09-29，HPM5301EVKLite + Kingst LA）

P1 代码写完后一直没上板。这次接上探针实测，**验收标准全部达成**，同时挖出 4 个只有硬件才
暴露得出来的坑 —— 全部已修并复测。

**验收结果**

| 项目 | 结果 |
|---|---|
| `loop`（J3[19]↔[21] 跳线回环，1/2/32/99/100/101/256/492 B，单线） | **16/16 PASS** |
| 边界长度 3/255/257/491/492/493 B | PASS |
| SPI 模式 0/1/2/3（CPOL/CPHA 四种组合） | 全部 PASS |
| SCLK 档位（256 B 回环） | 20 / 40 / 60 / **75** MHz PASS；**80 / 100 MHz FAIL** |
| `frames`（PING / DELAY / AUX_IN / CS） | PASS（DELAY 2000 µs 实测墙钟 2.1 ms） |
| 计数器 | `bytes_tx == bytes_rx`，`frames_err=0`，`out_overrun=0`，`in_drop=0` |
| 资源 | FLASH 13.85%、DLM 106592 B（**零增长**）、AHB_SRAM 24848 B |

80/100 MHz 的失败是**跳线本身的物理极限**（一根杜邦线跑 80 MHz 方波），不是固件问题；
真要定这两个档，得接真从器件或用 LA 看波形质量。

**坑 1：MS OS 2.0 描述符集超了 EP0 请求缓冲 → 整个复合设备 Code 10**

新接口要能被 libusb/WebUSB 打开，就得在 MS OS 2.0 描述符集里给它加一条 WinUSB
function subset（160 B）。加完之后描述符集从 490 B 变成 650 B，**超过
`CONFIG_USBDEV_REQUEST_BUFFER_LEN = 512`**，而 CherryUSB 对超出缓冲的 vendor 请求是
**直接 STALL**（`usbd_core.c`: "Request buffer too small"）。后果不是"这个接口打不开"，
而是**整个复合设备启动失败**：设备管理器黄叹号、Code 10、HID 与 bulk 全部消失，
拔插/删节点重扫都没用。

- 修：`CONFIG_USBDEV_REQUEST_BUFFER_LEN` 512 → **768**；
- 防回归：`usb_composite.c` 里加编译期断言
  `#if (USBD_WINUSB_DESC_SET_LEN > CONFIG_USBDEV_REQUEST_BUFFER_LEN) #error`。

**坑 2：SPI1 模块时钟顶到 720 MHz → SPI 一次都不移位**

原来的选频逻辑是把 PLL0 的 720/600/400 MHz 逐个试、取能整除出目标 SCLK 的那个，20 MHz
落在 720/36 上。实测后果：**事务"完成"但一个 bit 都没移**——LA 上 CS 拉了 614 µs、
MOSI 只有几个毛刺、**SCLK 全程不动**；`spi_write_read_data` 卡满 5000 次重试后超时，
之后每一笔都在 `spi_is_active()` 上直接 BUSY。

- 修：改成在「时钟源 × 整数分频」里搜**能整除出目标 SCLK 的最小模块时钟**（上限 240 MHz）。
  20 MHz 现在落在 40 MHz 模块时钟 / N=2 上，事务立刻正常。
- 另加 `cfg.module_clk_hz`（配置块偏移 24）作为在线扫频旋钮，0 = 自动。

**坑 3（最隐蔽）：SCLK 焊盘少了 `LOOP_BACK` → RX 移位不打拍，回读恒为"空闲电平"**

`IOC_PAD_FUNC_CTL_LOOP_BACK_MASK` 这个名字极具误导性，它在 HPM5300 的 IOC 里官方说明是
**"force input on"**。SDK 的 `hpm5301evklite/init_spi1_pins()` 把它**只加在 SCLK 上**
（MISO/MOSI 都没有）—— 因为 SPI 主机的**接收移位是靠 SCLK 这条输入通路回来打拍的**。
P1 当初以为是"自环测试专用"而特意删掉，于是出现了最迷惑人的现象：

- LA 波形**完全正确**：8 字节 = 64 拍 SCLK（周期 50 ns = 20 MHz）、MOSI 数据对、
  下降沿采样 `MOSI = MISO = 发送数据`**逐字节相同**；
- 寄存器**完全正常**：`TRANSCTRL` 里 wcount/rcount 对、`TIMING` 分频对、`SDK status=0`、
  两个 FIFO 都空（驱动确实读走了 N 个字节）；
- 但 **RX FIFO 里读出来是恒定的空闲电平**：CPOL=0 时全 `00`、CPOL=1 时全 `FF`。

"全 00 / 全 FF 跟着 CPOL 变"这一点是判据：说明一个 bit 都没移进来，而不是相位错位。
加回这一位后回环立通。

- 修：`init_spi1_bridge_pins()` 里 PA27 加 `IOC_PAD_FUNC_CTL_LOOP_BACK_MASK`，与 SDK 逐字一致；
  **不要**加在 PA28/PA29 上（实测那样会把波形搞坏：len=4 只发 8 拍、MOSI 几乎不动）。
- 调试手段（都留在固件里了，HID action 10/11/12）：
  `dbg` 读 SPI 寄存器现场快照、`pintest` 把 MOSI/MISO 当 GPIO 验跳线通断、
  `wiggle` 在 SCLK/CS/MOSI 上发慢方波给 LA 验接线。这三样把"线没插好 / 焊盘坏 /
  控制器没出时钟 / 只是收尾没清"四种情况彻底分开了。

**坑 4：同一 USB 包里的 DELAY 挡不住后面的帧，且 mchtmr 频率取成了 0**

`sb_delay_pending()` 原来只在 `spi_bridge_poll()` 入口检查，而 `sb_process_packets()`
会把一个包里后面的帧全执行掉 —— 面板初始化序列（"发完命令等 120 ms 再发下一条"，且多条
STEP 常挤在同一个包里）正好会踩。另外 `clock_get_frequency(clock_mchtmr0)` 在本 SoC 的
频率表里没有兜底、返回 0，导致所有延时都被夹成 1 个 tick（实测 500 ms 只等出 50 ms）。

- 修：① 每执行完一帧就检查 `sb_delay_pending()/s_rst_state`，有等待就立刻让出主循环；
  ② mchtmr 频率硬编码 24 MHz（与 scope/rtt/riscv 三个模块一致）。
  复测：2000/20000/50000/200000 µs 实测 2.60/20.66/50.59/200.50 ms（多出的 ~0.5 ms 是主机往返）。

**其它一并修掉的小问题**（都是上板前 review 或上板时发现的）

- 状态字 bit8..15 原先把 `frames_err` 的**计数**当"最近错误码"报，改成真正的 `last_err`；
- `sb_out_kick()` / `sb_in_kick()` 现在整个"查在飞 + 武装"过程关中断：它们既被主循环调、
  也被完成回调（ISR）调，中间被打断会重复 `usbd_ep_start_*`，而 DWC2 端口层对忙端点
  再来一笔是**静默顶掉**（回调永不来）→ 在飞标志永久停在 1；
- 事务失败后置 `s_spi_need_reset`，下一笔前先复位控制器（否则 SPIACTIVE 卡住，
  后续全级联成 BUSY，长度扫描从第二条起就什么都测不出来）；
- `pad_active_low` 默认值改 **0x06**（RST/CS 低有效）：原来默认 0 会让 RST 上电就被拉在
  低电平（一直摁住面板复位），而 CS 的有效电平又与文档说的不一致；
- `XFER` 的读数据**即使事务报错也照样带回**（原来只在 `SB_OK` 时才回）——
  "回读全 0 / 全 FF / 数据正确"是调 SPI 时唯一能区分"线没通"和"只是收尾没清"的线索。

**测试工具（`script_test/spi_bridge_test.py`）新增**

`dbg`（寄存器快照）、`pintest`（跳线通断）、`wiggle`（慢方波给 LA）、
`cfg --module-clk`（在线扫模块时钟）。另外 `loop` 默认只跑**单线**：双线/四线相位下
DAT0/DAT1 都是推挽输出，短接等于两个输出对打（我们的脚还配了最大驱动 DS=4），
多线相位要用 LA 看波形或接真从器件。

---

### 5.6 P2 实施记录：TX DMA + 阈值实测（2026-09-29）

**实现**（`sb_spi_xfer()` 现在有两条路径）

- **轮询**：还是 SDK 的 `spi_transfer()`。
- **DMA**：自己拼序列 —— `spi_control_init` → 武装并启动 DMA 通道 → 开 SPI 的 TX DMA 请求
  → 写 ADDR/CMD 触发 → **RX 侧仍由 CPU 轮询** → 等 DMA TC → 关请求、收尾。
  顺序照 SDK 头文件的原话："disable the SPI TX DMA request before configuring and enabling
  the DMA channels, and then start the SPI transfer"。
- 通道由 `dma_mgr` 动态申请（`HPM_DMA_SRC_SPI1_TX`，与 CDC 的 UART3 RX/TX 同一套，
  不硬编码）；**申请失败就 `s_dma_ok=0`，整条桥静默退回轮询**，不影响功能。
- 源地址是 OUT 环的槽（AHB SRAM）：HPM5301 没有 L1C ⇒ 免 cache 维护、免地址转换。
- 阈值：`cfg.tx_dma_threshold` 看 `tx_len`（0=全轮询、0xFF=全 DMA），
  单帧可用 `NO_DMA`/`FORCE_DMA` 覆盖；`STATUS` 的 `tx_poll_cnt`/`tx_dma_cnt` 分开计数，
  另外新增 `res[44..47] = last_ticks`（上一笔事务耗时，mchtmr 24 MHz tick），
  给阈值定档用 —— 它只量"一次事务从开始到收尾"，不含主机往返，比墙钟干净。

**实测（跳线回环，`bench` 与 `tools/spi_writeonly_bench.py`）**

全双工（`write_read_together`，收发等长）：

| SCLK | len | 轮询 µs | DMA µs | 倍率 |
|---|---|---|---|---|
| 20 MHz | 492 | 198.8 | 203.7 | 0.98× |
| 40 MHz | 256 | 59.1 | 57.0 | 1.04× |
| 40 MHz | 492 | 112.2 | 104.3 | 1.08× |
| 75 MHz | 128 | 31.3 | 26.3 | 1.19× |
| 75 MHz | 492 | 116.2 | 84.0 | 1.38× |

只写（面板刷像素那条路，CPU 在 DMA 期间不插手）：

| SCLK | len | 轮询 µs | DMA µs | 倍率 | 线速理论值 µs |
|---|---|---|---|---|---|
| 20 MHz | 492 | 200.8 | 201.8 | 1.00× | 196.8 |
| 75 MHz | 256 | 37.6 | 35.0 | 1.08× | 27.3 |
| 75 MHz | 492 | 69.0 | 61.9 | 1.12× | 52.5 |

**结论与阈值定档**

1. **两条路径都贴着 SPI 线速**（20 MHz 只写 492 B：200.8 µs vs 理论 196.8 µs，差 2%）。
   瓶颈是线速 + 每笔事务的固定开销，**不是 CPU**——和 DAP 那条路的结论同源。
2. DMA 的固定开销约 **1~4.5 µs**（配地址/长度/使能/等 TC/收尾）；边际成本和轮询一样。
   所以它**低时钟下略亏（1~5%）、高时钟大帧下反超**：40 MHz 约 200 B 交叉，
   75 MHz 约 128 B 交叉，75 MHz/492 B 时快 12%（只写）~38%（全双工）。
3. **默认阈值保持 100**（用户最初的要求），代价是 20 MHz 下小帧多花几个百分点；
   主机可以按 `GET_CFG` 回的**实际 SCLK** 自己调（`SET_CFG` 的 `tx_dma_threshold`）。
4. 全双工下 DMA 收益更明显，是因为 RX 侧两条路都要 CPU 逐字节收 FIFO，
   DMA 至少把 TX 那半边的逐字节开销省掉了。
5. 想让 DMA 真正"把主循环让出来"，得把事务做成**异步**（启动后先返回、下一轮再收尾）——
   现在 DMA 路径仍在 `sb_dma_tx_wait()` 里自旋等 TC。**列为 P2 之后的可选项**，
   真要做得连 RX 一起上 DMA。

**P2 验收**：101/256/492 B 在 `NO_DMA` 与 `FORCE_DMA` 两种覆盖下回环**全部通过**
（`loop --lens 4,8,32,101,256,492` → 12/12），完整 `loop` 464 帧零错，
`tx_poll=225 / tx_dma=239` 分开计数、`out_overrun=0`、`in_drop=0`。
资源：FLASH 127120 B (13.98%)、**DLM 106592 B 仍为零增长**、AHB_SRAM 24872 B。

---

### 5.8 P4 上板验收：天马 2P01 / AXS15352 真屏点亮（2026-09-29）

**结论：屏点亮了，颜色正确，75 MHz 无花屏。** 接线（全在 J3）：`SCLK=J3[23]`、`MOSI=J3[19]`、
`CS=J3[24]`、`DC=J3[13]`、`RST=J3[27]`、`BL=J3[28]`、`TE=J3[26]`、
屏的 `VCI`/`VDDI` 接 3V3、`LEDA/LEDK` 单独供背光（**这块屏的背光是裸 LED，不是使能脚**）。

**跑通配方（照抄即可）**：

```
profile=1 (spi_dcx)  dc_active_high=1  cs_hold_in_step=1  cs_policy=0  sclk=40~75 MHz
RESET{low_ms=10, post_ms=120}
STEP{0x36, [0x08]}        ← MADCTL：★ BGR=1（这块屏的色序）
STEP{0x3A, [0x55]}        ← COLMOD：★ RGB565/16bpp（厂家表里没有，必须补！）
…厂家 30 条…              ← 0xCE 5A A5 开头，含 0x11 + Delay(100ms) + 0x29
STEP{0x2A, [00 00 00 EF]} ← 列 0..239
STEP{0x2B, [00 00 01 27]} ← 行 0..295
XFER{dc_en, dc_level=0, tx=0x2C}  flags=CS_HOLD   ← RAMWR 是"命令"，DC 要低
XFER{dc_en, dc_level=1, tx=480B}  flags=CS_HOLD   ← 像素是"数据"，DC 要高；CS 一直不抬
…（重复）…                        flags=0          ← 最后一片释放 CS
```

**三个坑（都是"屏黑"级别的）**

1. **厂家初始化表里没有 `0x36`/`0x3A`** —— ESP-IDF 的 `esp_lcd_st77916` 组件是在
   vendor 序列**之前**自己补发 `MADCTL` 与 `COLMOD` 的。我们第一版直接照发厂家 30 条，
   屏**全黑**；补上这两条立刻点亮。
2. **`COLMOD` 缺失会让屏按未知像素格式收数据**；**`MADCTL` 的 bit3（BGR）缺失会让红蓝互换**
   （实测：`0x36=0x00` 时红填充显示成蓝，改成 `0x08` 才正确）。
   ESP32-P4 那份工程的注释里就写着"颜色不对先换 rgb_ele_order 为 BGR"。
3. **背光是裸 LED**（面板脚 `LEDA` 阳极 / `LEDK` 阴极），不是一根"BL 使能"脚 ——
   指望 GPIO 直驱往往不够亮，要按 LED 的做法供（限流/驱动）。本测试里背光是用户单独接的。

**吞吐实测（整屏 240×296×2 = 142 KB）**

| SCLK | 耗时 | 吞吐 | 占线速 | 备注 |
|---|---|---|---|---|
| 40 MHz | 32.1 ms | **4.22 MB/s** | **88%** | 几乎贴线速 |
| 60 MHz | 29.8 ms | 4.54 MB/s | 63% | 已到主机侧天花板 |
| 75 MHz | 28.8 ms | 4.70 MB/s | 53% | **无花屏/闪点** ✓ |

- **主机必须管道化**：每行（480 B）都带 `RSP` 的话，吞吐直接砍半（2.08 MB/s）——
  每帧等一次 USB 往返把整条流水线串行化了。正确姿势是**只有最后一片带 `RSP`**，
  中间帧靠设备 NAK 做背压。
- **40 MHz 以上吞吐饱和在 ~4.7 MB/s**，瓶颈不再是 SPI 线速，而是**每帧固定开销**
  （本协议"一帧不跨 USB 包" ⇒ 上限 480 像素/帧 ⇒ 296 帧，Python+libusb 每帧约 40 µs）。
  换 WebUSB / 原生客户端还能再快；探针侧不是瓶颈。
- ⚠️ **不要把多帧拼成一个 bulk 写**：协议要求一帧不跨包，拼起来会被 512 B 边界切断，
  设备判 `BAD_FRAME`（实测一次 `err=290`）。**一帧一次写**。

验收工具：`tools/panel_axs15352.py`（完整初始化 + 刷图）、`tools/panel_fill.py`
（`--madctl/--color/--sclk/--pipeline` 四个旋钮，用来试色序/时钟）。

---

### 5.7 P3 上板验证：手动 CS / CS_HOLD / 辅助脚 / 非阻塞延时（2026-09-29）

P3 的功能（辅助 GPIO、DC、RESET、AUX_IN、手动 CS、非阻塞 DELAY）在 P1 就一起写完了，
这里补上板验证。功能项用**状态字 bit2（CS 当前有效）**验逻辑（不用动 LA 接线），
再用 LA 取物理波形。

| 场景 | 期望 | 实测 |
|---|---|---|
| 自动 CS + `CS_HOLD` ×2 帧 | CS 一直有效 | `CS=1 / CS=1` ✓ |
| 之后一帧不带 `CS_HOLD` | CS 释放 | `CS=0` ✓ |
| 手动 CS（`cs_policy=2`）下普通 XFER | 不动 CS | `CS=0` ✓ |
| 手动 CS：`CS(1)` → 2 帧 → `CS(0)` | 窗口由 CS 帧界定 | `1 / 1 / 1 / 0` ✓ |
| `GPIO` 帧写 DC/BL | 状态 OK | ✓ |
| `RESET` 帧（low 2 ms + post 5 ms） | 非阻塞，立即回 ACK | ACK 0.1 ms 返回 ✓ |
| `AUX_IN`（TE 输入脚） | 回读位图 | `data=01` ✓ |
| 200 ms `DELAY` 期间 HID 还能应答 | 主循环没被堵死 | 20 ms 内 6 次 STATUS 往返 ✓ |

LA 物理波形（`captures/cshold.csv`）：565 µs 窗口内 **CH0(CS) 只有 1 个下降沿、全程不再抬起**，
同窗口 CH4 上是 20.6 MHz 的 SCLK —— 三帧确实共用一个连续 CS 窗口。

**LA 逐脚复验（CH0=DC/PB11、CH1=RST/PB12、CH2=BL/PB13、CH3=CS/PA26）**

- **profile 1 的 STEP**（`cmd=0xCE` + `5A A5`）：`CS↓` → **DC 在窗口内翻高** → `CS↑`，
  实测 `t=0 CS↓ / t=25.18 µs DC↑ / t=31.71 µs CS↑` —— 一个 CS 窗口内完成"命令→翻 DC→参数"，
  正是 AXS15352 要的时序（也是默认走 GPIO CS 而不是硬件 CS 的原因）。
- **`RESET` 帧**：请求 `low_ms=2 / post_ms=5`，实测 RST 低电平 **2009.2 µs** ✓
  （修 bug 前只有 **9.75 µs**，见下）。
- **`GPIO` 帧**：BL 开→关在 LA 上可见 ✓。
- **`AUX_IN`（TE 输入）**：TE 默认脚是 `PB10 = J3[26]`（板上丝印 **SPI_CS1**）。
  实时轮询读数：悬空 = `1`（内部上拉）→ 把 **J3[25](GND) 与 J3[26] 短接** = `0` →
  松开 = `1` → 再短接 = `0`，**两次都跟着外部电平变** ✓。
  （顺带记一笔：`PIN_CFG` 的 line 号里 **TE 是 4**、`CS_AUX` 才是 2，别写混；
  另外 PA02/PA09/PA00/PA01 这几根脚在这块板上悬空读出来是 0，不适合当"上拉默认高"的输入脚。）

**这里又抓到一个真 bug：`RESET` 的 `low_ms` 被当成微秒用了**

```c
s_rst_state = 1U;
sb_delay_us(rd_u16(pl));        /* ← low_ms 直接喂给了"微秒"接口 */
```
协议里 `low_ms` 是**毫秒**（释放后的等待那行写对了：`sb_delay_us(post_ms * 1000U)`），
所以请求 2 ms 实际只低了 **2 µs**（LA 实测 9.75 µs，含循环开销）。**差 1000 倍**，
而症状是"面板复位不了、屏不亮"这种最难查的。修：`sb_delay_us((uint32_t)rd_u16(pl) * 1000U)`，
复测 2009.2 µs ✓。

**这次挖出的环形缓冲坑：复位会把"复位前收到的包"复活成新帧**

症状：`enable` 之后的**第一帧**行为异常 —— 实测收到一条 `len=1 / data=01` 的应答，
而那是**十几分钟前** `loop --lens 1` 的帧（连 seq 都是 1，所以一开始完全看不出来）；
副作用是它顺手把 `CS_HOLD` 保持的 CS 给释放了，于是"第一帧 CS_HOLD 不生效、后面都正常"。

根因在复位那几行：原来把 `out_w / out_r / out_used` **和** `out_inflight` 一起清零。
- 清 `out_inflight` 是错的：端点上那一笔已武装的读照样会完成，置 0 会让 `sb_out_kick()`
  重复武装，而 DWC2 对忙端点再来一笔是静默顶掉（回调永不来）→ 在飞标志永久卡住；
- 清 `out_w` 也是错的：完成回调按**当时的** `out_w` 记账，于是把包写进一个刚被清零的环，
  等于把复位前的东西当成新帧执行。

修法：复位只做"排空"——`out_r = out_w; out_used = 0`（IN 侧同理），
**不动端点与在飞标志**。另外给环加了**代数（generation）**：武装读写时记下当时的代数，
完成回调发现代数对不上就丢弃。代数**只在 USB 总线复位时 +1**（那种情况下在飞传输被硬件
作废、回调不会来，必须清在飞标志）；HID RESET / ENABLE 清环时**不 bump 代数** ——
实测按代数丢弃会把 enable 后的第一帧直接吃掉（那一笔完成时收到的是复位后才发来的新数据）。

回归：`loop` 全量重跑 PASS，P3 各项全过。


---

## 6. 发送/接收策略与阈值

### 6.1 TX：`< 100` 轮询，`≥ 100` DMA

- **轮询**（`spi_transfer()`）：CPU 逐字节/逐字灌 TX FIFO。小帧时省掉 DMA 通道配置与完成等待，路径最短、无中断。
- **DMA**（`spi_setup_dma_transfer()`）：CPU 只做配置 + 等 TC。大帧时 CPU 占用从「与线速同步」降为 O(1)，主循环能继续照看 USB/DAP。
- 阈值 `tx_dma_threshold` 默认 **100 B**（用户指定），**可配、可单帧覆盖**（`NO_DMA`/`FORCE_DMA`）。
- 粗算依据（360 MHz CPU、8 项 FIFO、`data_merge=0`）：
  - DMA 侧固定开销：通道配置 + 启动 + 等 TC 轮询 ≈ 0.5~1 µs；
  - 轮询侧边际开销：约 3~8 周期/字节 ⇒ 100 B ≈ 1~2 µs；
  - 交叉点大致落在 100~200 B 区间，**与用户给的 100 一致**。
  - 注意：`data_merge=1` 能让轮询边际开销降到 ~1/4，交叉点会右移到 400 B 上下——所以这个阈值最终要靠上板实测（§8 的对照测试），我先按 100 实现。

### 6.2 RX：一律轮询

- 主机侧读长度已知（`rx_len`），CPU 收 FIFO 即可，无需 DMA 通道与描述符。
- 读数据直接写进 IN 槽 → 立刻排队发走，无二次搬运。
- 例外：将来若要支持「超长连续读」（> 504 B 的 D 类场景），可考虑 RX DMA —— 属于 v2 议题。

### 6.3 吞吐预估（不含主机与协议开销）

| 配置 | 线速上限 | 240×296×2 B（142 KB）| 360×360×2 B（259 KB，ST77916 整屏）|
|---|---|---|---|
| 单线 20 MHz | 2.5 MB/s | ≈ 57 ms | ≈ 104 ms |
| 单线 40 MHz | 5 MB/s | ≈ 28 ms | ≈ 52 ms |
| **四线 20 MHz** | **10 MB/s** | ≈ 14 ms | ≈ 26 ms |
| **四线 40 MHz** | **20 MB/s** | ≈ 7 ms | **≈ 13 ms**（面板规格值）|
| 四线 60 MHz | 30 MB/s | ≈ 4.7 ms | ≈ 8.6 ms（待实测）|
| 四线 80 MHz | 40 MB/s | ≈ 3.6 ms | ≈ 6.5 ms（待实测）|

- USB 侧：HS bulk 512 B/包。探针已有两条 bulk 通道的实测：J-Scope 采样器的 bulk IN `0x83` 在满速测试里跑到 **26.1 kHz × 512 B ≈ 13 MB/s** 的包速率，RTT-over-CDC 跑到 **1.39 MB/s（且是被 SWD 侧限速的结果）**。而 SPI 转发没有 SWD 那样的逐包处理开销，**USB 侧余量远大于 SPI 线速**，瓶颈在 SPI。
- 帧间空隙（协议解析 + 事务启动 ≈ 1~2 µs）相对 480 B/帧（四线 40 MHz ≈ 24 µs）只有几个百分点，**v1 不做流水线**；若以后需要，可让连续写帧在硬件 CS 自动控制下背靠背下发。
- SCLK 40/60/80 MHz 由 HID `sclk_hz` 指定（用户要求）：40 MHz 是 ST77916 的厂规值，把握较大；60/80 MHz 受「SPI1 模块时钟 / SPI 外设上限 / 排针走线与从器件能力」三方约束，**逐档上板实测**，回环跑通才算数。`GET_CFG` 会回读**实际生效频率**（分频后可能落在 20/24/30/40/60/80 之类的离散点上，不是任意值）。

---

## 7. 与网页侧的接口约定（契约要点）

1. 打开 `VID 0x0D28 / PID 0x0204` 的**新接口**（vendor specific，2 个 bulk 端点）。
2. 控制面走现有自定义 HID：`req[2] = 0x35`，动作见 §4.4；响应形状与 0x31/0x32/0x34 一致。
3. 数据面：bulk OUT 发帧（§4.2），bulk IN 收应答（§4.3）。
4. 使用顺序建议：`GET_CFG` → `SET_CFG`（SCLK/线数/CS 策略/辅助脚/阈值）→ `SET_PROFILE`（选面板档）→ `ENABLE 1` → `RESET` 帧 + 灌初始化步帧 → 刷像素 → 需要时 `ABORT`/`RESET`(action 2) → `ENABLE 0`。
5. 主机必须自己切片（单帧 ≤ 504 B），并自己维护 `seq` 配对。
6. 批量写建议「只有最后一片带 `RSP`」，避免 IN 流量拖慢灌数据。
7. **面板初始化表可以几乎原样搬过来**：把 `{cmd, data[n], n, delay_ms}` 一行编成一个 `STEP` 帧（§4.7），`{"type":"delay"}` 编成 `DELAY` 或并进 `STEP.delay_ms`，`{"type":"reset"}` 编成 `RESET` 帧。AXS15352 与 ST77916 两套序列都已经按此核对过（§2.9）。
8. 像素数据字节序（RGB565 高字节在前）由**主机**负责交换，探针做字节透明转发。

---

## 8. 实施阶段与验收

| 阶段 | 内容 | 上板验收标准 |
|---|---|---|
| **P1** | 协议头文件 + HID 0x35（配置/状态/使能/复位）+ SPI1 引脚与初始化 + 帧解析 + **轮询 TX/RX** + AHB SRAM 环 + 端点描述符 | 枚举后能看到新接口；`GET_CFG` 回读正确；**MISO↔MOSI 跳线回环**：1/2/32/99/100 B 收发逐字节一致 |
| **P2** | **DMA TX**（≥ 阈值）+ 阈值可配 + 统计计数 | 同上回环在 101/256/504/484 B 通过；对比 `NO_DMA`/`FORCE_DMA` 两种路径的耗时，定阈值默认值 |
| **P3** | 辅助 GPIO（DC/RESET/CS/BL）+ 辅助输入（TE）+ 手动 CS（`CS_HOLD`/`CS_OFF`/`CS` 帧）+ **非阻塞 `DELAY`** + `RESET` 帧 | 万用表/逻辑分析仪看 DC/RESET 电平与 CS 波形；`RESET` 帧后 `AUX_IN` 能读到 TE 电平变化；120 ms 延时期间 DAP/RTT 仍可用 |
| **P4** | **面板档 + `STEP` 帧** + 两块实屏：AXS15352（档 1）与 ST77916（档 2） | 逐条下发厂规初始化序列后屏能亮；ST77916 整屏刷图（259 KB）时间与 §6.3 同量级；`sclk_hz` 20→40 MHz（→60/80）逐档无错 |
| **P5** | 文档：`docs/web-handoff-spi-bridge.md`（给网页组的实现说明）+ README 更新 + DLM/Flash 占用复核 | 文档自洽；DLM 与 Flash 数字更新 |

**P1/P2 的关键验收手段是回环**（J3[19] MOSI ↔ J3[21] MISO 一根跳线）：
- 不依赖任何外部器件，能覆盖「轮询 vs DMA」「单线/双线」和协议解析；
- 与之前 UART 回环（`script_test/uart_loopback.py`）同一套路，便于回归；
- 新脚本：`script_test/spi_bridge_test.py`（WebUSB/裸 HID 均可），跑长度扫描 + 阈值对照 + 统计校验；再加 `--panel` 模式的 DC/CS 波形自检。

---

## 9. 风险与待确认

### 9.1 已确认（2026-09-28）

| # | 事项 | 结论 |
|---|---|---|
| 1 | 端点方案 | ✅ 同意：新开一对 bulk `0x8B/0x0B` |
| 2 | 辅助脚默认值 | ✅ 可以：DC=PB11(J3[13])、RST=PB12(J3[27])、BL=PB13(J3[28])、备用 CS=PB10(J3[26])（TE 输入默认 PB10/PB11 类脚，配置里可改） |
| 3 | SCLK | ✅ 默认 20 MHz；**由 HID 面板协议指定 40 / 60 / 80 MHz** |
| 4 | 目标屏 | ✅ 天马 2P01/AXS15352（SPI+DC）与 ST77916（QSPI）；初始化序列由网页逐条下发，探针逐条执行（§2.9 §4.7） |

### 9.2 技术风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| 描述符改动导致枚举回归（接口号顺移） | DAP/CDC/HID/WebUSB/DFU 全部要复测 | 接口追加在 HID 之后、宏统一顺移；改动后跑一遍现有 `script_test/` 全套 |
| 主循环被长帧拖住 | DAP/Scope 时序抖动 | 每轮帧数/字节上限；DMA 处理大帧；延时非阻塞；未使能时零开销 |
| 四线时序与从器件不匹配（dummy/CS 时序） | QSPI 屏读不到数据 | `cpol/cpha/cs2sclk/csht/dummy/addr_quad` 全部可配；先用 SPI NOR 或回环验证四线链路 |
| IN 环被未取走的数据填满 | 卡住帧处理 | 流控暂停 + 硬超时 + 计数器上报；文档里强调主机要取走 IN |
| 辅助脚与排针上其它用户（J3 共用）冲突 | 误驱动外部电路 | pad 表白名单 + 使能时才配置 + `PIN_CFG` 校验 |
| SCLK 60/80 MHz 上不去或误码 | 屏花屏 | 逐档实测；失败就回落 40 MHz；必要时降 `cs2sclk` 或加串阻；`GET_CFG` 回读实际频率 |
| 背光 PWM 调光（20 kHz） | v1 只有开关，无调光 | 列为后续可选：HPM5301 的 PWM 需占用相应 pad 的 PWM 复用，与辅助脚表存在耦合；先用 GPIO 开关，屏点亮后再评估 |
| 探针暂不可用 | 无法即时验证 | 本阶段只写代码与文档；所有数值（阈值、SCLK 上限）标注为待实测 |

---

## 10. 下一步

1. ✅ 用户已确认 §9.1 四项；方案定稿（本文档）。
2. 按 P1 → P5 顺序实现，每个阶段在分支上独立提交（中文 commit）。
3. 有板子后：构建（`cmd /c build_dfu_evklite.bat`）→ DFU 烧写（`hpm6800_flash_probe.py --no-build`）→ 跑 `script_test/spi_bridge_test.py` → 接屏跑 P4 → 出验收数据 → 更新文档与 README。


---

## ⚠️ 2026-09-30 更新：桥已从 SPI1 迁到 SPI2

本文正文里的引脚表是 **SPI1 时代**（CS=PA26 / SCLK=PA27 / MISO=PA28 / MOSI=PA29 /
IO2=PA30 / IO3=PA31，都在 J3 上）。因为 **PA30（USB0_PWR）被板上 Q1 常态短到地**，
四线 QSPI 的 IO2 在这块板上无解（实测把 PA30 配成 GPIO 也拉不动），桥整体搬到了 SPI2：

| 信号 | 现在 | J3 |
|---|---|---|
| CS | PB10 | 26 |
| SCLK | PB11 | 13 |
| D0/MOSI | PB13 | 28 |
| D1/MISO | PB12 | 27 |
| D2/IO2 | PB14 | 10 |
| D3/IO3 | PB15 | 8 |

固件侧：`SB_SPI=HPM_SPI2`、`clock_spi2`、`HPM_DMA_SRC_SPI2_TX`、CS 默认脚 PB10、
`reserved[]={PB10..PB15}`；CDC VCOM 从 UART3(PB15/PB14) 换到 UART2(PB08/PB09)，
console 仍是 UART0(PA00/PA01)。接线速查见 `docs/spi-bridge-wiring.md`（已按新引脚重写）。
