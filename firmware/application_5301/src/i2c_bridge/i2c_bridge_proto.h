/* SPDX-License-Identifier: Apache-2.0 */
/* Copyright (c) 2026 akaInstruments */

/*
 * USB -> I2C 转发协议（线路契约，唯一真源）
 *
 * 设计文档：docs/usb-i2c-bridge-plan.md
 * 本文件同时被固件与上位机（web）参照；改这里必须同步：
 *   - firmware/application_5301/src/i2c_bridge/i2c_bridge.c（实现）
 *   - script_test/i2c_bridge_test.py（协议自检）
 *   - docs/web-handoff-i2c-bridge.md（网页侧说明）
 *
 * 与 SPI 桥（0x35）的区别：I2C 慢、事务小（一次几百 µs、几十字节），
 * 所以**只走 HID** —— 不开 bulk 端点、不做 DMA、不需要第二套缓冲：
 *   · 控制面与数据面在同一条 HID 报文里（请求最多 51 B 写数据、响应最多 54 B 读数据）；
 *   · 一次 XFER = 一次完整事务（START … repeated START … STOP）；
 *   · I2C 事务在主循环里执行（最长几毫秒），HID 中断只**登记请求**，绝不阻塞 USB；
 *   · 主机发完 XFER 轮询 RESULT 取结果（与 CMD_RISCV / CMD_SCOPE 标定同一套模式）。
 *
 * 报文约定沿用 CMD 0x31/0x32/0x34/0x35：
 *   req[0]=0x01(report id) req[1]=长度 req[2]=0x36 req[3]=action req[4..]=参数
 *   res[0]=0x02(report id) res[1]=长度 res[2]=0x36 res[3]=action 回显
 *   res[4..7]=状态字(u32 小端) res[8..]=数据
 */

#ifndef I2C_BRIDGE_PROTO_H
#define I2C_BRIDGE_PROTO_H

#include <stdint.h>

/* ============================ HID 控制面（CMD 0x36） ============================ */

#define I2C_HID_CMD 0x36

typedef enum
{
    I2C_ACT_STATUS = 0,   /* 状态字 + 计数器块（res[8..47] = 10 × u32） */
    I2C_ACT_ENABLE = 1,   /* req[4]：0 关（释放引脚）/ 1 开（配引脚 + 初始化 I2C） */
    I2C_ACT_RESET = 2,    /* 总线恢复：9 个 SCL 脉冲 + STOP + 控制器复位；清计数器，保配置 */
    I2C_ACT_SET_CFG = 3,  /* req[4..19] = 配置块（16 B） */
    I2C_ACT_GET_CFG = 4,  /* res[8..23] = 配置块（16 B，含实际生效的 SCL 档） */
    I2C_ACT_XFER = 5,     /* req[4..] = 一次事务；立即回"已登记/忙"，结果用 RESULT 取 */
    I2C_ACT_RESULT = 6,   /* 取上一次完成事务的结果（错误码 + 读数据 / 扫描位图） */
    I2C_ACT_SCAN = 7,     /* 扫描 0x08..0x77（7 位地址全范围），结果同样用 RESULT 取 */
    I2C_ACT_DBG = 10,     /* 上板诊断：res[8..] = 12 × u32 现场快照（寄存器/引脚/计数） */
    I2C_ACT_PINTEST = 11, /* 上板诊断：SDA/SCL 在"内部上拉关/开"两种状态下的电平 */
    I2C_ACT_BITPROBE = 12,/* 上板诊断：**纯 GPIO 位翻转**发一次 START+地址+STOP，
                           * req[4] = 7 位地址。用来把"器件不在"和"外设的零数据探测
                           * 有猫腻"彻底分开 —— 位序、ACK 采样点都由我们自己控制。 */
} i2c_hid_action_t;

/* ============================ 状态字（res[4..7]） ============================ */

/* u32 小端 */
#define I2C_ST_ENABLED (1U << 0)   /* 桥已使能（引脚已被 I2C 占用） */
#define I2C_ST_PENDING (1U << 1)   /* 有一次请求已登记、还没执行完（主机应轮询 RESULT） */
#define I2C_ST_BUS_OK (1U << 2)    /* 控制器不在忙（BUSBUSY = 0）。注意：它**不**表示"没有挂起请求" ——
                                    * 那一位看 I2C_ST_PENDING。原注释写成"总线空闲：控制器不在忙、
                                    * 也没有挂起请求"，与实现不符，2026-10-03 按实现订正。 */
