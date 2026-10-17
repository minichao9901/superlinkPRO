/**
 * @file    swd_host_port.c
 * @brief   swd_host.c（ARM DAPLink 官方）在 akaLinkPro / HPM5301EVKLite 上的平台胶水
 *
 * 官方 swd_host.c 只依赖 4 个平台符号：
 *   swd_init() 里的 DAP_Setup()/PORT_SWD_SETUP()/DAP_Data —— akaLinkPro 的 DAP.c 已提供；
 *   SWJ_Sequence()                                      —— akaLinkPro 的 SW_DP.c 已提供；
 *   SWD_Transfer()                                      —— 本文件提供（分派到 SWD_Read/SWD_Write）；
 *   swd_set_target_reset()/clock_cpu_delay_ms()          —— 本文件提供 / HPM SDK 提供。
 *
 * SPDX-License-Identifier: Apache-2.0
 */

/* DAP_config.h 必须最先包含：DAP.h 依赖它提供的 __STATIC_FORCEINLINE、
 * DAP_SWD/DAP_JTAG 等开关。 */
#include "DAP_config.h"
#include "hpm_common.h"
#include "hpm_interrupt.h" /* disable_global_irq / restore_global_irq */
#include "DAP.h"
#include "swd_host.h"

/**
 * @brief DAPLink 的 SWD 单次传输入口
 *
 * request 编码与 akaLinkPro 的 SWD_Read/SWD_Write 完全一致：
 *   bit0     APnDP
 *   bit1     RnW（1 = 读）
 *   bit3:2   A[3:2]
 * 因此直接按 RnW 分派即可 —— 底层走的就是 DAP.c 主机通路同款的
 * “按速度预编译 bit-bang blob”（SWD_DynamicLoad_*），时序与主机通路一致。
 *
 * @param request A[3:2] | RnW | APnDP
 * @param data    读：出参（4 字节）；写：入参（4 字节）；允许 NULL（dummy read）
 * @return ACK[2:0]（0x01 = OK，0x02 = WAIT，0x04 = FAULT）
 */
uint8_t SWD_Transfer(uint32_t request, uint32_t *data)
{
    uint32_t dummy = 0;
    uint8_t ack;
    uint32_t level;

    if (data == NULL) {
        /* DAPLink 会用 NULL 做 dummy read（例如清 AP 读流水线的 RDBUFF 读），
         * 不能把空指针交给 blob 的读函数。 */
        data = &dummy;
    }

    /* 临界区：这一趟传输是纯 bit-bang 时序循环，中途被任何 ISR 打断都会把采样点
     * 推后、读回错误的 ACK。原来只有在目标运行 + CDC 持续搬运（USB ISR 频繁）时
     * 才会暴露：20 MHz（位周期 50 ns）扛得住，36 MHz（28 ns）开始逐轮随机失败。
     * 一个字约 1.1 µs，两个 CSR 操作的开销 ~2%，换掉高频档的随机性很值。
     * 注意：本函数也可能在 USB ISR 上下文里被 DAP 命令通路调用，此时 MIE 本来就是
     * 0，restore 传回的旧值也是 0，不会误开中断。 */
    level = disable_global_irq(CSR_MSTATUS_MIE_MASK);

    if ((request & 0x02U) != 0U) {
        ack = SWD_Read(request, data);
    } else {
        ack = SWD_Write(request, data);
    }

    restore_global_irq(level);
    return ack;
}

/**
 * @brief 控制目标 nRESET（官方实现在 target_reset.c，此处用 DAP_config.h 的引脚宏）
 * @param asserted 0 = 释放复位，非 0 = 拉低 nRESET
 */
void swd_set_target_reset(uint8_t asserted)
{
    PIN_nRESET_OUT(asserted ? 0U : 1U);
}
