/*
 * STM32H743（Cortex-M7）最小寄存器定义 —— 只够跑「J-Scope 采样靶子」这份固件用。
 *
 * 为什么手写而不用 ST 的 HAL/CMSIS 头：这份例程要能"一条 gcc 命令就编出来"、不引任何外部依赖
 * （与 stm32f103_scope / stm32h7b0_rtt_speed 同一套路）。
 *
 * ---------------------------------------------------------------------------
 * 地址与位域的出处（都是本机能查到的实物，**别再凭记忆改**）：
 *   ① 正点原子阿波罗 H743 标准例程里的 CMSIS 头（ST 原版 stm32h743xx.h）：
 *        RCC_BASE  = D3_AHB1PERIPH_BASE(0x58020000) + 0x4400 = 0x58024400
 *        PWR_BASE  = D3_AHB1PERIPH_BASE + 0x4800 = 0x58024800
 *        FLASH_BASE= 0x52002000      DBGMCU_BASE = 0x5C001000
 *        RCC 偏移：CR 0x00 / CFGR 0x10 / D1CFGR 0x18 / D2CFGR 0x1C / D3CFGR 0x20
 *                  PLLCKSELR 0x28 / PLLCFGR 0x2C / PLL1DIVR 0x30 / PLL1FRACR 0x34
 *   ② 同一份头文件里的位域（逐条核过）：
 *        PLLCKSELR : PLLSRC[1:0]（10 = HSE）、DIVM1[9:4]
 *                    🚨 字段里放的就是**分频值本身**，不是"分频值-1"！
 *                    依据：`__HAL_RCC_PLL_CONFIG` 宏直接写 `(__PLLM1__) << 4U`，
 *                    且 HAL_RCC_GetSysClockFreq 里 `pllm = (PLLCKSELR & DIVM1) >> 4`
 *                    之后**直接做除**（N/P/Q/R 才会有 `-1`）。
 *        PLL1DIVR  : N1[8:0] / P1[15:9] / Q1[22:16] / R1[30:24] —— 这四个才是"值-1"
 *        PLLCFGR   : FRACEN[0] / VCOSEL[1]（0 = 宽量程 192~836MHz）/ RGE[3:2]
 *                    （00:1~2MHz 01:2~4 10:4~8 11:8~16）/ DIVP1EN[16] / DIVQ1EN[17] / DIVR1EN[18]
 *        D1CFGR    : HPRE[3:0]（AXI/AHB）/ D1PPRE[6:4]（APB3）/ D1CPRE[11:8]（CPU）
 *                    ⚠️ 顺序是 HPRE 在最低位，**不是** D1CPRE 在最低位（照 RM0455 的 CDCFGR1 抄会错）
 *                    分频编码：0xxx = /1，1000 = /2，1001 = /4，1010 = /8，1011 = /16
 *        D2CFGR    : D2PPRE1[6:4]（APB1）/ D2PPRE2[10:8]（APB2）
 *        D3CFGR    : D3PPRE[6:4]（APB4）
 *        CFGR      : SW[2:0] / SWS[5:3]（011 = PLL1）
 *        PWR_CR3   : SCUEN[2]（供电配置更新使能）
 *        PWR_D3CR  : VOS[15:14] / VOSRDY[13]
 *        FLASH_ACR : LATENCY[3:0] / WRHIGHFREQ[5:4]
 */
#ifndef STM32H743_REGS_H
#define STM32H743_REGS_H

#include <stdint.h>

#define REG32(addr) (*(volatile uint32_t *)(uintptr_t)(addr))

