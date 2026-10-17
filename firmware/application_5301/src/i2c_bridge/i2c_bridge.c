/* SPDX-License-Identifier: Apache-2.0 */
/* Copyright (c) 2026 akaInstruments */

/*
 * USB -> I2C 转发桥（HID CMD 0x36）
 *
 * ── 为什么这么"小"──────────────────────────────────────────────────────────
 * I2C 比 SPI 慢一个数量级（100k/400k/1M），事务也小（寄存器读写几个字节、
 * 块读写几十字节），所以这套实现**故意**不做 SPI 桥那三件重家伙：
 *   · 不开 bulk 端点：一次事务的数据（写 ≤51 B / 读 ≤54 B）塞得进一条 HID 报文；
 *   · 不用 DMA：一个字节在 100 kHz 上要 90 µs，DMA 的固定开销比它自己还大；
 *   · 不要环/代数/帧流：这里的"顺序"天然由 HID 的请求-响应一对一保证。
 *
 * ── 为什么事务在主循环里做 ────────────────────────────────────────────────
 * 一次 XFER 最长可以是 51 B @100 kHz ≈ 4.8 ms。HID 的中断里做这个，USB 会掉包
 * （SPI 桥在 ENABLE/SET_CFG 上已经踩过一次：几百 µs 就够让 DAP/CDC 卡一下）。
 * 所以：**HID 中断只登记请求**（校验 + 拷 60 B + 置 pending），主循环
 * i2c_bridge_poll() 执行，结果放进结果槽；主机发完 XFER 轮询 RESULT 取数据。
 * 这套"登记 → 主循环执行 → 轮询取结果"与 CMD_RISCV / CMD_SCOPE 的标定完全同构。
 *
 * ── 引脚（HPM5301EVKLite）────────────────────────────────────────────────
 * I2C3 的 PA28=SDA(J3[21]) / PA29=SCL(J3[19])：这是 J3 排针上**唯一**一对
 * 引出来的硬件 I2C 脚（其它组合的另一半分别是 USER 按键 PA03、nRESET PA08、
 * CDC 的 UART2 PB08、USB0 的 PA24/PA25），与 SPI2(PB10~PB15)、SWD(PA04~PA08)、
 * UART0 都不冲突。⚠️ PA29 与 USB0_OC 网络共用（AP2151 的 nFAULT + R6 10k 上拉）：
 * 当开漏用没问题，但 USB 限流报故障时 nFAULT 会把 SCL 拉低 —— 遇到就 RESET 恢复。
 * 上拉：PA29 板上有 10k，PA28 **没有** —— 建议外接 4.7k~10k 到 3.3V，
 * 应急可以开内部上拉（SET_CFG 的 pullup=1，电流弱、只能低速短线）。
 *
 * 协议真源：src/i2c_bridge/i2c_bridge_proto.h；网页侧说明：docs/web-handoff-i2c-bridge.md
 */

#include <string.h>

#include "board.h"
#include "hpm_common.h"
#include "hpm_soc.h"
#include "hpm_interrupt.h"
#include "hpm_clock_drv.h"
#include "hpm_i2c_drv.h"
#include "hpm_gpio_drv.h"
#include "hpm_gpiom_drv.h"
#include "pinmux.h"
#include "i2c_bridge.h"
#include "spi_bridge.h"   /* 引脚仲裁：SPI 桥先占的辅助脚不能被 I2C 抢 */
#include "bus_periodic.h"

/* Shared wake gate is also available on boards without an I2C connector. */
volatile uint8_t i2c_bridge_req_kind;

#if !defined(BOARD_HAS_I2C_BRIDGE) || (BOARD_HAS_I2C_BRIDGE == 0)

/* ---------------------------------------------------------------------------
 * 本板没有引出的 I2C 排针（例如 akaLinkPro）：整个模块编成空实现，上层
 * （main / api_param / usb_composite）照旧可链接，HID 0x36 回"不支持"。
 * ------------------------------------------------------------------------- */

void i2c_bridge_init(void)
{
}

void i2c_bridge_poll(void)
{
    if (i2c_bridge_req_kind == BP_WAKE) bus_periodic_poll();
}

uint8_t i2c_bridge_periodic_ready(void) { return 0U; }
uint8_t i2c_bridge_periodic_check(const uint8_t *p, uint16_t len) {
    (void)p; (void)len; return I2C_E_DISABLED;
}
uint8_t i2c_bridge_periodic_exec(const uint8_t *p, uint16_t len, uint8_t *data, uint8_t *n) {
    (void)p; (void)len; (void)data; *n = 0U; return I2C_E_DISABLED;
}

void i2c_bridge_hid(uint8_t *req_hid, uint8_t *res_hid)
{
    res_hid[1] = 0x01;
    res_hid[2] = I2C_HID_CMD;
    res_hid[3] = req_hid[3];
}

void i2c_bridge_usb_reset(void)
{
}

uint8_t i2c_bridge_is_enabled(void)
{
    return 0U;
}

uint8_t i2c_bridge_owns_pad(uint16_t pad)
{
    (void)pad;
    return 0U;
}

#else /* BOARD_HAS_I2C_BRIDGE */

/* ============================== 参数 ============================== */

#define IB_I2C     BOARD_I2C_BRIDGE_BASE
#define IB_I2C_CLK BOARD_I2C_BRIDGE_CLK

/* MCHTMR = osc24m，硬编码 24 MHz（与 scope/rtt/riscv/spi_bridge 一致：
 * clock_get_frequency(clock_mchtmr0) 在本 SoC 没有兜底、返回 0）。 */
#define IB_MTIME (*(volatile uint32_t *)(HPM_MCHTMR_BASE + 0x00U))
#define IB_MCHTMR_HZ 24000000UL

/* 寄存器级忙等的上界（探测/恢复脉冲用）。SDK 自己的驱动用 5000 次，这里留一倍余量。 */
#define IB_WAIT_MAX 10000U

/* 扫描拆开做：每轮主循环最多探测这么多个地址（112 个 × ~110 µs 一次做完会占住
 * 主循环十几毫秒，DAP/CDC 会卡）。 */
#define IB_SCAN_PER_POLL 16U

/* 请求种类（i2c_bridge_req_kind） */
#define IB_REQ_NONE    0U
#define IB_REQ_XFER    1U
#define IB_REQ_SCAN    2U
#define IB_REQ_RECOVER 3U
/* USB 总线复位：让挂起的请求作废（由 i2c_bridge_usb_reset() 在中断里置位） */
#define IB_REQ_CANCEL  4U

/* XFER 参数区副本长度（req[4..63]） */
#define IB_REQ_BYTES 60U

/* SCL 三档（HPM 的 I2C 只有标准三档，`scl_hz` 是**档位选择器**不是精确频率） */
#define IB_SCL_STD   100000UL
#define IB_SCL_FAST  400000UL
#define IB_SCL_FPLUS 1000000UL

/* HID 报文里 res[4] 起是状态字；res_hid[1] 按同一套约定 = 报文总长度（含 res[0]） */
#define IB_RES_OFF 4U
#define IB_RES_LEN_BASE 8U   /* res[0..3]（id/len/cmd/action）+ res[4..7]（状态字） */

/* 状态字的位偏移一律以 i2c_bridge_proto.h 为唯一真源（i2c_bridge.h 已包含它）。
 * 这里原来又定义了一份 I2C_ST_SHIFT_CMD —— 两处同值时看不出问题，
 * 一旦只改一处就会静默错位。 */

/* ============================== 状态 ============================== */

static i2c_cfg_t s_cfg;
static uint8_t   s_enabled;
static uint32_t  s_actual_scl_hz;