#define I2C_ST_SDA (1U << 3)       /* SDA 线电平（控制器 LINESDA 感知，事务中读也安全） */
#define I2C_ST_SCL (1U << 4)       /* SCL 线电平（LINESCL） */
#define I2C_ST_SHIFT_ERR 8U        /* bit8..15：最近一次**完成**事务的错误码 */
#define I2C_ST_SHIFT_DONE 16U      /* bit16..23：已完成事务计数（低 8 位，回绕；比对"变了没有"即可） */
#define I2C_ST_SHIFT_CMD 24U       /* bit24..31：**本命令**的结果码（见下） */

/* 线电平来自 I2C 控制器自己的 LINESDA/LINESCL 状态位（HPM 的 IOC 焊盘没有回读
 * 寄存器，但控制器有）—— 所以这两位在事务进行中读也是真实的，主机可以拿它判
 * "总线是不是被谁拽住了"。 */
/*
 * 每条命令的响应都会在 bit24..31 回一个**本命令结果码**（i2c_status_t）：
 *   · XFER / SCAN / RESET：0 = 已登记（接下来轮询 RESULT），非 0 = 被拒的原因
 *     （I2C_E_BUSY = 上一条还没做完；I2C_E_DISABLED = 桥没开；I2C_E_RANGE = 参数越界）；
 *   · ENABLE / SET_CFG / RESULT / PINTEST：0 = 正常，非 0 = 错误码；
 *   · STATUS / GET_CFG / DBG：恒 0（这些动作不会失败）。
 * 这样主机发完 XFER 就能立刻知道"收下了没有"，不用去猜 done 计数。
 */

/* ============================ 错误码 ============================ */

typedef enum
{
    I2C_OK = 0,          /* 成功 */
    I2C_E_DISABLED = 1,  /* 桥未使能 */
    I2C_E_BUSY = 2,      /* 上一次请求还没做完，这一条被拒（计数在 STATUS 里） */
    I2C_E_NO_ADDR = 3,   /* 地址相位没收到 ACK：器件不在 / 地址写错 / 没接 */
    I2C_E_NO_ACK = 4,    /* 数据相位被 NACK（从机拒收，例如写保护 / 写满） */
    I2C_E_TIMEOUT = 5,   /* 超时（含控制器忙等超限） */
    I2C_E_RANGE = 6,     /* 参数越界（长度/地址/保留位非法） */
    I2C_E_BAD_FRAME = 7, /* 报文本身不合法（本命令目前只用于保留位/长度检查） */
    I2C_E_BUS_STUCK = 8, /* 总线被拉死（SDA 或 SCL 常低）：先 RESET 恢复 */
    I2C_E_STATE = 9,     /* 其它状态错误（控制器复位后仍不可用） */
} i2c_status_t;

/* ============================ 配置块（16 B，SET_CFG / GET_CFG） ============================ */
/* ⚠️ 主机按**显式字节偏移**打包（别按结构体对齐猜），布局改了要同步脚本。
 * 与 SPI 桥的 sclk_hz 一样：`scl_hz` 是**档位选择器**，不是精确频率 ——
 * HPM 的 I2C 只有三个标准档（100k / 400k / 1M），固件挑"不超过请求值"的最高档，
 * 并在 GET_CFG 的 actual_scl_hz 与 STATUS 的 [8] 里回报实际生效值。 */
typedef struct
{
    uint32_t scl_hz;      /* 0 = 默认 100 kHz；≤100k→100k，≤400k→400k，否则 1 MHz */
    uint8_t pullup;       /* 1 = 打开 PA28/PA29 的内部上拉（板上已有外部上拉时保持 0） */
    uint8_t retries;      /* 遇到 NACK/超时后整笔重试次数（0 = 不重试）；上限 8 */
    uint8_t flags;        /* 保留，必须 0（非 0 报 I2C_E_RANGE） */
    uint8_t reserved0;
    uint32_t actual_scl_hz; /* 只读：实际生效的 SCL（GET_CFG 回报） */
    uint32_t reserved1;
} i2c_cfg_t;

