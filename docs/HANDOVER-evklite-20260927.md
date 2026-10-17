# akaLinkPro × HPM5301EVKLite 移植 · 交接文档

日期：2026-09-27　作者：ZCode 会话（minichao 协作）
状态：**DAP 核心功能已验证可用；CDC 串口回环未通过（主遗留问题）**；工作区有未提交改动（含临时诊断代码，见 §3）。

---

## 0. 后续会话结论（2026-09-27 晚）：CDC 回环已修好 ✅

**问题不是 UART3/PB15-PB14 的移植，而是两个独立的原因：**

### 0.1 固件 bug（真正的"无任何回显"根因）

`usb_composite.c: usbd_cdc_acm_set_line_coding()` 只在 line coding **发生变化** 时才
`config_uart = 1`。而 `USBD_EVENT_RESET`（重插 USB、重枚举、主机重开端口）会清掉
`config_uart_transfer`。此后主机用**相同参数**（115200-8N1）再开串口时
`memcmp()` 相等 → 桥永远不再启动：COM 口能收数据，字节堆在 `g_usbrx` 里，
**一个字节都不会回显**，且完全静默。

实测证据（诊断固件 HID 读数）：

| 场景 | cdc_out | usbrx | uart_tx | 回显 |
| --- | --- | --- | --- | --- |
| 未重新武装（复现故障） | 27 | 27（卡住） | **0** | 无 |
| HID force-start 之后 | 27 | 27 | **27**（tx_tc=1 发送完成中断） | **27/27 原样返回** |

修复（`usb_composite.c`，已保留在代码里）：
1. `SET_LINE_CODING` **总是**重新武装：参数变了 → 记录并重配；参数没变但桥没在跑 → 也重配；
2. `USBD_EVENT_CONFIGURED` 时若已记住 line coding，直接重新武装（主机不复发编码也能恢复）；
3. 忽略全零 line coding（部分主机驱动在**关闭**串口时会下发，不能拿去配 0 波特率）。

### 0.2 接线：回环线必须真的插上

诊断固件在**没插回环线**时测得：UART3 内部回环自测 8/8 通过、主机 27 字节全部经
UART TX 发出（TX DMA 完成中断触发）、但 RX 侧 **0 字节** → 线上没有回环。
把 **J3.8(J3.8=UART_TXD=PB15) ↔ J3.10(UART_RXD=PB14)** 短接后，引脚直连自测返回
`0b01 FOLLOWS`（驱动 TX 高/低，RX 跟随），回环立刻全程打通。

> ⚠️ 接逻辑分析仪 **不等于** 接回环线；之前"跳线位置不确定"就是这里。

### 0.3 顺手修掉的移植隐患

`uartx_io_init()` 原本也受 `g_param.output_mode` 控制是否复用 UART 引脚：在
**UART 引脚与 JTAG 不共用**的板子（EVKLite）上，用户一旦在 WebUI 切到 SWD+JTAG
模式，COM 口会永久哑掉（直到下一次 DAP_Connect）。已改为仅当
`BOARD_UART2_SHARES_JTAG_PINS` 时才受 output_mode 影响。

### 0.4 复现/验证命令（新增脚本，均在 `script_test/`）

```bat
python evk_echo.py COM52 115200 fresh        :: 新启动回环，PASS
:: 用 PnP 重启制造 USB 复位（MCU 不复位）后再测——这是原来的故障场景
python evk_echo.py COM52 115200 after-reset  :: PASS
python uart_loopback_common.py COM52         :: 9600~10Mbps 全部 OK，10M→974KB/s
python evk_flash.py                          :: HID 进 DFU + 拖盘烧录 _pack.bin
```

- `evk_diag.py` / `evk_probe.py` / `evk_watch_jumper.py` / `evk_verify.py` /
  `evk_jtag_mode_test.py` 依赖 **TEMP-DIAG-2 诊断固件**（HID `CMD_GET_VOLTAGE` 索引读数 +
  `CMD_DIAG(0x30)` 控制：引脚直连自测 / UART 内部回环自测 / 计数器 / force-start）。
  诊断代码已按计划**全部移除**，`CMD_GET_VOLTAGE` 已还原为 2 字节电压；如需再诊断，
  按 git 历史里 21:23-21:31 的几个构建版本恢复。
- **DFU 拖盘小坑**：MSC 写入偶尔不触发 bootloader 提交，重写一次（或改用
  PowerShell `Copy-Item`）即可。

---

## 1. 一句话现状