/* 非 static：主循环用头文件里的 i2c_bridge_busy() 内联读它，避免为了看一眼标志就进 flash（见 i2c_bridge.h） */
static uint8_t  s_req[IB_REQ_BYTES];             /* XFER 参数副本 */
static uint8_t  s_scan_next;                     /* 扫描进度：下一个要探测的地址 */

static uint8_t  s_res_err = I2C_E_STATE;         /* 最近一次**完成**事务的错误码 */
static uint8_t  s_res_len;                       /* 最近一次完成事务的数据长度 */
static uint8_t  s_res_data[I2C_RD_MAX];          /* 读数据 / 扫描位图 */
static volatile uint32_t s_done_cnt;             /* 已完成事务计数 */

static uint32_t s_ok, s_err_cnt, s_tx_bytes, s_rx_bytes;
static uint32_t s_nack_addr, s_nack_data, s_timeouts, s_recover, s_busy_rej;
static uint32_t s_last_ticks;

static void ib_hw_init(void);

/* ============================== 小工具 ============================== */

static uint32_t mchtmr_now(void)
{
    return IB_MTIME;
}

static void ib_delay_us(uint32_t us)
{
    uint32_t t0 = mchtmr_now();
    uint32_t ticks = us * (IB_MCHTMR_HZ / 1000000UL);

    while ((uint32_t)(mchtmr_now() - t0) < ticks)
    {
    }
}

/* ============================== 引脚 ============================== */

/* 🚨 两根脚要当 GPIO 用，必须**先把它指派给 GPIO0 控制器**（set_pin_controller），
 * 再打开可见性 —— 只做 visibility 是不够的：DO/OE 写了也不出去，引脚还是老样子。
 * SPI 桥的 sb_gpiom_to_gpio0() 是同一个两步写法（它的 pintest 就靠这个才验得通）。 */
static void ib_gpiom_to_gpio0(uint16_t pad)
{
    gpiom_set_pin_controller(HPM_GPIOM, GPIO_GET_PORT_INDEX(pad), GPIO_GET_PIN_INDEX(pad),
                             gpiom_soc_gpio0);
    gpiom_enable_pin_visibility(HPM_GPIOM, GPIO_GET_PORT_INDEX(pad), GPIO_GET_PIN_INDEX(pad),
                                gpiom_soc_gpio0);
}

static void ib_pad_as_gpio_input(uint16_t pad, uint8_t pullup)
{
    ib_gpiom_to_gpio0(pad);
    HPM_IOC->PAD[pad].FUNC_CTL = IOC_PAD_FUNC_CTL_ALT_SELECT_SET(0);
    HPM_IOC->PAD[pad].PAD_CTL = IOC_PAD_PAD_CTL_OD_SET(1) |   /* 开漏：只可能被拉低 */
                                IOC_PAD_PAD_CTL_PE_SET(pullup ? 1U : 0U) |
                                IOC_PAD_PAD_CTL_PS_SET(pullup ? 1U : 0U) |
                                IOC_PAD_PAD_CTL_HYS_SET(1);
    gpio_set_pin_input(HPM_GPIO0, GPIO_GET_PORT_INDEX(pad), GPIO_GET_PIN_INDEX(pad));
}

static void ib_pad_as_gpio_od_output(uint16_t pad)
{
    ib_gpiom_to_gpio0(pad);
    HPM_IOC->PAD[pad].FUNC_CTL = IOC_PAD_FUNC_CTL_ALT_SELECT_SET(0);
    HPM_IOC->PAD[pad].PAD_CTL = IOC_PAD_PAD_CTL_OD_SET(1) |
                                IOC_PAD_PAD_CTL_PE_SET(0) |
                                IOC_PAD_PAD_CTL_PS_SET(0) |
                                IOC_PAD_PAD_CTL_SR_SET(1) |
                                IOC_PAD_PAD_CTL_SPD_SET(3) |
                                IOC_PAD_PAD_CTL_DS_SET(4);
    gpio_set_pin_output(HPM_GPIO0, GPIO_GET_PORT_INDEX(pad), GPIO_GET_PIN_INDEX(pad));
    gpio_write_pin(HPM_GPIO0, GPIO_GET_PORT_INDEX(pad), GPIO_GET_PIN_INDEX(pad), 1U); /* 放开 */
}

static void ib_pad_release(uint16_t pad)
{
    ib_pad_as_gpio_input(pad, 0U);
}

/* 控制器看到的线电平（LINESDA/LINESCL）—— 焊盘复用在 I2C 上时用它读，不用切焊盘。 */
static uint8_t ib_line_sda(void)
{
    return (IB_I2C->STATUS & I2C_STATUS_LINESDA_MASK) ? 1U : 0U;
}

static uint8_t ib_line_scl(void)
{
    return (IB_I2C->STATUS & I2C_STATUS_LINESCL_MASK) ? 1U : 0U;
}

/* 只改内部上拉，不动焊盘复用（PINTEST 用；切复用要等总线空闲，改上拉不用）。 */
static void ib_set_pullup(uint8_t on)
{
    const uint32_t pads[2] = {BOARD_I2C_BRIDGE_SDA_PAD, BOARD_I2C_BRIDGE_SCL_PAD};

    for (uint32_t i = 0U; i < 2U; i++)
    {
        uint32_t ctl = HPM_IOC->PAD[pads[i]].PAD_CTL;

        ctl &= ~(IOC_PAD_PAD_CTL_PE_MASK | IOC_PAD_PAD_CTL_PS_MASK);
        ctl |= IOC_PAD_PAD_CTL_PE_SET(on ? 1U : 0U) | IOC_PAD_PAD_CTL_PS_SET(on ? 1U : 0U);
        HPM_IOC->PAD[pads[i]].PAD_CTL = ctl;
    }
}

/* ============================== 控制器 ============================== */

static void ib_hw_init(void)
{
    i2c_config_t icfg;

    clock_add_to_group(IB_I2C_CLK, 0);

    icfg.is_10bit_addressing = false;
    icfg.i2c_mode = (s_cfg.scl_hz <= IB_SCL_STD)    ? i2c_mode_normal
                    : (s_cfg.scl_hz <= IB_SCL_FAST) ? i2c_mode_fast
                                                    : i2c_mode_fast_plus;
    s_actual_scl_hz = (icfg.i2c_mode == i2c_mode_normal) ? IB_SCL_STD
                      : (icfg.i2c_mode == i2c_mode_fast) ? IB_SCL_FAST
                                                         : IB_SCL_FPLUS;
    (void)i2c_init_master(IB_I2C, clock_get_frequency(IB_I2C_CLK), &icfg);
}

static void ib_enable(uint8_t on)
{
    if (on != 0U)
    {
        init_i2c_bridge_pins(s_cfg.pullup);
        ib_hw_init();
        i2c_bridge_req_kind = IB_REQ_NONE;
        s_enabled = 1U;
    }
    else
    {
        s_enabled = 0U;
        i2c_bridge_req_kind = IB_REQ_NONE;
        i2c_reset(IB_I2C);              /* 控制器先关掉，再松开引脚 */
        ib_pad_release(BOARD_I2C_BRIDGE_SDA_PAD);
        ib_pad_release(BOARD_I2C_BRIDGE_SCL_PAD);
    }
}

/* SDK 的 hpm_stat_t → 协议错误码。顺带把分类计数加上。 */
static uint8_t ib_map_status(hpm_stat_t st, uint8_t count_nack)
{
    switch (st)
    {
    case status_success:
        return I2C_OK;
    case status_i2c_no_addr_hit:
        if (count_nack != 0U) { s_nack_addr++; }
        return I2C_E_NO_ADDR;
    case status_i2c_no_ack:
        if (count_nack != 0U) { s_nack_data++; }
        return I2C_E_NO_ACK;
    case status_i2c_bus_busy:
        s_timeouts++;
        return I2C_E_BUS_STUCK;
    case status_timeout:
        s_timeouts++;
        return I2C_E_TIMEOUT;
    case status_invalid_argument:
        return I2C_E_RANGE;
    default:
        return I2C_E_STATE;
    }
}

