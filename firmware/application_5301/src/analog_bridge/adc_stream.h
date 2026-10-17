/* SPDX-License-Identifier: Apache-2.0 */
#ifndef ADC_STREAM_H
#define ADC_STREAM_H
#include <stdint.h>
#define ADC_DMA_WORDS 4096U
#define ADC_BLOCK_BYTES 4096U
#define ADC_BLOCK_SAMPLES 2031U /* 4094 bytes: short packet, no ZLP */
#define ADC_FAST_MAX_RATE 2000000U
#define ADC_FAST_CHANNEL 6U /* PB14, EVKLite J3[10] */
#define ADC_MAX_INFLIGHT 32U
uint8_t adc_stream_open(uint8_t bits, uint32_t rate, uint32_t count, uint32_t *token);
uint8_t adc_stream_open_pipeline(uint8_t bits, uint32_t rate, uint32_t count,
                                 uint8_t readers, uint32_t *token);
uint8_t adc_stream_start(void);
uint8_t adc_stream_close(void);
uint8_t adc_stream_enabled(void);
void adc_stream_end(void);
void adc_stream_poll(void);
void adc_stream_complete(void);
void adc_stream_reset(uint8_t configured);
void adc_stream_status(uint8_t *out);
uint8_t adc_hw_supported(void);
uint8_t adc_hw_prepare(uint32_t *buffer, uint8_t bits, uint32_t requested,
                       uint32_t count, uint32_t *actual);
void adc_hw_begin(void);
void adc_hw_abort(void); /* ISR-safe trigger/DMA gate, only on USB reset */
void adc_hw_stop(void);
void adc_hw_restore(void);
uint16_t adc_hw_position(void);
uint8_t adc_hw_fault(void);
void adc_hw_read_barrier(uint32_t *buffer, uint16_t first, uint16_t count);
void adc_hw_release_until(uint16_t position);
#endif
