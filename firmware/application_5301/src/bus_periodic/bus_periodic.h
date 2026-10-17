/* SPDX-License-Identifier: Apache-2.0 */
#ifndef BUS_PERIODIC_H
#define BUS_PERIODIC_H
#include <stdint.h>
#define BP_CMD 0x37U
#define BP_WAKE 5U /* Existing i2c_bridge_req_kind, no new main-loop poll. */
#define BP_TIMER_CHANNEL 1U
#define BP_JOBS 8U
#define BP_PROGRAM 512U
#define BP_STEPS 16U
#define BP_DATA 54U
#define BP_QUEUE 32U
#define BP_I2C 1U
#define BP_SPI 2U
#define BP_DELAY 3U
#define BP_ADC 4U
enum { BP_CAPS, BP_CLEAR, BP_PUT, BP_START, BP_STOP, BP_STATUS, BP_READ, BP_ACK, BP_RUN };
enum { BP_OK, BP_RANGE, BP_BUSY, BP_STATE, BP_EMPTY, BP_OVERFLOW };
/* Program records: kind:u8, flags:u8 (zero), length:u16 LE, bytes.
 * I2C: existing XFER parameter bytes. SPI: existing complete frame.
 * DELAY: u32 microseconds; nonblocking, maximum 60 seconds. */
void bus_periodic_hid(uint8_t *req, uint8_t *res);
void bus_periodic_poll(void);
void bus_periodic_irq(void);
void bus_periodic_reset(void);
uint8_t bus_periodic_owns(uint8_t bus);
void bus_periodic_wake(void);
/* Platform adapter; timer is GPTMR1 channel 1, shared IRQ with LED channel 0. */
uint64_t bp_now(void); /* 24 MHz MCHTMR */
uint32_t bp_lock(void);
void bp_unlock(uint32_t level);
uint8_t bp_timer_arm(uint64_t ticks); /* nonzero: invalid clock/configuration */
void bp_timer_stop(void);
#endif