/* ============================ 计数器块（STATUS 的 res[8..47]） ============================ */
/* 10 × u32 小端，顺序即线上顺序：
 *   [0] frames_ok      成功事务数
 *   [1] frames_err     失败事务数
 *   [2] bytes_tx       写出去的数据字节（不含地址与子地址）
 *   [3] bytes_rx       读回来的数据字节
 *   [4] nack_addr      地址相位 NACK 次数
 *   [5] nack_data      数据相位 NACK 次数
 *   [6] timeouts       超时次数
 *   [7] bus_recover    执行 RESET（总线恢复）的次数
 *   [8] actual_scl_hz  实际 SCL
 *   [9] last_ticks     最近一次事务耗时（MCHTMR ticks，24 MHz）
 * 主机侧不要按结构体读，按线序读（与 spi_bridge 的 STATUS 同一约定）。 */

#define I2C_STAT_WORDS 10U

/* ============================ XFER：一次事务 ============================ */
/*
 * req[4..63] 布局（参数区共 60 B）：
 *   偏移  长度  字段
 *    4     1   flags     保留，必须 0（bit0 = 10 位地址是 v2 的预留）
 *    5     1   dev       7 位从机地址（bit7 必须为 0）
 *    6     1   addr_len  子地址字节数 0..4（0 = 不带子地址）
 *    7     1   wr_len    写数据字节数 0..51
 *    8     1   rd_len    读数据字节数 0..54
 *    9     4   addr      子地址字节数组（**按原序发出**：addr[0] 先发；只用前 addr_len 个）
 *   13     n   wr_data   写数据（n = wr_len）
 *
 * ⚠️ 子地址就是"一串字节"，不是 u32 —— 主机想发 0x01,0x00 就填 addr[0]=0x01、addr[1]=0x00。
 *    （早期版本按"u32 小端取低 addr_len 字节、MSB 在前"解释，addr_len=1 时发出去的恒是
 *    0x00，读 EEPROM 永远从地址 0 开始 —— 这个坑踩过。）
 *
 * 线序（四种子情况，都由固件一支笔完成）：
 *   wr_len=0, rd_len=0            → START + dev+W + STOP          （地址探测：ACK/NACK）
 *   wr_len>0, rd_len=0            → START + dev+W + 子地址+数据 + STOP
 *   wr_len=0, rd_len>0, addr_len=0→ START + dev+R + 读 rd_len + STOP
 *   其余                          → START + dev+W + 子地址+数据
 *                                   + repeated START + dev+R + 读 rd_len + STOP
 *   （子地址与写数据在线上是**同一串字节**，拼起来一次发出；这就是最常见的
 *     "写寄存器地址再读"——中间是 repeated START，不发 STOP。）
 *
 * 说明：addr_len=0 且 wr_len>0 时，"写数据"就是那一串字节本身（例如给命令型
 * 器件写一个命令字）；子地址只是语义上的名字，线上没有任何区别。
 */

#define I2C_XFER_HDR 9U        /* req[4..12]：flags/dev/addr_len/wr_len/rd_len/addr */
#define I2C_WR_MAX 51U         /* 60 - 9 */
#define I2C_RD_MAX 54U         /* 响应侧：[10..63] */

/* ============================ RESULT：取结果 ============================ */
/*
 * res[4..7] = 当前状态字（bit1 = PENDING：还在做，先别读数据）
 * res[8]    = 本次事务的错误码（i2c_status_t）
 * res[9]    = 有效数据字节数 n
 * res[10..] = 数据（n 字节）
 *   · XFER 的结果：n = 实际读到的字节数（**出错时一律 0** —— 读到一半的值没有意义，
 *     主机看 res[8] 的错误码就行）
 *   · SCAN 的结果：n = 14，数据是 0x08..0x77 的位图（bit0 = 地址 0x08，1 = 有 ACK）
 * 主机取结果的推荐流程：
 *   1) 发 XFER（或 SCAN）→ 立刻回一次状态字：bit1=1 表示已登记，
 *      bit24..31 = 0；非 0 就是被拒（最常见 I2C_E_BUSY），重发即可；
 *   2) 轮询 RESULT，直到 bit1=0；
 *   3) 读 res[8] 错误码 + res[9] 长度 + res[10..] 数据。
 * 想更严谨就比对状态字 bit16..23（完成计数）：发出请求前记一次，
 * 轮询到它变了才说明这一笔真的做完了。
 */

/* SCAN 的位图覆盖范围（7 位地址的合法区间）与长度 */
#define I2C_SCAN_FIRST 0x08U
#define I2C_SCAN_LAST 0x77U
#define I2C_SCAN_BYTES 14U     /* ceil((0x77-0x08+1)/8) */

