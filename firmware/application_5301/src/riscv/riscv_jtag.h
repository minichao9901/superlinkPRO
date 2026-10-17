/* SPDX-License-Identifier: Apache-2.0 */
/* Copyright (c) 2026 akaInstruments */

/*
 * Probe-side RISC-V (Debug Module) access over JTAG.
 *
 * Why this exists: a host driving RISC-V memory through OpenOCD has to pay one
 * USB round trip per abstract command (~97 us measured on this probe), which
 * caps a 32-bit read at ~96 KB/s whatever the JTAG clock is. The probe can do
 * the same job locally: one DMI transfer is a single 41-bit DR scan, so the
 * only cost left is the wire itself (~46 TCK per access).
 *
 * Transport: the DMI is behind IR = 0x11 of the TAP. After the IR is loaded
 * once, every access is one DR scan of 41 bits
 *     [0:1] op   (0 = nop, 1 = read, 2 = write)
 *     [2:33] data
 *     [34:40] address
 * and the response to a request comes back in the *next* scan, so a stream of
 * posted requests runs at one scan per access (no round trip at all).
 *
 * Bulk memory goes through the Debug Module's System Bus Access (SBA) block,
 * which auto-increments the address, so a block read is
 *     sbcs = sbaccess32 | sbautoincrement | sbreadonaddr | sbreadondata
 *     sbaddress0 = addr          -> starts the first read
 *     read sbdata0 ...           -> one word per DMI read, next read started
 * and a block write is one posted write to sbdata0 per word.
 */

#ifndef RISCV_JTAG_H
#define RISCV_JTAG_H

#include <stdint.h>

/* Open the JTAG TAP, load IR = DMI and probe the debug module.
 * Returns 0 on success, negative on failure (and fills the getters). */
int riscv_jtag_open(void);

/* Release the port (pins back to their idle state). */
void riscv_jtag_close(void);

int riscv_jtag_is_open(void);

/* Identity read back by riscv_jtag_open(). */
uint32_t riscv_jtag_idcode(void);   /* TAP IDCODE (IR = 0x01) */
uint32_t riscv_jtag_dtmcs(void);    /* DTM control/status word */
uint32_t riscv_jtag_dmstatus(void); /* Debug Module status word */

/* Single word access (32-bit aligned). Returns 0 on success. */
int riscv_jtag_read_word(uint32_t addr, uint32_t *val);
int riscv_jtag_write_word(uint32_t addr, uint32_t val);

/* 单字**流水**读：把 SBA 抱在同一个地址上（关掉自增），之后每拍只有一次 DMI 扫描
 * —— 与 SWD 侧的 swd_read_word_hold_prepare()/swd_read_word_pipe() 完全同构
 * （J-Scope 的单变量快路径就是这套语义）。延迟一拍：hold_read() 收的是**上一次**
 * 投出去的读的结果，并同时投出下一次；所以第一次的结果要丢掉，最后一个值由调用方
 * 补收。地址不 4 字节对齐会被向下取整。
 *   hold_prepare(): 0 = ok；已经是"抱着这个地址"时空操作（每拍调也没关系）。
 *   hold_read():    0 = ok（*val 已填）；负 = 链路/DM 出错（此时"抱住"状态作废）。
 * 任何别的 SBA 操作（read/read_word/write）都会把"抱住"状态作废。 */
int riscv_jtag_hold_prepare(uint32_t addr);
int riscv_jtag_hold_read(uint32_t *val);

/* Block access through the SBA. `addr` may be unaligned; returns 0 on success,
 * negative when the debug module reported an error. */
int riscv_jtag_read(uint32_t addr, uint8_t *dst, uint32_t len);
int riscv_jtag_write(uint32_t addr, const uint8_t *src, uint32_t len);

/* Number of Run-Test/Idle TCKs before each DMI scan (clamped to 0..255).
 * Default is 8; this does NOT shorten GPIO half-periods. BUSY recovery may
 * increase it to give the DTM more completion time. */
void riscv_jtag_set_delay(uint32_t delay);
uint32_t riscv_jtag_get_delay(void);

/* 作废内部缓存（SBCS 配置 + 单字流水的"抱住"状态）。
 * 主机碰过 DAP 之后必须调：OpenOCD/DFU 的复位会把 DM 的 SBCS 清零，而缓存看不出来，
 * 后果是 SBA 读**静默返回 0**（见 rtt_bridge_note_dap_activity 的调用点）。
 * 另外 riscv_jtag 自己也会在"距离上次 DMI 活动超过 50 ms"时强制重写配置兜底。 */
void riscv_jtag_invalidate_cache(void);

/* Last error detail: dmstatus / sbcs observed by the failed transfer. */
uint32_t riscv_jtag_last_sbcs(void);
uint32_t riscv_jtag_last_dmstatus(void);

/* Clear the sticky SBA error bits (sbbusyerror / sberror are write-1-to-clear).
 * This is the RISC-V counterpart of DAPLink's swd_clear_errors(): a transient
 * system-bus error otherwise latches and every later SBA access returns FAULT.
 * The two are NOT interchangeable - swd_clear_errors() drives the SWD engine on
 * the same pins and wrecks the TAP state machine. Returns UINT32_MAX if SBCS
 * could not be read or cleared; callers must not interpret that as success. */
uint32_t riscv_jtag_clear_errors(void);

/* SBA sticky 错误（sbbusyerror / sberror）统计，诊断用：正常应当全 0。
 * 非 0 说明"SBA 曾经静默忽略访问、读值冻结"发生过（见 riscv_jtag.c 的
 * sba_check_errors 注释）。任一参数可为 NULL。 */
void riscv_jtag_sba_stats(uint32_t *events, uint32_t *first_sbcs,
                          uint32_t *retries, uint32_t *recovers);

/* Bring-up diagnostics: raw 41-bit DR values of the most recent DMI scans. */
uint64_t riscv_jtag_dbg(uint32_t idx);

/* Reset the TAP, load IR = DMI, shift `n` NOP requests and return the raw
 * 41-bit responses (n <= 8). */
void riscv_jtag_probe(uint32_t n, uint64_t *out);

#endif /* RISCV_JTAG_H */
