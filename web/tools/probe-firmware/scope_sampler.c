/* SPDX-License-Identifier: Apache-2.0 */
/* Copyright (c) 2026 akaInstruments */

/* 探针侧 HSS 采样器 —— 骨架、仲裁方式、时钟换挡全部照 src/rtt/rtt_bridge.c：
 *
 *   · SWD 访问**只在主循环里做**（scope_sampler_poll），绝不在 USB 中断里碰；
 *   · 复用桥的 SWD 原语（rtt_bridge_swd_ensure_ready / rtt_bridge_read）——
 *     那条路径里有一堆实测换来的细节（4 MHz 起手、20 MHz 斜坡、清 sticky、失败降档），
 *     两处各写一份迟早改漏一处。给桥加的三个 adapter 见 patch-notes.md §1。
 *   · 目标是「一次采样 = 一次或几次块读」：主机把变量按地址合并成 span（网页的
 *     planReads），这里对每个 span 调一次 swd_read_memory()。**别对每个变量各读一次** ——
 *     每次块读有 5 次传输的固定开销（CSW/TAR/prime/RDBUFF），散着读会慢 3~4 倍。
 *
 * 数据面：512 B 自描述包（16 B 头 + 496 B 载荷），DATA 包每包放 floor(496/frame_bytes) 个样本。
 * 包结构、类型编码、状态字位域与网页 app/scope/protocol.js 一一对应，改一边必须改另一边。
 *
 * ⚠️ 本文件是**补丁草稿**：在真机上编译/验证前不要当成已验证的东西。
 *    验证步骤见 tools/probe-firmware/README.md。
 */

#include <string.h>

#include "board.h"
#include "hpm_common.h"
#include "usb_composite.h"   /* usbd_ep_start_write / SWO_IN_EP / ATTR_PLACE_AT_NONCACHEABLE_BSS_* */
#include "rtt_bridge.h"      /* 复用的 SWD 原语（patch-notes.md §1） */
#include "scope_sampler.h"

/* ------------------------------------------------------------------ 常量 */

#define SCOPE_MCHTMR_HZ   24000000UL          /* MCHTMR = osc24m（与 rtt_bridge.c 一致） */
#define SCOPE_HDR         16U
#define SCOPE_PAYLOAD     (SCOPE_PACKET - SCOPE_HDR)   /* 496 */
#define SCOPE_MAGIC       0x4A53U             /* 'J','S'（小端下发：53 4A） */
#define SCOPE_VER         1U
#define SCOPE_KIND_DEF    1U
#define SCOPE_KIND_DATA   2U
#define SCOPE_KIND_STAT   3U
#define SCOPE_KIND_EVT    4U

#define SCOPE_MIN_PERIOD_US    5U             /* 比 5 µs 更快没有意义（一次读都做不完） */
#define SCOPE_MAX_PERIOD_US    1000000UL
#define SCOPE_MERGE_GAP        19U            /* 合并阈值：与网页 COST 模型同源（5.6 µs / 0.284 µs·B⁻¹） */
#define SCOPE_SPAN_MAX         256U           /* 单个 span 的读缓冲上限（8 个变量最多 ~200 B） */
#define SCOPE_FRAME_MAX        (SCOPE_MAX_VARS * 8U)   /* 一帧最多 64 B */
#define SCOPE_STAT_EVERY       64U            /* 每多少个包插一个 STAT */
#define SCOPE_YIELD_TICKS      (SCOPE_MCHTMR_HZ / 50U) /* 20 ms 内有 DAP 活动就让路一拍 */
#define SCOPE_BENCH_MAX_ITERS  100000UL

/* ------------------------------------------------------------------ 状态 */

static volatile uint8_t s_running;
static volatile uint8_t s_start_req;
static volatile int8_t  s_start_rc = -100;    /* -100 = 还没启动过（与 RTT 桥同一哨兵） */
static uint8_t  s_swd_ready;

static scope_var_t s_var[SCOPE_MAX_VARS];     /* 按地址升序 */
static uint8_t  s_nvars;
static uint16_t s_frame_bytes;
static uint8_t  s_per_packet;                 /* floor(496 / frame_bytes) */
static uint16_t s_frame_off[SCOPE_MAX_VARS];  /* 每个变量在帧内的偏移 */
static uint32_t s_period_us = 100U;
static uint8_t  s_flags;

