/* 启动文件（用 C 写，避免再引一个 .s）：向量表 + .data/.bss 初始化 + 跳 main
 *
 * 与 H743 那份的差别只有一处：**这里没有 FPU**。Cortex-M3 没有浮点单元，
 * 所以不需要 CPACR/FPCCR 那两行（H7 上少写就会在第一条 FPU 指令上 UsageFault，
 * 现场看着像"复位后一动不动"）。
 *
 * ⚠️ .data/.bss 在 SRAM(0x20000000)（见 ld 脚本）。F103 只有这一块 RAM，
 *    探针走 AHB-AP 读得到（这正是"被采样的变量能被读出来"的前提）。
 */
#include <stdint.h>

extern uint32_t _estack;
extern uint32_t _sidata, _sdata, _edata, _sbss, _ebss;

int main(void);
void SysTick_Handler(void);

void Reset_Handler(void);
void Default_Handler(void);

void NMI_Handler(void)         __attribute__((weak, alias("Default_Handler")));
void HardFault_Handler(void)   __attribute__((weak, alias("Default_Handler")));
void MemManage_Handler(void)   __attribute__((weak, alias("Default_Handler")));
void BusFault_Handler(void)    __attribute__((weak, alias("Default_Handler")));
void UsageFault_Handler(void)  __attribute__((weak, alias("Default_Handler")));
void SVC_Handler(void)         __attribute__((weak, alias("Default_Handler")));
void DebugMon_Handler(void)    __attribute__((weak, alias("Default_Handler")));
void PendSV_Handler(void)      __attribute__((weak, alias("Default_Handler")));
void SysTick_Handler(void)     __attribute__((weak, alias("Default_Handler")));

/* 外部中断在 0x40 之后；本工程一个都不开，所以向量表到 SysTick 为止就够。 */
__attribute__((section(".isr_vector"), used))
void (*const g_vectors[])(void) = {
  (void (*)(void))&_estack,       /* 0x00 初始栈顶（SRAM 顶） */
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

void Reset_Handler(void){
  /* .data 从 flash 拷到 SRAM、.bss 清零 */
  uint32_t *src = &_sidata, *dst = &_sdata;
  while (dst < &_edata) { *dst++ = *src++; }
  for (dst = &_sbss; dst < &_ebss; ) { *dst++ = 0; }

  main();
  for (;;) { }
}

void Default_Handler(void){
  for (;;) { }                    /* 留在这里等调试器接住 */
}