/* ---------------- RCC（0x58024400）---------------- */
#define RCC_BASE        0x58024400u
#define RCC_CR          REG32(RCC_BASE + 0x00)   /* HSION[0] HSIRDY[2] HSEON[16] HSERDY[17] PLL1ON[24] PLL1RDY[25] */
#define RCC_CFGR        REG32(RCC_BASE + 0x10)   /* SW[2:0] / SWS[5:3] */
#define RCC_D1CFGR      REG32(RCC_BASE + 0x18)   /* HPRE[3:0] D1PPRE[6:4] D1CPRE[11:8] */
#define RCC_D2CFGR      REG32(RCC_BASE + 0x1C)   /* D2PPRE1[6:4](APB1) D2PPRE2[10:8](APB2) */
#define RCC_D3CFGR      REG32(RCC_BASE + 0x20)   /* D3PPRE[6:4](APB4) */
#define RCC_PLLCKSELR   REG32(RCC_BASE + 0x28)   /* PLLSRC[1:0] / DIVM1[9:4]（写分频值本身） */
#define RCC_PLLCFGR     REG32(RCC_BASE + 0x2C)   /* FRACEN[0] VCOSEL[1] RGE[3:2] DIVP1EN[16] ... */
#define RCC_PLL1DIVR    REG32(RCC_BASE + 0x30)   /* N1[8:0] P1[15:9] Q1[22:16] R1[30:24]（值-1） */
#define RCC_PLL1FRACR   REG32(RCC_BASE + 0x34)

#define RCC_CR_HSEON    (1u << 16)
#define RCC_CR_HSERDY   (1u << 17)
#define RCC_CR_PLL1ON   (1u << 24)
#define RCC_CR_PLL1RDY  (1u << 25)

#define RCC_CFGR_SW_MASK   0x7u
#define RCC_CFGR_SW_HSI    0x0u
#define RCC_CFGR_SW_PLL1   0x3u
#define RCC_CFGR_SWS_MASK  0x38u
#define RCC_CFGR_SWS_PLL1  0x18u

/* PLLCKSELR */
#define RCC_PLLCKSELR_PLLSRC_HSI  0x0u
#define RCC_PLLCKSELR_PLLSRC_CSI  0x1u
#define RCC_PLLCKSELR_PLLSRC_HSE  0x2u
#define RCC_PLLCKSELR_DIVM1_SHIFT 4u             /* DIVM1[9:4]：写**分频值**（5 = /5） */

/* PLLCFGR */
#define RCC_PLLCFGR_PLL1FRACEN (1u << 0)
#define RCC_PLLCFGR_PLL1VCOSEL (1u << 1)         /* 0 = 宽量程 192~836MHz（我们要的） */
#define RCC_PLLCFGR_PLL1RGE_SHIFT 2u
#define RCC_PLLCFGR_DIVP1EN    (1u << 16)
#define RCC_PLLCFGR_DIVQ1EN    (1u << 17)
#define RCC_PLLCFGR_DIVR1EN    (1u << 18)

/* ---------------- PWR（0x58024800）----------------
 * H743 上用 CR3 选供电来源（复位默认 LDO），D3CR 设电压档 VOS[15:14]。
 * ⚠️ 本文件**只清 SCUEN 这一位**（而且只在它置位时才写），绝不整块写 CR3 ——
 *    无条件写供电寄存器在 H7 上出过"AP 事务恒 WAIT、只能整板断电"的事故
 *    （见 ../stm32h7b0_rtt_speed/RESULTS.md 第 1 条）。 */
#define PWR_BASE        0x58024800u
/* 🚨 偏移是 **0x0C**，不是 0x08！厂商头文件 PWR_TypeDef 的顺序是
 *      CR1(0x00) CSR1(0x04) CR2(0x08) **CR3(0x0C)** CPUCR(0x10) D3CR(0x18)
 *    （依据：实验39/.../USER/stm32h743xx.h 的 PWR_TypeDef 定义）。
 *    这里曾写成 +0x08（那是 **CR2**）—— 后果是 **SCUEN 永远清不掉**（写到了别的寄存器），
 *    而 SCUEN=1 时硬件会**静默忽略 VOS 的写**，于是 PWR_D3CR.VOSRDY 恒为 0：
 *      · 固件走"保命档"降级到 HSI 64MHz（g_z_clk_err = 0x81、g_z_hclk_hz = 64e6）；
 *      · 供电档没生效时内存/总线访问不可靠 → 实测随后在 D-Cache 失效那段吃到
 *        BFSR.IMPRECISERR + HFSR.FORCED，死在 Default_Handler（变量全程是初值 →
 *        J-Scope 一个数都采不到）。
 *    同一块板、同一套序列，兄弟例程 ../stm32h743_rtt_speed 用的是 0x0C，实测 VOSRDY=1、
 *    480MHz、g_clk_err=0 —— 差异就在这一个偏移上。 */
