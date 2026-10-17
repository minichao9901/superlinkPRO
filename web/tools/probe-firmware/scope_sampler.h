/* SPDX-License-Identifier: Apache-2.0 */
/* Copyright (c) 2026 akaInstruments */

#ifndef __SCOPE_SAMPLER_H__
#define __SCOPE_SAMPLER_H__

#include <stdint.h>

/* 探针侧 HSS 采样（给网页的「J-Scope 波形」页用）。
 *
 * 目标固件**一行都不用改**：探针自己按你设的周期，用 SWD 去读目标 RAM 里那 1~8 个
 * 变量（地址从目标 .elf 的 DWARF 里来），组好 512 B 的自描述包，从 **interface 0 上
 * 那个原本闲置的 bulk IN 端点 0x83**（SWO 端点，SWO_STREAM=0 所以一直没人用）推给主机。
 *
 * 与 RTT 桥的关系：**互斥**。两者都要独占 SWD，而且都靠主循环跑（绝不在 USB 中断里碰 SWD）。
 * 采样期间如果主机还在发 CMSIS-DAP 命令，默认「让路一拍」（见 SCOPE_FLAG_NO_YIELD）。
 *
 * 协议与网页侧实现：见本仓库 docs/scope-page.md（HID 0x32 控制面 + 0x83 数据面）。
 */

#define SCOPE_MAX_VARS   8U     /* 8 个变量正好装进一条 63 B 的 HID 配置报文 */
#define SCOPE_PACKET     512U   /* HS bulk 的 wMaxPacketSize（也是 DAP_PACKET_SIZE） */
#define SCOPE_TX_BUFS    4U     /* 在飞的包缓冲数；用完就丢包并计数 */

/* flags（HID 0x32 action 7 的第 5 字节） */
#define SCOPE_FLAG_ALLOW_60M 0x01U  /* 允许 60 MHz 档（长跑不稳，默认只到 45 MHz） */
#define SCOPE_FLAG_DISCARD   0x02U  /* 丢弃模式：照常采样但不推 USB（排障用，隔离 SWD 与 USB） */
#define SCOPE_FLAG_TRIGGER   0x04U  /* 探针侧触发（v2；当前主机侧触发已够用） */
#define SCOPE_FLAG_NO_YIELD  0x08U  /* 不让路：独占链路，周期更稳，但会打断并发的调试会话 */

typedef struct
{
    uint32_t addr;      /* 目标 RAM 地址 */
    uint8_t  size;      /* 1 / 2 / 4 / 8 */
    uint8_t  type;      /* 0=u8 1=i8 2=u16 3=i16 4=u32 5=i32 6=f32 7=f64（与网页同一张表） */
    uint16_t rsv;
} scope_var_t;

/* HID 0x32 action 7：周期 + 变量表。变量按地址排序由本模块自己做（主机只管给表）。 */
void scope_sampler_configure(uint32_t period_us, uint8_t flags, uint8_t nvars, const scope_var_t *vars);

/* HID 0x32 action 3：设 SWD 时钟（Hz，走 RTT 桥那套斜坡换挡）。0 = 不动。 */
void scope_sampler_set_clock(uint32_t hz);

/* HID 0x32 action 1：**只登记请求**，真正的 SWD 初始化发生在主循环里（见 scope_sampler_poll）。 */
void scope_sampler_request_start(void);

/* 最近一次启动的返回码：0 正常 / -1 SWJ_Clock / -2 SWD 初始化 / -3 变量表为空 /
 * -4 该档链路不可用 / -100 = 排队中（哨兵值，不是错误 —— 与 RTT 桥同一个约定） */
int  scope_sampler_start_result(void);

/* HID 0x32 action 0 */
void scope_sampler_stop(void);

/* 主循环里调（紧跟 rtt_bridge_poll()）。做三件事：处理排队的启动/标定请求、
 * 到点就采一拍、把满包推给 USB。 */
void scope_sampler_poll(void);

/* 状态字（12 个 u32，位域见 docs/scope-page.md §7.1）。返回写入的字数。 */
uint32_t scope_sampler_status(uint32_t *out, uint32_t words);

/* HID 0x32 action 8/9：用当前计划空跑 iters 次，回报 MCHTMR ticks（24 MHz）
 * —— 这就是「M0 标定」：拿到真实的 µs/样本，而不是模型估算。 */
void scope_sampler_request_bench(uint32_t iters);
int  scope_sampler_bench_result(uint32_t *iters, uint32_t *ticks, int32_t *err);

/* USB 完成回调（在 swo_in_callback 里调）：还回一个包缓冲。 */
void scope_sampler_tx_complete(void);

int      scope_sampler_is_running(void);
uint32_t scope_sampler_plan_hash(void);   /* 与网页 planHash 同一算法，用来对账"配置生效了吗" */

#endif /* __SCOPE_SAMPLER_H__ */