typedef struct { uint32_t start; uint16_t len; uint8_t first; uint8_t count; } scope_span_t;
static scope_span_t s_span[SCOPE_MAX_VARS];
static uint8_t  s_nspans;

static uint8_t  s_stage[SCOPE_SPAN_MAX];      /* 一次 span 的读缓冲 */
static uint8_t  s_frame[SCOPE_FRAME_MAX];     /* 一帧的像素（按变量表顺序紧排） */

/* 🚨 给 USB DMA 用的缓冲必须放在**非 cacheable** 段，否则 CPU 写进 D-cache
 *    而 DMA 直接读内存 → 主机收到的是旧数据（或全 0）。uart_rx_buf 也是这么放的。 */
ATTR_PLACE_AT_NONCACHEABLE_BSS_WITH_ALIGNMENT(4)
static uint8_t s_pkt[SCOPE_TX_BUFS][SCOPE_PACKET];
static volatile uint8_t s_tx_busy[SCOPE_TX_BUFS];
static uint8_t  s_inflight[SCOPE_TX_BUFS];    /* 在飞顺序（FIFO）：回调不带下标，只能按序还 */
static uint8_t  s_if_head, s_if_count;
static uint8_t  s_fill_buf;                   /* 正在填的缓冲；0xFF = 还没分配 */
static uint16_t s_fill_n;                     /* 已经填了几个样本 */
static uint32_t s_fill_t0;                    /* 本包第一个样本的 t_us */

static uint32_t s_seq;
static uint32_t s_t_us;                       /* 名义时间轴（µs）：每拍 +period，跳拍照样推进 */
static uint32_t s_next_tick;                  /* 下一次该采样的 MCHTMR 时刻 */

static uint32_t s_produced;                   /* 采到的样本数（含没推出去的） */
static uint32_t s_dropped;                    /* 追不上而跳掉的拍数（= 丢的样本数） */
static uint32_t s_usb_drop;                   /* 因为没空闲包缓冲而丢掉的样本数 */
static uint32_t s_swd_err;                    /* 读失败次数（那一拍作废） */
static uint32_t s_yield;                      /* 给 DAP 让路的次数 */
static uint32_t s_pkts;                       /* 推出去的包数 */
static uint32_t s_bytes;                      /* 推出去的字节数 */
static uint32_t s_discard_drops;
static uint32_t s_last_cmd, s_last_rsp;
static uint32_t s_clock_hz;

static uint32_t s_bench_req, s_bench_valid, s_bench_iters, s_bench_ticks;
static int32_t  s_bench_err;
static uint32_t s_hdr_t;                      /* 正在组的那一包的时间戳（DATA 用首样本时刻） */

static void scope_sampler_push_stat(void);    /* 前向声明（push_packet 里要用） */

/* MCHTMR 低 32 位（24 MHz → 约 179 s 绕回一次；这里只用来算"到点没有"，
 * 差值运算天然对绕回安全） */
static uint32_t mchtmr_now(void)
{
    return *(volatile uint32_t *)(HPM_MCHTMR_BASE + 0x00);
}

static uint32_t us_to_ticks(uint32_t us)
{
    return (uint32_t)(((uint64_t)us * SCOPE_MCHTMR_HZ) / 1000000ULL);
}

/* ------------------------------------------------------------------ 计划 */

static void put16(uint8_t *p, uint16_t v) { p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); }
static void put32(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); p[2] = (uint8_t)(v >> 16); p[3] = (uint8_t)(v >> 24);
}

/* 排一次读计划：变量按地址排序 → 间隙 ≤ SCOPE_MERGE_GAP 的并进同一个 span。
 * 与网页 planReads() 用同一条规则，所以两边的 span 数应当一致（DEF 包里回报 spans，
 * 主机拿它跟自己的估算对账；不一致就是有一边的合并规则被改动了）。 */
