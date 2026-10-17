# USB → I2C 转发桥：网页侧（主机侧）实现说明

> 协议真源：`firmware/application_5301/src/i2c_bridge/i2c_bridge_proto.h`（唯一权威，改那边必须同步这里）
> 固件实现：`firmware/application_5301/src/i2c_bridge/i2c_bridge.c`
> 自检工具：`script_test/i2c_bridge_test.py`（网页做之前可以先用它验协议）
> 平台：**仅 HPM5301EVKLite**（`BOARD_HAS_I2C_BRIDGE=1`）；akaLinkPro 原板编成空实现，
> HID 0x36 回"不支持"（`res[1]=0x01`）。

---

## 1. 接线

| 信号 | 探针 | J3 脚 | 说明 |
| --- | --- | --- | --- |
| I2C SDA | **PA28** | **J3[21]** | 板丝印还是 SPI1 时代的 `SPI_MISO`；**板上没有上拉** |
| I2C SCL | **PA29** | **J3[19]** | 板丝印 `SPI_MOSI`；**与 USB0_OC 网络共用**（AP2151 nFAULT + R6 10k 上拉） |
| GND | — | J3[6]/[9]/[14]/[20]/[25]/[30]/[34]/[39] | 必须接 |
| 3.3V | — | J3[1] / J3[17] | 给从器件供电（若器件另有电源可不接） |

- 硬件外设是 **I2C3**，与 SPI2(PB10~PB15)、SWD(PA04~PA08)、CDC UART2(PB08/PB09)、
  UART0(PA00/PA01) 都不冲突。
- **上拉**：SCL 有 R6 10k，SDA 没有。接一块常见 I2C 模块（大多自带 4.7k~10k 上拉）就够；
  裸芯片请外接 4.7k~10k 到 3.3V，或应急打开内部上拉（`SET_CFG` 的 `pullup=1`，电流弱、只适合低速短线）。
- 排查接线先跑 `PINTEST`（见 §7），它会告诉你桥这一侧有没有问题。

## 2. 报文约定（沿用 0x31/0x32/0x34/0x35 那一套）

```
主机 → 设备（HID Report ID = 1，63 B payload，**不足 64 B 要补零**）
    payload[0] = 长度（= 1 + 1 + 数据长度，固件不校验，照文档填）
    payload[1] = 0x36
    payload[2] = action
    payload[3..] = 参数

设备 → 主机（HID Report ID = 2）
    res[0] = 长度
    res[1] = 0x36（命令回显 —— 网页 `inputreport` 里 res[0] 就是它，与页面既有 hidProbe.xfer 一致）
    res[2] = action 回显
    res[3..6] = 状态字 u32 小端
    res[7..]  = 数据（按 action 不同）
```
> 网页侧 `app/hid/probe.js` 的 `buildRequest()` 已经会补零到 63 B；`xfer()` 回来的 `res`
> 已剥掉 Report ID，所以上面这张表里的 `res[i]` 就是网页拿到的 `res[i]`。

## 3. 动作表（HID 0x36）

| 值 | 动作 | 请求 | 响应 |
| --- | --- | --- | --- |
| 0 | **STATUS** | — | `res[7..46]` = 10 × u32 计数器（见 §5） |
| 1 | **ENABLE** | `req[3]` = 0 关 / 1 开 | 状态字。**开**才配引脚 + 初始化 I2C3；关会把两根脚放成高阻输入 |
| 2 | **RESET** | — | 总线恢复：9 个 SCL 脉冲 + STOP + 控制器复位；清计数器、保留配置 |
| 3 | **SET_CFG** | `req[3..18]` = 配置块（16 B，见 §4） | 状态字 |
| 4 | **GET_CFG** | — | `res[7..22]` = 配置块（16 B，含实际生效的 SCL） |
| 5 | **XFER** | `req[3..]` = 一次事务（见 §6） | 状态字 + `res[7]` = 受理结果（0 = 已登记） |
| 6 | **RESULT** | — | 状态字 + `res[7]` = 错误码 + `res[8]` = 数据长度 + `res[9..]` = 数据 |
| 7 | **SCAN** | — | 同 XFER：先回受理结果，再轮询 RESULT 取 14 B 位图 |
| 10 | **DBG** | — | `res[7..54]` = 12 × u32 现场快照（排障用，见 §7） |
| 11 | **PINTEST** | — | `res[7..10]` = 引脚/上拉/驱动自检字（见 §7） |
| 12 | **BITPROBE** | `req[3]` = 7 位地址 | 纯 GPIO 位翻转探测（**实验性**，见 §7 末尾） |

