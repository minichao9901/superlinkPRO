/* SPDX-License-Identifier: Apache-2.0 */
#ifndef ARM_JTAG_MEM_H
#define ARM_JTAG_MEM_H

#include <stdint.h>

int arm_jtag_mem_open(uint32_t clock_hz);
int arm_jtag_mem_read(uint32_t address, uint8_t *data, uint32_t bytes);
int arm_jtag_mem_write_word(uint32_t address, uint32_t value);
void arm_jtag_mem_invalidate(void);

#endif