static void scope_make_plan(void)
{
    /* 插入排序：只有 ≤8 个元素，别为它引 qsort */
    for (uint8_t i = 1; i < s_nvars; i++)
    {
        scope_var_t v = s_var[i];
        int8_t j = (int8_t)i - 1;
        while (j >= 0 && s_var[j].addr > v.addr) { s_var[j + 1] = s_var[j]; j--; }
        s_var[j + 1] = v;
    }
    /* 帧内偏移按**排序后**的顺序累加（主机解码也按这个顺序） */
    uint16_t off = 0U;
    for (uint8_t i = 0; i < s_nvars; i++)
    {
        s_frame_off[i] = off;
        off = (uint16_t)(off + s_var[i].size);
    }
    s_frame_bytes = off;
    s_per_packet = (uint8_t)((s_frame_bytes > 0U) ? (SCOPE_PAYLOAD / s_frame_bytes) : 0U);
    if (s_per_packet == 0U) { s_per_packet = 1U; }

    s_nspans = 0U;
    for (uint8_t i = 0; i < s_nvars; i++)
    {
        uint32_t end = s_var[i].addr + s_var[i].size;
        if ((s_nspans > 0U) &&
            ((s_var[i].addr - (s_span[s_nspans - 1U].start + s_span[s_nspans - 1U].len)) <= SCOPE_MERGE_GAP) &&
            ((end - s_span[s_nspans - 1U].start) <= SCOPE_SPAN_MAX))
        {
            scope_span_t *sp = &s_span[s_nspans - 1U];
            sp->len = (uint16_t)(end - sp->start);
            sp->count++;
        }
        else
        {
            scope_span_t *sp = &s_span[s_nspans];
            sp->start = s_var[i].addr;
            sp->len = (uint16_t)s_var[i].size;
            sp->first = i;
            sp->count = 1U;
            s_nspans++;
        }
    }
}

uint32_t scope_sampler_plan_hash(void)
{
    /* FNV-1a，与网页 app/scope/protocol.js 的 planHash() 逐字节一致 */
    uint32_t h = 0x811C9DC5UL;
    for (uint8_t i = 0; i < s_nvars; i++)
    {
        const uint8_t bytes[6] = {
            (uint8_t)(s_var[i].addr), (uint8_t)(s_var[i].addr >> 8),
            (uint8_t)(s_var[i].addr >> 16), (uint8_t)(s_var[i].addr >> 24),
            s_var[i].size, s_var[i].type,
        };
        for (uint8_t k = 0; k < 6U; k++) { h ^= bytes[k]; h *= 0x01000193UL; }
    }
    return h;
}

/* ------------------------------------------------------------------ 组包 */

static uint8_t *scope_pkt_header(uint8_t kind, uint16_t n, uint16_t aux)
{
    uint8_t *p = s_pkt[s_fill_buf];
    memset(p, 0, SCOPE_PACKET);
    put16(p, SCOPE_MAGIC);
    p[2] = SCOPE_VER;
    p[3] = kind;
    put32(p + 4U, s_seq);
    put32(p + 8U, s_hdr_t);          /* 🚨 DATA 包这里是**首个**样本的时刻（主机按它建时间轴） */
    put16(p + 12U, n);
    put16(p + 14U, aux);
    return p;
}

/* 还回一个空闲缓冲；没有就返回 0xFF。
 * 🚨 必须跳过**正在填的那个**：它在推出去之前不算 busy，否则会把自己交出去覆盖掉。 */
static uint8_t scope_alloc_buf(void)
{
    for (uint8_t i = 0; i < SCOPE_TX_BUFS; i++)
    {
        if (!s_tx_busy[i] && (i != s_fill_buf)) return i;
    }
    return 0xFFU;
}

/* 把当前填满的包交给 USB */
static void scope_push_packet(void)
{
    s_hdr_t = s_fill_t0;                       /* DATA 包的时间戳 = 首样本时刻 */
    (void)scope_pkt_header(SCOPE_KIND_DATA, s_fill_n, (uint16_t)(s_fill_n * s_frame_bytes));
    s_seq++;
    s_pkts++;
    s_bytes += SCOPE_PACKET;

    if (s_flags & SCOPE_FLAG_DISCARD)
    {
        s_discard_drops++;
    }
    else
    {
        s_tx_busy[s_fill_buf] = 1U;
        s_inflight[(s_if_head + s_if_count) % SCOPE_TX_BUFS] = s_fill_buf;
        s_if_count++;
        usbd_ep_start_write(0, SWO_IN_EP, s_pkt[s_fill_buf], SCOPE_PACKET);
    }

    /* 下一包 */
    s_fill_buf = scope_alloc_buf();
    s_fill_n = 0U;
    if (s_fill_buf == 0xFFU)
    {
        /* 缓冲用光了（主机没在读？）：这一包不要了，计数上报，然后复用 0 号继续。
         * 🚨 只丢当前包、**不清空 seq** —— 主机按 seq 缺口 + STAT 的 usb_err 就能看出丢了多少。 */
        s_usb_drop += s_per_packet;
        s_fill_buf = 0U;
    }

    /* STAT 每 N 包插一个（紧跟在 DATA 之后，包序里是连续的） */
    if ((s_pkts % SCOPE_STAT_EVERY) == 0U)
    {
        scope_sampler_push_stat();
    }
}

