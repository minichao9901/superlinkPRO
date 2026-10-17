/*
 * STM32H743 · **J-Scope 波形页（探针侧 HSS 采样）专用测试固件**
 *
 * 目的与 stm32f103_scope **完全同一套**：把"采样率对不对、有没有混叠、丢了多少、有没有撕裂读"
 * 从"看着像"变成**可客观判定** —— 每个被采样的量都有精确已知的数学波形，
 * 主机按取到的 `i_tick` 就能反算其余通道的应有值，从而逐点断言。
 *
 * 与 F103 那份的差别（**只有这三条**，变量契约一字不改）：
 *   ① 时钟：HSE 25MHz → PLL1 → SYSCLK 480MHz（CPU 240 / AXI 240，见 clock_init()）；
 *   ② **所有被采样的量都在 AXI SRAM(0x24000000)** —— H7 的 DTCM(0x20000000) 是内核私有总线，
 *      外部调试器走 AHB-AP **读不到**（本仓库 README 与 H743 兄弟例程都写明），
 *      所以链接脚本把 .data/.bss 整个放到 AXI SRAM（栈留在 DTCM，探针不需要读栈）。
 *      `arm-none-eabi-nm` 可以直接验：所有 g_* 的地址都是 0x24xxxxxx。
 *   ③ **D-Cache 默认关**（AHB-AP 读到的就是真内存）。这一版是仓库发货的那一份。
 *      想复现/研究"H7 D-cache 干扰"，另有两个编译开关（详见 README 第 4 节）：
 *        `-DCache`    = `-DDCACHE_ON=1`      → cache 开、无 MPU：变量会被 cache 兜住
 *                                              （探针读到旧值/冻住；⚠️ 这一版**只做演示**，
 *                                               别指望它跑得起来 —— 见 README 第 4.0 节）
 *        `-DNonCache` = `-DDCACHE_ON=1 -DNONCACHE_MPU=1`
 *                                            → cache 开 + **MPU 把 AXI SRAM 配成非缓存**：
 *                                              既享受 cache，探针又能读到实时值（推荐做法）
 *      三种固件都会把"自己是哪一版"写进 `g_z_dcache`：0 = cache 关 / 1 = cache 开 / 2 = cache 开 + MPU 非缓存。
 *
 * ⚠️ **时基仍是 10 kHz**（SysTick，H7 跑 10 kHz 毫无压力）：ISR 里所有量一次更新完。
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

#include "stm32h743_regs.h"

#ifndef DCACHE_ON
#define DCACHE_ON 0
#endif
#ifndef NONCACHE_MPU
#define NONCACHE_MPU 0
#endif
#if NONCACHE_MPU && !DCACHE_ON
#error "NONCACHE_MPU 只在 D-Cache 打开时才有意义（cache 关着本来就读到真内存）"
#endif

/* 向量表（定义在 startup.c），flash 版里在 0x08000000 —— 仍然显式告诉 VTOR，别依赖复位默认值。 */
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
volatile uint32_t g_z_dcache;                     /* 0 = D-Cache 关，1 = 开，2 = 开 + AXI SRAM 被 MPU 配成非缓存 */
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
 * 时钟初始化：目标 SYSCLK **480MHz** / HCLK 240MHz（**寄存器级，不依赖 HAL**）。
 *
 * 数值出处：正点原子阿波罗 H743 标准例程 SYSTEM/sys/sys.c 的 `Stm32_Clock_Init(160,5,2,2)`
 *   （400MHz 版），这里把 N1 从 160 提到 **192** 顶到 H7 的上限 **480MHz** ——
 *   与同板兄弟例程 `stm32h743_rtt_speed` **同一套数值**（那份一直是 192）。
 *     HSE 25MHz --/DIVM1=5--> 5MHz 参考 --×N1=192--> **960MHz VCO** --/P1=2--> **SYSCLK 480MHz**
 *     RGE = 4~8MHz 档（5MHz 参考正落在这一档）、VCOSEL = 0（宽量程 192~836MHz）
 *     HPRE = /2  → AXI/HCLK **240MHz**（AHB-AP 读内存的速度就看这个；也是 H743 AXI 的上限）
 *     D1CPRE = /2 → CPU 也是 240MHz
 *     APB1/2/3 = /2 (120MHz)、APB4 = /4 (60MHz)
 *     FLASH_ACR: LATENCY = 4 + WRHIGHFREQ = 0b11
 *        （厂商例程用 2 且不动 WRHIGHFREQ、兄弟例程用 4 + 0b10 —— 三种都能跑，等待周期只影响
 *          速度不影响正确性，这里取最保守的一组）
 *     VOS = D3CR.VOS = 0b11（老文档叫 Scale1 = 400MHz，新文档叫 Scale0 = 480MHz，都是 0b11）
 *
 * 🚨 **D1CPRE 与 HPRE 故意都取 /2**：H7 上"CPU 时钟"和"HCLK"可以是两个频率，而 SysTick 到底跟
 *    哪一个说法不一（ARM 说跟 processor clock，ST 的 HAL 却按 HCLK 算）。让两者相等，
 *    10 kHz 时基就不存在这个歧义 —— 反正被采样量的刷新与 AHB-AP 读只关心 HCLK。
 *    （兄弟例程 rtt_speed 是 CPU 480 / AXI 240、CPU≠HCLK —— 它不需要 SysTick 计时基，
 *      两边的 **SYSCLK 都是 480MHz**，只是 CPU 分频不同。）
 *
 * 三条纪律（H7 上都是踩过的坑）：
 *   ① **VOS 的写会被硬件静默忽略**，除非 PWR_CR3.SCUEN 已经清 0（这里只在它置位时才去清，
 *      绝不像 HAL 那样整块写 CR3 —— 无条件写供电寄存器出过"AP 事务恒 WAIT、只能整板断电"的事故）；
 *   ② **升频之前**先把 flash 等待周期给够；
 *   ③ 每一步等待都有上限，任何一步失败就整档放弃、留在 HSI 64MHz，并把出错位记进 g_z_clk_err。
 */