/* START + 地址 + STOP（零数据字节）：只问"这个地址在不在"。
 * 扫描 112 个地址全靠它 —— 绝不能用"写一个字节"来探测（那会真的往从机里写东西）。 */
static uint8_t ib_probe(uint8_t dev, uint8_t count_nack)
{
    uint32_t retry = 0U;

    IB_I2C->STATUS = I2C_STATUS_CMPL_MASK | I2C_STATUS_ADDRHIT_MASK;
    IB_I2C->CMD = I2C_CMD_CLEAR_FIFO;
    IB_I2C->ADDR = I2C_ADDR_ADDR_SET(dev);
    IB_I2C->CTRL = I2C_CTRL_PHASE_START_MASK | I2C_CTRL_PHASE_STOP_MASK |
                   I2C_CTRL_PHASE_ADDR_MASK | I2C_CTRL_DIR_SET(I2C_DIR_MASTER_WRITE) |
                   I2C_CTRL_DATACNT_SET(0);
    IB_I2C->CMD = I2C_CMD_ISSUE_DATA_TRANSMISSION;

    while ((IB_I2C->STATUS & I2C_STATUS_CMPL_MASK) == 0U)
    {
        if (++retry > IB_WAIT_MAX)
        {
            s_timeouts++;
            return I2C_E_TIMEOUT;
        }
    }
    if ((IB_I2C->STATUS & I2C_STATUS_ADDRHIT_MASK) == 0U)
    {
        if (count_nack != 0U) { s_nack_addr++; }
        return I2C_E_NO_ADDR;          /* 没人应答（器件不在 / 地址错 / 没接） */
    }
    return I2C_OK;
}

/* 发一次"探测"事务，同时轮询控制器自己的线感知，记录 SCL/SDA 有没有真的被拉低过。
 * 这是**"我们的脚到底能不能驱动总线"的直接证据**：GPIO 在输出模式下 DI 未必可读
 * （早期用它做自检会误报"拉不低"），而 LINESCL/LINESDA 一定跟着焊盘走。
 * 探测用 7 位地址里保留的 0x7E：START + 地址 + STOP，零数据，不会写坏任何东西。 */
static void ib_probe_watch(uint8_t dev, uint8_t *scl_low, uint8_t *sda_low)
{
    uint32_t retry = 0U;

    *scl_low = 0U;
    *sda_low = 0U;
    IB_I2C->STATUS = I2C_STATUS_CMPL_MASK | I2C_STATUS_ADDRHIT_MASK;
    IB_I2C->CMD = I2C_CMD_CLEAR_FIFO;
    IB_I2C->ADDR = I2C_ADDR_ADDR_SET(dev);
    IB_I2C->CTRL = I2C_CTRL_PHASE_START_MASK | I2C_CTRL_PHASE_STOP_MASK |
                   I2C_CTRL_PHASE_ADDR_MASK | I2C_CTRL_DIR_SET(I2C_DIR_MASTER_WRITE) |
                   I2C_CTRL_DATACNT_SET(0);
    IB_I2C->CMD = I2C_CMD_ISSUE_DATA_TRANSMISSION;

    while ((IB_I2C->STATUS & I2C_STATUS_CMPL_MASK) == 0U)
    {
        uint32_t st = IB_I2C->STATUS;

        if ((st & I2C_STATUS_LINESCL_MASK) == 0U) { *scl_low = 1U; }
        if ((st & I2C_STATUS_LINESDA_MASK) == 0U) { *sda_low = 1U; }
        if (++retry > IB_WAIT_MAX)
        {
            break;
        }
    }
}

/* I2C 控制器的 FIFO 深度。I2C_CFG.FIFOSIZE 是硬件只读字段（2/4/8/16 四档），
 * SDK 里没有写死本 SoC 的值，取文档里的**最小值**做保守判据：只要写串超过它，
 * 就不能再走"一口气塞 FIFO"的老路。 */
#define IB_FIFO_DEPTH 4U

/* "写串（子地址 + 写数据）→ repeated START → 读回"，中间不发 STOP。
 *
 * 为什么不用 SDK 的 i2c_master_address_read：它在 ISSUE 之前就把 addr_size 个字节
 * 一口气写进 FIFO，**且不查 FIFOFULL** —— FIFO 只有 4 字节深（对照：SDK 的
 * i2c_master_write 是查的，见 hpm_i2c_drv.c 里 pump 数据的那两段），写串超过 4 字节
 * 时多出来的字节直接丢：从机收到半截写、接着超时、SCL 被拽住，只能靠 RESET 恢复。
 * 这里照 SDK i2c_master_write 的正确节奏 —— 先 ISSUE，再按 FIFOFULL 逐字节泵。
 * 读帧与 SDK 的读帧逐位一致（START+STOP+ADDR+DATA / DIR=MASTER_READ）；因为写帧
 * 没有发 STOP，这里的 START 天然就是 repeated START。
 * 返回值语义与 SDK 一致（hpm_stat_t），交给 ib_map_status 统一映射。 */
