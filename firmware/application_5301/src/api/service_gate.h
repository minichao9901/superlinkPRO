/* SPDX-License-Identifier: Apache-2.0 */
#ifndef SERVICE_GATE_H
#define SERVICE_GATE_H
#include <stdint.h>
/* Authoritative byte flags, not a mirrored pending mask. ISR/main write separate
 * bytes; the main-loop gate reads aligned words. No read-modify-write, no IRQ
 * masking and no clearing in the gate. A late event is serviced next iteration.
 * GCC supports reading a union through another member; any nonzero byte wakes.
 */
typedef union {
    volatile uint8_t flag[8];
    volatile uint32_t word[2];
} service_gate_t;
static inline uint8_t service_gate_pending(const service_gate_t *g) {
    return (g->word[0] | g->word[1]) != 0U;
}
#endif
