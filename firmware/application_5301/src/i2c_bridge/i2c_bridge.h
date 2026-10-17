/* SPDX-License-Identifier: Apache-2.0 */
/* Copyright (c) 2026 akaInstruments */

#ifndef __I2C_BRIDGE_H__
#define __I2C_BRIDGE_H__

#include <stdint.h>
#include "i2c_bridge_proto.h"

/*
 * USB -> I2C 转发桥（探针侧）
 *
 * 控制面 + 数据面都在 HID CMD 0x36 一条报文里（不开 bulk、不做 DMA）：
 *   · HID 中断只**登记请求**（I2C 事务最长几毫秒，绝不能占着 USB 中断）；
 *   · 主循环 i2c_bridge_poll() 执行事务，结果放进结果槽；
 *   · 主机发 XFER/SCAN 后轮询 RESULT 取错误码与数据（与 CMD_RISCV 同一套模式）。
 *
 * 引脚（HPM5301EVKLite）：I2C3 = PA28/SDA(J3[21]) + PA29/SCL(J3[19])，
 * 与 SPI2(PB10~PB15)、CDC UART2(PB08/PB09)、SWD(PA04~PA08) 都不冲突。
 *
 * 设计文档：docs/usb-i2c-bridge-plan.md；网页侧说明：docs/web-handoff-i2c-bridge.md
 */

/* 一次性初始化（main() 里，board_init 之后）。只清状态，不动引脚。 */
void i2c_bridge_init(void);

/* 主循环：执行登记的请求（XFER / SCAN / 总线恢复）。未使能时只有一条分支。 */
void i2c_bridge_poll(void);
uint8_t i2c_bridge_periodic_ready(void);
uint8_t i2c_bridge_periodic_check(const uint8_t *p, uint16_t len);
uint8_t i2c_bridge_periodic_exec(const uint8_t *p, uint16_t len, uint8_t *data, uint8_t *n);

/* HID CMD 0x36：req/res 都是 64 B 的 HID 报文（约定见 api_param.c）。 */
void i2c_bridge_hid(uint8_t *req_hid, uint8_t *res_hid);

/* 总线复位：把挂起的请求与结果作废（真正的清账在主循环里做）。 */
void i2c_bridge_usb_reset(void);

/* 桥是否使能（引脚是否被 I2C 占用）。 */
uint8_t i2c_bridge_is_enabled(void);

/* 交叉检查：桥使能时是否占着这根 pad（SPI 桥把它当辅助脚之前会问一句）。 */
uint8_t i2c_bridge_owns_pad(uint16_t pad);

/* ---- 主循环热路径专用 ------------------------------------------------------
 * `i2c_bridge_poll()` 在 flash 里；主循环**每圈**都调它的话，那次 XPI 取指的
 * 固定开销会摊进采样周期 —— 实测 SCOPE 单变量标定 611 → 589 kHz（−3%）。
 * 所以主循环先内联读一下这个标志，只有真有请求时才进函数（零开销、延迟最小）。 */
extern volatile uint8_t i2c_bridge_req_kind;

static inline uint8_t i2c_bridge_busy(void)
{
    return (i2c_bridge_req_kind != 0U) ? 1U : 0U;
}

#endif /* __I2C_BRIDGE_H__ */