static hpm_stat_t ib_write_then_read(uint16_t dev, const uint8_t *wbuf, uint32_t wlen,
                                     uint8_t *rbuf, uint32_t rlen)
{
    uint32_t left;
    uint32_t retry;

    if ((wlen == 0U) || (rlen == 0U))
    {
        return status_invalid_argument;
    }

    /* 总线必须是空闲的：上一次事务可能没走干净（比如刚好超时退出） */
    retry = 0U;
    while ((IB_I2C->STATUS & I2C_STATUS_BUSBUSY_MASK) != 0U)
    {
        if (++retry > IB_WAIT_MAX) { return status_i2c_bus_busy; }
    }

    /* ---------------- 写帧：START + ADDR + DATA，不发 STOP ---------------- */
    IB_I2C->STATUS = I2C_STATUS_CMPL_MASK | I2C_STATUS_ADDRHIT_MASK; /* W1C */
    IB_I2C->CMD = I2C_CMD_CLEAR_FIFO;
    IB_I2C->CTRL = I2C_CTRL_PHASE_START_MASK | I2C_CTRL_PHASE_ADDR_MASK |
                   I2C_CTRL_PHASE_DATA_MASK | I2C_CTRL_DIR_SET(I2C_DIR_MASTER_WRITE) |
#ifdef I2C_CTRL_DATACNT_HIGH_MASK
                   I2C_CTRL_DATACNT_HIGH_SET(I2C_DATACNT_MAP(wlen) >> 8U) |
#endif
                   I2C_CTRL_DATACNT_SET(I2C_DATACNT_MAP(wlen));
    IB_I2C->ADDR = I2C_ADDR_ADDR_SET(dev);
    IB_I2C->CMD = I2C_CMD_ISSUE_DATA_TRANSMISSION;

    retry = 0U;
    while (i2c_is_addrhit(IB_I2C) == false)
    {
        if (++retry > IB_WAIT_MAX) { return status_i2c_no_addr_hit; }
    }
    IB_I2C->STATUS = I2C_STATUS_ADDRHIT_MASK;

    /* 按 FIFOFULL 的节奏泵：控制器一边移位一边腾空 FIFO */
    left = wlen;
    retry = 0U;
    while (left != 0U)
    {
        if ((IB_I2C->STATUS & I2C_STATUS_FIFOFULL_MASK) == 0U)
        {
            IB_I2C->DATA = *wbuf++;
            left--;
            retry = 0U;
        }
        else if (++retry > IB_WAIT_MAX)
        {
            return status_timeout;
        }
        else
        {
            /* 等 FIFO 腾位 */
        }
    }

    retry = 0U;
    while ((IB_I2C->STATUS & I2C_STATUS_CMPL_MASK) == 0U)
    {
        if (++retry > IB_WAIT_MAX) { return status_timeout; }
    }
    IB_I2C->STATUS = I2C_STATUS_CMPL_MASK; /* W1C：不清的话读帧的 CMPL 判据会立刻为真 */

    /* ---------------- 读帧：repeated START + ADDR + DATA + STOP ---------------- */
    IB_I2C->CMD = I2C_CMD_CLEAR_FIFO;
    IB_I2C->CTRL = I2C_CTRL_PHASE_START_MASK | I2C_CTRL_PHASE_STOP_MASK |
                   I2C_CTRL_PHASE_ADDR_MASK | I2C_CTRL_PHASE_DATA_MASK |
                   I2C_CTRL_DIR_SET(I2C_DIR_MASTER_READ) |
#ifdef I2C_CTRL_DATACNT_HIGH_MASK
                   I2C_CTRL_DATACNT_HIGH_SET(I2C_DATACNT_MAP(rlen) >> 8U) |
#endif
                   I2C_CTRL_DATACNT_SET(I2C_DATACNT_MAP(rlen));
    IB_I2C->CMD = I2C_CMD_ISSUE_DATA_TRANSMISSION;

    retry = 0U;
    while (i2c_is_addrhit(IB_I2C) == false)
    {
        if (++retry > IB_WAIT_MAX) { return status_i2c_no_addr_hit; }
    }
    IB_I2C->STATUS = I2C_STATUS_ADDRHIT_MASK;

    left = rlen;
    retry = 0U;
    while (left != 0U)
    {
        if ((IB_I2C->STATUS & I2C_STATUS_FIFOEMPTY_MASK) == 0U)
        {
            *rbuf++ = (uint8_t)IB_I2C->DATA;
            left--;
            retry = 0U;
        }
        else if (++retry > IB_WAIT_MAX)
        {
            return status_timeout;
        }
        else
        {
            /* 等下一个字节到达 */
        }
    }

    retry = 0U;
    while ((IB_I2C->STATUS & I2C_STATUS_CMPL_MASK) == 0U)
    {
        if (++retry > IB_WAIT_MAX) { return status_timeout; }
    }
    IB_I2C->STATUS = I2C_STATUS_CMPL_MASK;

    return status_success;
}

/* 一次完整事务（不带重试）。wlen/rlen 的组合决定线序，见 proto 的说明。 */
static uint8_t ib_transfer_once(uint8_t dev, const uint8_t *wb, uint32_t wlen, uint32_t rlen)
{
    hpm_stat_t st;

    if ((wlen == 0U) && (rlen == 0U))
    {
        return ib_probe(dev, 1U);
    }
    if (rlen == 0U)
    {
        st = i2c_master_write(IB_I2C, dev, (uint8_t *)(uintptr_t)wb, wlen);
    }
    else if (wlen == 0U)
    {
        st = i2c_master_read(IB_I2C, dev, s_res_data, rlen);
    }
    else if (wlen > IB_FIFO_DEPTH)
    {
        /* 写串超过 FIFO 深度：SDK 的 i2c_master_address_read 会丢字节（见函数注释），
         * 走自己的两段式。 */
        st = ib_write_then_read(dev, wb, wlen, s_res_data, rlen);
    }
    else
    {
        /* 子地址 + 数据 = 同一串字节先写出去，再 repeated START 读回来。
         * 这就是"写寄存器地址再读"的标准线序（中间不发 STOP）。
         * 写串 ≤ FIFO 深度时 SDK 那条路是对的（它一次性预装 FIFO 正好装得下），
         * 实测也一直是绿的，保持不动。 */
        st = i2c_master_address_read(IB_I2C, dev, (uint8_t *)(uintptr_t)wb, wlen, s_res_data, rlen);
    }

    if (st == status_success)
    {
        s_tx_bytes += wlen;
        s_rx_bytes += rlen;
    }
    return ib_map_status(st, 1U);
}

/* ============================== 请求执行（主循环） ============================== */

static void ib_finish(uint8_t err, uint8_t len, uint32_t t0)
{
    s_res_err = err;
    s_res_len = len;
    s_last_ticks = (uint32_t)(mchtmr_now() - t0);
    if (err == I2C_OK) { s_ok++; } else { s_err_cnt++; }
    s_done_cnt++;
    if (i2c_bridge_req_kind != IB_REQ_CANCEL) i2c_bridge_req_kind = IB_REQ_NONE;
}

static void ib_exec_xfer(void)
{
    const uint8_t *p = s_req;
    uint8_t dev = p[1];
    uint8_t addr_len = p[2];
    uint8_t wr_len = p[3];
    uint8_t rd_len = p[4];
    uint8_t wbuf[4U + I2C_WR_MAX];
    uint32_t wlen = 0U;
    uint32_t t0 = mchtmr_now();
    uint8_t err = I2C_E_STATE;
    uint8_t attempt = 0U;

    /* 子地址：主机给的字节**按原序**发出（p[5] 先发）。
     * 🚨 曾经写成"按 u32 小端、取低 addr_len 字节、MSB 在前"—— addr_len=1 时取到的是
     * u32 的最高字节（恒 0），于是每次读都从地址 0 开始（dump 出来永远是同一段，
     * 白查了一轮）。协议里就按字节数组给，别绕 u32。 */
    for (uint8_t i = 0U; i < addr_len; i++)
    {
        wbuf[wlen++] = p[5U + i];
    }
    if (wr_len != 0U)
    {
        memcpy(&wbuf[wlen], &p[9], wr_len);
        wlen += wr_len;
    }

    for (;;)
    {
        err = ib_transfer_once(dev, wbuf, wlen, rd_len);
        if ((err == I2C_OK) || (attempt >= s_cfg.retries))
        {
            break;
        }
        attempt++;
    }

    /* 出错时**不回**半截数据（读到一半的值没有意义，主机看错误码就行） */
    ib_finish(err, (err == I2C_OK) ? rd_len : 0U, t0);
}

static void ib_exec_scan(void)
{
    uint32_t t0 = mchtmr_now();
    uint8_t budget = IB_SCAN_PER_POLL;

    while ((budget-- > 0U) && (s_scan_next <= I2C_SCAN_LAST))
    {
        uint8_t a = s_scan_next++;
        if (ib_probe(a, 0U) == I2C_OK)         /* 扫描不记 NACK 计数（112 次会刷爆） */
        {
            uint8_t bit = (uint8_t)(a - I2C_SCAN_FIRST);
            s_res_data[bit >> 3] |= (uint8_t)(1U << (bit & 7U));
        }
    }
    if (s_scan_next > I2C_SCAN_LAST)
    {
        ib_finish(I2C_OK, I2C_SCAN_BYTES, t0);
    }
}

/* GPIO 输入寄存器读一根脚（焊盘切到 GPIO 时用它；输出脚的读回仅供参考）。 */
static uint8_t ib_gpio_level(uint16_t pad)
{
    return (gpio_read_pin(HPM_GPIO0, GPIO_GET_PORT_INDEX(pad), GPIO_GET_PIN_INDEX(pad)) != 0U) ? 1U : 0U;
}

