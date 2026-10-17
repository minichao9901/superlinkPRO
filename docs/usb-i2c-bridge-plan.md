# USB → I2C 转发桥（方案与实现记录）

> 分支 `feature/usb-i2c-bridge` · 协议真源 `src/i2c_bridge/i2c_bridge_proto.h`
> 网页侧说明 [`docs/web-handoff-i2c-bridge.md`](web-handoff-i2c-bridge.md)
> 自检工具 `script_test/i2c_bridge_test.py`

## 1. 目标与边界

把探针当"USB 转 I2C 主机"用：上位机（网页）通过 HID 发一次事务，探针在目标总线上
产生 `START … STOP`，读回的数据原样带回来。定位与 SPI 桥（0x35）一样是**工具**，
不是高速数据面。

**与 SPI 桥的三处不同（都是 I2C 的物理特性决定的）**：

| | SPI/QSPI 桥（0x35） | I2C 桥（0x36） |
| --- | --- | --- |
| 数据面 | 另开一对 bulk（0x0B/0x8B），有序帧流 | **只走 HID**：一条报文装一次事务（写 ≤51 B / 读 ≤54 B） |
| 传输引擎 | 轮询/DMA 两路可配 | **纯轮询**，不用 DMA（100 kHz 下一个字节 90 µs，DMA 的固定开销比它还大） |
| 执行位置 | 帧在主循环按预算执行 | 事务在主循环执行；HID 中断只**登记请求**（一次事务最长 ~5 ms，绝不能占着 USB 中断） |

不开 bulk 的账：I2C 400 kHz 下 512 B 要 11 ms，而"登记 + 轮询 RESULT"两次 HID 往返约 2 ms；
真要大块，主机分片（见 web-handoff §6）比开一套 bulk 端点 + 环 + 代数便宜得多。

## 2. 引脚选择（为什么是 PA28/PA29）

把 HPM5301 的**全部 I2C 功能脚**与 EVKLite 的 **J3 排针**交叉比对，只有一对同时满足
"引出来了 + 没被别的东西占"：

| 实例 | 候选脚对 | 结论 |
| --- | --- | --- |
| I2C0 | PA02(SCL)+PA03(SDA) | PA03 = USER 按键，占 |
| I2C0 | PA18/PA19、PB02/PB03、PX02/PX03 | 未引出到 J3 / QSPI NOR 专用 |
| I2C1 | PA06/PA07 | **SWD 目标口**，占 |
| I2C1 | PA22/PA23、PB06/PB07、PX06/PX07 | 未引出 |
| I2C2 | PA08(SCL)+PA09(SDA) | PA08 = nRESET，占 |
| I2C2 | PA24/PA25、PB08/PB09、PY02/PY03 | USB / CDC UART2 / 未引出 |
| **I2C3** | **PA28(SDA)+PA29(SCL)** | ✅ **J3[21] / J3[19]，本固件里空闲** |
| I2C3 | PB12/PB13 | SPI2 的 MISO/MOSI，占 |

电气注意（都已写进板级 `init_i2c_bridge_pins()`）：

- `OD=1` 开漏（I2C 硬要求）+ `LOOP_BACK`（IOC 手册里的 "force input on"，
  SDK 的 `init_i2c2_pins()` 对 I2C 也是这么加的）；
- 内部上拉只有 `pullup=1` 才开 —— **`PE=1/PS=0` 是下拉**，会把总线按住，所以"不用内部上拉"
  必须是 `PE=0`；
- ⚠️ **PA29 与 USB0_OC 网络共用**（AP2151 的 nFAULT + R6 10k 上拉）：开漏使用没问题，
  但 USB 限流报故障时 nFAULT 会把 SCL 拉低 —— 遇到就 `RESET` 恢复；
- ⚠️ **PA28 板上没有上拉**：裸芯片要外接 4.7k~10k 到 3.3V（常见 I2C 模块自带，接上就够）。

## 3. 协议（一句话版）

```
HID 0x36：STATUS / ENABLE / RESET / SET_CFG / GET_CFG / XFER / RESULT / SCAN
          + 诊断 DBG / PINTEST / BITPROBE(实验性)
XFER  = 一次事务（子地址 + 写数据 + repeated START + 读），主循环执行
RESULT= 取上一次完成的错误码与数据（主机轮询）
```
逐字节布局、状态字、错误码、计数器、取结果流程 → 见 web-handoff 文档（唯一权威是 proto.h）。

## 4. 代码结构

