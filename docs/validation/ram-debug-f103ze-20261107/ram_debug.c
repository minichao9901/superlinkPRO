#include <stdint.h>

extern uint32_t _stack_top;
void Reset_Handler(void);
void Default_Handler(void);

__attribute__((used, section(".isr_vector")))
const uintptr_t vectors[76] = {
    [0] = (uintptr_t)&_stack_top,
    [1] = (uintptr_t)Reset_Handler,
    [2] = (uintptr_t)Default_Handler,
    [3] = (uintptr_t)Default_Handler,
};

volatile uint32_t marker;
volatile uint32_t seed = 40;

int main(void)
{
    marker = seed + 1;
    marker += 1;
    for (;;) {
        __asm volatile ("nop");
    }
}

void Reset_Handler(void)
{
    main();
    for (;;) {}
}

void Default_Handler(void)
{
    for (;;) {}
}
