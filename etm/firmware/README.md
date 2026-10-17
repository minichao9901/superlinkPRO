# firmware/ —— CherryUSB CDC ACM + etm-trace 三个目标

三个 MCU 各一个独立工程，都用 **CherryUSB v1.6.1** 的 `demo/cdc_acm_template.c` 做 CDC ACM 设备，
并且都按同一约定提供 **etm-trace 钩子**：**固件不配置、不开启 ETM**，trace 由外部 SWD 工具配置
（与 `stm32h7b0_spi_trace` 一致）。

| 工程 | MCU | USB port / 外设 | USB 时钟 | CPU | Flash / RAM 占用 | 状态 |
|---|---|---|---|---|---|---|
| [stm32f103ze_cdc_trace](stm32f103ze_cdc_trace/README.md) | STM32F103CB（在线）/ F103ZE（目标） | `fsdev`，PA11/PA12 全速 | PLL/1.5 = 48MHz | 72MHz | 18.3KB / 4.8KB | **编译✓ 已烧录✓ 运行✓**（USB 主机枚举待接线确认） |
| [stm32h7b0_cdc_trace](stm32h7b0_cdc_trace/README.md) | STM32H7B0 | `dwc2`，`USB1_OTG_HS` PA11/PA12 全速 | PLL3Q=48MHz（25MHz HSE） | 280MHz | 约 34.8KB / 274.7KB(bss) | **编译✓**（未烧） |
| [stm32h743_cdc_trace](stm32h743_cdc_trace/README.md) | STM32H743（阿波罗） | `dwc2`，`USB_OTG_FS` PA11/PA12 全速 | HSI48 | 480MHz | 31.3KB / 273KB(bss) | **编译✓**（未烧，缺板） |

> bss 里 256KB 是 SEGGER RTT 上行缓冲（沿用 `stm32h7b0_spi_trace` 的配置），要省内存改
> `sdk/RTT_TRACE_LOG/SEGGER_RTT_Conf.h` 的 `BUFFER_SIZE_UP`。

## 共同的目录结构

```
<project>/
├── build.ps1              # arm-none-eabi-gcc 构建（不依赖 CubeMX / Keil）
├── flash.ps1              # F103 用：pyocd + CMSIS-DAP 烧录
├── ld/<chip>.ld           # 链接脚本（含 .noncacheable / .rtt_cb / .trace_diag 固定段）
├── src/
│   ├── main.c             # cdc_acm_template.c 改写 + 命令行 + 主循环
│   ├── trace_hooks.c/.h   # etm-trace 钩子（DWT 周期计数 + 打点 GPIO + 诊断结构体）
│   ├── boot_probe.c       # 启动阶段探针：PA1 脉冲 + g_boot_stage（卡死时用来定位）
│   ├── usb_dc_low_level.c # USB 时钟/引脚/中断 + HAL_PCD_MspInit（如适用）
│   └── (F103) system_clock.c / stm32f1xx_it.c / syscalls.c
├── sdk/                   # ST 官方 HAL + CMSIS + 启动文件（挑过的子集）
├── segger_rtt/ 或 sdk/RTT_TRACE_LOG/   # SEGGER RTT（日志通道，不需要 SWO 引脚）
├── cherryusb/             # CherryUSB v1.6.1：core / class-cdc / common / port / config
└── tools/                 # (F103) pyocd 内存读、DWT 写观察点
```

## 共用工具

```powershell
# 串口自检（三个固件通用）：PING/INFO/MARK/ECHO + 回环吞吐
python tools\cdc_test.py --list
python tools\cdc_test.py --port COMx
python tools\cdc_test.py --port COMx --speed --kb 200
```

`stm32f103ze_cdc_trace/tools/min_probe.py` 与 `watch_probe.py` 也适用于 H7 工程
（换 `--target stm32h7b0xx` / `stm32h743xx` 即可）。

## 三个工程的关键差异（改代码前必看）

| 项 | F103 | H7B0 | H743 |
|---|---|---|---|
| CherryUSB port | `fsdev` | `dwc2` | `dwc2` |
| `usb_dc_low_level_init` 签名 | **无参**（自写，不编 HAL PCD） | **带参 `uint8_t busid`**（走 `HAL_PCD_MspInit`） | 同 H7B0 |
| 官方 glue 能否直接用 | `usb_glue_st.c` 带参 + 要 HAL PCD → 不用 | 官方 `usb_glue_st.c` 会拖入 USB host 栈 → 自写 device-only glue | 同 H7B0 |
| DMA | 无（PMA 16 位访问，`PMA_ACCESS=2`） | 示例选择 FIFO/中断模式，DMA 未启用 | 示例选择 FIFO/中断模式，DMA 未启用 |
| D-Cache | 无此概念 | 关闭；`.noncacheable` 段名本身不配置 MPU | 同 H7B0 |
| USB 实例名 | `USB`（0x40005C00） | **`USB1_OTG_HS`（0x40040000）**，H7B0 没有 `USB_OTG_FS` | `USB_OTG_FS`（0x40080000） |
| ETM | CB/RB/C8 **无**；ZE/xE **有 ETMv3** | ETMv4（Cortex-M7） | ETMv4 |

## 相关既有工程

- `stm32h7b0_spi_trace/` —— H7B0 的 SPI 业务 + 外部配 ETM 的验收版（本批 H7 工程的风格来源）
- `stm32h7b0_etm/`、`stm32h7b0_etm_board_v1/` —— H7B0 固件自配 ETM 的低频验证版
