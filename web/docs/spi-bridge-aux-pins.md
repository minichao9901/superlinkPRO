# SPI/QSPI 桥 · 辅助脚现状与硬约束

> **这份是交接单**：给 akaLinkPro 固件侧更新它的「描述」与「约束」用。
> 结论来源：2026-09-30 真机 + 逻辑分析仪（KingstVIS MIPI16，500 MHz 采样）实测，并与固件源码逐条对账。
> 网页侧（`web-serial-rtt-tools`）对应的实现与复现脚本见文末第 8 节。

---

## 1. 一句话现状

探针实物是 **akaLinkPro**，但**跑的是 hpm5301evklite 那份板级固件**。在这套组合下，SPI/QSPI 桥的「辅助脚」真正能用的只有 **PA02 / PA09 / PA00 / PA01 / PA31** 五根；其余要么被 SPI2 占、要么被 LED 任务占、要么被板上电路短到地、要么 v1 不支持。

现场已验收可用配置（ST77916 360×360 QSPI 屏点亮）：

| 项 | 值 |
|---|---|
| 档位 | `profile 2`（qspi：`0x02` + 24 bit 地址 `00 XX 00` + 参数；像素 `0x32` 四线） |
| SCLK | 40 MHz |
| **模式** | **`mode 0`**（标准 SPI mode 0；见第 7 节第 1 条的历史坑） |
| RST | **PA02（pad 5，J3[7]）** |
| BL | **PA31（pad 13，J3[11]）** |
| DC | 不用（pad 0；QSPI 档不需要 DC） |
| 有效电平位图 | `0x06`（bit1 RST + bit2 CS 低有效；**BL 不在位图里**，逻辑 1 = 物理高） |

---

## 2. 板级身份（这一条决定了后面所有约束）

| 判据 | 事实 |
|---|---|
| `boards/hpm5301evklite/board.h` | `BOARD_HAS_SPI_BRIDGE (1)`；`LED1_PIN = LED2_PIN = IOC_PAD_PA10`；`BOARD_LED_ACTIVE_LOW (1)`；`nRESET = PA08`；`BOARD_HAS_JTRST (0)` |
| `boards/akaLinkPro/board.h` | `BOARD_HAS_SPI_BRIDGE **(0)**`；`LED1 = PB11 / LED2 = PB12`；`JTRST = PA31`；`JTAG_PARK_PIN = PA10`；`nRESET = PA26`；`BOARD_HAS_JTRST (1)` |
| 实测 | SPI 桥功能确实可用（194 条 ST77916 QSPI 初始化逐字节对账通过）⇒ **运行的固件编译自 evklite 板级**（akaLinkPro 那份根本没编进桥） |

⚠️ **请固件侧确认这是有意为之还是历史遗留**：「akaLinkPro 实物 + evklite 板级定义」会让 LED / nRESET / JTAG 这几项的引脚与实物对不上（影响见第 4 节 pad 11 与第 7 节）。若将来真的做出「akaLinkPro 板级 + 打开桥」的构建，**第 4 节的可用性矩阵必须重算**（那时 PA10 不再被 LED 占，但 PA31 变成 JTRST、PB11/PB12 变成 LED）。

---

## 3. SPI2 桥自己占用的引脚（权威来源：`boards/hpm5301evklite/pinmux.c:192 init_spi2_bridge_pins()`）

| pad | 功能 | J3 | 备注 |
|---|---|---|---|
| PB10 | `SPI2_CS_0` 或 `GPIO_B_10` | J3[26] | 由 `cs_policy` 决定：0/2 = 软件 GPIO CS，3 = 硬件 CS0 |
| PB11 | `SPI2_SCLK` | J3[13] | **必须带 `LOOP_BACK` 位**（见 `pinmux.c:196-211` 的注释：少了它 RX FIFO 读出来恒为 0x00/0xFF） |
| PB12 | `SPI2_MISO` | J3[27] | |
| PB13 | `SPI2_MOSI` | J3[28] | |
| PB14 | `SPI2_DAT2`（仅 quad 档） | J3[10] | 板上原接的 CH340 是 NC |
| PB15 | `SPI2_DAT3`（仅 quad 档） | J3[8] | 同上 |

⇒ **PB10~PB15 必须整段视为保留**（`sb_pad_usable()` 的 `reserved[]` 正是这六根 + PA30），无论当前是不是 quad 档。

