/* SPDX-License-Identifier: Apache-2.0 */
#ifndef ANALOG_BRIDGE_H
#define ANALOG_BRIDGE_H
#include <stdint.h>
#define ANALOG_CMD 0x38U
enum { ANALOG_ADC_CAPS, ANALOG_DAC_CAPS, ANALOG_DAC_CONFIG, ANALOG_DAC_BEGIN,
       ANALOG_DAC_WRITE, ANALOG_DAC_START, ANALOG_DAC_STOP, ANALOG_DAC_STATUS, ANALOG_DAC_GET_CONFIG,
       ANALOG_STREAM_CAPS, ANALOG_STREAM_OPEN, ANALOG_STREAM_END, ANALOG_STREAM_CLOSE,
       ANALOG_STREAM_START, ANALOG_STREAM_STATUS, ANALOG_STREAM_PIPELINE };
enum { ANALOG_OK, ANALOG_RANGE, ANALOG_BUSY, ANALOG_STATE, ANALOG_UNSUPPORTED };
void analog_bridge_hid(uint8_t *req, uint8_t *res);
uint8_t analog_periodic_ready(void);
uint8_t analog_periodic_check(const uint8_t *p, uint16_t len);
uint8_t analog_periodic_exec(const uint8_t *p, uint16_t len, uint8_t *data, uint8_t *n);
void analog_periodic_invalidate(void);
#endif
