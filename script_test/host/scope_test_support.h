/* Host-only hardware substitutes. Production sampler C is compiled by the runner. */
#include <assert.h>
#include <stdint.h>
static int adc_stream_enabled(void) { return 0; }
#include <stdio.h>
#include <string.h>
#define ATTR_PLACE_AT_NONCACHEABLE_BSS_WITH_ALIGNMENT(n) __attribute__((aligned(n)))
#define CSR_MSTATUS_MIE_MASK 8U
#define SWO_IN_EP 0x83U
static uint32_t test_clock, test_read_cost = 36U, test_previous, test_calls;
static uint32_t test_last_dap;
static int test_fail_read;
static uint32_t test_stop_call;
static uint32_t test_fail_call;
static unsigned test_tx_count, test_zlp_count;
static uint8_t test_tx_last[512];
void scope_sampler_stop(uint8_t drain_tail);
static uint8_t test_cdc = 1;
static uint32_t test_clock_read(void) { return test_clock++; }
static uint32_t disable_global_irq(uint32_t m) { (void)m; return 1; }
static void enable_global_irq(uint32_t m) { (void)m; }
static int usbd_ep_start_write(uint8_t b, uint8_t e, uint8_t *p, uint32_t n)
{
    assert(b == 0 && e == SWO_IN_EP);
    if (n == 0) { assert(!p); test_zlp_count++; }
    else { assert(n == sizeof test_tx_last); memcpy(test_tx_last, p, n); test_tx_count++; }
    return 0;
}
static uint8_t swd_read_word_hold_prepare(uint32_t a) { (void)a; return 1; }
static uint8_t swd_read_word_pipe(uint32_t *v)
{
    if (test_fail_read || (test_fail_call && test_calls + 1 == test_fail_call))
    { test_fail_read = 0; test_fail_call = 0; return 0; }
    if (v) *v = test_previous;
    test_previous = ++test_calls;
    if (test_stop_call && test_calls == test_stop_call) scope_sampler_stop(1);
    test_clock += test_read_cost;
    return 1;
}
static uint8_t swd_read_block4(uint32_t a, uint8_t *d, uint32_t n)
{ (void)a; memset(d, 0, n); test_clock += test_read_cost; return 1; }
static int riscv_jtag_read(uint32_t a, uint8_t *d, uint32_t n)
{ return swd_read_block4(a, d, n) ? 0 : -1; }
static int riscv_jtag_hold_prepare(uint32_t a) { return swd_read_word_hold_prepare(a) ? 0 : -1; }
static int riscv_jtag_hold_read(uint32_t *v) { return swd_read_word_pipe(v) ? 0 : -1; }
static int riscv_jtag_is_open(void) { return 1; }
static int riscv_jtag_open(void) { return 0; }
static int rtt_bridge_target_is_riscv(void) { return 0; }
static uint32_t rtt_bridge_swd_clock_hz(void) { return 60000000U; }
static int rtt_bridge_swd_is_ready(void) { return 1; }
static int rtt_bridge_swd_ensure_ready(void) { return 0; }
static int rtt_bridge_link_recover(void) { return 0; }
static int rtt_bridge_read(uint32_t a, uint8_t *d, uint32_t n)
{ return swd_read_block4(a, d, n) ? 0 : -1; }
static int rtt_bridge_dap_recent(uint32_t now) { return test_last_dap && now - test_last_dap < 480000U; }
static void rtt_bridge_request_swd_clock(uint32_t h) { (void)h; }
static uint8_t chry_dap_usb2uart_is_enabled(void) { return test_cdc; }
static void chry_dap_usb2uart_set_enabled(uint8_t v) { test_cdc = v; }
static struct { uint32_t clock_delay; } DAP_Data;