---

## 4. 辅助脚全表（协议索引 → 物理 pad → 接头 → 可用性 → 证据）

| idx | pad | J3 | 能当辅助脚？ | 证据 / 原因 |
|---|---|---|---|---|
| 0 | — | — | 不用 | 协议里 **0 = 不用**（不是 0xFF） |
| 1 | PB11 | J3[13] | ❌ | SPI2_SCLK |
| 2 | PB12 | J3[27] | ❌ | SPI2_MISO |
| 3 | PB13 | J3[28] | ❌ | SPI2_MOSI |
| 4 | PB10 | J3[26] | ❌ | SPI2_CS |
| 5 | **PA02** | **J3[7]** | ✅ **实测可用** | RST 脉冲：LA 抓到 **10.0 ms** 低电平（帧要求 10 ms），慢速方波 293 / 282 ms 档位干净 |
| 6 | PA09 | J3[32] | ⚠️ 可用但**有板载按键**（TinyUF2） | 未实测 |
| 7 | PA00 | J3[36] | ⚠️ 可用，可能是 UART0 TX / log | 未实测 |
| 8 | PA01 | J3[38] | ⚠️ 可用，可能是 UART0 RX / log | 未实测 |
| 9 | PY00 | J3[29] | ❌ | v1 不支持（`s_pad_table[9] = 0`） |
| 10 | PY01 | J3[31] | ❌ | 同上 |
| 11 | PA10 | J3[33] | ❌ **被固件 LED 任务占用** | 见下方专条 |
| 12 | PA30 | J3[37] | ❌ | USB0_PWR 网络被板上 Q1 短到地，拉不动 |
| 13 | **PA31** | **J3[11]** | ✅ **实测可用**（当慢速输出） | BL 电平：LA 抓到 327 / 343 / 345 / 347 / 335 ms 档位干净（命令要求 300 ms）。脚上有 USB0_ID 网络（100k 上拉 + BAT54A + 10k → Q1 栅极），推挽输出能压过去，但**只适合慢速静态电平**（BL/RST 这类），别拿它跑时钟 |

### 4.1 pad 11 / PA10 为什么不能用（重要，建议写进固件描述）

`boards/hpm5301evklite/board.h`：

```c
#define BOARD_LED1_PIN          IOC_PAD_PA10
#define BOARD_LED2_PIN          IOC_PAD_PA10
#define BOARD_LED_ACTIVE_LOW    (1)
```

`src/led/led_state.c`：`led_write(LED1_PIN, ...)` / `led_write(LED2_PIN, ...)`，由 `LED_TICK_TIMER` 驱动、**`LED_TICK_PERIOD_MS = 50`** —— 也就是**每 50 ms 就往 PA10 写一次**。

实测现象（LA 500 MHz，把 PA10 配成 BL 并命令它拉到物理低并保持 250 ms）：

```
CH7(PA10)：4.5 s 的整段采集里只有 12 个 ~20 ns 宽的毛刺，间隔精确是 50 ms 的整数倍，
           从来没有出现过持续电平；
同一份代码、同一次采集里把 BL 线改指到 PA02：290.8 / 284.0 / 282.4 ms 的档位干净利落。
```

即：**SPI 桥把 PA10 配成推挽输出后，LED 任务仍在每 50 ms 覆盖它**（20 ns 量级 = 同一寄存器的读-改-写竞争）。所以这一根**不能当辅助脚用**。

**建议固件侧二选一**：
1. `led_write()` 前判断该 pad 是否已被桥当作辅助脚占用（或 LED 引脚等于 `s_pad_*` 里任一非 0 值时跳过写入）；
2. 至少在协议文档/描述里把 pad 11 标成「不可用」并说明原因。

### 4.2 pad 16/17（PA28/PA29）＝ I2C 桥的固定 SDA/SCL（2026-10-03 起在引脚分配图上固定标出）

用户 2026-10-03 要求：「I2C 引脚固定、不做可变引脚，只希望显示在**引脚分配图**上，并且是固定显示位置的。」
所以 `app/spi/bus-view.js` 的 `renderPinMap()` 里，**J3[19] / J3[21] 这两行的角色写死成 `i2c`**：
`◆` + 「I2C 桥 SCL/SDA」+「固定」徽标 + `.is-i2c` 配色，图例与脚注也各加一句 ——
**与配置、与 I2C 有没有使能都无关**（接线的人必须先看到这两根已经名花有主）。

