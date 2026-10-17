/*
 * STM32F103 · SEGGER RTT **吞吐测试固件**
 *
 *   while(1) 里死循环发 "hello world!\n"，**不加任何延时**、不碰串口。
 *   RTT 用 BLOCK_IF_FIFO_FULL（见 segger_rtt/SEGGER_RTT_Conf.h）：缓冲满就阻塞 ——
 *   于是目标写多快**完全由主机取多快决定**，主机读到的字节/秒就是 RTT 的实际吞吐。
 *   这正是"拿同一块板子对比 WebUSB 与 OpenOCD 两条主机通路"的干净做法。
 *
 * 三个全局量留给主机读（交叉验证用）：
 *   g_bytes —— 目标实际写出去的字节数（阻塞模式下应当 ≈ 主机读到的字节数）
 *   g_loops —— 循环次数（×13 = g_bytes）
 *   g_ms    —— SysTick 毫秒数（**判断目标是否还活着**：阻塞在 RTT 写里时它照样在走，
 *              所以它不涨才是真卡死，而不是"在等主机读"）
 */
#include <stdint.h>

#include "stm32f103_regs.h"
#include "SEGGER_RTT.h"

/* ---------------------------------------------------------------------------
 * 目标主频
 *
 * 板上晶振实测 8.005 MHz，F103 的 PLL 倍频上限是 **×16** ⇒ 这块板子的物理上限
 * 就是 128 MHz。想要 144 MHz 必须换晶振（9 MHz ×16 或 12 MHz ×12）或从 OSC_IN
 * 灌外部时钟 —— ×18 这一档 F103 的 PLL 不存在。
 *
 * 这里默认 96 MHz（= HSE ×12）。F103 额定上限是 72 MHz，96 MHz 属于超频，但
 * flash 余量还够：latency 最多 2（3 周期），96 MHz 下 = 31 ns，而 F103 flash 实际
 * 需要 ~40 ns —— 仍在超，但比 128 MHz（23 ns）安全得多。实测 96 MHz 下固件自切
 * 不会硬故障、RTT 生产者跑到 ~3.3 MB/s（72 MHz 时 2.5 MB/s）。
 *
 * 再往上到 128 MHz（HSE ×16）就不行了：实测固件自己在开头切过去会立刻硬故障
 * （HFSR=FORCED、CFSR=IACCVIOL|STKERR，即取指出错跳飞）。128 MHz 只能在 halted
 * 状态下由上位机切换，让固件从复位向量就以该频率启动，紧凑循环靠预取缓冲勉强跑；
 * 想稳跑 128 MHz 应把热代码搬到 SRAM 执行。
 *
 * 超频三件套缺一不可：
 *   1. FLASH_ACR 先给足等待周期 + 预取；
 *   2. APB1 ≤ 36 MHz、APB2 ≤ 72 MHz；
 *   3. 换频必须走完整时序：SW→HSI、关 PLL、改倍频、开 PLL、SW→PLL ——
 *      PLL 的配置位在 PLLON=1 时是写保护的，直接改无效（最容易踩的一条）。
 * --------------------------------------------------------------------------- */
#define TARGET_HCLK_MHZ 96

#define FLASH_ACR (*(volatile uint32_t *)0x40022000)
#define RCC_CR_HSEON (1u << 16)
#define RCC_CR_HSERDY (1u << 17)
#define RCC_CR_PLLON (1u << 24)
#define RCC_CR_PLLRDY (1u << 25)
#define RCC_CFGR_SW_HSI (0u)
#define RCC_CFGR_SW_PLL (2u)
#define RCC_CFGR_SWS_MASK (3u << 2)

/* HSE 8 MHz ×16 = 128 MHz；别的晶振按 HSE_MHZ*倍频 = TARGET_HCLK_MHZ 改倍频值 */
#define HSE_MHZ 8u
#define PLL_MULL ((TARGET_HCLK_MHZ / HSE_MHZ) - 2u) /* PLLMULL 编码：0 == ×2 */

