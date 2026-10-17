/*
 * STM32H743 · SEGGER RTT **吞吐测试固件**（与 stm32f103_rtt_speed 同一套量法）
 *
 *   while(1) 里死循环发 "hello world!\n"，不加任何延时。
 *   RTT 用 BLOCK_IF_FIFO_FULL：缓冲满就阻塞 —— 目标写多快完全由主机取多快决定，
 *   主机读到的字节/秒就是 RTT 的实际吞吐。
 *
 * H743 与 F103 版的差别：
 *   1. RTT 控制块/环形缓冲必须在 **AXI SRAM(0x24000000)**（见 ld 脚本的说明：
 *      DTCM 走 AHB-AP 读不到，探针够不着）；
 *   2. **上电就把主频顶到 480 MHz**（见下面 clock_init_480mhz()；之前只跑复位默认的
 *      HSI 64 MHz，导致和 F103@96MHz 的对比一直不公平）；
 *   3. 关着 D-Cache（DCache 会让探针读到过期的环形缓冲，也会让目标读到过期的 RdOff），
 *      I-Cache 开着帮循环取指。
 */
#include <stdint.h>

#include "SEGGER_RTT.h"

#define FLASH_ACR   (*(volatile uint32_t *)0x52002000) /* H7: FLASH 在 0x52002000 */
#define SCB_CCR     (*(volatile uint32_t *)0xE000ED14)
#define SCB_ICIALLU (*(volatile uint32_t *)0xE000EF50)
#define SYST_CSR    (*(volatile uint32_t *)0xE000E010)
#define SYST_RVR    (*(volatile uint32_t *)0xE000E014)
#define SYST_CVR    (*(volatile uint32_t *)0xE000E018)

/* ---------------------------------------------------------------------------
 * 480 MHz 时钟初始化 —— **照抄厂商例程的序列，只把 PLLN 顶到 H7 的上限**
 *
 * 来源：正点原子阿波罗 H743 标准例程「实验3 串口通信实验」的 SYSTEM/sys/sys.c
 *       Stm32_Clock_Init(160, 5, 2, 2)：HSE 25MHz + PLLN=160/PLLM=5/PLLP=2/PLLQ=2
 *       ⇒ Fvco = 25×(160/5) = 800MHz ⇒ SYSCLK = 800/2 = 400MHz（厂商那份是 400）
 *       本固件把 **PLLN 从 160 提到 192**（下面 PLL1DIVR 写的 191）：
 *       ⇒ Fvco = 25×(192/5) = **960MHz** ⇒ SYSCLK = 960/2 = **480MHz**（H743 上限）
 *          AXI/HCLK=/2(**240**)  CPU(D1CPRE)=/1(**480**)  APB1/2/3=/2(120)  APB4=/4(60)
 *          PLL1VCOSEL=WIDE（192~836MHz）  PLL1RGE=RANGE_2（4~8MHz 参考）
 *       ⚠️ 采样靶子固件 `../stm32h743_scope` 的 SYSCLK 也是 480MHz，但它 D1CPRE 取 /2
 *          （CPU=AXI=240）—— 因为它要用 SysTick 做 10 kHz 时基，CPU 与 HCLK 相等才没有
 *          "SysTick 跟哪个时钟"的歧义。两者 SYSCLK 一致，差的是 CPU 分频。
 *
 * 三处必须照抄的点（自己猜必踩，实测过）：
 *   ① **PWR_CR3.SCUEN(bit2) 必须先清 0** —— 否则 PWR_D3CR.VOS 的写被硬件静默忽略；
 *      ⚠️ PWR_CR3 的偏移是 **0x0C**（0x08 是 CR2）—— 兄弟目录 stm32h743_scope 就是在这里
 *      写错偏移，结果 SCUEN 清不掉、VOSRDY 永不置位、整档降级到 HSI 64MHz。
 *   ② 本型号 PLLCKSELR 是 `PLLSRC[1:0]` / `DIVM1[9:4]`（不是 [23:22]/[5:0]），
 *      且 **DIVM1 写分频值本身**（只有 N1/P1/Q1/R1 是"值-1"）；
 *   ③ 升频**之前**先把 flash 等待周期加上（这里给 4 WS + WRHIGHFREQ=10b，
 *      比厂商的 FLASH_LATENCY_2 保守，慢一点但安全）。
 *
 * 任何一步等不到"就绪"就整档放弃、继续跑 64MHz（绝不卡死），
 * 结果记在 g_sysclk_mhz / g_clk_err 里供主机读数诊断。
 * --------------------------------------------------------------------------- */