EVKLite 上的 akaLinkPro（CMSIS-DAP + CDC + DFU/MSC）已经能烧、能调试外部目标、能 DFU/MSC 升级、
WebUSB 落地页已换成 `https://minichao9901.github.io/web-serial-rtt-tools/`；
**只剩 CDC 虚拟串口（UART3, PB15/PB14 = J3.8/J3.10）回环不通**，已做好零硬件依赖的诊断固件待烧录验证。

## 2. 板级引脚映射（EVKLite，当前有效）

| 功能 | 引脚 | 位置 | 备注 |
|---|---|---|---|
| SWCLK/TCK | PA06 | J5.9 | FGPIO 位带（SWD blob 已重生成） |
| SWDIO/TMS | PA07 | J5.7 | 单脚双向 |
| TDI / TDO | PA05 / PA04 | J5.5 / J5.13 | JTAG 模式 |
| **nRESET** | **PA08** | **J5.3**（nTRST 位） | 有，直出低有效复位；J5.15 是板子自身 RESET_N，勿接目标 |
| CDC VCOM | **PB15(TX)/PB14(RX)** | **J3.8 / J3.10**（板上丝印 UART_TXD/RXD） | **UART3**（应用户要求从 UART2/PB08-09 改来） |
| LED | PA10 | 板载 LED2 | 低电平点亮 |
| USER KEY | PA03 | 板载按键 | 运行时长按 1s 进 DFU；上电按住进 ROM ISP |
| USB0 OTG | PA24/PA25 | J1 | DAP 上行口 |
| UART0 console | PA00/PA01 | J3.36/J3.38 | printf 调试口（诊断时用） |

完整说明见 `docs/HPM5301EVKLite_port.md`（引脚表已同步为 UART3）。

## 3. 工作区未提交改动（git status 概览）⚠️

全部改动未 commit。分类：

**EVKLite 移植本体（应保留）**
- `firmware/*/boards/hpm5301evklite/`（app + bootloader 板级，新增）
- `firmware/*/build*_evklite.bat`、`flash_jlink_evklite*.bat`、`program_evklite.bat`、根 `Makefile`
- `src/dap/`：7 个汇编文件的引脚 shift 改为可用 `-D` 覆盖（默认值=akaLinkPro，两板共用）；
  `SW_DP.c` 按 `BOARD_SWD_BLOB_EVKLITE` 选择 blob；新增 `swd_blob_evklite.h`（重生成的 EVKLite blob）
- `DAP_config.h`：nRESET 极性（`BOARD_NRESET_ACTIVE_LOW`）、DIR/TRST/SWDIO 方向按板裁剪
- `cdc_interface.c/h`：UART 实例参数化（`BOARD_CDC_UART_*`，EVKLite=UART3）
- `bootloader_dfu`：CherryUSB 新旧两代 DFU 钩子兼容（`dfu_flash_port.c` + CMakeLists 条件编译 usbd_dfu.c）
- app CMakeLists：SDK 1.11 无 flash_dfu 类型 → `flash_xip` + 自定义 ld + `-DFLASH_XIP=0 -DFLASH_DFU=1` 仿真
- `usb_composite.c`：落地页 URL 改为用户网址（并修正了原 bLength 多 3 个 NUL 的 bug）
- `led_state.c`/`api_param.c`：LED 极性、VREF/5V 功能按板裁剪
- `.gitignore`、`README.md`、`docs/HPM5301EVKLite_port.md`

**⚠️ 临时诊断代码（串口问题定位后必须移除）**
- `cdc_interface.c/h`：`g_uart_diag0/1`、`uart_diag_fill()`、`uart_diag_banner_tick()`
- `api_param.c`：`CMD_GET_VOLTAGE(0x03)` 应答被替换为诊断字（res_hid[3..10] = diag0+diag1，len=9）
- `main.c`：上电强制 115200-8N1 配置 + 主循环里每 0.5s 发 `[UART-DIAG-ALIVE]` banner
- 恢复方法：搜 `TEMP-DIAG` 标记逐处删除；GET_VOLTAGE 恢复为 `led_state_get_external_mv()`

**测试脚本/产物（建议保留）**
- `script_test/`：`openocd_stm32f1_swd.cfg`、`sram_speed_test.py`、`rtt_speed_test.py`、
  `rtt_fast_poll.py`、`rtt_pyocd_test.py`（未跑通，见 §6）、`reset_akalink_usb.ps1`、
  `stm32f103_rtt_speed/`（从 web-serial-rtt-tools 拷来的 RTT 极速测试固件）、`rtt_result*.txt`
- `docs/evklite_ug.txt` 已删；`docs/HPM5301EVKLite_UG_V1.1.pdf` 为用户放入的资料

## 4. 已验证 ✅