/* DEF：变量表（主机先拿它建类型表，再按顺序解码 DATA） */
static void scope_push_def(void)
{
    uint8_t save = s_fill_buf;
    uint8_t buf = scope_alloc_buf();
    if (buf == 0xFFU) return;
    s_fill_buf = buf;
    s_hdr_t = s_t_us;
    uint8_t *p = scope_pkt_header(SCOPE_KIND_DEF, 0U, s_nvars);
    uint8_t *q = p + SCOPE_HDR;
    put32(q, s_clock_hz);
    put32(q + 4U, s_period_us);
    put16(q + 8U, s_flags);
    q[10] = s_nvars;
    q[11] = s_nspans;                     /* 主机拿它跟自己的计划对账 */
    uint8_t *e = q + 12U;
    for (uint8_t i = 0; i < s_nvars; i++)
    {
        put32(e, s_var[i].addr);
        e[4] = s_var[i].size;
        e[5] = s_var[i].type;
        e += 8U;
    }
    s_seq++;
    s_pkts++;
    s_bytes += SCOPE_PACKET;
    if (!(s_flags & SCOPE_FLAG_DISCARD))
    {
        s_tx_busy[buf] = 1U;
        s_inflight[(s_if_head + s_if_count) % SCOPE_TX_BUFS] = buf;
        s_if_count++;
        usbd_ep_start_write(0, SWO_IN_EP, s_pkt[buf], SCOPE_PACKET);
    }
    s_fill_buf = save;
}

/* 让 scope_push_packet() 里能调到（static 前向声明在上面） */
static void scope_sampler_push_stat(void)
{
    uint8_t save = s_fill_buf;
    uint8_t buf = scope_alloc_buf();
    if (buf == 0xFFU) return;
    s_fill_buf = buf;
    s_hdr_t = s_t_us;
    uint8_t *p = scope_pkt_header(SCOPE_KIND_STAT, 0U, 0U);
    uint8_t *q = p + SCOPE_HDR;
    put32(q, s_produced);
    put32(q + 4U, s_dropped + s_usb_drop);
    put32(q + 8U, s_pkts);
    put16(q + 12U, (uint16_t)(s_usb_drop & 0xFFFFU));
    put16(q + 14U, (uint16_t)(s_swd_err & 0xFFFFU));
    put32(q + 16U, s_period_us);
    q[20] = (uint8_t)(s_clock_hz / 1000000UL);   /* 当前 SWD 档位（MHz） */
    q[21] = (uint8_t)((s_flags & SCOPE_FLAG_DISCARD) ? 1U : 0U);
    s_seq++;
    s_pkts++;
    s_bytes += SCOPE_PACKET;
    if (!(s_flags & SCOPE_FLAG_DISCARD))
    {
        s_tx_busy[buf] = 1U;
        s_inflight[(s_if_head + s_if_count) % SCOPE_TX_BUFS] = buf;
        s_if_count++;
        usbd_ep_start_write(0, SWO_IN_EP, s_pkt[buf], SCOPE_PACKET);
    }
    s_fill_buf = save;
}

/* ------------------------------------------------------------------ 采样 */

/* 采一拍：按 span 块读，把每个变量的字节搬进帧里。
 * 返回 0 = 成功；-1 = 某个 span 读失败（这一拍作废，计入 swd_err）。 */