## 4. 状态字（`res[3..6]`）

| 位 | 含义 |
| --- | --- |
| bit0 | `ENABLED` 桥已使能（引脚被 I2C 占用） |
| bit1 | `PENDING` 有一次请求已登记、还没执行完 → **继续轮询 RESULT** |
| bit2 | `BUS_OK` 总线空闲（控制器不在忙、也没有挂起请求） |
| bit3 | `SDA` 线电平（控制器 LINESDA，事务中读也安全） |
| bit4 | `SCL` 线电平（LINESCL） |
| bit8..15 | `LAST_ERR` 最近一次**完成**事务的错误码 |
| bit16..23 | `DONE_CNT` 已完成事务计数（低 8 位，回绕；比对"变了没有"即可） |
| bit24..31 | `CMD_RC` **本命令**的结果码（0 = 正常；XFER/SCAN 下非 0 = 被拒，见 §5） |

### 配置块（16 B，SET_CFG/GET_CFG，按偏移打包，别按结构体对齐猜）

| 偏移 | 类型 | 字段 |
| --- | --- | --- |
| 0 | u32 | `scl_hz` 期望 SCL：0/≤100k → **100 kHz**，≤400k → **400 kHz**，>400k → **1 MHz** |
| 4 | u8 | `pullup` 1 = 打开内部上拉（默认 0） |
| 5 | u8 | `retries` 遇 NACK/超时后整笔重试次数（0..8，默认 0） |
| 6 | u8 | `flags` 保留，必须 0 |
| 7 | u8 | 保留 |
| 8 | u32 | `actual_scl_hz` **只读**：实际生效档位（100000/400000/1000000） |
| 12 | u32 | 保留 |

> `scl_hz` 是**档位选择器**不是精确频率 —— HPM 的 I2C 只有这三个标准档，固件挑"不超过
> 请求值"的最高档，实际值回在 `actual_scl_hz` 与 STATUS 的 `[8]`。

## 5. 错误码与计数器

错误码（`res[7]` / 状态字 bit8..15 / bit24..31）：

| 值 | 名字 | 含义 |
| --- | --- | --- |
| 0 | OK | 成功 / 已登记 |
| 1 | E_DISABLED | 桥没使能（先 `ENABLE 1`） |
| 2 | E_BUSY | 上一条还没做完 → 等一下重发（不要并发发 XFER） |
| 3 | E_NO_ADDR | 地址相位没 ACK：器件不在 / 地址错 / 没接 / 没供电 |
| 4 | E_NO_ACK | 数据相位被 NACK（器件写保护、写周期未完成等） |
| 5 | E_TIMEOUT | 超时 |
| 6 | E_RANGE | 参数越界（长度/flags/地址非法） |
| 7 | E_BAD_FRAME | 未知 action |
| 8 | E_BUS_STUCK | 总线被拉死 → 先发 `RESET` |
| 9 | E_STATE | 其它状态错误 |

STATUS 的 10 个 u32（`res[7..46]`，顺序即线上顺序）：

```
[0] frames_ok     成功事务数          [5] nack_data    数据相位 NACK 次数
[1] frames_err    失败事务数          [6] timeouts     超时次数
[2] bytes_tx      写出的数据字节      [7] bus_recover  RESET(总线恢复) 次数
[3] bytes_rx      读回的数据字节      [8] actual_scl_hz
[4] nack_addr     地址相位 NACK 次数  [9] last_ticks   最近一次事务耗时（24 MHz tick）
```

## 6. XFER：一次事务

请求（`req[3..]` 共 60 B 可用）：

| 偏移 | 长度 | 字段 |
| --- | --- | --- |
| 3 | 1 | `flags` 保留，必须 0 |
| 4 | 1 | `dev` 7 位从机地址（bit7 必须 0） |
| 5 | 1 | `addr_len` 子地址字节数 0..4 |
| 6 | 1 | `wr_len` 写数据字节数 0..51 |
| 7 | 1 | `rd_len` 读数据字节数 0..54 |
| 8 | 4 | `addr` 子地址字节数组，**按原序发出**（`addr[0]` 先发；只用前 `addr_len` 个） |
| 12 | n | `wr_data`（n = `wr_len`） |

线序（四种子情况都由固件一支笔完成）：

