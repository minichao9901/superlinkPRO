/* 启动文件（C 写，避免再引一个 .s）：向量表 + .data/.bss 初始化 + 跳 main
 *
 * Cortex-M7 与 M3 的差别只有一处要留意：**浮点单元**。本工程编译时开了
 * -mfpu=fpv5-d16 -mfloat-abi=hard（H7 的 FPU 是双精度），启动代码必须负责打开它 ——
 * 否则编译器一旦生成 FPU 指令就会跑飞。这里在进 main 前设好 CPACR 并把 FPU 上下文
 * 交给硬件（ASPEN/LSPEN）。
 */
#include <stdint.h>

extern uint32_t _estack;
extern uint32_t _sidata, _sdata, _edata, _sbss, _ebss;

int main(void);
void SysTick_Handler(void);
/* ST 的 CMSIS 系统初始化（system_stm32h7xx.c）：设向量表偏移、SystemCoreClock 等。
 * 官方 startup 一上电就调它，HAL 版本的固件必须也要调 —— 否则 HAL 拿到的时钟变量是错的。 */
void SystemInit(void);

void Reset_Handler(void);
void Default_Handler(void);

void NMI_Handler(void)        __attribute__((weak, alias("Default_Handler")));
void HardFault_Handler(void)  __attribute__((weak, alias("Default_Handler")));
void MemManage_Handler(void)  __attribute__((weak, alias("Default_Handler")));
void BusFault_Handler(void)   __attribute__((weak, alias("Default_Handler")));
void UsageFault_Handler(void) __attribute__((weak, alias("Default_Handler")));
void SVC_Handler(void)        __attribute__((weak, alias("Default_Handler")));
void DebugMon_Handler(void)   __attribute__((weak, alias("Default_Handler")));
void PendSV_Handler(void)     __attribute__((weak, alias("Default_Handler")));
void SysTick_Handler(void)    __attribute__((weak, alias("Default_Handler")));

/* H7 的外部中断在 0x40 之后；本工程一个都不开，所以向量表到 SysTick 为止就够。
 * （真要用中断时在这里往后补，或者换成 ST 的 startup 文件。） */
__attribute__((section(".isr_vector"), used))
void (*const g_vectors[])(void) = {
  (void (*)(void))&_estack,       /* 0x00 初始栈顶 */
  Reset_Handler,                  /* 0x04 */
  NMI_Handler,                    /* 0x08 */
  HardFault_Handler,              /* 0x0C */
  MemManage_Handler,              /* 0x10 */
  BusFault_Handler,               /* 0x14 */
  UsageFault_Handler,             /* 0x18 */
  0, 0, 0, 0,                     /* 保留 */
  SVC_Handler,                    /* 0x2C */
  DebugMon_Handler,               /* 0x30 */
  0,                              /* 保留 */
  PendSV_Handler,                 /* 0x38 */
  SysTick_Handler,                /* 0x3C */
};

/* CPACR：让 CPU 能用 CP10/CP11（FPU） */
#define SCB_CPACR  (*(volatile uint32_t *)0xE000ED88u)
/* FPU 的 FPCCR：ASPEN/LSPEN 置位，自动保存/恢复浮点上下文 */
#define FPU_FPCCR  (*(volatile uint32_t *)0xE000EF34u)

void Reset_Handler(void){
  uint32_t *src = &_sidata, *dst = &_sdata;
  while (dst < &_edata) *dst++ = *src++;
  for (dst = &_sbss; dst < &_ebss; ) *dst++ = 0;

  SCB_CPACR |= (0xFu << 20);                     /* CP10/CP11 全访问 */
  __asm__ volatile("dsb"); __asm__ volatile("isb");
  FPU_FPCCR |= (1u << 31) | (1u << 30);          /* ASPEN | LSPEN */

  SystemInit();                                  /* 官方 startup 也这么做（HAL 需要） */

  main();
  for (;;) { }
}

void Default_Handler(void){
  for (;;) { }                    /* 留在这里等调试器接住 */
}

/* 🚨 极简档（build.ps1 -Minimal）**不编** SDK 的 system_stm32h7xx.c，于是没人提供 SystemInit，
 *    而上面的 Reset_Handler 要调它 —— 原来这里会 `undefined reference to 'SystemInit'`，链不过。
 *    给个**弱定义**兜底：HAL/SDK 版里有强定义，链接器优先用它，这份会被 --gc-sections 丢掉。*/
__attribute__((weak)) void SystemInit(void) { }