static void clock_init(void)
{
  /* 1) 开 HSE 并等起振（起不来就退回 HSI/2×16 = 64 MHz，不会把板子跑死） */
  RCC_CR |= RCC_CR_HSEON;
  for (volatile uint32_t i = 0; i < 200000u; i++) {
    if (RCC_CR & RCC_CR_HSERDY) break;
  }
  if (!(RCC_CR & RCC_CR_HSERDY)) {
    RCC_CFGR = (RCC_CFGR & ~(0xFu | (7u << 8) | (7u << 11) | (3u << 16) | (0xFu << 18)))
             | (0x5u << 8) | (0x4u << 11) | (0u << 16) | (0xEu << 18); /* HSI/2 ×16 */
    RCC_CR |= RCC_CR_PLLON;
    while (!(RCC_CR & RCC_CR_PLLRDY)) { }
    RCC_CFGR = (RCC_CFGR & ~3u) | RCC_CFGR_SW_PLL;
    while ((RCC_CFGR & RCC_CFGR_SWS_MASK) != (RCC_CFGR_SW_PLL << 2)) { }
    return;
  }

  /* 2) Flash 等待周期 + 预取 —— 必须在提速之前 */
  FLASH_ACR = 0x12; /* latency 2 + prefetch */

  /* 3) 系统时钟先切回 HSI，并关掉 PLL，否则下面的倍频位写不进去 */
  RCC_CFGR = (RCC_CFGR & ~3u) | RCC_CFGR_SW_HSI;
  while ((RCC_CFGR & RCC_CFGR_SWS_MASK) != (RCC_CFGR_SW_HSI << 2)) { }
  RCC_CR &= ~RCC_CR_PLLON;
  while (RCC_CR & RCC_CR_PLLRDY) { }

  /* 4) 分频与倍频：AHB=/1, APB1=/4 (32 MHz), APB2=/2 (64 MHz), PLLSRC=HSE, ×N */
  RCC_CFGR = (RCC_CFGR & ~(0xFu | (7u << 8) | (7u << 11) | (3u << 16) | (0xFu << 18)))
           | (0x0u << 4)              /* HPRE  = /1 */
           | (0x5u << 8)              /* PPRE1 = /4 */
           | (0x4u << 11)             /* PPRE2 = /2 */
           | (1u << 16)               /* PLLSRC = HSE */
           | ((uint32_t)PLL_MULL << 18);

  /* 5) 开 PLL、等锁定、切过去 */
  RCC_CR |= RCC_CR_PLLON;
  while (!(RCC_CR & RCC_CR_PLLRDY)) { }
  RCC_CFGR = (RCC_CFGR & ~3u) | RCC_CFGR_SW_PLL;
  while ((RCC_CFGR & RCC_CFGR_SWS_MASK) != (RCC_CFGR_SW_PLL << 2)) { }
}

volatile uint32_t g_bytes;
volatile uint32_t g_loops;
volatile uint32_t g_ms;

void SysTick_Handler(void){ g_ms++; }

int main(void){
  clock_init();      /* 先把主频顶上去，再开外设、再初始化 RTT */

  /* 只开时钟，不碰任何外设：这条通路越干净，量出来的数字越可信 */
  RCC_APB2ENR |= RCC_APB2ENR_AFIOEN | RCC_APB2ENR_IOPAEN | RCC_APB2ENR_IOPCEN;
  SYST_RVR = (TARGET_HCLK_MHZ * 1000u) - 1u;   /* 1 ms 一跳（CLKSOURCE=1 用 HCLK） */
  SYST_CVR = 0;
  SYST_CSR = 7;

  SEGGER_RTT_Init();
  SEGGER_RTT_WriteString(0, "\r\n=== RTT 吞吐测试：BLOCK_IF_FIFO_FULL，死循环发 hello world ===\r\n");

  static const char msg[] = "hello world!\n";          /* 13 字节 */
  for (;;){
    unsigned n = SEGGER_RTT_Write(0, msg, sizeof(msg) - 1);
    g_bytes += n;                                      /* 阻塞模式下 n 恒等于 13 */
    g_loops++;
  }
}