| 组合 | 线上 |
| --- | --- |
| `wr_len=0, rd_len=0` | `START + dev+W + STOP`（地址探测，只问 ACK/NACK） |
| `wr_len>0, rd_len=0` | `START + dev+W + 子地址+写数据 + STOP` |
| `wr_len=0, rd_len>0, addr_len=0` | `START + dev+R + 读 rd_len + STOP` |
| 其余 | `START + dev+W + 子地址+写数据 + repeated START + dev+R + 读 rd_len + STOP` |

**读寄存器**（最常见的用法）= 子地址 1~2 B + `rd_len` N + `wr_len` 0 →
线上就是 `START 50 W <reg> [repeated START] 50 R <N 字节> STOP`。

### 取结果的推荐流程

```js
// 1) 发 XFER（或 SCAN）
const r1 = await xfer(0x36, xferData({ dev, addr: [0x00], rd: 8 }));
const rc = (statusWord(r1) >>> 24) & 0xff;
if (rc !== 0) throw new Error(rcText(rc));          // 2 = 忙，重发即可

// 2) 轮询 RESULT，直到 bit1(PENDING) 清零
let r;
do { r = await xfer(0x36, Uint8Array.of(6)); } while (statusWord(r) & 0x02);

// 3) 取错误码 + 数据
const err = r[7], n = r[8], data = r.slice(9, 9 + n);
```
- `SCAN` 的 RESULT：`n = 14`，数据是 **0x08..0x77 的位图**（bit0 = 地址 0x08，1 = 有 ACK）。
- 一次事务最长 ~5 ms（54 B @100 kHz），所以**必须**用"登记 + 轮询"，别指望一条 HID 往返拿到数据。
- 出错时 `n = 0`（读到一半的值不返回）。
- 想更严谨就比对状态字 bit16..23（`DONE_CNT`）：发请求前记一次，轮询到它变了才是这一笔做完。

### 带宽/尺寸限制

| 项 | 上限 | 说明 |
| --- | --- | --- |
| 单次写 | **51 B** | 含子地址前，`wr_len ≤ 51` |
| 单次读 | **54 B** | 超过就分片：先写子地址（`wr_len=0, rd_len=0` 不行 —— 要真发子地址就用 `wr_len=0, rd_len=1`？见下）|
| 扫描 | 112 个地址 / 一次请求 | 100 kHz 下约 15 ms、400 kHz 约 4 ms（主循环分 7 轮做完，不影响 DAP 太久） |

**读超过 54 B 的分片办法**（EEPROM/多数传感器都支持地址自增）：
先发一次"把地址指针推到 N"的**零长度写**（`addr=[N], wr_len=0, rd_len=0` → 线序是
`START dev+W + 子地址 + STOP`，正好是设置地址指针），再分片 `rd 54、54、…`
（每片 `addr_len=0`），设备的内部地址指针会在片间自增。**注意**：FIFO 型器件（读一次
就弹一个数）不能这么分，只能每次重新写地址。

## 7. 排障三件套

| 动作 | 用途 | 判读 |
| --- | --- | --- |
| **PINTEST** (11) | 接线第一件事 | `res[7..10]` 位 u32：bit0/1 = 空闲 SDA/SCL（都该 1）、bit2/3 = 打开内部上拉后的电平、**bit16/17 = 事务中 SCL/SDA 曾被拉低**（1 = 桥的脚确实在驱动总线）、bit8..15 = 问题位图（0 = 桥侧正常）。**bit16=1 且问题位图=0 ⇒ 桥没问题，去查器件侧（接线/供电/地址/上拉）** |
| **DBG** (10) | 卡住时看现场 | 12 × u32：I2C CTRL/STATUS/ADDR/CMD/SETUP/INTEN、PA28/PA29 的 FUNC_CTL\|PAD_CTL、配置、状态字、done 计数、被拒请求数 |
| **RESET** (2) | 总线被拽死（SDA 或 SCL 常低）后恢复 | 9 个 SCL 脉冲 + STOP + 控制器复位；之后重试 |
| ~~BITPROBE~~ (12) | **实验性，别用它判器件在不在** | 纯 GPIO 位翻转的 START+地址+STOP。实测在本板上**即使器件存在也会报 no-ACK**（GPIO 输出脚的 DI 回读不可靠 + 开漏释放时序没做干净，待修）。判"器件在不在"请用 `SCAN` / `XFER` 的地址探测 |