static int scope_sample_once(void)
{
    for (uint8_t sp = 0; sp < s_nspans; sp++)
    {
        scope_span_t *s = &s_span[sp];
        if (rtt_bridge_read(s->start, s_stage, s->len) != 0)
        {
            return -1;
        }
        for (uint8_t k = 0; k < s->count; k++)
        {
            uint8_t vi = (uint8_t)(s->first + k);
            uint16_t src = (uint16_t)(s_var[vi].addr - s->start);
            memcpy(&s_frame[s_frame_off[vi]], &s_stage[src], s_var[vi].size);
        }
    }

    if (s_fill_n == 0U) { s_fill_t0 = s_t_us; }
    memcpy(&s_pkt[s_fill_buf][SCOPE_HDR + (uint16_t)s_fill_n * s_frame_bytes], s_frame, s_frame_bytes);
    s_fill_n++;
    s_produced++;
    s_t_us += s_period_us;                 /* 名义时间轴：跳拍也要推进，否则主机的轴会压缩 */

    if (s_fill_n >= s_per_packet) { scope_push_packet(); }
    return 0;
}

static int scope_start_now(void)
{
    if (s_nvars == 0U || s_frame_bytes == 0U) return -3;
    int rc = rtt_bridge_swd_ensure_ready();      /* 复用桥的 SWD 初始化（含斜坡换挡） */
    if (rc != 0) { s_swd_ready = 0U; return rc; }
    s_swd_ready = 1U;
    scope_make_plan();

    s_seq = 0U; s_t_us = 0U; s_produced = 0U; s_dropped = 0U; s_usb_drop = 0U;
    s_swd_err = 0U; s_yield = 0U; s_pkts = 0U; s_bytes = 0U; s_discard_drops = 0U;
    s_if_head = 0U; s_if_count = 0U;
    memset((void *)s_tx_busy, 0, sizeof(s_tx_busy));
    s_fill_buf = scope_alloc_buf();
    s_fill_n = 0U;
    s_fill_t0 = 0U;

    scope_push_def();                            /* 先发变量表 */
    s_next_tick = mchtmr_now() + us_to_ticks(s_period_us);
    s_running = 1U;
    return 0;
}

void scope_sampler_poll(void)
{
    if (s_start_req)
    {
        s_start_req = 0U;
        s_start_rc = (int8_t)scope_start_now();
    }
    if (s_bench_req)
    {
        s_bench_req = 0U;
        uint32_t t0 = mchtmr_now();
        int32_t err = 0;
        if (s_nspans == 0U) { err = -3; }
        for (uint32_t i = 0; (err == 0) && (i < s_bench_iters); i++)
        {
            for (uint8_t sp = 0; sp < s_nspans; sp++)
            {
                if (rtt_bridge_read(s_span[sp].start, s_stage, s_span[sp].len) != 0) { err = -4; break; }
            }
        }
        s_bench_ticks = mchtmr_now() - t0;
        s_bench_err = err;
        s_bench_valid = 1U;
    }

    if (!s_running) return;

    uint32_t now = mchtmr_now();
    if ((int32_t)(now - s_next_tick) < 0) return;            /* 还没到点 */

    /* 让路：最近 SCOPE_YIELD_TICKS 内有 DAP 命令 → 跳过这一拍（周期会豁一个口，
     * 但不会把正在调试的会话打断）。要绝对稳的周期就设 SCOPE_FLAG_NO_YIELD。 */
    if (!(s_flags & SCOPE_FLAG_NO_YIELD))
    {
        uint32_t last_dap = rtt_bridge_last_dap_ticks();
        if ((last_dap != 0U) && ((uint32_t)(now - last_dap) < SCOPE_YIELD_TICKS))
        {
            s_yield++;
            s_next_tick = now + us_to_ticks(s_period_us);
            return;
        }
    }

    if (scope_sample_once() != 0)
    {
        s_swd_err++;
        s_next_tick = now + us_to_ticks(s_period_us);
        return;
    }

    /* 追不上就跳拍：把 next_tick 一次性推到将来，并把跳过的拍数计入 dropped。
     * 🚨 必须计数 —— 界面上"丢样本"就是从这里来的，绝不静默。 */
    uint32_t period_ticks = us_to_ticks(s_period_us);
    if (period_ticks == 0U) { period_ticks = 1U; }
    s_next_tick += period_ticks;
    if ((int32_t)(now - s_next_tick) > 0)
    {
        uint32_t behind = (uint32_t)(now - s_next_tick) / period_ticks + 1U;
        s_next_tick += behind * period_ticks;
        s_dropped += behind;
        s_t_us += behind * s_period_us;
    }
}

/* ------------------------------------------------------------------ 控制面 */