#define RCC_CR        (*(volatile uint32_t *)0x58024400u)
#define RCC_CFGR      (*(volatile uint32_t *)0x58024410u)
#define RCC_D1CFGR    (*(volatile uint32_t *)0x58024418u)
#define RCC_D2CFGR    (*(volatile uint32_t *)0x5802441Cu)
#define RCC_D3CFGR    (*(volatile uint32_t *)0x58024420u)
#define RCC_PLLCKSELR (*(volatile uint32_t *)0x58024428u)
#define RCC_PLLCFGR   (*(volatile uint32_t *)0x5802442Cu)
#define RCC_PLL1DIVR  (*(volatile uint32_t *)0x58024430u)
#define PWR_CR3       (*(volatile uint32_t *)0x5802480Cu)
#define PWR_D3CR      (*(volatile uint32_t *)0x58024818u)  /* ⚠️ 偏移 0x18，不是 0x10
                                                            * （0x10 是 PWR_CPUCR，写进去
                                                            * 读 CPUCR 的 bit13 永远等不到
                                                            * VOSRDY —— 踩过）*/

volatile uint32_t g_bytes;
volatile uint32_t g_loops;
volatile uint32_t g_ms;
volatile uint32_t g_sysclk_mhz; /* 实测 SYSCLK（MHz）：480 成功 / 64 降级。
                                 * ⚠️ 旧名 g_hclk_mhz 是错的：D1CPRE=/1 时它就是 CPU 时钟，
                                 *    真正的 AXI/HCLK 是 240MHz（HPRE=/2）。SysTick 也按它算 1ms。 */
volatile uint32_t g_clk_err;    /* bit0=VOS0 未就绪 bit1=HSE 未起振 bit2=PLL1 未锁定 bit3=切换失败 */

static uint32_t clock_init_480mhz(void)
{
  uint32_t err = 0u, i;

  PWR_CR3 &= ~(1u << 2);                              /* ① SCUEN = 0 */
  PWR_D3CR = (PWR_D3CR & ~0xC000u) | 0xC000u;         /* VOS = 0b11 = Scale0 */
  for (i = 0; i < 200000u; i++) {
    if (PWR_D3CR & 0x2000u) break;                    /* VOSRDY */
  }
  if (!(PWR_D3CR & 0x2000u)) err |= 1u;

  FLASH_ACR = (FLASH_ACR & ~0x3Fu) | 0x24u;           /* ③ latency=4WS + WRHIGHFREQ=10b */

  RCC_CR |= (1u << 16);                               /* HSEON */
  for (i = 0; i < 400000u; i++) {
    if (RCC_CR & (1u << 17)) break;                   /* HSERDY */
  }
  if (!(RCC_CR & (1u << 17))) err |= 2u;

  if (err == 0u) {
    RCC_PLLCKSELR = (2u << 0) | (5u << 4);            /* ② PLLSRC=HSE, DIVM1=5 */
    RCC_PLL1DIVR  = 191u | (1u << 9) | (1u << 16) | (1u << 24);  /* N=192 P=2 Q=2 R=2 → VCO 960 → 480MHz */
    RCC_PLLCFGR   = (1u << 16) | (1u << 17) | (1u << 18) | 0x8u; /* DIVP/Q/R1EN, RGE=4~8MHz */
    RCC_CR |= (1u << 24);                             /* PLL1ON */
    for (i = 0; i < 400000u; i++) {
      if (RCC_CR & (1u << 25)) break;                 /* PLL1RDY */
    }
    if (!(RCC_CR & (1u << 25))) err |= 4u;
  }

  if (err == 0u) {
    RCC_D1CFGR = 0x48u;                               /* AXI/HCLK /2 = 240MHz, APB3 /2 = 120MHz,
                                                       * D1CPRE = /1 → CPU 480MHz
                                                       * （字段序：HPRE[3:0] D1PPRE[6:4] D1CPRE[11:8]） */
    RCC_D2CFGR = 0x440u;                              /* APB1 /2 = 120MHz, APB2 /2 = 120MHz */
    RCC_D3CFGR = 0x50u;                               /* APB4 /4 = 60MHz */
    RCC_CFGR = (RCC_CFGR & ~7u) | 3u;                 /* SW = PLL1 */
    for (i = 0; i < 200000u; i++) {
      if ((RCC_CFGR & 0x18u) == 0x18u) break;         /* SWS = PLL1 */
    }
    if ((RCC_CFGR & 0x18u) != 0x18u) err |= 8u;
  }

  g_clk_err = err;
  return (err == 0u) ? 480u : 64u;                    /* 失败就按 64MHz 继续跑 */
}

void SysTick_Handler(void){ g_ms++; }

int main(void){
  g_sysclk_mhz = clock_init_480mhz();

  SCB_ICIALLU = 0;
  SCB_CCR |= (1u << 17);                              /* I-Cache 开，D-Cache 故意不开 */

  SEGGER_RTT_Init();
  SEGGER_RTT_WriteString(0, "\r\n=== H743 RTT 吞吐测试：480MHz + BLOCK_IF_FIFO_FULL ===\r\n");

  SYST_RVR = (g_sysclk_mhz * 1000u) - 1u;             /* 1 ms（SysTick 跟 CPU 时钟：480MHz → 479999） */
  SYST_CVR = 0;
  SYST_CSR = 7;

  static const char msg[] = "hello world!\n";         /* 13 字节 */
  for (;;){
    unsigned n = SEGGER_RTT_Write(0, msg, sizeof(msg) - 1);
    g_bytes += n;
    g_loops++;
  }
}
