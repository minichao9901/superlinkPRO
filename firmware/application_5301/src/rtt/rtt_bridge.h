/* SPDX-License-Identifier: Apache-2.0 */
/* Copyright (c) 2026 akaInstruments */

#ifndef __RTT_BRIDGE_H__
#define __RTT_BRIDGE_H__

#include <stdint.h>
#include "service_gate.h"
extern service_gate_t rtt_bridge_gate;
static inline uint8_t rtt_bridge_needs_service(void) { return service_gate_pending(&rtt_bridge_gate); }


/* Probe-side SEGGER RTT bridge.
 *
 * Letting the host poll the target's RTT control block over CMSIS-DAP costs
 * three host<->probe round trips per drain, which caps the stream at ~1.1 MB/s
 * on this probe. Here the probe polls the control block itself through the
 * same SWD engine and pushes the drained bytes into the CDC ringbuffer, so the
 * host only has to read a COM port - no round trips at all.
 *
 * The SWD bus is shared with the DAP command engine: polling only starts when
 * no DAP command has been executed for RTT_BRIDGE_DAP_IDLE_MS, so an active
 * debug session always keeps priority.
 */

#define RTT_BRIDGE_DEFAULT_ADDR 0x20000000UL /* where to search for the CB */
#define RTT_BRIDGE_DEFAULT_SIZE 0x00010000UL
#define RTT_BRIDGE_SIGNATURE    "SEGGER RTT"
#define RTT_BRIDGE_SIG_LEN      10U
#define RTT_BRIDGE_DAP_IDLE_MS  20U

/* Start bridging: search [addr, addr+size) for the control block, drain
 * up-buffer `channel` into the CDC. Returns 0 on success, negative on error. */
int rtt_bridge_start(uint32_t addr, uint32_t size, uint8_t channel);

/* Same, but only arms a request: the SWD access happens from the main loop
 * (rtt_bridge_poll), never from the USB interrupt context. Call this from
 * command handlers. */
void rtt_bridge_request_start(uint32_t addr, uint32_t size, uint8_t channel);
int  rtt_bridge_start_result(void);

/* Runtime tuning (HID CMD_RTT action 7). chunk_bytes is the block size of one
 * swd_read_memory() call (clamped to [64, 4096]); swd_clock_hz = 0 keeps the
 * current setting. `discard` = 1 drops the drained bytes instead of pushing
 * them into the CDC ring, which isolates the raw SWD drain rate from the USB
 * path. delay_override != 0xFF forces DAP_Data.clock_delay (the one timing
 * knob the bit-bang blobs expose) instead of the value Set_Clock_Delay()
 * picked for the requested clock. Takes effect immediately. */
void rtt_bridge_configure(uint32_t swd_clock_hz, uint32_t chunk_bytes, uint8_t discard,
                          uint8_t delay_override);

/* Pure SWD read benchmark (HID CMD_RTT actions 8/9): read `bytes` from `addr`
 * of the TARGET `iters` times through the same swd_host path the bridge uses,
 * measuring MCHTMR ticks. Deferred to the main loop like every other SWD op. */
void rtt_bridge_request_bench(uint32_t addr, uint32_t bytes, uint32_t iters);
/* Returns 1 once a result is available, 0 while pending. */
int  rtt_bridge_bench_result(uint32_t *bytes, uint32_t *ticks, int32_t *err);

/* Bring-up debugging: record the last host DAP request/response pair. */
extern volatile uint8_t g_dap_trace[64];
void rtt_bridge_trace_dap(const uint8_t *req, const uint8_t *resp);

/* Deferred raw CMSIS-DAP passthrough (bring-up debugging): queue a request,
 * run it from the main loop, read the response back later. */
void rtt_bridge_request_raw(const uint8_t *req, uint32_t len);
uint32_t rtt_bridge_raw_result(uint8_t *out, uint32_t max);

/* 目标类型：0 = SWD/ARM，1 = RISC-V（JTAG）。HID CMD_RTT action 10 调用。 */
void rtt_bridge_set_target(uint32_t kind);

/* 1 = 当前目标类型是 RISC-V/JTAG。scope 采样器用它选传输后端（见 scope_sampler.c
 * 的 scope_be_* 分派）：类型是全局的，所以网页不做任何改动也能采 RISC-V 目标。 */
uint8_t rtt_bridge_target_is_riscv(void);

/* Stop bridging (the CDC keeps working as the UART bridge). */
void rtt_bridge_stop(void);

