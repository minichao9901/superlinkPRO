#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>
#include <assert.h>

static uint32_t test_clock;
static struct { uint32_t debug_port, clock_delay; } DAP_Data;
#define DAP_PORT_JTAG 2U
#define DAP_PORT_DISABLED 0U
#define PORT_JTAG_SETUP() ((void)0)
#define PORT_OFF() ((void)0)
void JTAG_Sequence(uint32_t info, const uint8_t *tdi, uint8_t *tdo);
