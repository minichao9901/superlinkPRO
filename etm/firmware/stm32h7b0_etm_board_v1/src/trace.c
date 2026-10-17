/* RM0455 Rev 13, chapter 64: Cortex-M7 ETMv4, full SoC TPIU.
 * SWO SPPR/ACPR and Cortex-M3/M4 ETMv3 registers do not apply here.
 */
#include "main.h"
#include "trace.h"

#define CS_REG(base, offset) (*(volatile uint32_t *)((base) + (offset)))
#define ETM_BASE       0xE0041000u
#define FUNNEL_BASE    0x5C013000u
#define ETF_BASE       0x5C014000u
#define TPIU_BASE      0x5C015000u
#define CS_UNLOCK_KEY  0xC5ACCE55u
#define WAIT_LIMIT     100000u

volatile TraceDiagnostics g_trace;

static int wait_bits(uint32_t base, uint32_t offset, uint32_t mask, uint32_t expected)
{
    for (uint32_t i = 0; i < WAIT_LIMIT; ++i) {
        if ((CS_REG(base, offset) & mask) == expected) { return 1; }
    }
    return 0;
}

static int unlock(uint32_t base)
{
    CS_REG(base, 0xFB0u) = CS_UNLOCK_KEY;
    __DSB();
    return (CS_REG(base, 0xFB4u) & 2u) == 0u;
}

static void snapshot(void)
{
    g_trace.dbgmcu_idcode = DBGMCU->IDCODE;
    g_trace.dbgmcu_cr = DBGMCU->CR;
    for (uint32_t i = 0; i < 6u; ++i) {
        g_trace.etm_idr[i] = CS_REG(ETM_BASE, 0x1E0u + i * 4u);
    }
    g_trace.etm_auth = CS_REG(ETM_BASE, 0xFB8u);
    g_trace.etm_status = CS_REG(ETM_BASE, 0x00Cu);
    g_trace.etm_power = CS_REG(ETM_BASE, 0x314u);
    g_trace.etm_config = CS_REG(ETM_BASE, 0x010u);
    g_trace.etm_traceid = CS_REG(ETM_BASE, 0x040u);
    g_trace.etm_victl = CS_REG(ETM_BASE, 0x080u);
    g_trace.etm_syncp = CS_REG(ETM_BASE, 0x034u);
    g_trace.etm_prgctl = CS_REG(ETM_BASE, 0x004u);
    g_trace.funnel_ctrl = CS_REG(FUNNEL_BASE, 0x000u);
    g_trace.etf_ctrl = CS_REG(ETF_BASE, 0x020u);
    g_trace.etf_mode = CS_REG(ETF_BASE, 0x028u);
    g_trace.etf_status = CS_REG(ETF_BASE, 0x00Cu);
    g_trace.etf_ffcr = CS_REG(ETF_BASE, 0x304u);
    g_trace.tpiu_port_size = CS_REG(TPIU_BASE, 0x004u);
    g_trace.tpiu_ffcr = CS_REG(TPIU_BASE, 0x304u);
    g_trace.tpiu_ffsr = CS_REG(TPIU_BASE, 0x300u);
    g_trace.gpioe_moder = GPIOE->MODER;
    g_trace.gpioe_afrl = GPIOE->AFR[0];
}

static int fail(uint32_t error)
{
    g_trace.error = error;
    snapshot();
    return 0;
}

