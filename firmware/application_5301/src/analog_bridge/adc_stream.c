/* SPDX-License-Identifier: Apache-2.0 */
#include <string.h>
#include "usb_composite.h"
#include "spi_bridge.h"
#include "bus_periodic.h"
#include "analog_bridge.h"
#include "scope_sampler.h"
#include "rtt_bridge.h"
#include "adc_stream.h"
/* SPI OUT ring = ADC DMA; SPI IN ring = two USB blocks. No new sample buffers. */
static uint32_t *capture;
static uint8_t *transmit;
static volatile uint8_t configured, owned, start_req, stop_req, close_req, reset_req, busy, ended;
static uint8_t running, prepared, dma_ready, bits, fault, wslot, rslot, end_queued;
static uint8_t end_depth, end_completed;
static volatile uint8_t queued;
static uint16_t read_pos, last_pos, lengths[2];
static volatile uint32_t generation;
static uint32_t requested, target, rate, received, sent, block_seq;
static uint64_t last_push;
static void word(uint8_t *p, uint32_t v) {
    for (uint8_t i=0;i<4;i++) p[i]=(uint8_t)(v>>(8U*i));
}
uint8_t adc_stream_enabled(void) { return owned; }
void adc_stream_reset(uint8_t ready) {
    configured=ready; generation++;
    if (owned) { reset_req=1U; adc_hw_abort(); busy=0U; }
}
uint8_t adc_stream_open(uint8_t width, uint32_t hz, uint32_t count, uint32_t *token) {
    return adc_stream_open_pipeline(width,hz,count,1U,token);
}
uint8_t adc_stream_open_pipeline(uint8_t width, uint32_t hz, uint32_t count,
                                 uint8_t readers, uint32_t *token) {
    if (!adc_hw_supported()) return ANALOG_UNSUPPORTED;
    if ((width!=8U && width!=10U && width!=12U && width!=16U) || !hz || hz>ADC_FAST_MAX_RATE ||
        !readers || readers>ADC_MAX_INFLIGHT)
        return ANALOG_RANGE;
    uint32_t level=bp_lock(); uint8_t rc=ANALOG_OK;
    if (!configured) rc=ANALOG_STATE;
    else if (owned || bus_periodic_owns(BP_ADC) || bus_periodic_owns(BP_I2C) ||
             bus_periodic_owns(BP_SPI) || scope_sampler_is_running() || rtt_bridge_is_running()) rc=ANALOG_BUSY;
    else if (!spi_bridge_adc_claim(&capture,&transmit)) rc=ANALOG_BUSY;
    else {
        owned=1U; bits=width; requested=hz; target=count; *token=++generation;
        start_req=stop_req=close_req=reset_req=busy=ended=0U;
        running=prepared=dma_ready=fault=wslot=rslot=queued=end_queued=0U;
        end_depth=readers; end_completed=0U;
        read_pos=last_pos=0U; received=sent=block_seq=rate=0U; last_push=bp_now();
    }
    bp_unlock(level); return rc;
}
uint8_t adc_stream_start(void) {
    uint32_t level=bp_lock(); uint8_t rc=ANALOG_OK;
    if (!owned || ended || end_queued || stop_req) rc=ANALOG_STATE;
    else if (running || prepared || start_req) rc=ANALOG_BUSY;
    else start_req=1U;
    bp_unlock(level); return rc;
}
void adc_stream_end(void) { if (owned) stop_req=1U; }
uint8_t adc_stream_close(void) {
    uint32_t level=bp_lock(); uint8_t rc=ANALOG_OK;
    if (owned) { if (!ended || busy || queued) rc=ANALOG_BUSY; else { close_req=1U; rc=ANALOG_BUSY; } }
    bp_unlock(level); return rc;
}
static void kick(void) {
    uint32_t level=bp_lock();
    if (owned && configured && !reset_req && !busy && queued) {
        busy=1U;
        if (usbd_ep_start_write(0,SPI_IN_EP,transmit+ADC_BLOCK_BYTES*rslot,lengths[rslot])) busy=0U;
    }
    bp_unlock(level);
}
void adc_stream_complete(void) {
    uint32_t level=bp_lock();
    if (owned && busy && queued) {
        /* One END per negotiated native IN request: CLOSE cannot release the
         * shared endpoint while another reader can still consume next-run data. */
        if (transmit[ADC_BLOCK_BYTES*rslot+5U]==2U && ++end_completed==end_depth) ended=1U;
        rslot^=1U; queued--; busy=0U;
    }
    bp_unlock(level); kick(); /* ISR only returns a USB slot. */
}
void adc_stream_status(uint8_t *out) {
    uint32_t level=bp_lock();
    word(out,generation); word(out+4U,rate); word(out+8U,received); word(out+12U,sent);
    word(out+16U,fault); out[20]=owned; out[21]=running; out[22]=ended; out[23]=prepared;
    bp_unlock(level);
}
static void retire(void) {
    if (prepared) { adc_hw_stop(); adc_hw_restore(); prepared=0U; }
    uint32_t level=bp_lock();
    owned=0U; spi_bridge_adc_release(); reset_req=close_req=0U;
    bp_unlock(level);
}
void adc_stream_poll(void) {
    if (!owned) return;
    if (reset_req || close_req) { retire(); return; }
    if (start_req) {
        start_req=0U; prepared=1U;
        fault=adc_hw_prepare(capture,bits,requested,target,&rate);
        dma_ready=!fault;
        if (reset_req) { retire(); return; }
        uint32_t level=bp_lock();
        if (fault || stop_req || scope_sampler_is_running() || rtt_bridge_is_running()) stop_req=1U;
        else if(!reset_req) { running=1U; adc_hw_begin(); }
        bp_unlock(level);
        if(reset_req){retire();return;}
    }
    if (running) {
        uint8_t error=adc_hw_fault();
        if (error) { fault=error; stop_req=1U; }
        if (stop_req) { adc_hw_stop(); running=0U; }
    }
    if (dma_ready) {
        uint16_t pos=adc_hw_position();
        received+=(pos-last_pos)&(ADC_DMA_WORDS-1U); last_pos=pos;
        if (target && received>=target) {
            if (running) adc_hw_stop();
            running=0U; stop_req=1U; received=target;
            last_pos=(uint16_t)(target&(ADC_DMA_WORDS-1U));
            if (fault==5U) fault=0U; /* planned exact-count stop */
        }
    }
    kick();
    if (end_queued>=end_depth || queued==2U) return;
    uint16_t available=(uint16_t)((last_pos-read_pos)&(ADC_DMA_WORDS-1U));
    uint16_t count=available>ADC_BLOCK_SAMPLES?ADC_BLOCK_SAMPLES:available;
    if (running && count<ADC_BLOCK_SAMPLES && bp_now()-last_push<48000U) return;
    if (!count && !stop_req) return;
    uint8_t *p=transmit+ADC_BLOCK_BYTES*wslot;
    memcpy(p,"ADS2",4); p[4]=2U; p[5]=count?1U:2U; p[6]=bits; p[7]=0U;
    word(p+8U,generation); word(p+12U,block_seq++); word(p+16U,sent); word(p+20U,rate);
    p[24]=(uint8_t)count; p[25]=(uint8_t)(count>>8); p[26]=ADC_FAST_CHANNEL; p[27]=2U; word(p+28U,fault);
    if (count) {
        adc_hw_read_barrier(capture,read_pos,count);
        for (uint16_t i=0;i<count;i++) {
            uint16_t raw=(uint16_t)capture[(read_pos+i)&(ADC_DMA_WORDS-1U)];
            raw>>=16U-bits; p[32U+2U*i]=(uint8_t)raw; p[33U+2U*i]=(uint8_t)(raw>>8);
        }
        read_pos=(read_pos+count)&(ADC_DMA_WORDS-1U); sent+=count;
        if (running) {
            uint32_t space=ADC_DMA_WORDS-1U;
            if (target && target-sent<space) space=target-sent;
            adc_hw_release_until((uint16_t)((read_pos+space)&(ADC_DMA_WORDS-1U)));
        }
    } else end_queued++;
    lengths[wslot]=(uint16_t)(32U+2U*count); last_push=bp_now();
    uint32_t level=bp_lock(); queued++; wslot^=1U; bp_unlock(level); kick();
}
