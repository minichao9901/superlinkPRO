/* SPDX-License-Identifier: Apache-2.0 */
#include <string.h>
#include "DAP_config.h"
#include "DAP.h"
#include "debug_cm.h"
#include "JTAG_DP.h"
#include "arm_jtag_mem.h"

#define H743_CPU_TAP 0x0ba00477U
#define H743_BOUNDARY_TAP 0x06450041U
#define TAP_ID_MASK 0x0fffffffU
#define AP_PAGE_SIZE 1024U
#define BLOCK_WORDS 256U
#define DP_ERROR_FLAGS (STICKYORUN | STICKYCMP | STICKYERR)
#define AP_CSW_WORDS (CSW_RESERVED | CSW_MSTRDBG | CSW_HPROT | CSW_DBGSTAT | CSW_SADDRINC | CSW_SIZE32)

static uint8_t ready;
static uint8_t block_response[4U + BLOCK_WORDS * sizeof(uint32_t)];

static void put_word(uint8_t *p, uint32_t value)
{
    p[0] = (uint8_t)value;
    p[1] = (uint8_t)(value >> 8);
    p[2] = (uint8_t)(value >> 16);
    p[3] = (uint8_t)(value >> 24);
}

static uint32_t get_word(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
           ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

/* Reuse the CMSIS-DAP JTAG engine: it handles the posted JTAG DP/AP
 * responses, bypass bits and bounded WAIT retries for both ports. */
static int write_register(uint8_t request, uint32_t value)
{
    uint8_t packet[8] = { ID_DAP_Transfer, 0, 1, request };
    uint8_t response[8];
    put_word(packet + 4, value);
    DAP_ExecuteCommand(packet, response);
    return response[1] == 1U && response[2] == DAP_TRANSFER_OK ? 0 : -1;
}

static int read_register(uint8_t request, uint32_t *value)
{
    const uint8_t packet[] = { ID_DAP_Transfer, 0, 1, request | DAP_TRANSFER_RnW };
    uint8_t response[8];
    DAP_ExecuteCommand(packet, response);
    if (response[1] != 1U || response[2] != DAP_TRANSFER_OK) { return -1; }
    *value = get_word(response + 3);
    return 0;
}

static int check_status(void)
{
    uint32_t status;
    if (read_register(DP_CTRL_STAT, &status) || (status & DP_ERROR_FLAGS) != 0U)
    {
        ready = 0U;
        return -1;
    }
    return 0;
}

static int select_h743_chain(void)
{
    const uint8_t high[8] = { 255, 255, 255, 255, 255, 255, 255, 255 };
    const uint8_t low = 0;
    uint8_t ids[8];
    JTAG_Sequence_Slow(0, 0x46, high, ids); /* Test-Logic-Reset */
    JTAG_Sequence_Slow(0, 1, &low, ids);    /* Run-Test/Idle */
    JTAG_Sequence_Slow(0, 0x41, high, ids);
    JTAG_Sequence_Slow(0, 2, &low, ids);    /* Shift-DR */
    JTAG_Sequence_Slow(0, 0x80, high, ids); /* Default IDCODEs, LSB first */
    JTAG_Sequence_Slow(0, 0x42, high, ids + 4);
    JTAG_Sequence_Slow(0, 1, &low, ids + 4);
    if ((get_word(ids) & TAP_ID_MASK) != H743_CPU_TAP ||
        (get_word(ids + 4) & TAP_ID_MASK) != H743_BOUNDARY_TAP)
    {
        return -1;
    }
    const uint8_t config[] = { ID_DAP_JTAG_Configure, 2, 4, 5 };
    DAP_ExecuteCommand(config, ids);
    return ids[1] == DAP_OK ? 0 : -1;
}

int arm_jtag_mem_open(uint32_t clock_hz)
{
    const uint8_t connect[] = { ID_DAP_Connect, DAP_PORT_JTAG };
    const uint8_t settings[] = { ID_DAP_TransferConfigure, 0, 64, 0, 0, 0 };
    const uint8_t swj[] = { ID_DAP_SWJ_Sequence, 88, 255, 255, 255, 255, 255,
                            255, 255, 255, 0x3c, 0xe7, 0x1f };
    uint8_t clock[5] = { ID_DAP_SWJ_Clock };
    uint8_t response[8];
    ready = 0U;
    DAP_ExecuteCommand(connect, response);
    if (response[1] != DAP_PORT_JTAG) { return -2; }
    put_word(clock + 1, clock_hz);
    DAP_ExecuteCommand(clock, response);
    if (response[1] != DAP_OK) { return -2; }
    DAP_ExecuteCommand(swj, response);
    /* Allow extra turnaround on AP transactions when the H7 is running.
     * The SWJ clock curve is calibrated for SWD, not the JTAG GPIO path. */
    if (DAP_Data.clock_delay < 6U) { DAP_Data.clock_delay = 6U; }
    if (response[1] != DAP_OK || select_h743_chain() != 0) { return -2; }
    DAP_ExecuteCommand(settings, response);
    if (write_register(DP_SELECT, 0U) ||
        write_register(DP_CTRL_STAT, CDBGPWRUPREQ | CSYSPWRUPREQ | DP_ERROR_FLAGS))
    {
        return -4;
    }
    for (unsigned i = 0; i < 128U; i++)
    {
        uint32_t status;
        if (read_register(DP_CTRL_STAT, &status)) { return -4; }
        if ((status & (CDBGPWRUPACK | CSYSPWRUPACK)) == (CDBGPWRUPACK | CSYSPWRUPACK))
        {
            if (write_register(DAP_TRANSFER_APnDP | AP_CSW, AP_CSW_WORDS) || check_status()) { return -4; }
            ready = 1U;
            return 0;
        }
    }
    return -4;
}

static int read_words(uint32_t address, uint32_t count)
{
    const uint8_t packet[] = { ID_DAP_TransferBlock, 0, (uint8_t)count,
        (uint8_t)(count >> 8), DAP_TRANSFER_APnDP | AP_DRW | DAP_TRANSFER_RnW };
    if (write_register(DAP_TRANSFER_APnDP | AP_TAR, address)) { return -1; }
    DAP_ExecuteCommand(packet, block_response);
    if (block_response[1] != (uint8_t)count || block_response[2] != (uint8_t)(count >> 8) ||
        block_response[3] != DAP_TRANSFER_OK)
    {
        return -1;
    }
    return check_status();
}

int arm_jtag_mem_read(uint32_t address, uint8_t *data, uint32_t bytes)
{
    if (!ready || (uint64_t)address + bytes > (UINT64_C(1) << 32)) { return -1; }
    while (bytes)
    {
        uint32_t offset = address & 3U;
        uint32_t length = AP_PAGE_SIZE - (address & (AP_PAGE_SIZE - 1U));
        if (length > bytes) { length = bytes; }
        if (read_words(address - offset, (offset + length + 3U) / 4U))
        {
            ready = 0U;
            return -1;
        }
        memcpy(data, block_response + 4U + offset, length);
        address += length;
        data += length;
        bytes -= length;
    }
    return 0;
}

int arm_jtag_mem_write_word(uint32_t address, uint32_t value)
{
    if (!ready || (address & 3U) != 0U) { return -1; }
    uint32_t observed;
    if (write_register(DAP_TRANSFER_APnDP | AP_TAR, address) ||
        write_register(DAP_TRANSFER_APnDP | AP_DRW, value) ||
        write_register(DAP_TRANSFER_APnDP | AP_TAR, address) ||
        read_register(DAP_TRANSFER_APnDP | AP_DRW, &observed) ||
        observed != value || check_status())
    {
        ready = 0U;
        return -1;
    }
    return 0;
}

void arm_jtag_mem_invalidate(void)
{
    ready = 0U;
}