int trace_init(void)
{
    GPIO_InitTypeDef pins = {0};
    g_trace.magic = 0x45544D34u;
    g_trace.nominal_traceclk_hz = 4000000u;
    g_trace.stage = 1u;
    CoreDebug->DEMCR |= CoreDebug_DEMCR_TRCENA_Msk;
    DBGMCU->CR |= DBGMCU_CR_DBG_TRACECKEN | DBGMCU_CR_DBG_CKCDEN |
        DBGMCU_CR_DBG_CKSRDEN;
    __DSB();
    __ISB();
    __HAL_RCC_GPIOE_CLK_ENABLE();
    pins.Pin = GPIO_PIN_2 | GPIO_PIN_3 | GPIO_PIN_4 | GPIO_PIN_5 | GPIO_PIN_6;
    pins.Mode = GPIO_MODE_AF_PP;
    pins.Pull = GPIO_NOPULL;
    pins.Speed = GPIO_SPEED_FREQ_VERY_HIGH;
    pins.Alternate = GPIO_AF0_TRACE;
    HAL_GPIO_Init(GPIOE, &pins);

    if (!unlock(ETM_BASE) || !unlock(FUNNEL_BASE) ||
        !unlock(ETF_BASE) || !unlock(TPIU_BASE)) { return fail(1u); }
    ITM->LAR = CS_UNLOCK_KEY;
    ITM->TER = 0u;
    ITM->TCR = 0u;
    CS_REG(ETM_BASE, 0x310u) = 8u;       /* Keep ETM powered. */
    if (!wait_bits(ETM_BASE, 0x314u, 1u, 1u)) { return fail(2u); }
    CS_REG(ETM_BASE, 0x004u) = 0u;       /* Disable before programming. */
    __DSB();
    __ISB();
    if (!wait_bits(ETM_BASE, 0x00Cu, 1u, 1u)) { return fail(3u); }
    g_trace.stage = 2u;

    if ((CS_REG(TPIU_BASE, 0x300u) & 2u) == 0u) {
        CS_REG(TPIU_BASE, 0x304u) |= (1u << 12) | (1u << 6);
        if (!wait_bits(TPIU_BASE, 0x300u, 2u, 2u)) { return fail(4u); }
    }
    CS_REG(ETF_BASE, 0x020u) = 0u;
    if (!wait_bits(ETF_BASE, 0x00Cu, 4u, 4u)) { return fail(5u); }
    CS_REG(FUNNEL_BASE, 0x000u) = 0x301u;
    CS_REG(ETF_BASE, 0x028u) = 2u;
    CS_REG(ETF_BASE, 0x304u) = 1u;
    CS_REG(TPIU_BASE, 0x004u) = 8u;
    CS_REG(TPIU_BASE, 0x400u) = 0x100u;
    CS_REG(TPIU_BASE, 0x304u) = 3u;
    CS_REG(ETF_BASE, 0x020u) = 1u;
    g_trace.stage = 3u;

    if (((CS_REG(ETM_BASE, 0x1E4u) >> 8) & 0xFu) != 4u) { return fail(6u); }
    CS_REG(ETM_BASE, 0x010u) = 0u; /* No return stack/cycles/timestamps/data. */
    CS_REG(ETM_BASE, 0x020u) = 0u;
    CS_REG(ETM_BASE, 0x024u) = 0u;
    CS_REG(ETM_BASE, 0x02Cu) = 0u; /* CPU stalling disabled. */
    CS_REG(ETM_BASE, 0x040u) = 2u; /* Non-zero trace source ID. */
    CS_REG(ETM_BASE, 0x088u) = 0u;
    CS_REG(ETM_BASE, 0x08Cu) = 0u;
    /* Resource 1 = always true, SSSTATUS = started. No comparators.
     * Cortex-M7 OS Lock is unimplemented; SYNCP is fixed/read-only (1024 B).
     */
    CS_REG(ETM_BASE, 0x080u) = (1u << 9) | 1u;
    __DSB();
    __ISB();
    CS_REG(ETM_BASE, 0x004u) = 1u;
    __DSB();
    __ISB();
    if (!wait_bits(ETM_BASE, 0x00Cu, 2u, 0u)) { return fail(7u); }
    snapshot();
    if (g_trace.etm_traceid != 2u || g_trace.etm_victl != 0x201u ||
        g_trace.etm_prgctl != 1u || g_trace.tpiu_port_size != 8u ||
        g_trace.funnel_ctrl != 0x301u || g_trace.etf_mode != 2u ||
        g_trace.etf_ctrl != 1u || g_trace.tpiu_ffcr != 3u) { return fail(8u); }
    g_trace.stage = 4u;
    return 1;
}
