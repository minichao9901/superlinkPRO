/*
 * STM32H7B0 · **J-Scope 波形页（探针侧 HSS 采样）专用测试固件**
 *
 * 目的与 stm32f103_scope **完全同一套**：把"采样率对不对、有没有混叠、丢了多少、有没有撕裂读"
 * 从"看着像"变成**可客观判定** —— 每个被采样的量都有精确已知的数学波形，
 * 主机按取到的 `i_tick` 就能反算其余通道的应有值，从而逐点断言。
 *
 * 与 F103 那份的差别（**只有这三条**，变量契约一字不改）：
 *   ① 时钟：HSE 25MHz → PLL1 → **SYSCLK/HCLK 280MHz**（VOS0 + FLASH 7 等待；见 clock_init()）。
 *      数值与板子自带 SDK 的 SystemClock_Config() 一致（那份在同一块板上实测跑到 280MHz）。
 *   ② **所有被采样的量都在 AXI SRAM(0x24000000)** —— H7 的 DTCM(0x20000000) 是内核私有总线，
 *      外部调试器走 AHB-AP **读不到**（本仓库 ../stm32h7b0_rtt_speed/RESULTS.md 里就记着这条），
 *      所以链接脚本把 .data/.bss 整个放到 AXI SRAM（栈留在 DTCM，探针不需要读栈）。
 *      `arm-none-eabi-nm` 可以直接验：所有 g_* 的地址都是 0x24xxxxxx。
 *   ③ D-Cache **默认关**（AHB-AP 读到的就是真内存）；想复现"H7 D-cache 干扰"，
 *      用 `build.ps1 -DCache`（= `-DDCACHE_ON=1`）编一版，main() 里会把 SCB.CCR.bit16 置起来
 *      （配 DSB/ISB）。开关与现象见 README 第 4 节。
 *
 * ⚠️ 寄存器布局是 **RM0455**（H7B0），不是 H743 的 RM0433 —— 详见 src/stm32h7b0_regs.h 里的清单。
 * ⚠️ **绝不无条件写 PWR_CR3**（供电来源）：那样会把板子带进"AP 事务恒 WAIT、只能整板断电"的
 *    死状态（../stm32h7b0_rtt_speed/RESULTS.md 第 1 条，本机真踩过）。这里只**读**。
 *
 * ---------------------------------------------------------------------------
 * 变量表（t = 10 kHz tick 序号；两组变量在地址上刻意分开，用来对比"读计划"的两条路径）
 *
 * ── g_pack：**连续 24 B**（→ 一次块读全拿到 = 快路径）
 *      off  名字     类型   应有值
 *      0    f_sin    f32    SIN100[t%100] / 1000        （100 Hz 正弦，±1.0）
 *      4    f_tri    f32    100 Hz 三角，峰值在 t%100==50（与正弦差 90°）
 *      8    i_tick   i32    = t                          （10 kHz 斜坡，主对齐量）
 *      12   u_ramp   u16    = t % 1000                   （100 Hz 锯齿 0..999）
 *      14   i_sq1k   i16    = (t%10 < 5) ? +1000 : -1000  （1 kHz 方波）
 *      16   u_cnt    u8     = (uint8_t)t                 （约 39 Hz 回绕）
 *      17   i_saw    i8     = (int8_t)(t*3)              （8 位锯齿，步进 3）
 *      18   rsv0     u8     保留（仅为让 u_hi 4 字节对齐）
 *      20   u_hi     u32    = 0x10000000 | (t & 0xFFFF)  （高位非零 → 验 u32 精度）
 *
 * ── 散落量（→ 多 span，走"每变量 2 个 op"的慢路径）
 *      g_tick     u32  = t
 *      g_isr_count u32 = t                               （与 g_tick 同值，验"两个地址都对"）
 *      g_sq5k     i16  = (t&1) ? +1000 : -1000           （**5 kHz 方波**：采样率 <10 kHz 必混叠）
 *      g_pulse    u8   = (t % 2000) < 100 ? 1 : 0        （5 Hz 脉冲、5% 占空比 → 触发测试用）
 *      g_pair_a   u16  = (uint16_t)t
 *      g_pair_b   u16  = (uint16_t)~t                    （**撕裂自检**：a^b != 0xFFFF 即读到两次更新之间）
 *      g_lfsr     u32  xorshift32 伪随机（宽带，验"看不出规律"的数据）
 *      g_ramp64   f64  主循环更新：1.0 + t*1e-6          （验 8 字节载荷与浮点精度）
 *      g_far_sq100 i16 100 Hz 方波 ±1000                 （离上面那坨有 4 KB 空洞 → 第二个 span）
 *      g_far_cnt  u32  = t                               （跨 span 一致性）
 *      g_hole     u8[4096] 占位（不采），只负责把地址拉开 4 KB
 *
 * ---------------------------------------------------------------------------
 * 更新顺序（**契约**，主机侧据此放宽/收紧断言）：
 *      ① `g_tick` 先更 → 主机看到新 tick 时，载荷可能还是上一 tick 的 ⇒ 允许 ±1 tick；
 *      ② 再更 `g_pack` 的各字段（**逐个 store，不是原子快照**，所以 g_pack 内部也可能撕裂）；
 *      ③ 最后更散落量；
 *      ④ `g_pair_a/g_pair_b` 是**成对**的（a^b==0xFFFF 才算一致）→ 撕裂率可以被量化。
 *
 * 另外几个"不属契约"的诊断量（同样在 AXI SRAM 里，页面可以顺手采）：
 *      g_z_dcache（0 = D-Cache 关，1 = 开）、g_z_sysclk_hz / g_z_hclk_hz / g_z_clk_src / g_z_clk_err。
 */
