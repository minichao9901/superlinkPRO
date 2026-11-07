#include "pico/stdlib.h"
#include "SEGGER_RTT.h"

volatile uint32_t g_ticks;
volatile uint32_t g_mix;
volatile uint32_t g_flood;
volatile uint32_t g_bench[65536 / sizeof(uint32_t)];

__attribute__((noinline)) uint32_t fixture_leaf(uint32_t n) {
    uint32_t mixed = n ^ 0x23502350u;
    mixed = (mixed << 5) | (mixed >> 27);
    g_mix = mixed;
    return mixed;
}

__attribute__((noinline)) void fixture_cycle(void) {
    uint32_t next = g_ticks + 1u;
    fixture_leaf(next);
    g_ticks = next;
}

int main(void) {
    static const char hello[] = "RP2350-UP\n";
    static const char flood[] = "RP2350-FLOOD\n";
    SEGGER_RTT_Init();
    SEGGER_RTT_SetFlagsUpBuffer(0, SEGGER_RTT_MODE_NO_BLOCK_SKIP);
    g_bench[0] = 0x2350be11u;
    for (;;) {
        char down[64];
        fixture_cycle();
        if (g_flood) {
            SEGGER_RTT_Write(0, flood, sizeof(flood) - 1);
            continue;
        }
        if (g_ticks % 20u == 1u) {
            SEGGER_RTT_Write(0, hello, sizeof(hello) - 1);
        }
        unsigned n = SEGGER_RTT_Read(0, down, sizeof(down));
        if (n) {
            SEGGER_RTT_WriteString(0, "RP2350-ECHO:");
            SEGGER_RTT_Write(0, down, n);
            SEGGER_RTT_WriteString(0, ":END\n");
        }
        sleep_ms(1);
    }
}
