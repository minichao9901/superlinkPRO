/* SPDX-License-Identifier: Apache-2.0 */
#include "board.h"
#include "clock.h"
#include "hpm_soc.h"
#include "hpm_interrupt.h"
#include "hpm_clock_drv.h"
#include "hpm_gptmr_drv.h"
#include "bus_periodic.h"
#define BP_TIMER HPM_GPTMR1
#define BP_CHANNEL BP_TIMER_CHANNEL
static uint8_t initialized;
uint32_t bp_lock(void) { return disable_global_irq(CSR_MSTATUS_MIE_MASK); }
void bp_unlock(uint32_t level) { restore_global_irq(level); }
uint64_t bp_now(void) {
    volatile uint32_t *t = (volatile uint32_t *)HPM_MCHTMR_BASE;
    uint32_t hi, lo;
    do { hi = t[1]; lo = t[0]; } while (hi != t[1]);
    return ((uint64_t)hi << 32) | lo;
}
void bp_timer_stop(void) {
    if (!initialized) return;
    gptmr_disable_irq(BP_TIMER, GPTMR_CH_RLD_IRQ_MASK(BP_CHANNEL));
    gptmr_stop_counter(BP_TIMER, BP_CHANNEL);
    gptmr_clear_status(BP_TIMER, GPTMR_CH_RLD_STAT_MASK(BP_CHANNEL));
}
uint8_t bp_timer_arm(uint64_t ticks) {
    init_gptmr1_clock();
    uint32_t frequency = clock_get_frequency(clock_gptmr1);
    if (!frequency) return 1U;
    if (!initialized) {
        gptmr_channel_config_t cfg;
        gptmr_channel_get_default_config(BP_TIMER, &cfg);
        cfg.mode = gptmr_work_mode_no_capture; cfg.reload = 1000U;
        cfg.cmp[0] = cfg.cmp[1] = 0U;
        if (gptmr_channel_config(BP_TIMER, BP_CHANNEL, &cfg, false) != status_success) return 1U;
        initialized = 1U;
        /* IRQ/priority already installed by LED; never change its clock or channel. */
    }
    uint64_t reload = (ticks * frequency + 23999999U) / 24000000U;
    if (reload < 2U) reload = 2U;
    if (reload > 0xFFFFFFFEU) reload = 0xFFFFFFFEU;
    bp_timer_stop();
    gptmr_channel_config_update_reload(BP_TIMER, BP_CHANNEL, (uint32_t)reload);
    gptmr_channel_reset_count(BP_TIMER, BP_CHANNEL);
    gptmr_enable_irq(BP_TIMER, GPTMR_CH_RLD_IRQ_MASK(BP_CHANNEL));
    gptmr_start_counter(BP_TIMER, BP_CHANNEL);
    return 0U;
}