它们仍然**留在辅助脚下拉里、且不灰掉**：固件侧是**动态**拒的，不是永远不能用 ——

| 事实 | 出处 |
|---|---|
| SDA = `BOARD_I2C_BRIDGE_SDA_PAD` = **PA28 = J3[21]**；SCL = `BOARD_I2C_BRIDGE_SCL_PAD` = **PA29 = J3[19]**（编译期常量，不可配） | `firmware/application_5301/src/i2c_bridge/i2c_bridge.c` 顶部注释 + `i2c_bridge.h` |
| I2C 桥**使能期间**这两根被它占用：`i2c_bridge_owns_pad(pad)` 命中即拒 | `i2c_bridge.c:649` |
| SPI 桥侧据此拒它们当辅助脚 | `spi_bridge.c:1918`（`if (i2c_bridge_owns_pad(pad) != 0U) …`） |
| 若配置里真把 DC/RST/BL 配到这两根上（而 I2C 正使能），引脚图上会把它标红 ⚠ 并写明原因 | `renderPinMap()` 的 `stale` / `why`（`i2c: 'I2C 桥的固定脚（I2C 一使能就被它占用）'`） |

回归：`tools/selftest/spi-bus-page.test.mjs` 第 3 节（6 条断言：两行的 PA29/PA28 + SCL/SDA 文案、
「固定」徽标与配色各 2 处、图例里有 ◆、脚注给出 J3[19]/J3[21]）。

---

## 5. LA 通道映射（实测得出，不是照文档抄的）

| LA 通道 | 实测承载 | 探针侧 |
|---|---|---|
| CH0 | SCLK | PB11 / J3[13] |
| CH1 | D0 / MOSI | PB13 / J3[28] |
| CH2 | D1 / MISO | PB12 / J3[27] |
| CH3 | D2 | PB14 / J3[10]（命令相位恒高 = WP#/HOLD# 释放） |
| CH4 | D3 | PB15 / J3[8]（同上） |
| CH5 | **RST** | PA02 / J3[7] |
| CH6 | CS | PB10 / J3[26] |
| CH7 | **BL** | PA31 / J3[11] |

⚠️ 注意：**通道映射必须用「指纹实验」现场确认**（给某个 pad 发特征波形，看哪一路动）。本次排查中曾因一次探针 USB 掉线期的数据把通道认错，报告跟着错了一次。

---

## 6. 时钟/采样沿的真值（LA 测得，和模式字段对应）

判据：给一条数据线发已知字节，量它相对 SCLK 的跳转时刻。

| 固件 `mode` | 数据换在哪个沿 | 从机应在哪个沿采样 | 解码方式 |
|---|---|---|---|
| **0** | **下降沿**（实测 7 次跳转里 6 次落在下降沿后 0~2 ns；剩下 1 次是 CS 拉低后、第一个时钟沿之前的首次建立） | **上升沿**（= 标准 CPHA=0） | 上升沿采样 → 与内置表逐字节一致（`02 00 F0 00 28` = `{0xF0,{0x28}}`） |
| 1 | 上升沿 | 下降沿 | 上升沿采样会解出右移一位的错值 |

（上表是 **2026-09-30 CPHA 修复之后**的行为，固件 commit `e7bc24f`。）

---

## 7. 建议固件侧更新到「描述 / 约束」里的点