| 项 | 结果 |
|---|---|
| J-Link 首刷（J5, JTAG） | `flash_jlink_evklite_full.bat` 成功，bootloader+APP 校验通过 |
| SWD 调试外部目标 | OpenOCD 探到 STM32F103（DPIDR 0x1BA01477，reset halt，读 flash OK），cfg 在 `script_test/openocd_stm32f1_swd.cfg` |
| SRAM 20KB 读写 | 1MHz 85KB/s → 60MHz 写 2.9 / 读 2.6 MB/s（`sram_speed_test.py`，逐字节校验通过） |
| RTT（OpenOCD） | 关键：**`rtt polling_interval 1`**（默认 100ms 限速 9.2KB/s）。1ms 时 10MHz=403KB/s，36/60MHz≈285KB/s |
| RTT（自定义快速轮询器） | `rtt_fast_poll.py`：1/10/36/60MHz = 73/347/492/522 KB/s，13 字节流无缝零丢失，g_bytes 对账一致。**注意主机必须回写 RdOff（`mww`，不是 `mdw`）** |
| DFU/MSC 拖盘升级 | 用户实测两次成功 |
| HID `CMD_ENTER_DFU(0xFF)` | 实测有效（帧：`[0x01,0x01,0xFF,0...]`，经 MI_03 custom HID，usage_page=0xFF00）→ 设备重启进 DFU（PID_0207），JTAG 恢复可烧 |
| WebUSB 落地页 | 已 = `minichao9901.github.io/web-serial-rtt-tools`（pyusb GET_URL wValue=1 实测） |

## 5. 主遗留问题：CDC 串口回环 ❌

现象：短接 VCOM TX↔RX，PC 打开 CDC COM 口发送，无任何回显。pyserial（COM52, 115200）复现同样失败。
已排除：COM 口选错（pyserial 同样失败）、固件旧（落地页已变=新固件在跑）。
已做过：UART2/PB08-09（J3.5/J3.3）→ 换成 UART3/PB15-14（J3.8/J3.10，用户指定）后**仍未验证**（跳线位置不确定 + 烧录被打断）。

### 5.1 诊断固件（已编译未烧录，`build_dfu_evklite/output/akaLinkPro_App_pack.bin` @21:03）

- 每约 0.5s 从 VCOM UART TX 发 `[UART-DIAG-ALIVE]`（强制 115200-8N1 配置，绕过主机 SET_LINE_CODING）
- HID `CMD_GET_VOLTAGE(0x03)` 返回诊断字：
  - 请求帧：`[0x01, 0x01, 0x03, 0×61]`（64B，MI_03 custom HID，usage_page=0xFF00）
  - 应答帧：`[0x02, 9, 0x03, d0..d7]`
  - **diag0** = `tx_FUNC_CTL[7:0] | rx_FUNC_CTL[15:8] | com_mode<<16 | rx_dma_ok<<17 | tx_dma_ok<<18 | uart_clk_MHz<<19`
    （期望：tx=2、rx=2、com=1、rxok=1、txok=1、clk=80）
  - **diag1** = 最近一次 SET_LINE_CODING 后的实际波特率（期望 115200；为 0 说明 CDC 配置回调没跑）
- 判定树：
  1. J3.8↔J3.10 短接后 COM 口能收到 banner → 整链 OK，之前失败是跳线位置
  2. 收不到 banner + diag0 正常 → TX pad→引脚输出有问题（查 PB15 是否真的到 J3.8，见 §5.2）
  3. diag0 中 tx/rx FUNC_CTL ≠ 2 → mux 未生效（查 `BOARD_UART_*_FUNC`、`uartx_mux_to_uart` 调用时序）
  4. rxok/txok=0 → dma_mgr 通道分配问题（注意 HPM5301 有 HDMA 和 DMAMUX 两路，UART 握手在 DMAMUX 路）
  5. diag1=0 → USB CDC SET_LINE_CODING 未到达固件（CherryUSB CDC 层问题）
- 诊断完成后：删 `TEMP-DIAG` 代码、重建、重烧。

### 5.2 烧录诊断固件的方法（二选一）
- **推荐**：长按 USER KEY 1s（或发 HID DFU 命令）进 DFU → 虚拟盘拖 `akaLinkPro_App_pack.bin`（用户已熟练）
- 或 DFU 模式下跑 `make flash-app` / `flash_jlink_evklite.bat`（DFU 模式 JTAG 可用；APP 运行中 JTAG 被占，直接连必失败）
- 读取诊断：`hid.enumerate(0x0d28,0x0204)` 选 `usage_page==0xFF00`，`write([0x01,0x01,0x03]+[0]*61)` 后 `read(64)` 解码