static void clock_init(void)
{
  uint32_t err = 0u;

  /* ① SCUEN：只在它置位时才清（其余情况一个字节都不写 PWR_CR3） */
  if (PWR_CR3 & PWR_CR3_SCUEN) { PWR_CR3 &= ~PWR_CR3_SCUEN; }

  /* ② VOS：最高电压档 */
  PWR_D3CR = (PWR_D3CR & ~PWR_D3CR_VOS_MASK) | PWR_D3CR_VOS_HIGH;
  if (!wait_field(PWR_BASE + 0x18u, PWR_D3CR_VOSRDY, PWR_D3CR_VOSRDY, WAIT_SPINS)) {
    err |= CLK_ERR_VOSRDY;
  }

  /* ③ Flash 等待周期 + WRHIGHFREQ —— **必须在升频之前** */
  FLASH_ACR = (FLASH_ACR & ~0x3Fu)
            | FLASH_ACR_LATENCY(4)
            | FLASH_ACR_WRHIGHFREQ(3);

  /* ④ HSE 25MHz 起振 */
  RCC_CR |= RCC_CR_HSEON;
  if (!wait_field(RCC_BASE + 0x00u, RCC_CR_HSERDY, RCC_CR_HSERDY, WAIT_SPINS)) {
    err |= CLK_ERR_HSE_RDY;
  }

  /* ⑤ PLL1：HSE/5 = 5MHz 参考，×192 = 960MHz VCO，/2 = 480MHz */
  if (err == 0u) {
    /* 🚨 DIVM1 写的是**分频值本身**（5 = /5），不是 5-1 */
    RCC_PLLCKSELR = RCC_PLLCKSELR_PLLSRC_HSE | (5u << RCC_PLLCKSELR_DIVM1_SHIFT);
    RCC_PLL1FRACR = 0u;                                    /* 不用小数分频 */
    RCC_PLL1DIVR  = ((192u - 1u) << 0)                     /* N1[8:0]  */
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
  if (err == 0u) {
    RCC_D1CFGR = (8u << 8)      /* D1CPRE = /2 → CPU  240MHz */
               | (4u << 4)      /* D1PPRE = /2 → APB3 120MHz */
               | (8u << 0);     /* HPRE   = /2 → AXI/HCLK 240MHz */
    RCC_D2CFGR = (4u << 8)      /* D2PPRE2 = /2 → APB2 120MHz */
               | (4u << 4);     /* D2PPRE1 = /2 → APB1 120MHz */
    RCC_D3CFGR = (5u << 4);     /* D3PPRE  = /4 → APB4  60MHz */
    RCC_CFGR = (RCC_CFGR & ~RCC_CFGR_SW_MASK) | RCC_CFGR_SW_PLL1;
    if (!wait_field(RCC_BASE + 0x10u, RCC_CFGR_SWS_MASK, RCC_CFGR_SWS_PLL1, WAIT_SPINS)) {
      err |= CLK_ERR_SW_PLL1;
    }
  }

  if (err == 0u) {
    g_z_clk_src   = CLK_SRC_HSE_PLL;
    g_z_sysclk_hz = SYSCLK_HZ;
    g_z_hclk_hz   = HCLK_HZ;
    g_z_clk_err   = 0u;
    return;
  }

  /* 保命档：留在 HSI 64MHz 直出（总线分频全 /1，就是复位默认值）。
   * 慢，但固件一定能跑起来 —— 能跑就能读 g_z_clk_err 定位是哪一步没成。 */
  RCC_CR &= ~RCC_CR_PLL1ON;
  RCC_CFGR = (RCC_CFGR & ~RCC_CFGR_SW_MASK) | RCC_CFGR_SW_HSI;
  (void)wait_field(RCC_BASE + 0x10u, RCC_CFGR_SWS_MASK, 0u, WAIT_SPINS);
  RCC_D1CFGR = 0u;                     /* HPRE/D1PPRE/D1CPRE 全 /1 */
  RCC_D2CFGR = 0u;
  RCC_D3CFGR = 0u;
  g_z_clk_src   = CLK_SRC_HSI;
  g_z_sysclk_hz = HSI_HZ;
  g_z_hclk_hz   = HSI_HZ;
  g_z_clk_err   = err | CLK_ERR_FALLBACK;
}

/* ===========================================================================
 *  D-Cache / MPU —— H7 上当"数据要给外部调试器看"时必须一起想的两件事
 * ===========================================================================
 *
 * ① **使能 D-Cache 之前必须让整片 D-Cache 失效**（`dcache_invalidate_all()`）。
 *    复位后 cache 里的内容是 UNKNOWN，带随机脏行直接置 `SCB_CCR.DCACHE` 是有风险的，
 *    所以这一步照 ARM/CMSIS 的做法保留（CMSIS 的 `SCB_EnableDCache()` 就是"先失效再使能"）。
 *
 *    ⚠️ **2026-10 上板定因：那次"第一个 SysTick 就死在 Default_Handler"跟 invalidate 本身无关**，
 *    真因是**失效寄存器地址写错**（`SCB_DCISW` 写成了 0xE000EF5C = **DCIMVAC 按地址失效**，
 *    正确是 0xE000EF60）：于是 set/way 编码（0、32、64…）被当成**内存地址**送进 cache 维护引擎
 *    → `BFSR.IMPRECISERR` + HFSR.FORCED。压栈现场取证：PC=0x080002E8（`str.w r7,[r2,#0xF5C]`）。
 *    修掉地址后同一份代码在本机 400MHz/200MHz 下都干净跑起来。详见 stm32h743_regs.h 的偏移表。
 *
 *    （同一轮还查出一个会让**整个时钟降级**的错：`PWR_CR3` 写成了 `PWR_BASE + 0x08`，
 *      那是 CR2；SCUEN 因此从未清掉 → VOS 的写被硬件静默忽略 → VOSRDY 恒 0。
 *      这也解释了现场看到的 `g_z_clk_err = 0x81 / g_z_hclk_hz = 64e6`。）
 *
 * ② **被采样变量所在的内存必须是非缓存的**（`mpu_noncache_axi()`，`-DNonCache`）。
 *    AXI SRAM 在 M7 默认内存映射里是 Normal / Write-Back / Write-Allocate；
 *    探针走 AHB-AP 读的是**物理内存**，CPU 的写停在 cache 行里 ⇒ 读到旧值。
 *    两种解法：把 D-Cache 关掉（默认版走的路），或把这块内存配成非缓存（本函数）。
 *
 *  顺序很重要：**先配 MPU 属性 → 再使能 cache**。反过来（cache 已开再改属性）
 *  按 ARM 的要求就得先按地址做 cache 维护，麻烦且容易漏。
 * =========================================================================== */

/** 让整片 L1 D-Cache 失效（CMSIS `SCB_InvalidateDCache()` 的寄存器级等价实现，不引 CMSIS）。
 *  几何从 CCSIDR 现读：组数-1 在 [27:13]、相联度-1 在 [12:3]；两者都是 2 的幂。
 *  读到的几何不合理时退到 H7 的实测几何（128 组 × 4 路），保证不会"什么都没清"。 */
#if DCACHE_ON
static void dcache_invalidate_all(void)
{
  __asm__ volatile("dsb");
  uint32_t ccsidr = SCB_CCSIDR;
  uint32_t sets = ((ccsidr >> 13) & 0x7FFFu) + 1u;   /* 组数   */
  uint32_t ways = ((ccsidr >>  3) & 0x3FFFu) + 1u;   /* 相联度 */
  if ((sets & (sets - 1u)) != 0u || sets > 4096u) { sets = 128u; }   /* 非 2 的幂 → 用实测几何 */
  if ((ways & (ways - 1u)) != 0u || ways > 16u)   { ways = 4u; }

  uint32_t wshift = 31u - (uint32_t)__builtin_clz(ways);   /* log2(ways) */
  for (uint32_t w = 0; w < ways; w++) {
    for (uint32_t s = 0; s < sets; s++) {
      /* DCISW：Way 在最高位（[31:32-log2(ways)]），Set 在 [(log2(sets)+4):5] */
      SCB_DCISW = (w << (32u - wshift)) | (s << 5);
    }
  }
  __asm__ volatile("dsb");
  __asm__ volatile("isb");
}
#endif /* DCACHE_ON */

/** 把整块 AXI SRAM（0x24000000，512 KB —— 被采样变量都住这里）配成 Normal / Non-cacheable。
 *
 *  属性编码：TEX=001、C=0、B=0、S=1 → Normal memory, Non-cacheable（shareable）。
 *  RASR.SIZE = log2(512K) - 1 = 18；区域基址必须按大小对齐（0x24000000 是 512K 对齐 ✓）。
 *  `PRIVDEFENA=1` 是**必须的**：没被 region 覆盖的地址（flash、外设、PPB）继续走默认映射，
 *  否则一开 MPU 就会在访问 SYST_RVR / RCC 这些外设时取到 MemManage 故障。
 *  MPU 的 region 只用了 0 号一个；改配置前按 ARM 要求先关 MPU 再写。 */
#if NONCACHE_MPU
static void mpu_noncache_axi(void)
{
  MPU_CTRL = 0u;                       /* 改 region 前先关 */
  __asm__ volatile("dsb");
  __asm__ volatile("isb");

  MPU_RNR  = 0u;                       /* region 0 */
  MPU_RBAR = AXI_SRAM_BASE;            /* 基址（region 号由 RNR 给） */
  MPU_RASR = MPU_RASR_ENABLE
           | MPU_RASR_SIZE(18u)        /* 2^(18+1) = 512 KB */
           | MPU_RASR_AP(3u)           /* 特权/非特权全访问 */
           | MPU_RASR_TEX(1u)          /* TEX=001 */
           | MPU_RASR_S                /* S=1  */
           | MPU_RASR_XN;              /* 数据区不可取指（本工程代码全在 flash） */

  MPU_CTRL = MPU_CTRL_ENABLE | MPU_CTRL_PRIVDEFENA;
  __asm__ volatile("dsb");
  __asm__ volatile("isb");
}
#endif /* NONCACHE_MPU */

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

  /* 向量表基址：flash 版 = 0x08000000（本目录**只出 flash 版**，早先那个全 RAM 版已删除）。
   * 显式设 VTOR 而不是依赖复位默认值 0（= flash 别名）—— 将来若真搬去别处运行，
   * 忘了这行就会出现"SysTick 一进中断就取到错向量"这种极难查的飞。 */
  SCB_VTOR = (uint32_t)(uintptr_t)g_vectors;

  /* 先把采样窗口的内存属性定下来（MPU），**必须在开 cache 之前** —— 见上面那段说明。
   * 默认版（cache 关）不需要 MPU：不缓存就永远读到真内存。 */
#if NONCACHE_MPU
  mpu_noncache_axi();
#endif

  /* 取指走 I-Cache；**D-Cache 由编译开关决定**（见文件头第 ③ 条） */
  SCB_ICIALLU = 0;                       /* I-Cache 整片无效（同样必须在使能之前） */
  SCB_CCR |= SCB_CCR_ICACHE;
#if DCACHE_ON
  dcache_invalidate_all();               /* 🚨 缺了这句 = 随机脏行回写随机地址 → 不精确总线错误 */
  SCB_CCR |= SCB_CCR_DCACHE;
  __asm__ volatile("dsb");
  __asm__ volatile("isb");
#endif
  /* 告诉主机"这一版是什么"：0 = cache 关 / 1 = cache 开 / 2 = cache 开 + AXI 非缓存 */
#if DCACHE_ON && NONCACHE_MPU
  g_z_dcache = 2u;
#elif DCACHE_ON
  g_z_dcache = 1u;
#else
  g_z_dcache = 0u;
#endif

  SYST_RVR = (g_z_hclk_hz / TICK_HZ) - 1u;   /* 10 kHz：240MHz → 23999，降级到 64MHz → 6399 */
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
