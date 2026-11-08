"""Exercise the production CDC IN completion and stop boundary on the host."""

import os
from pathlib import Path
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
source = (root / 'firmware/application_5301/src/usb/usb_composite.c').read_text(encoding='utf-8')
start = source.index('static void cdc_in_kick(')
end = source.index('struct usbd_endpoint dap_out_ep', start)
model = r'''
#include <assert.h>
#include <stddef.h>
#include <stdint.h>
#define CDC_SOURCE_UART 0
#define CDC_SOURCE_RTT 1
#define CDC_IN_EP 0x85
#define CSR_MSTATUS_MIE_MASK 8

static uint8_t usbtx_idle_flag = 1, cdc_in_needs_short, source;
static uint32_t g_uartrx, queued, bytes_read, data_packets, zlp_packets;
static int interrupt_locked, reject_next;

static uint8_t uartx_get_cdc_source(void) { return source; }
static uint32_t disable_global_irq(uint32_t mask) { assert(mask == 8 && !interrupt_locked); interrupt_locked = 1; return 0; }
static void restore_global_irq(uint32_t prior) { assert(prior == 0 && interrupt_locked); interrupt_locked = 0; }
static uint32_t chry_ringbuffer_get_used(uint32_t *ring) { assert(interrupt_locked); return *ring; }
static uint8_t *chry_ringbuffer_linear_read_setup(uint32_t *ring, uint32_t *length) {
    static uint8_t data[4096]; *length = *ring; return data;
}
static void chry_ringbuffer_linear_read_done(uint32_t *ring, uint32_t n) {
    assert(interrupt_locked && n && n <= *ring); *ring -= n; bytes_read += n;
}
static uint16_t usbd_get_ep_mps(uint8_t bus, uint8_t ep) { assert(bus == 0 && ep == CDC_IN_EP); return 512; }
static int usbd_ep_start_write(uint8_t bus, uint8_t ep, uint8_t *data, uint32_t n) {
    assert(interrupt_locked && bus == 0 && ep == CDC_IN_EP);
    if (reject_next) { reject_next = 0; return -1; }
    queued = n;
    if (n) { assert(data); data_packets++; }
    else { assert(!data); zlp_packets++; }
    return 0;
}
''' + source[start:end] + r'''
static void service(void) { uint32_t level = disable_global_irq(8); cdc_in_kick(0); restore_global_irq(level); }
static void complete(void) { assert(!usbtx_idle_flag); usbd_cdc_acm_bulk_in(0, CDC_IN_EP, queued); }

int main(void) {
    source = CDC_SOURCE_RTT;
    g_uartrx = 2048;
    service(); complete();
    service();
    assert(data_packets == 1 && zlp_packets == 0 && bytes_read == 2048);

    /* STOP after the endpoint already went idle must still finish the read. */
    source = CDC_SOURCE_UART;
    reject_next = 1;
    service();
    assert(zlp_packets == 0 && usbtx_idle_flag);
    service(); service();
    assert(zlp_packets == 1 && !usbtx_idle_flag);
    complete(); service();
    assert(zlp_packets == 1 && bytes_read == 2048);

    /* STOP while an aligned IN is still in flight. */
    source = CDC_SOURCE_RTT;
    g_uartrx = 512;
    service();
    source = CDC_SOURCE_UART;
    complete(); complete();
    assert(zlp_packets == 2 && bytes_read == 2560);

    /* A real short packet completes the tail without an extra transfer. */
    source = CDC_SOURCE_RTT;
    g_uartrx = 2112;
    service();
    source = CDC_SOURCE_UART;
    complete(); service();
    assert(zlp_packets == 2 && bytes_read == 4672);
    return 0;
}
'''

with tempfile.TemporaryDirectory() as folder:
    path = Path(folder)
    (path / 'test.c').write_text(model, encoding='utf-8')
    binary = path / ('test.exe' if os.name == 'nt' else 'test')
    subprocess.run([os.getenv('CC', 'gcc'), '-std=c11', '-Wall', '-Wextra', '-Werror',
                    str(path / 'test.c'), '-o', str(binary)], check=True)
    subprocess.run([str(binary)], check=True)
print('CDC IN: continuous streaming, aligned STOP in-flight/idle, submission retry and short tail PASS')
