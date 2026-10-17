/*
 * 调试器压力测试靶子 —— 主程序（2026-10）
 *
 * 这块固件**只干一件事**：给调试器页提供"取之不尽"的可停、可走、可看的代码。
 *
 * 与兄弟例程（stm32h743_scope / stm32h743_rtt_speed）的两点不同，都是故意的：
 *   ① **不动时钟**：复位后就是 HSI 64 MHz。压测要的是"确定性"，不是主频；
 *      PWR/VOS/MPU/D-Cache 那套在 scope 例程里已经踩平了，这里不重复引入变量。
 *   ② **D-Cache 关着**：探针走 AHB-AP 读到的就是真内存，监视窗口看到的值
 *      必须等于 CPU 刚写进去的值 —— 压测要拿这个当判据（cache 一开就得配 MPU，
 *      那是另一个故事，别混进来）。
 *
 * 主循环是个 11 段的流水线：每轮把每一段都跑一遍，`g_stage` 就是"现在在第几段"。
 * 这样任何一段下断点都会**每轮必命中**，而且停下来一眼能看出停在哪一段。
 *
 * 两个中断：
 *   · SysTick     10 kHz（每 100 µs 一次）—— 高频中断里下断点/单步的靶子
 *   · PendSV      每 1000 个 tick 软触发一次（~10 Hz）—— 慢速中断，适合"进中断再跳出"
 */
#include <stdint.h>
#include "engine.h"
#include "model.h"

/* 内核私有外设（不需要开任何时钟） */
#define SYST_CSR   (*(volatile uint32_t *)0xE000E010u)
#define SYST_RVR   (*(volatile uint32_t *)0xE000E014u)
#define SYST_CVR   (*(volatile uint32_t *)0xE000E018u)
#define SCB_ICSR   (*(volatile uint32_t *)0xE000ED04u)
#define ICSR_PENDSVSET  (1u << 28)

#define SYSCLK_HZ   64000000u          /* 复位后的 HSI */
#define TICK_HZ     10000u
#define PENDSV_EVERY 1000u             /* 每 1000 tick 触发一次 PendSV（10 Hz） */

#define STAGE_COUNT 11u

volatile uint32_t g_ticks;             /* SysTick 计数（ISR 里 ++） */
volatile uint32_t g_pendsv_count;      /* PendSV 计数 */
volatile uint32_t g_loops;             /* 主循环轮数 */
volatile uint32_t g_stage;             /* 当前在第几段（0..STAGE_COUNT-1） */
volatile uint32_t g_last_result;       /* 上一段返回值 */
volatile uint32_t g_checksum;          /* 每轮算一次：用来判断"代码真的在跑" */
volatile uint32_t g_seq_slot[8];       /* engine_linear / engine_linear_os 的写出目标 */
volatile uint32_t g_isr_seen;          /* ISR 里能看到的"主循环轮数快照" */

void SysTick_Handler(void)
{
  g_ticks = g_ticks + 1u;
  g_isr_seen = g_loops;
  if ((g_ticks % PENDSV_EVERY) == 0u){
    SCB_ICSR = ICSR_PENDSVSET;         /* 软触发 PendSV（最低优先级） */
  }
}

void PendSV_Handler(void)
{
  g_pendsv_count = g_pendsv_count + 1u;
}

/* 空转：让主循环一轮大约几十微秒 —— 单步时不会被"下一轮"追着跑 */
static void spin(uint32_t n)
{
  while (n != 0u){
    __asm__ volatile("nop");
    n--;
  }
}

static uint32_t stage_run(uint32_t s)
{
  switch (s){
    case 0:  return engine_linear(&g_seq_slot[0], g_loops);            /* O0 线性序列（对照靶子） */
    case 1:  return engine_linear_os(&g_seq_slot[0], g_loops ^ 0x5A5A5A5Au);
    case 2:  return engine_deep_chain(g_loops);                        /* 6 层嵌套 */
    case 3:  return engine_rec_fib(10u);                               /* 递归 */
    case 4:  return engine_rec_ack(2u, 3u);                            /* 更深递归 */
    case 5:  return (uint32_t)engine_mutual(g_loops);                  /* 互递归 */
    case 6:  return engine_dispatch(g_loops & 3u, g_loops);            /* 函数指针（BLX Rn） */
    case 7:  return engine_branchy(g_loops);                           /* 分支/循环/switch */
    case 8:  return engine_uses_inline(g_loops);                       /* 内联 */
    case 9:  model_bitfield_touch(g_loops); return g_model.flags.word; /* 位域 */
    default: return model_update(g_loops);                             /* 结构体全量更新 */
  }
}

int main(void)
{
  model_init();

  /* SysTick：处理器时钟（= HSI 64 MHz）→ 10 kHz */
  SYST_RVR = (SYSCLK_HZ / TICK_HZ) - 1u;
  SYST_CVR = 0u;
  SYST_CSR = 0x7u;                     /* ENABLE | TICKINT | CLKSOURCE=处理器时钟 */

  for (;;){
    uint32_t s;

    g_loops = g_loops + 1u;
    for (s = 0; s < STAGE_COUNT; s++){
      g_stage = s;                     /* volatile：停下来就能看到"停在第几段" */
      g_last_result = stage_run(s);
    }
    g_checksum = model_checksum() ^ g_seq_slot[7] ^ g_last_result;
    spin(4000u);
  }
}