> ⚠️ **ENABLE=0 会把两根脚放成高阻输入**（不还原成 SPI 复用）。要用 SPI 桥的辅助脚
> （pad 16/17 = PA28/PA29）时先关桥 —— 桥使能期间 SPI 桥的 `PIN_CFG`/`SET_CFG` 会
> 直接拒掉这两根（交叉检查 `i2c_bridge_owns_pad()`）。
> ⚠️ 探针复位/重新烧录后桥回到**未使能**状态，配置也回默认（scl=100 kHz、pullup=0）——
> 页面每次连接后都应该先 `GET_CFG` 确认，再按需 `SET_CFG` + `ENABLE 1`。
> ⚠️ I2C 事务在主循环里跑，`SCAN` 与长写会让 DAP/CDC 顿一下（扫描 ~4~15 ms、
> 54 B @100 kHz ~5 ms）。面板上最好提示"扫描中"。

## 8. 一个够用的页面形态（建议）

1. **连接区**：`GET_CFG` 显示当前档位/上拉/实际 SCL；一个"使能"开关（ENABLE）；一个速度下拉（100k/400k/1M）。
2. **扫描区**：一个"扫描总线"按钮 → 表格列出 ACK 的地址（位图 → 地址列表），点一行即选中 `dev`。
3. **寄存器区**：地址（子地址，1~4 B）+ 长度 + "读"/"写"按钮；写的时候给字节输入框。
   每次操作后显示错误码文本（§5 的表）与耗时（`last_ticks / 24` µs）。
4. **连续读/日志区**：定时轮询某个寄存器（例如传感器数据），把值画成曲线 —— 这就是
   "I2C 版 J-Scope" 的最小形态（采样率受 I2C 与 HID 往返限制，100 kHz 下大约几十 Hz~几百 Hz）。
5. **排障区**：三个按钮（PINTEST / DBG / RESET）+ 计数器表格（STATUS）。

## 9. 已验证 / 未验证（截至 2026-10-02）

| 项 | 状态 |
| --- | --- |
| HID 控制面（STATUS/CFG/ENABLE/DBG/PINTEST/RESULT） | ✅ 上板验证 |
| SCAN 扫全总线 | ✅ 上板验证（AT24Cxx 模块，找到 0x50） |
| 寄存器/块读（1~54 B，子地址 1 B） | ✅ 上板验证（读到 EEPROM 内容 "WELCOM TO RTT…"） |
| 页写 + 回读逐字节对账 + 还原 | ✅ 上板验证（`script_test/i2c_bridge_test.py eeprom` 全绿） |
| 100 kHz / 400 kHz / 1 MHz 三档 | ✅ 上板验证（54 B 读：5243 µs → 1309 µs，4×；1 MHz 该 EEPROM 也跟得上） |
| `RESET` 总线恢复 | ✅ 跑通（它会清计数器 —— `frames_ok=1 / bus_recover=1` —— 且执行后总线上事务照常）；⚠️ **真·卡死现场没遇到过**，"能不能把被拽死的总线放开"未验证 |
| 错误路径（无器件 NACK / 参数越界 / 未使能 / 忙） | ✅ 上板验证（`... err` 11 项全绿） |
| 多字节子地址（2~4 B）、10 位地址、时钟拉伸极限 | ⏳ 未专门验证（协议已留位置：`addr_len ≤ 4`；10 位地址是 v2 预留） |
| `BITPROBE`（GPIO 位翻转探测） | ❌ 实测不可信（见 §7），别用 |

## 10. 探针"整个哑掉"时怎么办（已知现象）

如果页面突然**一个命令都不回**、同时 **COM 口也打不开**（Windows 报"设备没有发挥作用"），
而设备管理器里探针还在：这不是 I2C 桥的问题 —— **拔插一次 USB 线**（或按板上 RESET）
即可恢复。2026-10-02 遇到过一次，当时桥处于空闲、15 轮门禁压测无法复现；最可能是
主机侧一次 USB 复位之后没有重新配置设备，导致类端点一直没被重新打开（详见
[`docs/usb-i2c-bridge-plan.md`](usb-i2c-bridge-plan.md) §8）。

页面侧可以做两件事改善体验：
- HID 请求连续超时 2~3 次就提示"探针可能已失联，请拔插 USB"，并**停止后续自动轮询**
  （否则串口/波形页会一直刷超时错误）；
- 重新连接按钮走既有的 `_reacquire()`（重新枚举取设备对象），能自愈"句柄过期"那类问题
  —— 但对上面这种"设备侧端点没打开"无效，只有拔插能救。