#include <stdint.h>

#include "stm32h7b0_regs.h"

#ifndef DCACHE_ON
#define DCACHE_ON 0
#endif

/* 向量表（定义在 startup.c）。flash 版它在 0x08000000 —— 也要显式告诉 VTOR，
 * 免得以后想搬去 RAM 跑时忘了这一句。 */
extern void (*const g_vectors[])(void);

/* 所有等待都必须有上限：PLL 锁不上时宁可退到慢时钟，也不要让固件静静卡死在 while 里
 * （那样主机只看到"连上了但一个数都不动"，排查成本极高）。 */
#define WAIT_SPINS  20000000u

typedef struct {
  float    f_sin;    /*  0 */
  float    f_tri;    /*  4 */
  int32_t  i_tick;   /*  8 */
  uint16_t u_ramp;   /* 12 */
  int16_t  i_sq1k;   /* 14 */
  uint8_t  u_cnt;    /* 16 */
  int8_t   i_saw;    /* 17 */
  uint8_t  rsv0;     /* 18 保留 */
  uint32_t u_hi;     /* 20 */
} scope_pack_t;

volatile scope_pack_t g_pack;

volatile uint32_t g_tick;
volatile uint32_t g_isr_count;
volatile int16_t  g_sq5k;
volatile uint8_t  g_pulse;
volatile uint16_t g_pair_a;
volatile uint16_t g_pair_b;
volatile uint32_t g_lfsr = 0x12345678u;
volatile double   g_ramp64;                       /* 主循环更新 */

/* --- 故意制造"远距离"：中间隔 4 KB 空洞，逼读计划出现**第二个 span**（走慢路径） --- */
volatile uint8_t  g_hole[4096];                   /* 占位用，不采它（见 main 里那句引用） */
volatile int16_t  g_far_sq100;                    /* 100 Hz 方波 ±1000 */
volatile uint32_t g_far_cnt;                      /* = t（与 g_tick 同值 → 跨 span 一致性） */

/* --- 诊断量（不在契约里；都是给主机/排障看的）---
 * 🚨 名字里的 `z_` 是**故意的**：-fdata-sections 下链接器按**字母序**排 .bss.* 各输入段，
 *    只有排在 `g_tick` 之后的字母（u/v/w/x/y/z）才不会挤进"g_hole 制造的 4 KB 空洞"两侧 ——
 *    否则 span A 里会混进这几个变量，契约里"span A = 三个量"的对照就不干净了。 */
volatile uint32_t g_z_dcache;                     /* 0 = D-Cache 关，1 = 开 */
volatile uint32_t g_z_sysclk_hz;                  /* 实际切过去的 SYSCLK */
volatile uint32_t g_z_hclk_hz;                    /* 实际 HCLK（= SysTick 的时基） */
volatile uint32_t g_z_clk_src;                    /* CLK_SRC_* */
volatile uint32_t g_z_clk_err;                    /* CLK_ERR_* 位或（0 = 全程顺利） */