/* 纯 GPIO 位翻转的一次"START + 地址 + STOP"（BITPROBE 用）：
 * 不经过 I2C 外设，位序/时序/采样点全在这里，用来和 SDK 路径交叉验证。
 * 两脚都切成 GPIO 开漏输出；返回 ACK 位上 SDA 的电平（0 = 被从机拉低 = ACK）。
 * 顺带把 8 个地址位**回读**的实际电平填进 bits（bit0..7 = bit7..bit0 的采样值），
 * bit8 = ACK 采样值，bit9/bit10 = 放开 SDA 后与 SCL 拉高后的电平。 */
static uint32_t ib_bitbang_probe(uint8_t dev, uint8_t *ack)
{
    uint32_t bits = 0U;

    ib_pad_as_gpio_od_output(BOARD_I2C_BRIDGE_SDA_PAD);
    ib_pad_as_gpio_od_output(BOARD_I2C_BRIDGE_SCL_PAD);
    ib_delay_us(10U);

    /* START：SCL 高时把 SDA 拉低 */
    gpio_write_pin(HPM_GPIO0, GPIO_GET_PORT_INDEX(BOARD_I2C_BRIDGE_SDA_PAD),
                   GPIO_GET_PIN_INDEX(BOARD_I2C_BRIDGE_SDA_PAD), 0U);
    ib_delay_us(5U);
    gpio_write_pin(HPM_GPIO0, GPIO_GET_PORT_INDEX(BOARD_I2C_BRIDGE_SCL_PAD),
                   GPIO_GET_PIN_INDEX(BOARD_I2C_BRIDGE_SCL_PAD), 0U);
    ib_delay_us(5U);

    /* 8 个地址位 + 写方向位，MSB 在前 */
    for (uint8_t i = 0U; i < 8U; i++)
    {
        uint8_t bit = (uint8_t)((dev >> (7U - i)) & 1U);

        gpio_write_pin(HPM_GPIO0, GPIO_GET_PORT_INDEX(BOARD_I2C_BRIDGE_SDA_PAD),
                       GPIO_GET_PIN_INDEX(BOARD_I2C_BRIDGE_SDA_PAD), bit);
        ib_delay_us(5U);
        gpio_write_pin(HPM_GPIO0, GPIO_GET_PORT_INDEX(BOARD_I2C_BRIDGE_SCL_PAD),
                       GPIO_GET_PIN_INDEX(BOARD_I2C_BRIDGE_SCL_PAD), 1U);   /* SCL 高 */
        ib_delay_us(5U);
        /* 回读（此时焊盘是 GPIO 输出脚，DI 未必可读，仅作参考 —— 真正的 ACK 判定看下面） */
        bits |= (uint32_t)ib_gpio_level(BOARD_I2C_BRIDGE_SDA_PAD) << i;
        gpio_write_pin(HPM_GPIO0, GPIO_GET_PORT_INDEX(BOARD_I2C_BRIDGE_SCL_PAD),
                       GPIO_GET_PIN_INDEX(BOARD_I2C_BRIDGE_SCL_PAD), 0U);   /* SCL 低 */
        ib_delay_us(5U);
    }

    /* 第 9 拍：放开 SDA，SCL 拉高，这一拍的电平就是 ACK */
    gpio_write_pin(HPM_GPIO0, GPIO_GET_PORT_INDEX(BOARD_I2C_BRIDGE_SDA_PAD),
                   GPIO_GET_PIN_INDEX(BOARD_I2C_BRIDGE_SDA_PAD), 1U);       /* 放开（开漏） */
    /* 放开后改读 GPIO 输入（输出脚 DI 不可靠），且保留开漏 */
    HPM_IOC->PAD[BOARD_I2C_BRIDGE_SDA_PAD].FUNC_CTL = IOC_PAD_FUNC_CTL_ALT_SELECT_SET(0);
    gpio_set_pin_input(HPM_GPIO0, GPIO_GET_PORT_INDEX(BOARD_I2C_BRIDGE_SDA_PAD),
                       GPIO_GET_PIN_INDEX(BOARD_I2C_BRIDGE_SDA_PAD));
    ib_delay_us(5U);
    gpio_write_pin(HPM_GPIO0, GPIO_GET_PORT_INDEX(BOARD_I2C_BRIDGE_SCL_PAD),
                   GPIO_GET_PIN_INDEX(BOARD_I2C_BRIDGE_SCL_PAD), 1U);
    ib_delay_us(5U);
    *ack = (gpio_read_pin(HPM_GPIO0, GPIO_GET_PORT_INDEX(BOARD_I2C_BRIDGE_SDA_PAD),
                          GPIO_GET_PIN_INDEX(BOARD_I2C_BRIDGE_SDA_PAD)) != 0U) ? 1U : 0U;
    bits |= (uint32_t)(*ack) << 8;

    /* STOP：SCL 高时把 SDA 从低拉到高 —— 先把 SDA 拉回输出并置低 */
    gpio_set_pin_output(HPM_GPIO0, GPIO_GET_PORT_INDEX(BOARD_I2C_BRIDGE_SDA_PAD),
                        GPIO_GET_PIN_INDEX(BOARD_I2C_BRIDGE_SDA_PAD));
    gpio_write_pin(HPM_GPIO0, GPIO_GET_PORT_INDEX(BOARD_I2C_BRIDGE_SDA_PAD),
                   GPIO_GET_PIN_INDEX(BOARD_I2C_BRIDGE_SDA_PAD), 0U);
    ib_delay_us(5U);
    gpio_write_pin(HPM_GPIO0, GPIO_GET_PORT_INDEX(BOARD_I2C_BRIDGE_SCL_PAD),
                   GPIO_GET_PIN_INDEX(BOARD_I2C_BRIDGE_SCL_PAD), 1U);
    ib_delay_us(5U);
    gpio_write_pin(HPM_GPIO0, GPIO_GET_PORT_INDEX(BOARD_I2C_BRIDGE_SDA_PAD),
                   GPIO_GET_PIN_INDEX(BOARD_I2C_BRIDGE_SDA_PAD), 1U);
    ib_delay_us(5U);

    /* 放开之后两根线都该被上拉拉回高 —— 顺便把这两位报回去（缺上拉一眼可见） */
    bits |= (uint32_t)((gpio_read_pin(HPM_GPIO0, GPIO_GET_PORT_INDEX(BOARD_I2C_BRIDGE_SDA_PAD),
                                      GPIO_GET_PIN_INDEX(BOARD_I2C_BRIDGE_SDA_PAD)) != 0U) ? 1U : 0U) << 9;
    bits |= (uint32_t)((gpio_read_pin(HPM_GPIO0, GPIO_GET_PORT_INDEX(BOARD_I2C_BRIDGE_SCL_PAD),
                                      GPIO_GET_PIN_INDEX(BOARD_I2C_BRIDGE_SCL_PAD)) != 0U) ? 1U : 0U) << 10;

    init_i2c_bridge_pins(s_cfg.pullup);          /* 装回 I2C 复用 */
    return bits;
}

/* 总线恢复：9 个 SCL 脉冲 + 一个 STOP（I2C 标准做法，用来放开被从机拽死的总线），
 * 然后复位并重配控制器。打拍期间两根脚切成 GPIO 开漏输出。 */