1. **CPHA 枚举语义写清楚（已修，但建议留注释）**：`spi_sclk_sampling_odd_clk_edges = 0` = 在**前导沿**（SCLK 空闲低时即上升沿）采样 = 标准 CPHA=0；`even = 1` = 后沿采样 = 标准 CPHA=1。曾经的写法 `cpha = (mode & 1) ? odd : even` 把两者写反，导致 `mode 0` 出来的是标准 mode 1 —— **回环自测发现不了**（跳线短接时主机收发用同一个 cpha，自洽就能回读），只有接固定 CPHA 的真器件才暴露。建议在协议文档里附一句「mode 字段 = 标准 `CPOL<<1 | CPHA`，且给出两个枚举的物理含义」。
2. **pad 11 / PA10 标注不可用**（第 4.1 节），或在固件里让 LED 任务避让辅助脚。
3. **PB10~PB15 整段保留**，别只在 quad 档才视为占用。
4. **PA30 不可用**（被 Q1 短地）；**PY00/PY01 v1 不支持** —— 这两条建议在 `GET_CFG` 的描述里也写明。
5. **`GET_CFG` 的回读语义**：回报前会经 `sb_pad_usable()` 消毒（不可用的一律回 0）。要写明「回读 0 只表示『不是可用辅助脚』，不等于硬件一定没被配过」。
6. **`PIN_CFG` 只在 `s_enabled != 0` 时才落地硬件**（`spi_bridge.c:2346-2349`）。即：失能状态下改辅助脚，**`GET_CFG` 会回报新值但引脚没变**。这是「回读值 ≠ 已生效引脚」的真实边界，建议写进协议描述。另外 PIN_CFG 的请求第 4 字节目前是保留（主机恒发 0），若要扩展请注意兼容。
7. **`SB_T_GPIO` 的取数不一致**：`DC/RST/BL` 取的是硬件缓存 `s_pad_dc/rst/bl`，而 `CS_AUX` 取的是 `sb_pad_of(s_cfg.pad_cs_aux)`（`spi_bridge.c:1425-1435`）。两者在「改了配置但还没使能」的窗口里会不一致 —— 建议统一，或至少在描述里注明。
8. **`SET_PROFILE` 会误清 PA30/PA31**：`sb_cfg_validate(&s_cfg)` 因**任何**原因失败时（不只是 pad，还包括 bits/mode/cs_policy/撞保留脚），都会把五个辅助脚槽里等于 PA30/PA31 的悄悄清成 `SB_PAD_NONE`（`spi_bridge.c:2378-2399`）。而 `sb_pad_ok()` 的注释又写着「SPI2 时代 quad 与否都不再占用 PA30/PA31，旧规则必须去掉」—— 两处自相矛盾。建议要么删掉这段清理，要么把它限制成「仅当确实与 quad 冲突时」。
9. **复位脉冲可能被留在有效电平**：HID 侧的 RESET / ABORT / USB reset 若落在 RST 脉冲中途，脚会停在拉低状态。建议收尾统一释放，或加个「退出时释放辅助脚」的保证。
10. **探针重枚举后桥配置清零**：这是实测到的现场行为（重烧/重枚举后 `padRst/padBl/sclk` 回默认、档位回 raw）。若这是设计如此，建议在协议文档里明确写「配置不持久化，主机每次连接后需重新下发」。

---

## 8. 复现与测量方法（网页侧脚本，`web-serial-rtt-tools/tmp/`）

| 脚本 | 作用 |
|---|---|
| `la-run2.py` | 采集：自由采集要发 `set-trigger --reset`（**别**用悬空通道当"永不命中"的触发）；采样率是离散档（`get-supported-sample-rate` 查，25 MHz 会 NAK）；`set-sample-time` 会把采样率自动降到达深度以内；带 GO/READY 双向握手，短窗口必须缩短 READY 延迟 |
| `la-decode.py` | **自己解码** LA 导出的跳变 CSV（按 SCLK 上升/下降沿分别采样四条数据线，MSB-first 拼字节），不依赖 KingstVIS 的解码器设置 |
| `la-edge-rel.py` | 量「数据相对 SCLK 换在哪个沿」—— 判断桥实际跑的是哪个模式 |
| `hw-mode.mjs <0..3>` | 改桥模式 + 重放表头几帧，配合上面的脚本验证 |
| `hw-bl-pin.mjs <padIdx>` | 换辅助脚 + 带参照脉冲（同时给 PA02 打一个已知好脉冲，证明采集链路是活的） |
| `hw-tabs.mjs --close` | 清理占用 WebUSB 接口的僵尸标签/浏览器实例 |
| `hw-heal.mjs` | 探针掉线恢复（`teardown → connectHid → connectUsb → setEnabled`），恢复后**必须重下发配置** |

经验小结（都踩过）：
- 采样率必须远高于 SCLK（40 MHz 时钟用 20 MHz 采样会欠采样，只看得到 1 个边沿）。
- 「发一条带 RSP 的帧看 `rsps[0].status`」才是可靠的连通判据；`counters` 是轮询快照，`dataReady` 掉线后仍为 true。
- 帧类型：**GPIO = 0x03、PING = 0x05** —— 把 PING 当 GPIO 发，固件会老实回 `status=0`，而线上一个电平都不动（本次排查为此白跑了好几轮）。