/* sin(2πi/100) × 1000，四舍五入；100 点 = 100 Hz@10 kHz，周期正好 100 个 tick */
static const int16_t SIN100[100] = {
       0,    63,   125,   187,   249,   309,   368,   426,   482,   536,   /*   0..  9 */
     588,   637,   685,   729,   771,   809,   844,   876,   905,   930,   /*  10.. 19 */
     951,   969,   982,   992,   998,  1000,   998,   992,   982,   969,   /*  20.. 29 */
     951,   930,   905,   876,   844,   809,   771,   729,   685,   637,   /*  30.. 39 */
     588,   536,   482,   426,   368,   309,   249,   187,   125,    63,   /*  40.. 49 */
       0,   -63,  -125,  -187,  -249,  -309,  -368,  -426,  -482,  -536,   /*  50.. 59 */
    -588,  -637,  -685,  -729,  -771,  -809,  -844,  -876,  -905,  -930,   /*  60.. 69 */
    -951,  -969,  -982,  -992,  -998, -1000,  -998,  -992,  -982,  -969,   /*  70.. 79 */
    -951,  -930,  -905,  -876,  -844,  -809,  -771,  -729,  -685,  -637,   /*  80.. 89 */
    -588,  -536,  -482,  -426,  -368,  -309,  -249,  -187,  -125,   -63,   /*  90.. 99 */
};

/**
 * 轮询直到 (REG32(addr) & mask) == want（want = 0 表示"字段清空"）。返回 1 = 等到，0 = 超时。
 * addr 用物理地址，避免对 REG32 宏取地址。
 */
static int wait_field(uintptr_t addr, uint32_t mask, uint32_t want, uint32_t spins)
{
  while (spins--) {
    if ((REG32(addr) & mask) == want) { return 1; }
  }
  return 0;
}

/**
 * 时钟初始化：目标 SYSCLK = HCLK = **280MHz**（寄存器级，不依赖 HAL）。
 *
 * 数值出处 = **板子自带 SDK（STM32H7B0VBT6 KIT）SystemClock_Config()**，那份在同一块板子上
 * 实测跑到 280MHz（../stm32h7b0_rtt_speed/RESULTS.md）：
 *     HSE 25MHz --/DIVM1=5--> 5MHz 参考 --×N1=112--> **560MHz VCO** --/P1=2--> **SYSCLK 280MHz**
 *     RGE = 4~8MHz 档（5MHz 参考正落在这一档）、VCOSEL = 0（宽量程 192~836MHz）
 *     HPRE = /1、CDCPRE = /1 → CPU 与 AXI/HCLK **都是 280MHz**（这样 SysTick 的时基没有歧义）
 *     APB3(CDPPRE)/APB1/APB2/APB4(SRDPPRE) = /2 → 140MHz
 *     FLASH_ACR: LATENCY = 7 + WRHIGHFREQ = 0b11
 *     VOS = SRDCR.VOS = 0b11（Scale 0，280MHz 必需）
 *
 * 🚨 DIVM1 写的是**分频值本身**（5 = /5），不是 5-1 —— 兄弟例程的 -Minimal 版就是在这里
 *    写成了 `divm - 1`，实际跑的是 350MHz 而固件自报 280MHz。证据见 src/stm32h7b0_regs.h 头注释。
 *
 * 三条纪律：
 *   ① **绝不无条件写 PWR_CR3**（供电来源）—— 本机真踩过：写它 → ACTVOSRDY 永不置位 →
 *      目标内部时钟域停摆 → DP 还能读 IDCODE 但**所有 AP 事务恒 WAIT/FAULT** → 只有整板断电
 *      才能恢复。这里只**读**它，不是 LDO 就记个标志位，绝不替板子的硬件做决定；
 *   ② **升频之前**先把 flash 等待周期给够；
 *   ③ 每一步等待都有上限，任何一步失败就整档放弃、留在 HSI 64MHz，并把出错位记进 g_z_clk_err。
 */