static uint8_t ib_exec_recover(void)
{
    uint32_t t0 = mchtmr_now();

    /* 协议里 RESET = "清计数器，保配置"：把统计清零（done_cnt 是给主机跟踪完成次序的
     * 序号，不清 —— 清了会让正在轮询 RESULT 的主机误判）。清完这一笔恢复自己会记成
     * frames_ok=1 / bus_recover=1，正好让主机拿到一个确定的起点。 */
    s_ok = 0U;
    s_err_cnt = 0U;
    s_tx_bytes = 0U;
    s_rx_bytes = 0U;
    s_nack_addr = 0U;
    s_nack_data = 0U;
    s_timeouts = 0U;
    s_recover = 0U;
    s_busy_rej = 0U;
    s_last_ticks = 0U;

    i2c_reset(IB_I2C);
    init_i2c_bridge_pins_gpio(s_cfg.pullup);
    ib_pad_as_gpio_od_output(BOARD_I2C_BRIDGE_SDA_PAD);
    ib_pad_as_gpio_od_output(BOARD_I2C_BRIDGE_SCL_PAD);
    ib_delay_us(5U);

    for (uint8_t i = 0U; i < 9U; i++)
    {
        gpio_write_pin(HPM_GPIO0, GPIO_GET_PORT_INDEX(BOARD_I2C_BRIDGE_SCL_PAD),
                       GPIO_GET_PIN_INDEX(BOARD_I2C_BRIDGE_SCL_PAD), 0U);
        ib_delay_us(5U);
        gpio_write_pin(HPM_GPIO0, GPIO_GET_PORT_INDEX(BOARD_I2C_BRIDGE_SCL_PAD),
                       GPIO_GET_PIN_INDEX(BOARD_I2C_BRIDGE_SCL_PAD), 1U);
        ib_delay_us(5U);
    }
    /* STOP：SCL 高时把 SDA 从低拉到高 */
    gpio_write_pin(HPM_GPIO0, GPIO_GET_PORT_INDEX(BOARD_I2C_BRIDGE_SDA_PAD),
                   GPIO_GET_PIN_INDEX(BOARD_I2C_BRIDGE_SDA_PAD), 0U);
    ib_delay_us(5U);
    gpio_write_pin(HPM_GPIO0, GPIO_GET_PORT_INDEX(BOARD_I2C_BRIDGE_SDA_PAD),
                   GPIO_GET_PIN_INDEX(BOARD_I2C_BRIDGE_SDA_PAD), 1U);
    ib_delay_us(5U);

    init_i2c_bridge_pins(s_cfg.pullup);
    ib_hw_init();

    s_recover++;
    ib_finish(I2C_OK, 0U, t0);
    return I2C_OK;
}

void i2c_bridge_poll(void)
{
    /* 🚨 热路径：这个函数在主循环里**每圈**都跑，探针的 DAP/SCOPE 吞吐直接吃它的开销。
     * 所以只留**一次** volatile 读 + 一条分支 —— USB 复位的作废请求被编码成
     * `IB_REQ_CANCEL`（见 i2c_bridge_usb_reset()），不再单独查一个标志位
     * （实测两个标志 1.7% 的采样率，合并后回到 main 的水平）。 */
    uint8_t kind = i2c_bridge_req_kind;

    if (kind == BP_WAKE) { bus_periodic_poll(); return; }

    if (kind == IB_REQ_NONE)
    {
        return;
    }
    if (kind == IB_REQ_CANCEL)
    {
        i2c_bridge_req_kind = IB_REQ_NONE;      /* 主机重新枚举：挂起的请求作废 */
        bus_periodic_wake();
        return;
    }
    if (s_enabled == 0U)
    {
        i2c_bridge_req_kind = IB_REQ_NONE;
        return;
    }

    switch (kind)
    {
    case IB_REQ_XFER:
        ib_exec_xfer();
        break;
    case IB_REQ_SCAN:
        ib_exec_scan();
        break;
    case IB_REQ_RECOVER:
        (void)ib_exec_recover();
        break;
    default:
        i2c_bridge_req_kind = IB_REQ_NONE;
        break;
    }
    bus_periodic_wake(); /* A nested timer IRQ may have arrived during a normal request. */
}

void i2c_bridge_usb_reset(void)
{
    /* 中断上下文：一条字节写即原子。挂起的请求直接作废（与旧行为一致）。 */
    i2c_bridge_req_kind = IB_REQ_CANCEL;
}

uint8_t i2c_bridge_is_enabled(void)
{
    return s_enabled;
}

uint8_t i2c_bridge_owns_pad(uint16_t pad)
{
    if (s_enabled == 0U)
    {
        return 0U;
    }
    return ((pad == BOARD_I2C_BRIDGE_SDA_PAD) || (pad == BOARD_I2C_BRIDGE_SCL_PAD)) ? 1U : 0U;
}

/* ============================== HID 控制面（中断上下文） ============================== */

static uint32_t ib_status_word(void)
{
    uint32_t w = 0U;

    /* 🚨 只有**使能之后**才允许碰 I2C 的寄存器。
     * 原因（2026-10-02 用 J-Link 抓到的现场）：`init_board_clock()` 里没有
     * `clock_i2c3` —— 上电时 I2C3 的 IP 时钟是**关着**的。而本函数每次 HID 命令
     * 都会被调到（`ib_put_status()` → 每条 0x36 都走），它以前**无条件**读
     * `IB_I2C->STATUS`：给没开时钟的 IP 发 AHB 读，事务可能永远不完成 ⇒ CPU 就
     * 停在 ISR 里 ⇒ EP0/HID/CDC 一起哑掉，只能断电或复位恢复。表现就是"发一条
     * 0x36 命令，整个探针死掉"（main 分支没有 0x36 处理，所以看起来只有本分支会死）。
     * 现在双保险：① `i2c_bridge_init()` 里上电就把 IP 时钟打开；② 这里没使能就
     * 一个寄存器都不读。 */
    if (s_enabled != 0U)
    {
        uint32_t st = IB_I2C->STATUS;

        w |= I2C_ST_ENABLED;
        if ((st & I2C_STATUS_BUSBUSY_MASK) == 0U)
        {
            w |= I2C_ST_BUS_OK;
        }
        /* I2C 控制器自己的线电平感知（LINESDA/LINESCL）：读它不用切焊盘，
         * 事务进行中读也安全 —— 诊断"线有没有被拽住/有没有上拉"就看这两位。 */
        if ((st & I2C_STATUS_LINESDA_MASK) != 0U)
        {
            w |= I2C_ST_SDA;
        }
        if ((st & I2C_STATUS_LINESCL_MASK) != 0U)
        {
            w |= I2C_ST_SCL;
        }
    }
    if (i2c_bridge_req_kind != IB_REQ_NONE)
    {
        w |= I2C_ST_PENDING;
    }
    w |= ((uint32_t)(s_res_err & 0xFFU)) << I2C_ST_SHIFT_ERR;
    w |= ((uint32_t)(s_done_cnt & 0xFFU)) << I2C_ST_SHIFT_DONE;
    return w;
}

static void ib_put_status(uint8_t *res_hid, uint8_t cmd_rc)
{
    uint32_t w = ib_status_word() | ((uint32_t)cmd_rc << I2C_ST_SHIFT_CMD);

    res_hid[IB_RES_OFF + 0U] = (uint8_t)(w >> 0);
    res_hid[IB_RES_OFF + 1U] = (uint8_t)(w >> 8);
    res_hid[IB_RES_OFF + 2U] = (uint8_t)(w >> 16);
    res_hid[IB_RES_OFF + 3U] = (uint8_t)(w >> 24);
}

static void ib_put32(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)(v >> 0);
    p[1] = (uint8_t)(v >> 8);
    p[2] = (uint8_t)(v >> 16);
    p[3] = (uint8_t)(v >> 24);
}

/* XFER 请求校验（参数区 p = &req_hid[4]，60 B） */
static uint8_t ib_xfer_check(const uint8_t *p)
{
    if (p[0] != 0U)                                /* flags 保留，必须 0 */
    {
        return I2C_E_RANGE;
    }
    if ((p[1] & 0x80U) != 0U)                      /* 7 位地址：bit7 必须 0 */
    {
        return I2C_E_RANGE;
    }
    if (p[2] > 4U)                                 /* addr_len */
    {
        return I2C_E_RANGE;
    }
    if (p[3] > I2C_WR_MAX)                         /* wr_len */
    {
        return I2C_E_RANGE;
    }
    if (p[4] > I2C_RD_MAX)                         /* rd_len */
    {
        return I2C_E_RANGE;
    }
    return I2C_OK;
}

