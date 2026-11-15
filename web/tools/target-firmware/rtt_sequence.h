#ifndef SUPERLINKPRO_RTT_SEQUENCE_H
#define SUPERLINKPRO_RTT_SEQUENCE_H

#include <stdint.h>
#include "SEGGER_RTT.h"

/* 32 records per millisecond leave room for STOP drain in a 64 MiB capture. */
static inline void rtt_sequence_stream(volatile uint32_t *millis,
                                       volatile uint32_t *bytes,
                                       volatile uint32_t *records)
{
    char line[] = "seq 0000000\r\n";
    uint32_t seq = 0;
    uint32_t previous_tick = *millis;

    for (;;) {
        while (*millis == previous_tick) {}
        previous_tick = *millis;
        for (unsigned record = 0; record < 32; ++record) {
            uint32_t value = seq;
            for (unsigned digit = 0; digit < 7; ++digit) {
                line[10 - digit] = (char)('0' + value % 10);
                value /= 10;
            }
            *bytes += SEGGER_RTT_Write(0, line, sizeof(line) - 1);
            ++*records;
            if (++seq == 10000000U) {
                seq = 0;
            }
        }
    }
}

#endif