/* ============================ DBG：现场快照（12 × u32） ============================ */
/*
 * res[8..55]：
 *   [0] I2C CTRL     [1] I2C STATUS   [2] I2C ADDR     [3] I2C CMD
 *   [4] I2C SETUP    [5] I2C INTEN    [6] PA28 的 FUNC_CTL/PAD_CTL（低 16 / 高 16 位）
 *   [7] PA29 的 FUNC_CTL/PAD_CTL（低 16 / 高 16 位）
 *   [8] cfg 原始（scl_hz）   [9] cfg 实际（actual_scl_hz） [10] 状态字快照
 *   [11] 队列里被拒（I2C_E_BUSY）的请求数
 * 出问题时先看它：CTRL 有没有 IICEN/MASTER、两根脚是不是 I2C 复用（FUNC_CTL）、
 * 上拉有没有打开（PAD_CTL 的 PE/PS）。线的实时电平用 PINTEST 看。
 */

/* ============================ PINTEST：引脚/上拉/驱动自检 ============================ */
/*
 * 接线出问题时第一个跑它（**只在总线空闲时可用**：它会发一次真实探测事务，
 * 事务进行中调用回 I2C_E_BUSY）。
 *
 * res[8..11] = u32：
 *   bit0  空闲 SDA 电平       bit1  空闲 SCL 电平          （控制器线感知）
 *   bit2  打开内部上拉后 SDA  bit3  打开内部上拉后 SCL
 *   bit16 事务中 SCL 曾被拉低（1 = 我们的 SCL 确实在驱动总线）
 *   bit17 事务中 SDA 曾被拉低
 *   bit8..15 问题位图（**0 = 桥这一侧一切正常**）：
 *     bit0 SCL 在事务里从未被拉低 —— 桥侧驱动异常
 *     bit1 空闲 SCL 常低 —— 被谁拽住（短路 / 器件拉住）
 *     bit2 空闲 SDA 常低 —— 同上
 *   ↑ bit16/17 是**最硬的证据**：GPIO 输出脚的 DI 未必可读（早期用它自检会误报
 *     "拉不低"），而控制器自己的 LINESCL/LINESDA 一定跟着焊盘走。事务用 7 位地址里
 *     保留的 0x7E，零数据字节，不会写坏任何东西。
 *
 * 判读口诀：**bit16=1 且问题位图=0 ⇒ 桥没问题，没 ACK 就往器件侧查**
 * （接线、供电、地址、上拉）。bit0..3 用来看上拉：空闲就是 1、打开内部上拉还是 1，
 * 说明线上有上拉（外部或器件的）；空闲 1、打开内部上拉前是 0，说明只有内部上拉在撑。
 *
 * ⚠️ 本板 PA29(SCL) 上有 R6 10k 上拉（USB0_OC 网络），PA28(SDA) 上**没有**上拉 ——
 *    SDA 通常要外接 4.7k~10k 到 3.3V，或者打开内部上拉（pullup=1）应急。
 *    器件板一般自带 4.7k~10k 上拉，接上就够。
 */

/* ============================ BITPROBE：纯 GPIO 位翻转探测 ============================ */
/*
 * req[4] = 7 位地址。响应：
 *   res[8]  = 0 = 收到 ACK（器件在）；3 = 没 ACK（E_NO_ADDR）；其余见 i2c_status_t
 *   res[9]  = 采样到的 9 个位：bit0..7 = 我们发出的地址位（回读 SDA 的实际电平），
 *             bit8 = ACK 位上 SDA 的电平（0 = 被从机拉低 = ACK）
 *   res[10] = SDA 在位 7 之后被放开时的电平、res[11] = 同一时刻 SCL 的电平
 *             （两位都该是 1：证明上拉真的把线拉起来了）
 * 为什么要有它：外设的"零数据字节探测"是我自己拼寄存器实现的（CTRL 只开 START/ADDR/
 * STOP），万一写得不对，现象和"器件不在"一模一样。位翻转这条路径不依赖外设 ——
 * 位序、时序、采样点全在代码里，和 SDK 的 I2C 驱动完全独立。
 * 采样回来的地址位还能反过来验"我们发出去的电平有没有真的上线"。
 */

#endif /* I2C_BRIDGE_PROTO_H */