| 文件 | 作用 |
| --- | --- |
| `src/i2c_bridge/i2c_bridge_proto.h` | **线路契约（唯一真源）**：动作/状态字/错误码/配置块/XFER 布局/诊断位定义 |
| `src/i2c_bridge/i2c_bridge.c` | 实现：请求登记（ISR）/ 事务执行（主循环）/ 引脚与总线恢复 / 诊断 |
| `src/i2c_bridge/i2c_bridge.h` | 对外接口（init / poll / hid / usb_reset / owns_pad） |
| `boards/hpm5301evklite/pinmux.c` | `init_i2c_bridge_pins()` / `..._pins_gpio()`（板级焊盘配置） |
| `script_test/i2c_bridge_test.py` | 自检与手动工具（`eeprom` 是数据面门禁；`err` 是错误路径门禁） |

接入点：`api_param.c`（CMD 0x36 分派）、`main.c`（init + poll）、`usb_composite.c`（USB 复位清账）、
`CMakeLists.txt`（源与 include）。两块板靠 `BOARD_HAS_I2C_BRIDGE` 切换，原板编成空实现。

**与 SPI 桥的交叉检查**：PA28/PA29 同时也在 SPI 桥的辅助脚 pad 表里（索引 16/17）。
I2C 桥使能时 `i2c_bridge_owns_pad()` 返回 1，SPI 桥的 `sb_pad_usable()` 会直接拒掉这两根
（`PIN_CFG` 回 E_RANGE），避免两个模块抢同一根脚 —— 谁后配谁赢那种静默故障。

## 5. 上板验证记录（2026-10-02，AT24Cxx @0x50）

**数据面**（`python script_test/i2c_bridge_test.py eeprom`）：

```
1. 扫描总线         → 0x50
2. 基线读 0xE0 × 8  → 00 00 00 00 00 00 00 00
3. 写 A5 5A DE AD BE EF 12 34 → 回读逐字节一致；等 10 ms 写周期
4. 还原原内容        → 回读一致
5. 54 B 一次块读     → 57 45 4c 43 4f 4d 20 54 4f 20 52 54 54 (\"WELCOM TO RTT\")…
6. frames_ok=12 frames_err=0 bytes_rx=158
```

**档位**：同一笔 54 B 读，100 kHz → **5243 µs**，400 kHz → **1309 µs**（4.0×，与档位一致）；
本模块在 1 MHz 档下也跟得上。**错误路径**（`... err`）：无器件 NACK、参数越界（flags/addr_len/
wr_len/rd_len）、未知 action、未使能、事务中重发 → 全部按预期报码。

## 6. 踩过的坑（改这块之前先看）

1. **子地址曾经按"u32 小端取低 addr_len 字节、MSB 在前"解释** —— `addr_len=1` 时取到的是
   u32 的最高字节（恒 0），于是**每次读都从地址 0 开始**（dump 出来永远是同一段，白查一轮）。
   现在协议就是"字节数组按原序发出"：`addr[0]` 先发。
2. **GPIO 接管焊盘必须两步**：`gpiom_set_pin_controller()` + `gpiom_enable_pin_visibility()`。
   只做 visibility 的话，`gpio_write_pin()` 写了也不出去（引脚还是老样子），现象和"脚没接上"
   一模一样。SPI 桥的 `sb_gpiom_to_gpio0()` 是同一个写法（总线恢复打拍也依赖它）。
3. **别用 GPIO 输出脚的 DI 去判线电平**：本板上读回来恒 1，据此做"拉低自检"会误报
   "SDA/SCL 拉不低"。判总线请用**控制器自己的 `LINESDA/LINESCL`**（`PINTEST` 的 bit16/17
   就是靠它在真实事务里采样出来的），这是"我们的脚到底有没有驱动总线"的硬证据。
4. **HID 中断里不能做事务**（最长 5 ms）；HID 只登记，主循环执行，主机轮询 RESULT ——
   与 CMD_RISCV / CMD_SCOPE 标定同一套模式。回包必须在 OUT 回调里同步就绪，所以
   XFER 的应答只可能是"受理结果"，数据只能靠轮询。
5. **请求报文要补零到 64 B**（WebHID 的 `buildRequest()` 已经这么做）：短报文会让固件读到
   上一次残留的字节（HID 缓冲区是复用的）。

## 7. 还没做的

