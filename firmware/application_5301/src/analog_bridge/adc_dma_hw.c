/* SPDX-License-Identifier: Apache-2.0 */
#include <string.h>
#include "board.h"
#include "clock.h"
#include "hpm_soc.h"
#include "hpm_adc16_drv.h"
#include "hpm_gptmr_drv.h"
#include "hpm_trgm_drv.h"
#include "hpm_trgmmux_src.h"
#include "hpm_clock_drv.h"
#include "hpm_l1c_drv.h"
#include "analog_bridge.h"
#include "adc_stream.h"
#if BOARD_HAS_SPI_BRIDGE && !BOARD_HAS_VREF_ADC
#define TIMER HPM_GPTMR1
#define ADC_TIMER_CH 2U /* LED ch0, periodic buses ch1: keep their clocks and IRQs untouched. */
#define TRIGGER HPM_TRGM0_OUTPUT_SRC_ADC0_STRGI
static uint8_t saved;
static uint32_t mux, pad, route, timer_cr, timer_rld, timer_cmp[2], int_en;
static clk_src_t ana_source, adc_source;
static uint32_t ana_div;
uint8_t adc_hw_supported(void) { return 1U; }
void adc_hw_abort(void) {
    if (!saved) return;
    gptmr_stop_counter(TIMER,ADC_TIMER_CH);
    adc16_seq_disable_hw_trigger(HPM_ADC0);
    HPM_ADC0->SEQ_DMA_CFG |= ADC16_SEQ_DMA_CFG_DMA_RST_MASK;
}
void adc_hw_stop(void) {
    if (!saved) return;
    gptmr_stop_counter(TIMER,ADC_TIMER_CH);
    adc16_seq_disable_hw_trigger(HPM_ADC0);
    /* Finish at most one conversion/DMA write before observing final WR pointer. */
    board_delay_us(2);
}
void adc_hw_restore(void) {
    if (!saved) return;
    adc_hw_abort();
    HPM_ADC0->ADC_CFG0 &= ~ADC16_ADC_CFG0_ADC_AHB_EN_MASK;
    HPM_ADC0->INT_EN=int_en;
    HPM_TRGM0->TRGOCFG[TRIGGER]=route;
    TIMER->CHANNEL[ADC_TIMER_CH].CMP[0]=timer_cmp[0]; TIMER->CHANNEL[ADC_TIMER_CH].CMP[1]=timer_cmp[1];
    TIMER->CHANNEL[ADC_TIMER_CH].RLD=timer_rld; TIMER->CHANNEL[ADC_TIMER_CH].CR=timer_cr;
    HPM_IOC->PAD[IOC_PAD_PB14].PAD_CTL=pad; HPM_IOC->PAD[IOC_PAD_PB14].FUNC_CTL=mux;
    clock_set_source_divider(clock_ana0,ana_source,ana_div);
    clock_set_adc_source(clock_adc0,adc_source);
    analog_periodic_invalidate(); saved=0U;
}
uint8_t adc_hw_prepare(uint32_t *buffer, uint8_t bits, uint32_t hz, uint32_t count, uint32_t *actual) {
    mux=HPM_IOC->PAD[IOC_PAD_PB14].FUNC_CTL; pad=HPM_IOC->PAD[IOC_PAD_PB14].PAD_CTL;
    route=HPM_TRGM0->TRGOCFG[TRIGGER]; timer_cr=TIMER->CHANNEL[ADC_TIMER_CH].CR;
    timer_rld=TIMER->CHANNEL[ADC_TIMER_CH].RLD;
    timer_cmp[0]=TIMER->CHANNEL[ADC_TIMER_CH].CMP[0]; timer_cmp[1]=TIMER->CHANNEL[ADC_TIMER_CH].CMP[1];
    ana_source=clock_get_source(clock_ana0); ana_div=clock_get_divider(clock_ana0);
    adc_source=clock_get_source(clock_adc0); int_en=HPM_ADC0->INT_EN; saved=1U;
    HPM_IOC->PAD[IOC_PAD_PB14].FUNC_CTL=IOC_PAD_FUNC_CTL_ANALOG_MASK;
    HPM_IOC->PAD[IOC_PAD_PB14].PAD_CTL=0U;
    /* Configure only ADC/analog clock. Never call board init_adc0_bus_clock (changes CPU). */
    uint32_t source=clock_get_frequency(clk_pll0clk2);
    uint32_t div=(source+49999999U)/50000000U;
    if (!div || clock_set_source_divider(clock_ana0,clk_src_pll0_clk2,div)!=status_success ||
        clock_set_adc_source(clock_adc0,clk_adc_src_ana0)!=status_success) return 3U;
    clock_add_to_group(clock_adc0,0); clock_add_to_group(clock_mot0,0); init_gptmr1_clock();
    uint32_t conv=clock_get_frequency(clock_adc0), timer=clock_get_frequency(clock_gptmr1);
    if (!conv || conv>50000000U || !timer) return 3U;
    adc16_config_t cfg; adc16_channel_config_t ch; adc16_seq_config_t seq={0};
    adc16_get_default_config(&cfg);
    cfg.res=bits==16U?adc16_res_16_bits:bits==12U?adc16_res_12_bits:
        bits==10U?adc16_res_10_bits:adc16_res_8_bits;
    cfg.conv_mode=adc16_conv_mode_sequence; cfg.adc_clk_div=adc16_clock_divider_1;
    cfg.adc_ahb_en=true;
#if !defined(HPM_IP_FEATURE_ADC16_FORCE_SYNC_AHB) || !HPM_IP_FEATURE_ADC16_FORCE_SYNC_AHB
    cfg.sel_sync_ahb=false;
#endif
    /* Reject impossible conversion periods; keep 3 ADC clocks of acquisition. */
    uint32_t max=conv/(cfg.res+3U);
    uint32_t period=(timer+hz-1U)/hz;
    if (!period || timer/period>max) return 1U;
    if (adc16_init(HPM_ADC0,&cfg)!=status_success) return 3U;
    HPM_ADC0->INT_EN=0U; /* no per-sample IRQ */
    adc16_get_channel_default_config(&ch); ch.ch=ADC_FAST_CHANNEL; ch.sample_cycle=3U;
    if (adc16_init_channel(HPM_ADC0,&ch)!=status_success) return 3U;
    /* Process the one-entry queue to completion on each hardware trigger. */
    seq.seq_len=1U; seq.queue[0].ch=ADC_FAST_CHANNEL; seq.cont_en=true; seq.hw_trig_en=false;
    if (adc16_set_seq_config(HPM_ADC0,&seq)!=status_success) return 3U;
    adc16_seq_disable_hw_trigger(HPM_ADC0);
    adc16_dma_config_t dma={0}; dma.start_addr=buffer; dma.buff_len_in_4bytes=ADC_DMA_WORDS;
    dma.stop_en=true; dma.stop_pos=count && count<ADC_DMA_WORDS?count:ADC_DMA_WORDS-1U;
    if (adc16_init_seq_dma(HPM_ADC0,&dma)!=status_success) return 3U;
    l1c_dc_flush((uint32_t)(uintptr_t)buffer,ADC_DMA_WORDS*4U);
    adc16_clear_status_flags(HPM_ADC0,0xFFFFFFFFU);
    /* Keep both GPTMR compares: CMP0 raises the pulse halfway through the
     * period and CMP1 returns it at reload (the SDK PWM example uses the same
     * compare-pair pattern). */
    gptmr_channel_config_t t; gptmr_channel_get_default_config(TIMER,&t);
    t.reload=period; t.cmp[0]=period/2U; t.cmp[1]=period; t.enable_cmp_output=true;
    t.cmp_initial_polarity_high=false;
    if (gptmr_channel_config(TIMER,ADC_TIMER_CH,&t,false)!=status_success) return 3U;
    trgm_output_t trig={0}; trig.input=HPM_TRGM0_INPUT_SRC_GPTMR1_OUT2;
    trig.type=trgm_output_same_as_input;
    trgm_output_config(HPM_TRGM0,TRIGGER,&trig);
    *actual=timer/period; return 0U;
}
void adc_hw_begin(void) {
    /* Resetting the counter can change its compare output. Keep the ADC trigger
     * gated until the timer has a clean phase, then enable conversion and run. */
    gptmr_channel_reset_count(TIMER,ADC_TIMER_CH);
    adc16_clear_status_flags(HPM_ADC0,0xFFFFFFFFU);
    adc16_seq_enable_hw_trigger(HPM_ADC0);
    gptmr_start_counter(TIMER,ADC_TIMER_CH);
}
uint16_t adc_hw_position(void) {
    return (uint16_t)ADC16_SEQ_WR_ADDR_SEQ_WR_POINTER_GET(HPM_ADC0->SEQ_WR_ADDR);
}
uint8_t adc_hw_fault(void) {
    uint32_t flags=HPM_ADC0->INT_STS;
    if (flags & ADC16_INT_STS_SEQ_HW_CFLCT_MASK) return 6U;
    if (flags & ADC16_INT_STS_DMA_FIFO_FULL_MASK) return 7U;
    if (flags & ADC16_INT_STS_AHB_ERR_MASK) return 8U;
    if (flags & ADC16_INT_STS_SEQ_DMAABT_MASK) return 5U;
    return 0U;
}
void adc_hw_release_until(uint16_t position) { adc16_set_seq_stop_pos(HPM_ADC0,position); }
static void invalidate_samples(uint32_t *buffer, uint16_t first, uint16_t count) {
    uint32_t start=HPM_L1C_CACHELINE_ALIGN_DOWN((uint32_t)(uintptr_t)(buffer+first));
    uint32_t end=HPM_L1C_CACHELINE_ALIGN_UP((uint32_t)(uintptr_t)(buffer+first+count));
    if (count) l1c_dc_invalidate(start,end-start);
}
void adc_hw_read_barrier(uint32_t *buffer, uint16_t first, uint16_t count) {
    /* The CPU only reads this cache-line-aligned DMA allocation. Round each
     * consumed span to complete lines, splitting at ring wrap; no dirty CPU
     * data or neighboring allocation can be invalidated. A later block
     * invalidates its lines again, including a line shared with live DMA. */
    uint16_t span=ADC_DMA_WORDS-first;
    if (span>count) span=count;
    invalidate_samples(buffer,first,span);
    if (count>span) invalidate_samples(buffer,0U,count-span);
}
#else
/* Board with VREF/LED ADC sharing and no SPI endpoints: explicitly unsupported. */
uint8_t adc_hw_supported(void) { return 0U; }
uint8_t adc_hw_prepare(uint32_t *p,uint8_t b,uint32_t r,uint32_t n,uint32_t *a) {
    (void)p;(void)b;(void)r;(void)n;(void)a;return ANALOG_UNSUPPORTED;
}
void adc_hw_begin(void) {} void adc_hw_abort(void) {} void adc_hw_stop(void) {} void adc_hw_restore(void) {}
uint16_t adc_hw_position(void) { return 0U; } uint8_t adc_hw_fault(void) { return 0U; }
void adc_hw_release_until(uint16_t p) {(void)p;}
void adc_hw_read_barrier(uint32_t *p,uint16_t f,uint16_t n) {(void)p;(void)f;(void)n;}
#endif
