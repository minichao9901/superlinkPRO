/* 启动文件（C 写，避免再引 .s）：向量表 + FPU 使能 + .data/.bss + 跳 main */
#include <stdint.h>

extern uint32_t _estack;
extern uint32_t _sidata, _sdata, _edata, _sbss, _ebss;

int main(void);
void SysTick_Handler(void);

void Reset_Handler(void);
void Default_Handler(void);

void NMI_Handler(void)        __attribute__((weak, alias("Default_Handler")));
void HardFault_Handler(void)  __attribute__((weak, alias("Default_Handler")));
void SVC_Handler(void)        __attribute__((weak, alias("Default_Handler")));
void PendSV_Handler(void)     __attribute__((weak, alias("Default_Handler")));
void SysTick_Handler(void)    __attribute__((weak, alias("Default_Handler")));

__attribute__((section(".isr_vector"), used))
void (*const g_vectors[])(void) = {
  (void (*)(void))&_estack,       /* 0x00 初始栈顶 */
  Reset_Handler,                  /* 0x04 */
  NMI_Handler,                    /* 0x08 */
  HardFault_Handler,              /* 0x0C */
  0, 0, 0, 0, 0, 0, 0,             /* 保留 */
  SVC_Handler,                    /* 0x2C */
  0, 0,                           /* 保留 */
  PendSV_Handler,                 /* 0x38 */
  SysTick_Handler,                /* 0x3C */
};

void Reset_Handler(void){
  /* H7 是 Cortex-M7：用硬浮点编译就要先开 CP10/CP11，否则一碰 FPU 就 UsageFault */
  *(volatile uint32_t *)0xE000ED88 |= (0xFu << 20);

  uint32_t *src = &_sidata, *dst = &_sdata;
  while (dst < &_edata) *dst++ = *src++;
  for (dst = &_sbss; dst < &_ebss; ) *dst++ = 0;

  main();
  for (;;) { }
}

void Default_Handler(void){
  for (;;) { }                    /* 留在这里等调试器接住 */
}