/* 同上，但**排队到主循环执行**：收尾时要把还没落地的 RdOff 补写回去，而补写要碰 SWD
 * —— HID 命令是在 USB 中断上下文里跑的，位翻转引擎绝不能在那儿驱动。
 * HID 的 STOP / scope 的 START 都走这个（见 api_param.c），语义与 rtt_bridge_stop()
 * 一致，只是最多晚一个主循环节拍。 */
void rtt_bridge_request_stop(void);

int rtt_bridge_is_running(void);

/* Call from the main loop. Does nothing unless the bridge is running and the
 * DAP has been idle long enough. */
void rtt_bridge_poll(void);

/* Called by the DAP command engine whenever it executes a host request, so the
 * RTT poller stays out of the way. */
void rtt_bridge_note_dap_activity(void);

/* Status/telemetry for the HID readout (also handy when debugging the bridge).
 * words[0] = running | channel<<8 | flags<<16
 * words[1] = control block address
 * words[2] = up-buffer address / size
 * words[3] = drained bytes (total)
 * words[4] = poll iterations / drains
 * words[5] = read errors / write errors
 * words[6] = bursts (transfers) / bytes moved in the last poll
 * words[7] = DAP idle gate hits / ring space left
 * Returns the number of words written (up to 8). */
uint32_t rtt_bridge_status(uint32_t *out, uint32_t words);

/* ------------------------------------------------------------------ */
/* 给 scope 采样器复用（见 src/scope/scope_sampler.c）                  */
/*                                                                     */
/* 采样器不自己写一份 SWD 初始化：那条路径里全是实测换来的细节 ——     */
/* 4 MHz 起手 → 20 MHz 斜坡换挡 → 换挡后热身 → 清 sticky → 失败返回    */
/* -4 让上层降档。两处各写一份迟早改漏一处。                           */
/* ------------------------------------------------------------------ */

/* 确保链路可用（0 = ok；语义与 rtt_swd_init 一致：-1 SWJ_Clock、-2 SWD init、-4 该档不可用） */
int      rtt_bridge_swd_ensure_ready(void);

/* 桥这一侧的链路现在是不是就绪的。**采样器不能只看自己那份 s_swd_ready** ——
 * 主机碰过 DAP（rtt_bridge_note_dap_activity）、桥 stop、链路恢复失败，都会把桥
 * 这一侧清掉，而采样器那份还是 1，于是它既不重新初始化、也拿不到新装的时钟档。 */
int      rtt_bridge_swd_is_ready(void);

/* 块读目标内存（0 = 成功）。内部按 s_chunk 切块，带失败重试。 */
int      rtt_bridge_read(uint32_t addr, uint8_t *dst, uint32_t len);

/* 最近一次 DAP 命令的 MCHTMR 时刻（0 = 从未执行过）——采样器用它做"让路"判断 */
uint32_t rtt_bridge_last_dap_ticks(void);

/* 运行时改 SWD 时钟档（运行中也能改；换挡走桥那套斜坡路径） */
void     rtt_bridge_set_swd_clock(uint32_t hz);
uint32_t rtt_bridge_swd_clock_hz(void);

/* 采样器专用换挡：请求档位 **并把链路标成"下次要重新初始化"**。
 *
 * 与 rtt_bridge_set_swd_clock 的区别很关键：那个只在链路已经就绪时才真的装载
 * blob，链路没就绪就只把值记下来（等将来的初始化去装）。采样器那边有一份独立的
 * s_swd_ready，两边一旦不同步就会踩到"报的是新频率、跑的还是旧 blob"——
 * 实测 SWJ_Clock 请求 1 MHz 与 60 MHz 的 M4 标定读数一模一样，而且从慢档直接
 * 跳回快档时第一次访问就 -4，因为少了 rtt_swd_init() 里那段 20 MHz 斜坡。
 * 所以换挡一律走"下次重新初始化"，斜坡也就跟着走了。 */
void     rtt_bridge_request_swd_clock(uint32_t hz);

/* 链路"重来一次"：SWD 侧 = 能降就降一档 + 清 sticky + 重新初始化（含 20 MHz 斜坡），
 * RISC-V 侧 = 重开 TAP/DM。返回 0 = 重新初始化成功。
 *
 * 给 scope 的验收读用：换挡后的第一次 AP 访问会瞬态失败（实测 60 MHz 档约 13% 的
 * START 会踩到，45 MHz 及以下 0/301），而**光重试没有用**（失败后 0~100 ms 连探
 * 都读不动，清 sticky 也不够）——只有重新初始化才恢复。桥自己的自愈走的就是这条。 */
int      rtt_bridge_link_recover(void);

#endif /* __RTT_BRIDGE_H__ */
