/*
 * 启动阶段探针（boot stage probe）—— 给"卡在哪一步"提供硬证据
 *
 * 引脚：PA1（推挽输出）。每调一次 probe_stage(n)：
 *   PA1 拉高 -> 延时约 20us（纯 NOP 循环，不依赖时钟）-> 拉低 -> 记录 g_boot_stage = n
 * 逻辑分析仪看脉冲个数 = 走过几步；SWD 读 g_boot_stage 看最后停在第几步。
 *
 * H7 与 F1 的寄存器地址不同（GPIOA 在 0x58020000，RCC 在 0x58024400），
 * 所以本文件按芯片用宏分开。这段代码不依赖 HAL、不依赖时钟配置，尽量早调用。
 */
#include <stdint.h>

#if defined(STM32H7B0xx) || defined(STM32H743xx) || defined(STM32H750xx)
/* H7：GPIOA @0x58020000，RCC @0x58024400 */
#define PROBE_GPIOA_MODER (*(volatile uint32_t *)0x58020000u)
#define PROBE_GPIOA_BSRR  (*(volatile uint32_t *)0x58020018u)
#define PROBE_RCC_AHB4ENR (*(volatile uint32_t *)0x580244E0u)
#define PROBE_GPIOA_BIT   1u
#define PROBE_IS_H7       1
#else
/* F1：GPIOA @0x40010800，RCC @0x40021000 */
#define PROBE_GPIOA_CRL   (*(volatile uint32_t *)0x40010800u)
#define PROBE_GPIOA_BSRR  (*(volatile uint32_t *)0x40010810u)
#define PROBE_GPIOA_BRR   (*(volatile uint32_t *)0x40010814u)
#define PROBE_RCC_APB2ENR (*(volatile uint32_t *)0x40021018u)
#define PROBE_GPIOA_BIT   1u
#define PROBE_IS_H7       0
#endif

/* 固定变量 + 不被优化掉：用 nm 查符号即可 */
__attribute__((used)) volatile uint32_t g_boot_stage = 0xFFFFFFFFu;
__attribute__((used)) volatile uint32_t g_boot_stage_prev = 0xFFFFFFFFu;

void probe_gpio_init(void)
{
#if PROBE_IS_H7
    PROBE_RCC_AHB4ENR |= (1u << 0); /* GPIOAEN */
    (void)PROBE_RCC_AHB4ENR;
    {
        uint32_t moder = PROBE_GPIOA_MODER;
        moder &= ~(0x3u << (PROBE_GPIOA_BIT * 2));
        moder |= (0x1u << (PROBE_GPIOA_BIT * 2)); /* 通用推挽输出 */
        PROBE_GPIOA_MODER = moder;
    }
    PROBE_GPIOA_BSRR = (1u << (PROBE_GPIOA_BIT + 16)); /* 先输出低 */
#else
    PROBE_RCC_APB2ENR |= (1u << 2); /* IOPAEN */
    (void)PROBE_RCC_APB2ENR;
    {
        uint32_t crl = PROBE_GPIOA_CRL;
        crl &= ~(0xFu << (PROBE_GPIOA_BIT * 4));
        crl |= (0x1u << (PROBE_GPIOA_BIT * 4)); /* 通用推挽输出，2MHz */
        PROBE_GPIOA_CRL = crl;
    }
    PROBE_GPIOA_BRR = (1u << PROBE_GPIOA_BIT);
#endif
}

void probe_stage(uint32_t stage)
{
    volatile uint32_t i;

    g_boot_stage_prev = g_boot_stage;
    g_boot_stage = stage;
    probe_gpio_init(); /* 幂等 */

    PROBE_GPIOA_BSRR = (1u << PROBE_GPIOA_BIT);
    for (i = 0; i < 400u; i++) {
        __asm volatile("nop");
    }
#if PROBE_IS_H7
    PROBE_GPIOA_BSRR = (1u << (PROBE_GPIOA_BIT + 16));
#else
    PROBE_GPIOA_BRR = (1u << PROBE_GPIOA_BIT);
#endif
}