#define PWR_CR3         REG32(PWR_BASE + 0x0C)   /* LDOEN[1] BYPASS[0] SCUEN[2] */
#define PWR_D3CR        REG32(PWR_BASE + 0x18)   /* VOS[15:14] VOSRDY[13]（⚠️ 偏移 0x18，不是 0x10） */

#define PWR_CR3_SCUEN       (1u << 2)
#define PWR_D3CR_VOS_MASK   0xC000u
#define PWR_D3CR_VOS_HIGH   0xC000u              /* 0b11：最高电压档（老文档 Scale1=400MHz / 新文档 Scale0=480MHz） */
#define PWR_D3CR_VOSRDY     (1u << 13)

/* ---------------- FLASH（0x52002000）----------------
 * ACR：LATENCY[3:0] + WRHIGHFREQ[5:4]（Flash 信号延时档）。
 * 🚨 LATENCY 宁大不小：给少了会取到错指令（现象千奇百怪，极难查）。 */
#define FLASH_BASE      0x52002000u
#define FLASH_ACR       REG32(FLASH_BASE + 0x00)
#define FLASH_ACR_LATENCY(n)     ((n) & 0x0Fu)
#define FLASH_ACR_WRHIGHFREQ(n)  (((n) & 0x3u) << 4)

/* ---------------- DBGMCU（0x5C001000）----------------
 * 让内核在调试器停住时**时钟继续跑**（否则排查时钟/时序问题时会误判）。 */
#define DBGMCU_BASE     0x5C001000u
#define DBGMCU_CR       REG32(DBGMCU_BASE + 0x04)
#define DBGMCU_CR_DBG_CLOCKS 0x7u

/* ---------------- Cortex-M7 内核外设 ---------------- */
#define SYST_CSR        REG32(0xE000E010u)
#define SYST_RVR        REG32(0xE000E014u)
#define SYST_CVR        REG32(0xE000E018u)
#define SCB_CCR         REG32(0xE000ED14u)       /* bit16 = DCACHEEN, bit17 = ICACHEEN */
#define SCB_CCR_ICACHE  (1u << 17)
#define SCB_CCR_DCACHE  (1u << 16)
#define SCB_VTOR        REG32(0xE000ED08u)       /* 向量表偏移（RAM 运行版必须显式指过去） */
#define SCB_ICIALLU     REG32(0xE000EF50u)
#define SCB_CPACR       REG32(0xE000ED88u)       /* CP10/CP11 = FPU 访问许可 */
#define FPU_FPCCR       REG32(0xE000EF34u)       /* ASPEN[31] / LSPEN[30] */

/* ---------------- D-Cache 识别 / 维护（ARMv7-M，地址固定）----------------
 * 为什么需要：**使能 D-Cache 之前必须让整片 cache 失效**（CMSIS 的 `SCB_EnableDCache()`
 * 也是"先按 set/way 失效、再置 CCR.DC"）—— 复位后 cache 里是什么，ARM 的说法是 UNKNOWN。
 * ⚠️ 曾经把这次崩溃归因于"少了一次 invalidate"，**2026-10 上板定因后更正**：真因是
 *    `SCB_DCISW` 的地址写错（见下面那段偏移表），失效操作打到了 DCIMVAC 上。
 *
 * 几何从 CCSIDR 读，不写死（本机 H743 读回 **0xF00FE019**：组数-1 = [27:13] = 0x7F → 128 组、
 * 相联度-1 = [12:3] = 3 → 4 路；CTR.DminLine 另证行长 32 B ⇒ 128×4×32 = 16 KB）。
 * 复位后 CSSELR = 0 = L1 D-Cache，本文件不去动它。 */
#define SCB_CCSIDR      REG32(0xE000ED80u)       /* Cache Size ID：几何 */
#define SCB_CSSELR      REG32(0xE000ED84u)       /* Cache Size Selection（0 = L1 D-Cache） */
/* 🚨 这几个偏移**一格都不能错**，而且错了不会报编译错，只会在运行时炸总线。
 *    权威表（厂商 core_cm7.h 的 SCB_Type，SCB_BASE = 0xE000ED00）：
 *      0x250 ICIALLU / 0x258 ICIMVAU / 0x25C **DCIMVAC** / 0x260 **DCISW**
 *      0x264 DCCMVAU / 0x268 DCCMVAC / 0x26C DCCSW / 0x270 DCCIMVAC / 0x274 DCCISW
 *    本文件曾把 DCISW 写成 0xE000EF5C（那是 **DCIMVAC = 按地址失效**）——
 *    于是 set/way 编码（0、32、64…）被当成**内存地址**送进 cache 维护引擎，
 *    实测立刻吃到 BFSR.IMPRECISERR + HFSR.FORCED，死在 Default_Handler
 *    （压栈现场 PC 正好是那条 `str.w r7,[r2,#0xF5C]`；两版时钟下都必现）。
 *    ⇒ 使能 D-Cache 之前的那次"整片失效"必须打到 0xE000EF60。 */