uint8_t i2c_bridge_periodic_ready(void) {
    return s_enabled && (i2c_bridge_req_kind == IB_REQ_NONE || i2c_bridge_req_kind == BP_WAKE);
}
uint8_t i2c_bridge_periodic_check(const uint8_t *p, uint16_t len) {
    if (len < I2C_XFER_HDR || len > IB_REQ_BYTES || len != I2C_XFER_HDR + p[3]) return I2C_E_RANGE;
    return ib_xfer_check(p);
}
uint8_t i2c_bridge_periodic_exec(const uint8_t *p, uint16_t len, uint8_t *data, uint8_t *n) {
    *n = 0U;
    if (!i2c_bridge_periodic_ready()) return I2C_E_BUSY;
    uint8_t err = i2c_bridge_periodic_check(p, len); if (err) return err;
    memset(s_req, 0, sizeof(s_req)); memcpy(s_req, p, len);
    i2c_bridge_req_kind = IB_REQ_XFER;
    ib_exec_xfer(); *n = s_res_len; memcpy(data, s_res_data, *n);
    return s_res_err;
}

void i2c_bridge_hid(uint8_t *req_hid, uint8_t *res_hid)
{
    uint8_t action = req_hid[3];
    uint8_t *p = &req_hid[4];
    uint8_t rc = I2C_OK;

    res_hid[1] = IB_RES_LEN_BASE;                  /* 默认：只回状态字 */
    res_hid[2] = I2C_HID_CMD;
    res_hid[3] = action;

    if (bus_periodic_owns(BP_I2C) && action != I2C_ACT_STATUS &&
        action != I2C_ACT_GET_CFG && action != I2C_ACT_RESULT && action != I2C_ACT_DBG) {
        ib_put_status(res_hid, I2C_E_BUSY);
        return;
    }

    switch (action)
    {
    case I2C_ACT_STATUS:
    {
        ib_put32(&res_hid[IB_RES_OFF + 4U], s_ok);
        ib_put32(&res_hid[IB_RES_OFF + 8U], s_err_cnt);
        ib_put32(&res_hid[IB_RES_OFF + 12U], s_tx_bytes);
        ib_put32(&res_hid[IB_RES_OFF + 16U], s_rx_bytes);
        ib_put32(&res_hid[IB_RES_OFF + 20U], s_nack_addr);
        ib_put32(&res_hid[IB_RES_OFF + 24U], s_nack_data);
        ib_put32(&res_hid[IB_RES_OFF + 28U], s_timeouts);
        ib_put32(&res_hid[IB_RES_OFF + 32U], s_recover);
        ib_put32(&res_hid[IB_RES_OFF + 36U], s_actual_scl_hz);
        ib_put32(&res_hid[IB_RES_OFF + 40U], s_last_ticks);
        res_hid[1] = (uint8_t)(IB_RES_LEN_BASE + 4U * I2C_STAT_WORDS);
        break;
    }

    case I2C_ACT_ENABLE:
        if (i2c_bridge_req_kind != IB_REQ_NONE)
        {
            /* 事务在跑（含 RECOVER 打节拍）：此刻重新初始化控制器会把线序打断；
             * 而且在打拍期间收到 ENABLE=0 时，主循环随后会把这两根脚复用回 I2C，
             * 但 s_enabled 已经是 0 —— SPI 桥会以为这两根脚可以拿去用。 */
            rc = I2C_E_BUSY;
            s_busy_rej++;
        }
        else if ((p[0] != 0U) &&
                 ((spi_bridge_owns_pad(BOARD_I2C_BRIDGE_SDA_PAD) != 0U) ||
                  (spi_bridge_owns_pad(BOARD_I2C_BRIDGE_SCL_PAD) != 0U)))
        {
            /* 反方向的引脚仲裁：SPI 桥已经把 PA28/PA29 配成辅助脚了，
             * I2C 再使能就会把那两根脚抢过来（原来只查了 SPI 抢 I2C 这一个方向）。
             * 报 E_BUSY 而不是新错误码：主机侧语义就是"资源被占，先让开再重试"，
             * 不需要动协议。 */
            rc = I2C_E_BUSY;
            s_busy_rej++;
        }
        else
        {
            ib_enable((p[0] != 0U) ? 1U : 0U);
        }
        break;

    case I2C_ACT_RESET:
        if (s_enabled == 0U)
        {
            rc = I2C_E_DISABLED;
        }
        else if (i2c_bridge_req_kind != IB_REQ_NONE)
        {
            rc = I2C_E_BUSY;
            s_busy_rej++;
        }
        else
        {
            i2c_bridge_req_kind = IB_REQ_RECOVER;
        }
        break;

    case I2C_ACT_SET_CFG:
    {
        i2c_cfg_t c;
        if (i2c_bridge_req_kind != IB_REQ_NONE)
        {
            /* 事务在跑：下面会把引脚/时钟/档位全部重新落地，事务中途改这些会把
             * 线序打断（也和 ENABLE 一样会踩到"引脚归属"的竞态）。等它做完再配。 */
            rc = I2C_E_BUSY;
            s_busy_rej++;
            break;
        }
        memcpy(&c, p, sizeof(c));
        if ((c.flags != 0U) || (c.pullup > 1U) || (c.retries > 8U))
        {
            rc = I2C_E_RANGE;
            break;
        }
        c.actual_scl_hz = 0U;                      /* 只读字段，忽略主机填的值 */
        c.reserved0 = 0U;
        c.reserved1 = 0U;
        s_cfg = c;
        if (s_enabled != 0U)                       /* 引脚/时钟/档位都在这里落地 */
        {
            init_i2c_bridge_pins(s_cfg.pullup);
            ib_hw_init();
        }
        break;
    }

    case I2C_ACT_GET_CFG:
    {
        i2c_cfg_t out = s_cfg;
        out.actual_scl_hz = s_actual_scl_hz;
        memcpy(&res_hid[IB_RES_OFF + 4U], &out, sizeof(out));
        res_hid[1] = (uint8_t)(IB_RES_LEN_BASE + sizeof(out));
        break;
    }

    case I2C_ACT_XFER:
        if (s_enabled == 0U)
        {
            rc = I2C_E_DISABLED;
        }
        else if (i2c_bridge_req_kind != IB_REQ_NONE)
        {
            rc = I2C_E_BUSY;
            s_busy_rej++;
        }
        else
        {
            rc = ib_xfer_check(p);
            if (rc == I2C_OK)
            {
                memcpy(s_req, p, IB_REQ_BYTES);
                i2c_bridge_req_kind = IB_REQ_XFER;
            }
        }
        break;

    case I2C_ACT_SCAN:
        if (s_enabled == 0U)
        {
            rc = I2C_E_DISABLED;
        }
        else if (i2c_bridge_req_kind != IB_REQ_NONE)
        {
            rc = I2C_E_BUSY;
            s_busy_rej++;
        }
        else
        {
            memset(s_res_data, 0, I2C_SCAN_BYTES);
            s_scan_next = I2C_SCAN_FIRST;
            i2c_bridge_req_kind = IB_REQ_SCAN;
        }
        break;

    case I2C_ACT_RESULT:
    {
        res_hid[IB_RES_OFF + 4U] = s_res_err;
        if (i2c_bridge_req_kind == IB_REQ_NONE)
        {
            res_hid[IB_RES_OFF + 5U] = s_res_len;
            if (s_res_len != 0U)
            {
                memcpy(&res_hid[IB_RES_OFF + 6U], s_res_data, s_res_len);
            }
            res_hid[1] = (uint8_t)(IB_RES_OFF + 6U + s_res_len);
        }
        else
        {
            res_hid[IB_RES_OFF + 5U] = 0U;         /* 还在做：先别读数据 */
            res_hid[1] = (uint8_t)(IB_RES_OFF + 6U);
        }
        break;
    }

    case I2C_ACT_DBG:
    {
        /* 未使能时寄存器还没配过，读它没有意义（而且历史上这正是那个"挂死总线"
         * 的路径）—— 直接回 DISABLED，别碰寄存器。 */
        if (s_enabled == 0U)
        {
            rc = I2C_E_DISABLED;
            break;
        }
        ib_put32(&res_hid[IB_RES_OFF + 4U], IB_I2C->CTRL);
        ib_put32(&res_hid[IB_RES_OFF + 8U], IB_I2C->STATUS);
        ib_put32(&res_hid[IB_RES_OFF + 12U], IB_I2C->ADDR);
        ib_put32(&res_hid[IB_RES_OFF + 16U], IB_I2C->CMD);
        ib_put32(&res_hid[IB_RES_OFF + 20U], IB_I2C->SETUP);
        ib_put32(&res_hid[IB_RES_OFF + 24U], IB_I2C->INTEN);
        ib_put32(&res_hid[IB_RES_OFF + 28U],
                 (uint32_t)(HPM_IOC->PAD[BOARD_I2C_BRIDGE_SDA_PAD].FUNC_CTL & 0xFFFFU) |
                 ((HPM_IOC->PAD[BOARD_I2C_BRIDGE_SDA_PAD].PAD_CTL & 0xFFFFU) << 16));
        ib_put32(&res_hid[IB_RES_OFF + 32U],
                 (uint32_t)(HPM_IOC->PAD[BOARD_I2C_BRIDGE_SCL_PAD].FUNC_CTL & 0xFFFFU) |
                 ((HPM_IOC->PAD[BOARD_I2C_BRIDGE_SCL_PAD].PAD_CTL & 0xFFFFU) << 16));
        ib_put32(&res_hid[IB_RES_OFF + 36U], s_cfg.scl_hz);
        ib_put32(&res_hid[IB_RES_OFF + 40U], s_actual_scl_hz);
        ib_put32(&res_hid[IB_RES_OFF + 44U], ib_status_word());
        ib_put32(&res_hid[IB_RES_OFF + 48U], s_done_cnt);
        ib_put32(&res_hid[IB_RES_OFF + 52U], s_busy_rej);
        res_hid[1] = (uint8_t)(IB_RES_LEN_BASE + 12U * 4U);
        break;
    }

    case I2C_ACT_PINTEST:
    {
        /* 三项自检，结论在 bit8..15 的问题位图（0 = 桥这一侧没问题）：
         *   ① 空闲电平（控制器线感知，不切焊盘）：两根都该是高；常低 = 被谁拽住
         *   ② 打开内部上拉后再读：判断"线上到底有没有上拉"
         *   ③ 发一次真实探测事务，看控制器自己的线感知有没有被拉低过 ——
         *      这是"我们的脚确实在驱动总线"的**硬证据**（GPIO 输出脚的 DI 不可信，
         *      早先用它做自检会误报"拉不低"）。
         * 只有 ③ 需要驱动总线，所以整段仍然只在空闲时做。 */
        uint32_t out = 0U;
        uint32_t prob = 0U;
        uint8_t scl_low = 0U, sda_low = 0U;

        if (s_enabled == 0U)
        {
            rc = I2C_E_DISABLED;
            break;
        }
        if (i2c_bridge_req_kind != IB_REQ_NONE)
        {
            rc = I2C_E_BUSY;
            break;
        }

        out |= (uint32_t)ib_line_sda() << 0;
        out |= (uint32_t)ib_line_scl() << 1;

        ib_set_pullup(1U);
        ib_delay_us(200U);
        out |= (uint32_t)ib_line_sda() << 2;
        out |= (uint32_t)ib_line_scl() << 3;

        ib_probe_watch(0x7EU, &scl_low, &sda_low);      /* 切焊盘之前先把总线的活干了 */
        out |= (uint32_t)scl_low << 16;
        out |= (uint32_t)sda_low << 17;

        if (scl_low == 0U) { prob |= 0x01U; }           /* SCL 从未被拉低：桥侧驱动异常 */
        if (((out >> 1) & 1U) == 0U) { prob |= 0x02U; } /* 空闲 SCL 常低：被拽住 */
        if ((out & 1U) == 0U) { prob |= 0x04U; }        /* 空闲 SDA 常低：被拽住 */

        init_i2c_bridge_pins(s_cfg.pullup);             /* 装回主机设定的上拉 */
        out |= prob << 8;
        ib_put32(&res_hid[IB_RES_OFF + 4U], out);
        res_hid[1] = (uint8_t)(IB_RES_LEN_BASE + 4U);
        break;
    }

    case I2C_ACT_BITPROBE:
    {
        /* 纯 GPIO 位翻转探测（不经过 I2C 外设）：用来把"器件不在"和"外设的零数据
         * 探测有猫腻"分开。要切焊盘，所以只在总线空闲时做。 */
        uint8_t ack = 1U;
        uint32_t bits;

        if (s_enabled == 0U)
        {
            rc = I2C_E_DISABLED;
            break;
        }
        if (i2c_bridge_req_kind != IB_REQ_NONE)
        {
            rc = I2C_E_BUSY;
            break;
        }

        bits = ib_bitbang_probe((uint8_t)(p[0] & 0x7FU), &ack);
        rc = (ack == 0U) ? I2C_OK : I2C_E_NO_ADDR;
        res_hid[IB_RES_OFF + 4U] = (uint8_t)(bits & 0xFFU);
        res_hid[IB_RES_OFF + 5U] = (uint8_t)((bits >> 8) & 0xFFU);
        res_hid[IB_RES_OFF + 6U] = (uint8_t)((bits >> 16) & 0xFFU);
        res_hid[IB_RES_OFF + 7U] = (uint8_t)((bits >> 24) & 0xFFU);
        res_hid[1] = (uint8_t)(IB_RES_LEN_BASE + 4U);
        break;
    }

    default:
        rc = I2C_E_BAD_FRAME;
        break;
    }

    ib_put_status(res_hid, rc);
}

