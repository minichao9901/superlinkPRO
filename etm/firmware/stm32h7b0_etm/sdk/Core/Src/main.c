/* STM32H7B0 ETM bring-up. HAL/CMSIS retained from the board example. */
#include "main.h"
#include "trace.h"

volatile uint32_t g_round, g_result, g_systick_count, g_error, g_sysclk_hz;

/* Keep real call/return instructions and both branch outcomes in the ELF. */
__attribute__((noinline)) uint32_t trace_even(uint32_t value)
{
    return (value * 3u) + 1u;
}

__attribute__((noinline)) uint32_t trace_odd(uint32_t value)
{
    return (value ^ 0x55u) + 7u;
}

__attribute__((noinline)) uint32_t trace_workload(uint32_t round)
{
    uint32_t result = round;
    for (uint32_t i = 0; i < 8u; ++i) {
        if (((round + i) & 1u) == 0u) {
            result = trace_even(result);
        } else {
            result = trace_odd(result);
        }
    }
    return result;
}

void SystemClock_Config(void)
{
    RCC_OscInitTypeDef osc = {0};
    RCC_ClkInitTypeDef clocks = {0};
    /* Preserve the board's supply mode and voltage scale; no PWR_CR3 writes.
     * HSI/8 gives CPU/HCLK = 8 MHz and physical DDR TRACECLK = 4 MHz.
     * RM0455: RCC_CFGR.SW selects both sys_ck and traceportck.
     */
    osc.OscillatorType = RCC_OSCILLATORTYPE_HSI;
    osc.HSIState = RCC_HSI_DIV8;
    osc.HSICalibrationValue = RCC_HSICALIBRATION_DEFAULT;
    osc.PLL.PLLState = RCC_PLL_OFF;
    if (HAL_RCC_OscConfig(&osc) != HAL_OK) {
        g_error = 1u;
        Error_Handler();
    }
    clocks.ClockType = RCC_CLOCKTYPE_SYSCLK | RCC_CLOCKTYPE_HCLK |
        RCC_CLOCKTYPE_PCLK1 | RCC_CLOCKTYPE_PCLK2 |
        RCC_CLOCKTYPE_D1PCLK1 | RCC_CLOCKTYPE_D3PCLK1;
    clocks.SYSCLKSource = RCC_SYSCLKSOURCE_HSI;
    clocks.SYSCLKDivider = RCC_SYSCLK_DIV1;
    clocks.AHBCLKDivider = RCC_HCLK_DIV1;
    clocks.APB1CLKDivider = RCC_APB1_DIV1;
    clocks.APB2CLKDivider = RCC_APB2_DIV1;
    clocks.APB3CLKDivider = RCC_APB3_DIV1;
    clocks.APB4CLKDivider = RCC_APB4_DIV1;
    if (HAL_RCC_ClockConfig(&clocks, FLASH_LATENCY_7) != HAL_OK) {
        g_error = 2u;
        Error_Handler();
    }
    SystemCoreClockUpdate();
    g_sysclk_hz = SystemCoreClock;
    if (g_sysclk_hz != 8000000u) {
        g_error = 3u;
        Error_Handler();
    }
}

int main(void)
{
    /* No caches, RTT, UART or external crystal are needed. */
    SCB->VTOR = FLASH_BASE;
    __DSB();
    __ISB();
    HAL_Init();
    SystemClock_Config();
    HAL_Delay(100u);
    if (!trace_init()) {
        g_error = 0x100u | g_trace.error;
        Error_Handler();
    }
    for (;;) {
        g_result = trace_workload(g_round);
        ++g_round;
        /* Continuous traffic, with a 1 kHz SysTick exception path. */
        for (volatile uint32_t i = 0; i < 64u; ++i) {
            __NOP();
        }
    }
}

void HAL_IncTick(void)
{
    uwTick += (uint32_t)uwTickFreq;
    ++g_systick_count;
}

void Error_Handler(void)
{
    __disable_irq();
    for (;;) { __NOP(); }
}

#ifdef USE_FULL_ASSERT
void assert_failed(uint8_t *file, uint32_t line)
{
    (void)file;
    g_error = 0x80000000u | line;
    Error_Handler();
}
#endif