static void clock_init(void)
{
  uint32_t err = 0u;

  /* ① PWR_CR3：只读不写（判据见函数头注释） */
  if ((PWR_CR3 & (PWR_CR3_LDOEN | PWR_CR3_BYPASS)) != PWR_CR3_LDOEN) {
    err |= CLK_ERR_SUPPLY_NOLDO;
  }

  /* ② VOS0（Scale 0）—— 280MHz 必需 */
  PWR_SRDCR = (PWR_SRDCR & ~PWR_SRDCR_VOS_MASK) | PWR_SRDCR_VOS0;
  if (!wait_field(PWR_BASE + 0x18u, PWR_SRDCR_VOSRDY, PWR_SRDCR_VOSRDY, WAIT_SPINS)) {
    err |= CLK_ERR_VOSRDY;
  }

  /* ③ Flash 等待周期 + WRHIGHFREQ —— **必须在升频之前** */
  FLASH_ACR = (FLASH_ACR & ~0x3Fu)
            | FLASH_ACR_LATENCY(7)
            | FLASH_ACR_WRHIGHFREQ(3);

  /* ④ HSE 25MHz 起振 */
  RCC_CR |= RCC_CR_HSEON;
  if (!wait_field(RCC_BASE + 0x00u, RCC_CR_HSERDY, RCC_CR_HSERDY, WAIT_SPINS)) {
    err |= CLK_ERR_HSE_RDY;
  }

  /* ⑤ PLL1：HSE/5 = 5MHz 参考，×112 = 560MHz VCO，/2 = 280MHz */
  if ((err & ~CLK_ERR_SUPPLY_NOLDO) == 0u) {
    /* 🚨 DIVM1 写的是**分频值本身**（5 = /5），不是 5-1 */
    RCC_PLLCKSELR = RCC_PLLCKSELR_PLLSRC_HSE | (5u << RCC_PLLCKSELR_DIVM1_SHIFT);
    RCC_PLL1FRACR = 0u;                                    /* 不用小数分频 */
    RCC_PLL1DIVR  = ((112u - 1u) << 0)                     /* N1[8:0]  */
                  | ((2u - 1u) << 9)                       /* P1[15:9] */
                  | ((2u - 1u) << 16)                      /* Q1[22:16] */
                  | ((2u - 1u) << 24);                     /* R1[30:24] */
    RCC_PLLCFGR   = RCC_PLLCFGR_DIVP1EN | RCC_PLLCFGR_DIVQ1EN | RCC_PLLCFGR_DIVR1EN
                  | (2u << RCC_PLLCFGR_PLL1RGE_SHIFT);     /* RGE=4~8MHz；VCOSEL=0 = 宽量程 */
    RCC_CR |= RCC_CR_PLL1ON;
    if (!wait_field(RCC_BASE + 0x00u, RCC_CR_PLL1RDY, RCC_CR_PLL1RDY, WAIT_SPINS)) {
      err |= CLK_ERR_PLL_RDY;
    }
  }

  /* ⑥ 总线分频 + 切 SYSCLK 到 PLL1 */
  if ((err & ~CLK_ERR_SUPPLY_NOLDO) == 0u) {
    RCC_CDCFGR1 = (0u << 8)     /* CDCPRE  = /1 → CPU  280MHz */
                | (4u << 4)     /* CDPPRE  = /2 → APB3 140MHz */
                | (0u << 0);    /* HPRE    = /1 → AXI/HCLK 280MHz */
    RCC_CDCFGR2 = (4u << 8)     /* CDPPRE2 = /2 → APB2 140MHz */
                | (4u << 4);    /* CDPPRE1 = /2 → APB1 140MHz */
    RCC_SRDCFGR = (4u << 4);    /* SRDPPRE = /2 → APB4 140MHz */
    RCC_CFGR = (RCC_CFGR & ~RCC_CFGR_SW_MASK) | RCC_CFGR_SW_PLL1;
    if (!wait_field(RCC_BASE + 0x10u, RCC_CFGR_SWS_MASK, RCC_CFGR_SWS_PLL1, WAIT_SPINS)) {
      err |= CLK_ERR_SW_PLL1;
    }
  }

  if ((err & ~CLK_ERR_SUPPLY_NOLDO) == 0u) {
    g_z_clk_src   = CLK_SRC_HSE_PLL;
    g_z_sysclk_hz = SYSCLK_HZ;
    g_z_hclk_hz   = HCLK_HZ;
    g_z_clk_err   = err;                       /* 只有"供电不是 LDO"这一条警告时也算成功 */
    return;
  }

  /* 保命档：留在 HSI 64MHz 直出（总线分频全 /1，就是复位默认值）。
   * 慢，但固件一定能跑起来 —— 能跑就能读 g_z_clk_err 定位是哪一步没成。 */
  RCC_CR &= ~RCC_CR_PLL1ON;
  RCC_CFGR = (RCC_CFGR & ~RCC_CFGR_SW_MASK) | RCC_CFGR_SW_HSI;
  (void)wait_field(RCC_BASE + 0x10u, RCC_CFGR_SWS_MASK, 0u, WAIT_SPINS);
  RCC_CDCFGR1 = 0u;                  /* HPRE/CDPPRE/CDCPRE 全 /1 */
  RCC_CDCFGR2 = 0u;
  RCC_SRDCFGR = 0u;
  g_z_clk_src   = CLK_SRC_HSI;
  g_z_sysclk_hz = HSI_HZ;
  g_z_hclk_hz   = HSI_HZ;
  g_z_clk_err   = err | CLK_ERR_FALLBACK;
}

