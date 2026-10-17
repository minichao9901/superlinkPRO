/* SPDX-License-Identifier: Apache-2.0 */
/* Copyright (c) 2026 akaInstruments */

/*
 * Deferred RISC-V service layer: the HID command handler only queues work, the
 * JTAG engine itself runs from the main loop. The bit-bang engine must not run
 * in the USB interrupt context (same rule the SWD RTT bridge follows).
 */

#ifndef RISCV_SVC_H
#define RISCV_SVC_H

#include <stdint.h>
#include "service_gate.h"
extern service_gate_t riscv_svc_gate;
static inline uint8_t riscv_svc_needs_service(void) { return riscv_svc_gate.word[0] != 0U; }


/* Queued actions (mirrors the HID CMD_RISCV action byte). */
#define RISCV_ACT_STOP    0U
#define RISCV_ACT_OPEN    1U
#define RISCV_ACT_RBENCH  2U  /* args: addr, bytes, iters   */
#define RISCV_ACT_WBENCH  3U  /* args: addr, bytes, iters   */
#define RISCV_ACT_SBENCH  4U  /* args: iters                */
#define RISCV_ACT_RCHECK  5U  /* args: addr, words          */
#define RISCV_ACT_STATUS  6U
#define RISCV_ACT_CONFIG  7U  /* args: clock delay override */
#define RISCV_ACT_DMIPROBE 8U /* args: number of NOP scans (<= 8) */
#define RISCV_ACT_SBASTAT  9U /* 回读 DM 的 SBCS + SBA sticky 错误统计（诊断） */

void riscv_svc_request(uint32_t action, uint32_t addr, uint32_t arg1, uint32_t arg2);
void riscv_svc_poll(void);

/* Timing knob, applied immediately (does not touch the queued action). */
void riscv_svc_set_delay(uint32_t delay);

/* Status block (also used for the "request accepted" reply). */
uint32_t riscv_svc_status(uint32_t *out, uint32_t words);

#endif /* RISCV_SVC_H */
