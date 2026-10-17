# J3 40 针 · 该限制谁 / 该释放谁（判断表）

> 原则（用户定的）：**我们用不到的脚就该让 probe 释放，别一刀切限制；确实不好用的才限制。**
> 判据来源：当前实跑固件（`application_5301` + **hpm5301evklite** 板级，只有它 `BOARD_HAS_SPI_BRIDGE=1`）的源码 + 逻辑分析仪实测。
> 图例：★ = 板上丝印标注的「显示接口」脚　🔒 = 建议限制　🔓 = 建议释放　⚠️ = 有条件的释放，需要你/固件拍板　🚫 = 实测物理不可用

---

## 1. 图（J3 40 针，两列同构）

```
 1 ● 3V3                                           5V ● 2
 3 ● PB09  I2C_SDA        🔒 CDC VCOM(RXD)         5V ● 4
 5 ● PB08  I2C_SCL        🔒 CDC VCOM(TXD)      GND    ● 6
 7 ● PA02  (空)           🔓 实测可用（现作 RST）  UART_TXD ● 8   PB15  🔒 quad 档 DAT3
 9 ● GND                                    UART_RXD ● 10  PB14  🔒 quad 档 DAT2
11 ● PA31  ★QSPI IO3      🔓 实测可用（现作 BL）     NC ● 12
13 ● PB11  ★DC            🔒 SPI2 SCLK          GND ● 14
15 ● NC                                             NC ● 16
17 ● 3V3                                            NC ● 18
19 ● PA29  ★MOSI / IO0    🔓 当前固件没人用     GND ● 20
21 ● PA28  ★MISO / IO1    🔓 当前固件没人用        NC ● 22
23 ● PA27  ★SCLK          🔓 当前固件没人用  SPI_CS0 ● 24  PA26 ★CS   🔓 当前固件没人用
25 ● GND                                    SPI_CS1 ● 26  PB10 ★TE   🔒 SPI2 CS
27 ● PB12  ★RST           🔒 SPI2 MISO          GPIO ● 28  PB13 ★BL   🔒 SPI2 MOSI
29 ● PY00  (USB0_ID)      ⚠️ PIOC 域，要写代码    GND ● 30
31 ● PY01  (USB0_OC)      ⚠️ PIOC 域，要写代码    GPIO ● 32  PA09  🔓 上面有 TinyUF2 按键
33 ● PA10  (板载 LED)     ⚠️ LED 任务每 50ms 写它  GND ● 34
35 ● NC                                    UART_LOG_TX ● 36  PA00  🔒 控制台 log
37 ● PA30  ★QSPI IO2      🚫 物理不可用（Q1 短地）UART_LOG_RX ● 38  PA01  🔒 控制台 log
39 ● GND                                            NC ● 40
```

---

## 2. 逐条判断与依据

### 🔒 建议继续限制（12 根）

| 引脚 | pad | 当前固件用途 | 依据 |
|---|---|---|---|
| 13 | PB11 | **SPI2 SCLK** | `pinmux.c:211`（且**必须**带 `LOOP_BACK` 位，否则 RX FIFO 恒读 0x00/0xFF） |
| 27 | PB12 | **SPI2 MISO** | `pinmux.c:212` |
| 28 | PB13 | **SPI2 MOSI** | `pinmux.c:213` |
| 26 | PB10 | **SPI2 CS**（硬件 CS0 或 GPIO CS） | `pinmux.c:194` |
| 8 | PB15 | **SPI2 DAT3**（仅 quad 档 mux） | `pinmux.c:226`；见下面 ⚠️ 条 |
| 10 | PB14 | **SPI2 DAT2**（仅 quad 档 mux） | `pinmux.c:227`；见下面 ⚠️ 条 |
| 5 | PB08 | **UART2 TXD = CDC VCOM** | `pinmux.c:144`（2026-09-30 从 UART3/PB14-15 迁过来） |
| 3 | PB09 | **UART2 RXD = CDC VCOM** | `pinmux.c:145` |
| 36 | PA00 | **UART0 TXD = 控制台 log** | `pinmux.c:50`，且 `board.c:301` 确实调用了 `init_uart0_pins()` |
| 38 | PA01 | **UART0 RXD = 控制台 log** | 同上 |
| 37 | PA30 | **物理不可用** | `pinmux.c:180-184`：经 R5 连到 AP2151 的 EN 节点，Q1 栅极被 CC1/CC2→BAT54A 常态拉高 ⇒ Q1 常态导通，把整根网络低阻拉到地；实测配成 GPIO 也拉不动、LA 全 0 跳变 |
| — | PY00/PY01 | 见 ⚠️（不是硬限制，是"要写代码"） | `pinmux.c:125-130` |

### 🔓 建议释放（**白捡 5 根**）

