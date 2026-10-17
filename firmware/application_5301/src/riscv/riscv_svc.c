/* SPDX-License-Identifier: Apache-2.0 */
/* Copyright (c) 2026 akaInstruments */

#include "riscv_svc.h"

#include "riscv_jtag.h"
#include "board.h"
#include "hpm_common.h"

/* Authoritative service flags: cold gate reads words, producers write bytes. */
service_gate_t riscv_svc_gate;
#define s_pending (riscv_svc_gate.flag[0])

/* MCHTMR runs at 24 MHz (osc24m) - same reference the RTT bridge bench uses. */
#define RISCV_MCHTMR_HZ 24000000UL

#define RISCV_STAGE_BYTES 1024U

static uint32_t s_action;
static uint32_t s_addr;
static uint32_t s_arg1;
static uint32_t s_arg2;

static volatile uint32_t s_rc;
static volatile uint32_t s_moved;
static volatile uint32_t s_ticks;
static volatile uint32_t s_iters_done;
static volatile uint32_t s_check;
static volatile uint32_t s_check_words[2];
static volatile uint32_t s_bytes_per_s;
static volatile uint32_t s_probe[8];

static uint8_t s_stage[RISCV_STAGE_BYTES];

static uint32_t mchtmr_now(void)
{
    return *(volatile uint32_t *)(HPM_MCHTMR_BASE + 0x00);
}

void riscv_svc_request(uint32_t action, uint32_t addr, uint32_t arg1, uint32_t arg2)
{
    s_action = action;
    s_addr = addr;
    s_arg1 = arg1;
    s_arg2 = arg2;
    s_pending = 1U;
}

void riscv_svc_set_delay(uint32_t delay)
{
    riscv_jtag_set_delay(delay);
}

uint32_t riscv_svc_status(uint32_t *out, uint32_t words)
{
    uint32_t n = 0U;

    if (words > 0U) { out[n++] = (uint32_t)riscv_jtag_is_open() |
                                 ((uint32_t)(s_pending != 0U) << 8) |
                                 (((uint32_t)s_rc & 0xFFU) << 16) |
                                 ((uint32_t)s_action << 24); }
    if (words > 1U) { out[n++] = riscv_jtag_idcode(); }
    if (words > 2U) { out[n++] = riscv_jtag_dtmcs(); }
    if (words > 3U) { out[n++] = riscv_jtag_dmstatus(); }
    if (words > 3U) {
        /* DMIPROBE replaces the rest of the block with the raw scans. */
        if (s_action == RISCV_ACT_DMIPROBE)
        {
            for (uint32_t i = 0U; (i < 8U) && (n < words); i++)
            {
                out[n++] = s_probe[i];
            }
            return n;
        }
        out[n++] = s_moved;
    }
    if (words > 5U) { out[n++] = s_ticks; }
    if (words > 6U) { out[n++] = riscv_jtag_last_sbcs(); }
    if (words > 7U) { out[n++] = riscv_jtag_get_delay() |
                                 (s_iters_done << 8); }
    if (words > 8U) { out[n++] = s_bytes_per_s; }
    if (words > 9U) { out[n++] = s_check; }
    if (words > 10U) { out[n++] = s_check_words[0]; }
    if (words > 11U) { out[n++] = s_check_words[1]; }
    return n;
}

static void run_read_bench(uint32_t addr, uint32_t bytes, uint32_t iters)
{
    uint32_t t0;
    uint32_t i = 0U;
    uint32_t moved = 0U;

    if (bytes > RISCV_STAGE_BYTES)
    {
        bytes = RISCV_STAGE_BYTES;
    }

    t0 = mchtmr_now();
    while (i < iters)
    {
        if (riscv_jtag_read(addr, s_stage, bytes) != 0)
        {
            s_rc = 0xFFFFFFFFU; /* read failed */
            break;
        }
        moved += bytes;
        i++;
    }
    s_ticks = mchtmr_now() - t0;
    s_moved = moved;
    s_iters_done = i;
}

static void run_write_bench(uint32_t addr, uint32_t bytes, uint32_t iters)
{
    uint32_t t0;
    uint32_t i = 0U;
    uint32_t moved = 0U;

    if (bytes > RISCV_STAGE_BYTES)
    {
        bytes = RISCV_STAGE_BYTES;
    }
    for (uint32_t k = 0U; k < bytes; k++)
    {
        s_stage[k] = (uint8_t)(k * 7U + 3U);
    }

    t0 = mchtmr_now();
    while (i < iters)
    {
        if (riscv_jtag_write(addr, s_stage, bytes) != 0)
        {
            s_rc = 0xFFFFFFFFU;
            break;
        }
        moved += bytes;
        i++;
    }
    s_ticks = mchtmr_now() - t0;
    s_moved = moved;
    s_iters_done = i;
}

/* Raw DMI scan rate: the engine's ground truth, independent of the block path.
 * A single-word SBA read is 4 DMI scans (sbcs, sbaddress0, read sbdata0 x2),
 * so this measures the per-scan cost directly. */