void scope_sampler_configure(uint32_t period_us, uint8_t flags, uint8_t nvars, const scope_var_t *vars)
{
    if (nvars > SCOPE_MAX_VARS) { nvars = SCOPE_MAX_VARS; }
    if (period_us < SCOPE_MIN_PERIOD_US) { period_us = SCOPE_MIN_PERIOD_US; }
    if (period_us > SCOPE_MAX_PERIOD_US) { period_us = SCOPE_MAX_PERIOD_US; }
    s_period_us = period_us;
    s_flags = flags;
    s_nvars = nvars;
    for (uint8_t i = 0; i < nvars; i++) { s_var[i] = vars[i]; }
    if (s_running)
    {
        /* 运行中改配置：停掉再等主机启动 —— 免得半新半旧地跑（周期/变量表混用） */
        s_running = 0U;
        s_start_rc = -100;
    }
    s_last_cmd = 7U;
}

void scope_sampler_set_clock(uint32_t hz)
{
    if (hz == 0U) return;
    (void)rtt_bridge_set_swd_clock(hz);
    s_clock_hz = rtt_bridge_swd_clock_hz();
    s_last_cmd = 3U;
}

void scope_sampler_request_start(void)
{
    s_start_req = 1U;
    s_start_rc = -100;                 /* 排队中：与 RTT 桥的约定一致 */
}

int scope_sampler_start_result(void)
{
    return (int)s_start_rc;
}

void scope_sampler_stop(void)
{
    s_running = 0U;
    s_start_req = 0U;
    s_start_rc = -100;
    s_last_cmd = 0U;
}

int scope_sampler_is_running(void)
{
    return (int)s_running;
}

void scope_sampler_request_bench(uint32_t iters)
{
    if (iters == 0U) { iters = 1000U; }
    if (iters > SCOPE_BENCH_MAX_ITERS) { iters = SCOPE_BENCH_MAX_ITERS; }
    s_bench_iters = iters;
    s_bench_valid = 0U;
    s_bench_req = 1U;
}

int scope_sampler_bench_result(uint32_t *iters, uint32_t *ticks, int32_t *err)
{
    if (!s_bench_valid) return 0;
    if (iters) { *iters = s_bench_iters; }
    if (ticks) { *ticks = s_bench_ticks; }
    if (err)   { *err = s_bench_err; }
    return 1;
}

void scope_sampler_tx_complete(void)
{
    if (s_if_count == 0U) return;                  /* 丢弃模式下没有在飞的包 */
    uint8_t idx = s_inflight[s_if_head];
    s_if_head = (uint8_t)((s_if_head + 1U) % SCOPE_TX_BUFS);
    s_if_count--;
    if (idx < SCOPE_TX_BUFS) { s_tx_busy[idx] = 0U; }
}

/* 12 个状态字，位域见 docs/scope-page.md §7.1（网页 parseScopeStatus 按同一张表解） */
uint32_t scope_sampler_status(uint32_t *out, uint32_t words)
{
    if (words < 12U) { return 0U; }
    out[0] = (uint32_t)(s_running ? 1U : 0U) |
             ((uint32_t)s_nspans << 8) |
             ((uint32_t)(s_swd_ready ? 1U : 0U) << 16) |
             ((uint32_t)s_nvars << 24);
    out[1] = s_clock_hz;
    out[2] = s_produced;
    out[3] = s_dropped + s_usb_drop;
    out[4] = (s_bytes & 0xFFFFU) | ((s_usb_drop & 0xFFFFU) << 16);
    out[5] = (s_swd_err & 0xFFFFU) | ((s_yield & 0xFFFFU) << 16);
    out[6] = s_seq;
    out[7] = (s_dropped & 0xFFFFU) | ((s_discard_drops & 0xFFFFU) << 16);
    out[8] = scope_sampler_plan_hash();
    out[9] = (s_last_cmd & 0xFFU) | ((s_last_rsp & 0xFFU) << 8);
    out[10] = (uint32_t)(int32_t)s_start_rc;
    out[11] = (s_period_us & 0xFFFFU) |
              ((uint32_t)((s_flags & SCOPE_FLAG_DISCARD) ? 1U : 0U) << 16) |
              ((uint32_t)(s_clock_hz / 1000000UL) << 24);
    return 12U;
}