- 多字节子地址（2~4 B）、10 位地址（v2 预留）、时钟拉伸极限：协议都留了位置，未专门验证。
- `RESET` 总线恢复：实现完整，但**没遇到过真卡死的总线**，未验证（判定方法：PINTEST 报
  "空闲 SCL/SDA 常低" → 发 RESET → 再看电平）。
- `BITPROBE`（纯 GPIO 位翻转探测）：**实测不可信**（器件存在也报 no-ACK），要么修好
  （把 ACK 采样改成"交回 I2C 复用后读 LINESDA"），要么删掉。判器件在不在请用 SCAN/XFER。
- 桥没有"总线占用计数/自动让路"：长事务会顿一下 DAP/CDC（文档已写明，面板该提示）。

## 8. 已结案：探针"整个哑掉、只能拔插恢复"——真凶是"未使能时读没开时钟的 I2C 寄存器"

**症状**：探针照样枚举（设备管理器里全套接口都在、EP0 还能读描述符），但 **HID 一个命令也不回**、
**CDC 端口打不开**（Windows 报"连接到系统上的设备没有发挥作用"）；USB 端口复位（libusb `reset()`）
救不回来，只能拔插/按 RESET。**在 main 上怎么折腾都不出现**，只在 I2C 分支上出现——所以最初误判成
"新功能把 USB 层搞坏了"。

**真因（2026-10-02 用 J-Link 抓现场 + 代码对照确认）**：
`ib_status_word()` 被**每一条** HID 0x36 命令调到（`ib_put_status()`），而它以前**无条件**读
`IB_I2C->STATUS`；可 `boards/hpm5301evklite/clock.c` 的 `init_board_clock()` 里**没有 `clock_i2c3`**
——上电时 I2C3 这个 IP 的**时钟是关着的**，只有 `ENABLE` 时才会被 `ib_hw_init()` 打开。
**给"没开时钟的 IP"发一次 AHB 读，事务可能永远不完成** ⇒ CPU 就停在 USB 中断里 ⇒ EP0/HID/CDC
一起哑掉 ⇒ 只能复位。它时好时坏（同样的 `info` 有时成功），所以 15 轮压测复现不出来 —— 压测时
桥是**已使能**的，时钟开着，根本不会踩到。

**修法（commit a4d5205）**，双保险：
1. `i2c_bridge_init()` 上电就把 IP 时钟打开（`clock_add_to_group(clock_i2c3, 0)`）；
2. `ib_status_word()` **未使能时一个寄存器都不读**；`DBG` 动作未使能直接回 `E_DISABLED`。

**验证**：修复版上 `info/enable/pintest/scan(0x50)/eeprom/err` 门禁全过，`eeprom` 连跑 **10 遍 10/10**，
每遍之后探针都健在；修复前"发第一条 0x36 ⇒ 1 分钟内 EP0 就 Pipe error"。

**排查这个坑时踩到的三个"假象"（下次别再被骗）**：
1. **浏览器 WebHID 会抢 HID IN 的应答**：网页开着时命令行 `hid` 写进去没回音、看着像"HID 死了"
   （烧录/命令行测试前先把页面断开）；
2. **HID 报文的 `req[1]`（长度）不能写 0**，会被当空请求丢掉 —— 正确写法见
   `script_test/i2c_bridge_test.py` 的 `xfer()`：`req = [0x01, 2+len(data), cmd, action, ...]`；
3. **Windows 侧 USB 管道卡住**时报 EP0 `Pipe error` / CDC "设备没有发挥作用"，只有拔插能清
   （设备本身可能完全正常）。

**芯片自身 JTAG = PA04~PA07（J5）**：APP 运行时会被 `DAP_SETUP()` 复用掉，所以 J-Link 只能
attach 到"没跑 APP"的芯片（**上电时按住 USER 键进 ROM ISP** 最稳）；要在 APP 运行时看现场，
可以临时把 `DAP_SETUP()` 的引脚复用跳过一版再编。

**顺带发现（与本功能无关，未并入本分支）**：`main.c` 的 `dfu_key_poll()` 里
`clock_get_frequency(clock_mchtmr0)` 在本 SoC 返回 **0**（没有兜底），于是"按住 1 秒进 DFU"的阈值
退化成 0 ⇒ **按一下 USER 键就立刻复位进 DFU**；表现为"APP 枚举完就被打断、看起来像 USB 半死"。
修法：`if (mchtmr_freq == 0U) mchtmr_freq = 24000000UL;`（工程里 scope/rtt/riscv/spi_bridge 都这么兜）。