void i2c_bridge_init(void)
{
    /* 上电就把 I2C3 的 IP 时钟打开：`init_board_clock()` 里没有它，不打开的话
     * 这个 IP 的寄存器是"没时钟的"——从 ISR 里读它有可能把 AHB 事务挂死
     * （见 ib_status_word() 的说明）。打开后所有寄存器访问都是安全的。 */
    clock_add_to_group(IB_I2C_CLK, 0);

    memset(&s_cfg, 0, sizeof(s_cfg));
    s_cfg.scl_hz = 0U;                             /* 0 = 默认 100 kHz 档 */
    s_cfg.pullup = 0U;                             /* 依赖外部上拉 */
    s_cfg.retries = 0U;
    s_enabled = 0U;
    s_actual_scl_hz = 0U;
    i2c_bridge_req_kind = IB_REQ_NONE;
    s_res_err = I2C_E_STATE;
    s_res_len = 0U;
    s_done_cnt = 0U;
    s_ok = s_err_cnt = s_tx_bytes = s_rx_bytes = 0U;
    s_nack_addr = s_nack_data = s_timeouts = s_recover = s_busy_rej = 0U;
    s_last_ticks = 0U;
    s_scan_next = I2C_SCAN_FIRST;
}

#endif /* BOARD_HAS_I2C_BRIDGE */