| 引脚 | pad | 板上丝印 | 为什么可以释放 | 建议 |
|---|---|---|---|---|
| **24** | **PA26** | SPI_CS0 | 当前固件里**零引用**（`init_uart0_pins` 用 PA00/01、nRESET=PA08、CDC=PB08/09、SWD=PA06/07） | 加进 pad 表当普通辅助脚 |
| **23** | **PA27** | SCLK | 同上。⚠️ akaLinkPro 板级定义里它是 **JTCK/SWCLK(target)**，但这份固件走的是 `swd_blob_evklite.h`（SWCLK=**PA06**） | 同上（换板级构建前要复核） |
| **21** | **PA28** | MISO / IO1 | 同上。⚠️ akaLinkPro 板级里是 **JTMS/SWDIO(target)** | 同上 |
| **19** | **PA29** | MOSI / IO0 | 同上（akaLinkPro 里那行 `BOARD_PIN_JTMS_OUT` 已被注释掉） | 同上 |
| **32** | **PA09** | GPIO | 当前固件没用；但 `spi_bridge_proto.h:247` 自己标着「TinyUF2 按键脚，慎用」——**作输出没问题**，作输入要当心按键按下会把线拉低 | 释放并标注警告 |

### ⚠️ 需要你（或固件）拍板（4 处）

| 项 | 现状 | 两条路 |
|---|---|---|
| **PA10 / 引脚 33** | `board.h` 里 `LED1_PIN = LED2_PIN = IOC_PAD_PA10` + `LED_TICK_PERIOD_MS = 50`，**LED 任务每 50 ms 写它**（实测：配成 BL 后只有 ~20 ns 毛刺、周期精确 50 ms，永远出不来持续电平） | ① **释放**：让 `led_write()` 在该 pad 已被桥占用时跳过（akaLinkPro 实物的 LED 其实是 PB11/PB12，PA10 是 evklite 的板载 LED）→ 多一根可用脚<br>② **限制**：保持现状，在协议文档里写明 pad 11 不可用 |
| **PY00 / PY01（29、31）** | 被配成 **USB0_ID / USB0_OC**，且属 **PIOC 域**（要同时配 IOC + PIOC + GPIOM 才能当 GPIO，见 `pinmux.c:9-14` 的说明） | ① **释放**：做 USB device 时 ID/OC 一般用不上 → 固件补 PIOC 分支，能再多 2 根<br>② **限制**：v1 明确不支持，文档写死 |
| **PB14 / PB15（10、8）** | 只在 **quad 档**才 mux 成 SPI2 DAT2/DAT3；`sb_pad_usable()` 却是**无条件**保留 | ① **按档位动态释放**：非 quad（raw / spi_dcx）时放出来当辅助脚<br>② 保持无条件保留（更简单，代价是单线档白白少 2 根） |
| **PA09 的按键** | 未实测按键是否真的接在 PA09 | 释放后实测一次：拉低/拉高各保持 1 s，看是否被按键网络拉回 |

---

## 3. 如果按上面释放，固件需要做什么

1. `s_pad_table[]`（`spi_bridge_proto.h:241-255`）**新增索引**：PA26 / PA27 / PA28 / PA29（+ 视决定再加 PA10、PY00、PY01）。注意 `SB_PAD_MAX` 要跟着改，且这是**协议兼容性变更**（主机侧的下拉列表要同步）。
2. `sb_pad_usable()` 的 `reserved[]`：PB10~PB13 永远保留；**PB14/PB15 改成按当前档位判断**（quad 才保留）。
3. `led_write()`：若 `BOARD_LED*_PIN` 等于当前 `s_pad_dc/rst/bl/cs_aux/te` 里任一非 0 值 → 跳过（或至少在桥使能期间跳过）。
4. PY00/PY01 若要支持：`sb_pad_as_output()` 与 `sb_gpiom_to_gpio0()` 需要走 **PIOC** 分支（不是 `HPM_IOC`）。

---

## 4. 重要提醒：换板级构建会翻盘

上面全部结论建立在**当前实跑的那份固件 = `hpm5301evklite` 板级**之上（判据：只有它的 `BOARD_HAS_SPI_BRIDGE` 是 1，而 SPI 桥实测可用）。

**若切到 `boards/akaLinkPro` 板级**，这些会变：

| pad | evklite（现状） | akaLinkPro 板级 |
|---|---|---|
| PA10 | LED（占） | 无 LED，但 `JTAG_PARK_PIN = PA10` |
| PA26 | **空闲** | `nRESET` + `UART break`（占） |
| PA27 | **空闲** | `JTCK` / SWCLK(target)（占） |
| PA28 | **空闲** | `JTMS` / SWDIO(target)（占） |
| PA30 | 物理不可用 | `JTMS_DIR`（SWDIO 方向控制，占） |
| PB11 / PB12 | SPI2 SCLK / MISO | LED1 / LED2 |
| SWD target 脚 | PA06 / PA07 | PA27 / PA28 |

⇒ **"哪几根能释放"这个问题，答案取决于最终产品用哪个板级构建。** 建议先把这个拍下来，再定 pad 表。
