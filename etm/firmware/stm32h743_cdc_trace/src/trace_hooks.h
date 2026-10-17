/*
 * etm-trace 钩子（H7 系列 / Cortex-M7）—— 只留钩子，ETM 由外部 SWD 工具配置
 *
 * 设计原则（与 stm32h7b0_spi_trace 完全一致）：
 *   **固件普通启动不配置、不开启 ETM**。ETMv4 / CSTF / ETF / TPIU / 引脚复用全部由
 *   外部工具（OpenOCD / pyOCD / 上位机网页）通过 SWD 写寄存器完成。固件这边只提供：
 *
 *   trace_hook_entry()   稳定的业务入口 —— 外部工具在这里下硬件断点，作为执行流起点
 *   trace_hook_ready()   置位 g_trace.ready —— 外部工具轮询它判断"可以开始采了"
 *   trace_hook_mark()    主循环打点 —— 配合 DWT 周期计数定位时间轴
 *   trace_etm_off()      明确把 ETM/TPIU 关掉（保证"固件不碰 ETM"这件事可验证）
 *
 * H7B0 的 trace 引脚（外部工具配置时会用到）：
 *   PE2 = TRACECLK，PE3~PE6 = TRACED0~D3，AF0，推挽，无上下拉。
 *
 * 为什么这么设计：TRACECLK 在 280MHz CPU 档下最高 70MHz，固件里开 ETM 会让
 * "谁配置了 trace"变得不可证；放外部配置后，网页工具能回读 ETM 寄存器做校验。
 */
#ifndef __TRACE_HOOKS_H
#define __TRACE_HOOKS_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* 诊断变量（链接到 DTCM 固定地址，外部工具按地址直接读） */
typedef struct {
    uint32_t magic;      /* 'T','R','C','4' —— 4 = ETMv4 平台 */
    uint32_t sysclk_hz;  /* 实测 SYSCLK */
    uint32_t ready;      /* trace_hook_ready() 置 1 */
    uint32_t loops;      /* 主循环轮数 */
    uint32_t marks;      /* trace_hook_mark() 调用次数 */
    uint32_t cyc_last;   /* 最近一次打点的 DWT 周期数 */
    uint32_t cyc_delta;  /* 与上一次打点的周期差 */
    uint32_t usb_rx;     /* CDC 收到的字节数 */
    uint32_t usb_tx;     /* CDC 发出的字节数 */
    uint32_t etm_touched;/* 固件是否动过 ETM/TPIU 寄存器（恒为 0 = 没碰，可验证） */
} trace_diag_t;

extern volatile trace_diag_t g_trace;

void trace_hook_init(void);
void trace_hook_entry(void);
void trace_hook_ready(void);
void trace_hook_mark(void);

/* 明确关掉 ETM/TPIU（幂等）：保证"固件不配 trace"这一点可被外部工具回读验证 */
void trace_etm_off(void);

/* 读 DWT 周期计数（Cortex-M7 支持 DWT_CYCCNT） */
static inline uint32_t trace_hook_cycles(void)
{
    return *(volatile uint32_t *)0xE0001004u;
}

/* trace 相关输出（SEGGER RTT），也给 CherryUSB 的 CONFIG_USB_PRINTF 用 */
int trace_log_printf(const char *fmt, ...);

#ifdef __cplusplus
}
#endif

#endif /* __TRACE_HOOKS_H */
