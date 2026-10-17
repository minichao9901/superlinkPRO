/* SPDX-License-Identifier: Apache-2.0 */
#include <string.h>
#include "board.h"
#include "clock.h"
#include "hpm_adc16_drv.h"
#include "hpm_clock_drv.h"
#include "hpm_soc.h"
#include "spi_bridge.h"
#include "led_state.h"
#include "analog_bridge.h"
#include "adc_stream.h"
/* Only board-confirmed inputs are advertised. Never commandeer debug/UART/LED pads. */
#if BOARD_HAS_VREF_ADC
#define ANALOG_CHANNEL 2U
#define ANALOG_GAIN 2U
#else
#define ANALOG_CHANNEL 3U /* EVKLite PB11, shared with SPI2. */
#define ANALOG_GAIN 1U
static uint8_t ready;
#endif
void analog_periodic_invalidate(void) {
#if !BOARD_HAS_VREF_ADC
    ready=0U;
#endif
}
static uint32_t stream_word(const uint8_t *p) {
    return p[0]|((uint32_t)p[1]<<8)|((uint32_t)p[2]<<16)|((uint32_t)p[3]<<24);
}
void analog_bridge_hid(uint8_t *req, uint8_t *res) {
    res[1] = 8U; res[2] = ANALOG_CMD; res[3] = req[3];
    memset(res + 4, 0, 4);
    if (req[1] < 2U || req[1] > 62U) { res[4] = ANALOG_RANGE; return; }
    if (req[3] >= ANALOG_STREAM_CAPS && req[3] <= ANALOG_STREAM_PIPELINE) {
        /* HID length counts CMD + action + arguments (the length byte itself is not counted).
         * Legacy OPEN = 11; extended OPEN = 12, with a negotiated IN depth. */
        if (req[3]==ANALOG_STREAM_OPEN ? (req[1]!=11U && req[1]!=12U) : req[1]!=2U) {
            res[4] = ANALOG_RANGE; return;
        }
        if (req[3] == ANALOG_STREAM_CAPS) {
            memset(res+8U,0,20U); memcpy(res+8U,"ADB2",4);
            res[12]=0x8BU; res[13]=2U; res[14]=0x0FU; res[15]=ADC_FAST_CHANNEL;
            for (uint8_t i=0;i<4;i++) res[16U+i]=(uint8_t)(ADC_FAST_MAX_RATE>>(8U*i));
            res[21]=0x10U; res[23]=0x10U; /* DMA 4096 words, block 4096 bytes */
            res[24]=spi_bridge_adc_flags(); res[25]=adc_hw_supported();
            res[26]=3300U&255U; res[27]=3300U>>8; res[1]=28U;
        } else if (req[3] == ANALOG_STREAM_OPEN) {
            uint32_t token = 0U;
            if (req[1]==12U)
                res[4]=adc_stream_open_pipeline(req[4],stream_word(req+5U),stream_word(req+9U),req[13],&token);
            else res[4]=adc_stream_open(req[4],stream_word(req+5U),stream_word(req+9U),&token);
            for (uint8_t i = 0; i < 4; i++) res[8U + i] = (uint8_t)(token >> (8U * i));
            res[1] = 12U;
        } else if (req[3] == ANALOG_STREAM_END) adc_stream_end();
        else if (req[3] == ANALOG_STREAM_CLOSE) res[4]=adc_stream_close();
        else if (req[3] == ANALOG_STREAM_START) res[4]=adc_stream_start();
        else if (req[3] == ANALOG_STREAM_STATUS) { adc_stream_status(res+8U); res[1]=32U; }
        else { res[8]=1U; res[9]=ADC_MAX_INFLIGHT; res[1]=10U; } /* counted END v1, 1..32 readers */
        return;
    }
    if (req[3] == ANALOG_DAC_CAPS) {
        if (req[1] != 2U) { res[4] = ANALOG_RANGE; return; }
        /* Versioned reservation, no buffers, pin writes, IRQs or clock setup. */
        memset(res + 8, 0, 16); memcpy(res + 8, "DAC1", 4); res[12] = 1U;
        res[1] = 24U; return;
    }
    if (req[3] >= ANALOG_DAC_CONFIG && req[3] <= ANALOG_DAC_GET_CONFIG) {
        const uint8_t minimum[] = {10U, 5U, 12U, 11U, 7U, 3U, 3U};
        uint8_t expected = minimum[req[3] - ANALOG_DAC_CONFIG];
        if (req[3] == ANALOG_DAC_WRITE) {
            if (req[1] < 12U) { res[4] = ANALOG_RANGE; return; }
            uint8_t n = req[11];
            if (!n || n > 25U) { res[4] = ANALOG_RANGE; return; }
            expected = (uint8_t)(10U + 2U * n);
        }
        res[4] = req[1] == expected ? ANALOG_UNSUPPORTED : ANALOG_RANGE;
        return;
    }
    if (req[1] != 2U || req[3] != ANALOG_ADC_CAPS) { res[4] = ANALOG_RANGE; return; }
    memcpy(res + 8, "ANA1", 4);
    res[12] = ANALOG_CHANNEL; res[13] = 16U; res[14] = ANALOG_GAIN;
    res[15] = 0U; /* no physical DAC */
    res[16] = 3300U & 255U; res[17] = 3300U >> 8;
    res[18] = 1000U & 255U; res[19] = 1000U >> 8;
    res[1] = 20U;
}
uint8_t analog_periodic_ready(void) { return !spi_bridge_is_enabled() && !adc_stream_enabled(); }
uint8_t analog_periodic_check(const uint8_t *p, uint16_t len) {
    return len != 2U || p[0] != ANALOG_CHANNEL ||
        (p[1] != 8U && p[1] != 10U && p[1] != 12U && p[1] != 16U);
}
uint8_t analog_periodic_exec(const uint8_t *p, uint16_t len, uint8_t *data, uint8_t *n) {
    uint16_t raw = 0U; *n = 0U;
    if (analog_periodic_check(p, len)) return 1U;
    if (!analog_periodic_ready()) return 2U;
#if BOARD_HAS_VREF_ADC
    if (led_state_read_vref_raw(&raw)) return 3U;
#else
    if (!ready) {
        adc16_config_t cfg; adc16_channel_config_t ch;
        /* The board init_adc0_bus_clock() also changes CPU0: never call it here. */
        if (clock_set_adc_source(clock_adc0, clk_adc_src_ahb0) != status_success) return 3U;
        clock_add_to_group(clock_adc0, 0); adc16_get_default_config(&cfg);
        cfg.res = adc16_res_16_bits; cfg.conv_mode = adc16_conv_mode_oneshot;
        cfg.adc_clk_div = adc16_clock_divider_8; cfg.sel_sync_ahb = true;
        if (adc16_init(HPM_ADC0, &cfg) != status_success) return 3U;
        adc16_get_channel_default_config(&ch); ch.ch = ANALOG_CHANNEL; ch.sample_cycle = 20U;
        if (adc16_init_channel(HPM_ADC0, &ch) != status_success) return 3U;
        adc16_enable_oneshot_mode(HPM_ADC0); ready = 1U;
    }
    uint32_t mux = HPM_IOC->PAD[IOC_PAD_PB11].FUNC_CTL;
    HPM_IOC->PAD[IOC_PAD_PB11].FUNC_CTL = IOC_PAD_FUNC_CTL_ANALOG_MASK;
    hpm_stat_t rc = adc16_get_oneshot_result(HPM_ADC0, ANALOG_CHANNEL, &raw);
    HPM_IOC->PAD[IOC_PAD_PB11].FUNC_CTL = mux;
    if (rc != status_success) return 3U;
#endif
    /* Preserve shared ADC native resolution; selected width is output quantization. */
    raw >>= 16U - p[1]; data[0] = raw & 255U; data[1] = raw >> 8; *n = 2U;
    return 0U;
}