static void run_scan_bench(uint32_t addr, uint32_t iters)
{
    uint32_t t0 = mchtmr_now();
    uint32_t i = 0U;

    for (i = 0U; i < iters; i++)
    {
        uint32_t v = 0U;

        if (riscv_jtag_read_word(addr, &v) != 0)
        {
            s_rc = 0xFFFFFFFFU;
            break;
        }
    }
    s_ticks = mchtmr_now() - t0;
    s_moved = 0U;
    s_iters_done = i;
}

/* Read `words` words and report a checksum plus the first four words, so the
 * host can verify the SBA path against data it wrote itself. */
static void run_read_check(uint32_t addr, uint32_t words)
{
    uint32_t sum = 0U;
    uint32_t n = words;

    if (n > (RISCV_STAGE_BYTES / 4U))
    {
        n = RISCV_STAGE_BYTES / 4U;
    }
    if (n > 0U)
    {
        if (riscv_jtag_read(addr, s_stage, n * 4U) != 0)
        {
            s_rc = 0xFFFFFFFFU;
            return;
        }
        for (uint32_t i = 0U; i < n; i++)
        {
            uint32_t w;
            uint8_t *p = &s_stage[i * 4U];

            w = (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
                ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
            sum += w;
        }
        for (uint32_t i = 0U; (i < 2U) && ((i * 4U + 4U) <= (n * 4U)); i++)
        {
            const uint8_t *p = &s_stage[i * 4U];

            s_check_words[i] = (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
                               ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
        }
    }
    s_check = sum;
    s_moved = n * 4U;
}

void riscv_svc_poll(void)
{
    if (s_pending == 0U)
    {
        return;
    }

    uint32_t action = s_action;
    uint32_t addr = s_addr;
    uint32_t a1 = s_arg1;
    uint32_t a2 = s_arg2;

    /* s_pending stays set while the action runs: it is what the host polls to
     * know the operation finished (clearing it up front let the host read the
     * counters mid-operation and see all zeros). */
    s_rc = 0U;
    s_moved = 0U;
    s_ticks = 0U;
    s_iters_done = 0U;

    switch (action)
    {
    case RISCV_ACT_OPEN:
    {
        int rc = riscv_jtag_open();

        s_rc = (rc == 0) ? 0U : (uint32_t)(-rc);
        break;
    }
    case RISCV_ACT_STOP:
        riscv_jtag_close();
        break;
    case RISCV_ACT_RBENCH:
        if (riscv_jtag_is_open() != 0)
        {
            run_read_bench(addr, a1, a2);
        }
        else
        {
            s_rc = 1U;
        }
        break;
    case RISCV_ACT_WBENCH:
        if (riscv_jtag_is_open() != 0)
        {
            run_write_bench(addr, a1, a2);
        }
        else
        {
            s_rc = 1U;
        }
        break;
    case RISCV_ACT_SBENCH:
        if (riscv_jtag_is_open() != 0)
        {
            run_scan_bench(addr, a1);
        }
        else
        {
            s_rc = 1U;
        }
        break;
    case RISCV_ACT_RCHECK:
        if (riscv_jtag_is_open() != 0)
        {
            run_read_check(addr, a1);
        }
        else
        {
            s_rc = 1U;
        }
        break;
    case RISCV_ACT_DMIPROBE:
    {
        uint64_t raw[8] = {0};

        riscv_jtag_probe((addr == 0U) ? 6U : addr, raw);
        for (uint32_t i = 0U; i < 4U; i++)
        {
            s_probe[i * 2U + 0U] = (uint32_t)(raw[i] & 0xFFFFFFFFULL);
            s_probe[i * 2U + 1U] = (uint32_t)((raw[i] >> 32) & 0x1FFULL);
        }
        break;
    }
    case RISCV_ACT_SBASTAT:
    {
        /* 硬件回读 SBCS（不信缓存里的那份）+ sticky 错误统计。
         * 状态字：[6] = SBCS 实值、[9] = 错误事件数、[10] = 首次出错的 SBCS、
         * [11] = 重读/重挂次数（低位 16 位重读整块、高位 16 位单字重挂）。 */
        uint32_t ev = 0U;
        uint32_t first = 0U;
        uint32_t retries = 0U;
        uint32_t recovers = 0U;

        (void)riscv_jtag_clear_errors();      /* 回读 SBCS（顺带清 sticky），更新 last_sbcs */
        riscv_jtag_sba_stats(&ev, &first, &retries, &recovers);        s_check = ev;
        s_check_words[0] = first;
        s_check_words[1] = (retries & 0xFFFFU) | ((recovers & 0xFFFFU) << 16);
        break;
    }
    case RISCV_ACT_CONFIG:
    case RISCV_ACT_STATUS:
    default:
        break;
    }

    if (s_ticks != 0U)
    {
        /* Report the rate as bytes/s so the host does not have to know the
         * MCHTMR frequency. */
        s_bytes_per_s = (uint32_t)(((uint64_t)s_moved * RISCV_MCHTMR_HZ) / s_ticks);
    }

    s_pending = 0U;
}