#define SCB_DCISW       REG32(0xE000EF60u)       /* 按 set/way 让 D-Cache 失效（0x260） */
#define SCB_DCIMVAC     REG32(0xE000EF5Cu)       /* 按地址无效到 PoC（0x25C） */
#define SCB_DCCMVAC     REG32(0xE000EF68u)       /* 按地址清到 PoC（0x268） */

/* ---------------- MPU（ARMv7-M，0xE000ED90 起）----------------
 * 只用到"把 AXI SRAM 配成 Normal / Non-cacheable"这一个 region。 */
#define MPU_TYPE        REG32(0xE000ED90u)       /* [15:8] DREGION = region 个数 */
#define MPU_CTRL        REG32(0xE000ED94u)
#define MPU_RNR         REG32(0xE000ED98u)
#define MPU_RBAR        REG32(0xE000ED9Cu)
#define MPU_RASR        REG32(0xE000EDA0u)

#define MPU_CTRL_ENABLE      (1u << 0)
#define MPU_CTRL_HFNMIENA    (1u << 1)
#define MPU_CTRL_PRIVDEFENA  (1u << 2)           /* 没被 region 覆盖的地址走默认内存映射 */
#define MPU_RASR_ENABLE      (1u << 0)
#define MPU_RASR_SIZE(s)     (((s) & 0x1Fu) << 1)   /* 区域 = 2^(SIZE+1) 字节，SIZE = log2(大小)-1 */
#define MPU_RASR_AP(n)       (((n) & 0x7u) << 24)   /* 011 = 特权/非特权全访问 */
#define MPU_RASR_TEX(n)      (((n) & 0x7u) << 19)
#define MPU_RASR_S           (1u << 18)          /* shareable */
#define MPU_RASR_C           (1u << 17)          /* cacheable */
#define MPU_RASR_B           (1u << 16)          /* bufferable */
#define MPU_RASR_XN          (1u << 28)          /* 不可取指 */

/* ---------------- 内存布局（与 ld 脚本一致）---------------- */
#define AXI_SRAM_BASE   0x24000000u              /* AXI SRAM 512KB：被采样的变量全住这里 */

/* ---------------- 本板时钟目标 ---------------- */
#define HSE_HZ          25000000u                /* 阿波罗 H743 板载 25MHz 晶振 */
#define HSI_HZ          64000000u                /* H7 的 HSI = 64MHz（无晶振时的后备） */
#define SYSCLK_HZ       480000000u               /* HSE/5 ×192 /2 = 480MHz（VCO 960MHz）—— 与兄弟例程 rtt_speed 一致 */
#define HCLK_HZ         240000000u               /* HPRE=/2，且 D1CPRE 也 =/2 → CPU 与 AXI 同为 240MHz */
#define TICK_HZ         10000u                   /* 采样靶子的时基 */

/* 时钟路径（g_clk_src） */
#define CLK_SRC_HSE_PLL 0u                       /* 25MHz 晶振 → PLL1 → 400MHz（正常路径） */
#define CLK_SRC_HSI     1u                       /* HSI 64MHz 直出（PLL 任一步没成时的保命档） */

/* 时钟初始化出错位（g_clk_err）：不接调试器也能知道卡在哪一步 */
#define CLK_ERR_VOSRDY   (1u << 0)
#define CLK_ERR_HSE_RDY  (1u << 1)
#define CLK_ERR_PLL_RDY  (1u << 2)
#define CLK_ERR_SW_PLL1  (1u << 3)
#define CLK_ERR_FALLBACK (1u << 7)               /* 最终没跑到 400MHz，退在 HSI 64MHz */

#endif /* STM32H743_REGS_H */