void SysTick_Handler(void){
  /* ① 主计数先更（主机据此知道"新一 tick 开始"） */
  uint32_t t = g_tick + 1u;
  g_tick = t;
  g_isr_count = t;

  /* ② 连续块（逐个 store） */
  uint32_t ph = t % 100u;
  g_pack.f_sin  = (float)SIN100[ph] * 0.001f;
  int32_t tri   = (ph < 50u) ? ((int32_t)ph * 40 - 1000) : (3000 - (int32_t)ph * 40);
  g_pack.f_tri  = (float)tri * 0.001f;
  g_pack.i_tick = (int32_t)t;
  g_pack.u_ramp = (uint16_t)(t % 1000u);
  g_pack.i_sq1k = (int16_t)(((t % 10u) < 5u) ? 1000 : -1000);
  g_pack.u_cnt  = (uint8_t)t;
  g_pack.i_saw  = (int8_t)(t * 3u);
  g_pack.u_hi   = 0x10000000u | (t & 0xFFFFu);

  /* ③ 散落量 */
  g_sq5k   = (int16_t)((t & 1u) ? 1000 : -1000);
  g_pulse  = (uint8_t)(((t % 2000u) < 100u) ? 1u : 0u);
  g_pair_a = (uint16_t)t;
  g_pair_b = (uint16_t)~t;                 /* a ^ b == 0xFFFF 才算"同一 tick 内读到" */
  g_far_sq100 = (int16_t)(((t % 100u) < 50u) ? 1000 : -1000);
  g_far_cnt   = t;
  uint32_t l = g_lfsr;                     /* xorshift32：便宜、周期长、频谱平 */
  l ^= l << 13; l ^= l >> 17; l ^= l << 5;
  g_lfsr = l;
}

int main(void){
  DBGMCU_CR |= DBGMCU_CR_DBG_CLOCKS;   /* 调试器停住时让时钟继续跑（排障少走弯路） */

  clock_init();                        /* 先把主频顶上去 —— SysTick 重载值是按实际 HCLK 算的 */

  /* 向量表基址显式指到本固件的向量表（flash 版就是 0x08000000，与复位默认一致；写一次更稳） */
  SCB_VTOR = (uint32_t)(uintptr_t)g_vectors;

  /* 取指走 I-Cache；**D-Cache 由编译开关决定**（见文件头第 ③ 条） */
  SCB_ICIALLU = 0;
  SCB_CCR |= SCB_CCR_ICACHE;
#if DCACHE_ON
  SCB_CCR |= SCB_CCR_DCACHE;
  __asm__ volatile("dsb");
  __asm__ volatile("isb");
  g_z_dcache = 1u;
#else
  g_z_dcache = 0u;
#endif

  SYST_RVR = (g_z_hclk_hz / TICK_HZ) - 1u;  /* 10 kHz：280MHz → 27999，降级到 64MHz → 6399 */
  SYST_CVR = 0;
  SYST_CSR = 7;                            /* 内核时钟 + 中断使能 + 计数使能 */

  /* 🚨 g_hole 的作用只是"占位拉开地址距离"，编译器/linker 看不到任何引用就会把它
   *    连同那 4 KB 一起 gc 掉（-fdata-sections + --gc-sections）。这里给它一个真实引用。 */
  g_hole[0] = 0;

  /* 主循环：按 tick 节拍更新那个 f64 慢斜坡（H7 有 FPU，但仍放在 ISR 外，别污染 10 kHz 时基） */
  uint32_t last = 0;
  for (;;){
    uint32_t t = g_tick;
    if (t != last){
      last = t;
      g_ramp64 = 1.0 + (double)t * 1e-6;   /* t=10000 → 1.01；t=600000（60 s）→ 1.6 */
    }
  }
}