### 5.3 可能用到的硬件资源（按需）
- 无必须——诊断固件零硬件依赖
- 可选：3.3V USB-TTL 接 J3.36/38（UART0 console，看 `uart2 baud: req X -> Y` 等 printf）
- 可选：示波器/逻辑分析仪测 PB15（J3.8）有没有波形（banner 期间）
- 参考对照：MicroLink 探针的 CDC（COM33/COM5）可当 TTL 读码器用

### 5.4 备选排查方向（若 banner 能发出但回环仍不通）
- RX 侧：flush 定时器（GPTMR0 reload ISR）→ `uartx_rx_flush_locked` → `g_uartrx` → CDC IN
- DMA RX 循环缓冲（DMAV2 infiniteloop）DSTADDR 是否前进（可用 diag 扩展读 `dma_resource_pools[0].base->CHCTRL[].DSTADDR`）
- 对比 akaLinkPro 原板 `script_test/uart_loopback_common.py` 的测试方法

## 6. pyOCD 现状（未完成）

pyOCD 0.45.1 已装在 **Python 3.13**（`C:\Users\Administrator\AppData\Local\Programs\Python\Python313\python.exe`，
含 pyusb+libusb-package；默认 `python` 是 3.14 没装）。
卡点：连接时的 board-info 查询走 HID 接口，与 Chrome WebHID（配置工具页开着时）冲突报 `OSError: read error`。
结论：**测 pyOCD 前先在浏览器里点"断开连接"**，或用 `ConnectHelper`（默认优先 v2 bulk，枚举正常）。
`script_test/rtt_pyocd_test.py` 已写好（当前版本以 `stm32f103ze` 为 target_override、halt 后 resume、
target 级 API 读 RTT CB），跑通后预期 RTT 可逼近 SRAM 裸读带宽（~2.5MB/s）。

## 7. 构建 / 烧录速查

```
make build / build-boot / build-app     编译（bat 已修：make 内部 cd /d 进工程目录）
make flash / flash-app                  J-Link 烧录（首刷 / 仅APP）
make dfu                                ✗ 本机无 dfu-util（待装或用 MSC 拖盘替代）
make reset-usb                          设备 USB 重枚举（bulk 端点卡死时用）
```
- 工具链：`E:\sdk_env_v1.11.0`（SDK 1.11.0），J-Link 在 `C:\Program Files\SEGGER\JLink_V882`
- 本机 OpenOCD：`E:\sdk_env_v1.11.0\tools\openocd`（scripts 树被裁剪，无 target/，用 repo 里的 cfg）
- Python：诊断/测试脚本用 py313（pyserial/pyocd/hid/pyusb/libusb 都在）；系统默认 python=3.14
- 烧录前提：**DFU/ISP 模式**（APP 运行中 JTAG 引脚被 DAP 占用，J-Link 连不上）。
  进 DFU：长按 USER KEY 1s / HID 0xFF / dfu-util DETACH；救砖：USB ISP（USER KEY+复位）
- ⚠️ bulk 端点曾出现高负载下卡死（"error submitting USB write"）→ `make reset-usb` 或重插 USB

## 8. WebUSB 备忘

- Chrome 设备通知**只显示 iLandingPage 一个链接**；多 URL/allowed-origins 机制已被 Chromium 移除
  （bug 711443）。当前落地页 = 用户网址（已上板验证）。
- GET_URL 按 wValue 返回对应 URL 描述符（CherryUSB 内核即支持，无效索引 STALL）；
  页面可自行 control transfer 读 URL 做工具互跳（JS 片段见会话记录/docs）。
- DAP bulk 接口是 vendor class，任何 origin 都能 requestDevice+claim，无需 URL 在册。

## 9. TODO 清单

1. **CDC 串口回环定位**（§5 判定树）→ 移除 TEMP-DIAG → 重建重烧 → 回归测试
2. pyOCD RTT 测试（先断开浏览器配置工具）
3. `make dfu`：安装 dfu-util（放 `firmware/tools/` 并让 program_evklite.bat 优先用本地副本）
4. 提交：建议按"移植本体 / 测试脚本 / 临时诊断"拆 commit；确认 TEMP-DIAG 已删后再提交 api_param/main/cdc
5. `docs/HPM5301EVKLite_port.md` 的 §6 验证清单随串口结论更新

## 10. 设备当前状态（交接时刻）

最后一次 HID DFU 命令已发出（约 21:05），诊断固件**尚未烧入**；设备可能停在 DFU 模式
（PID_0207，虚拟 U 盘 `AKALINKPRO`）。重新上电/复位即退出 DFU：bootloader 校验现有 APP（有效）
后正常运行。诊断固件产物在 `firmware/application_5301/build_dfu_evklite/output/`（21:03 构建）。
