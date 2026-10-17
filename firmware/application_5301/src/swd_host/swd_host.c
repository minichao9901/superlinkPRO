/**
 * @file    swd_host.c
 * @brief   Implementation of swd_host.h
 *
 * DAPLink Interface Firmware
 * Copyright (c) 2009-2019, ARM Limited, All Rights Reserved
 * Copyright 2019, Cypress Semiconductor Corporation
 * or a subsidiary of Cypress Semiconductor Corporation.
 * SPDX-License-Identifier: Apache-2.0
 *
 * Licensed under the Apache License, Version 2.0 (the "License"); you may
 * not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 * http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
 * WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

/* ---------------------------------------------------------------------------
 * 本文件为 ARM DAPLink 官方 swd_host.c 的移植版（akaLinkPro / HPM5301EVKLite）。
 * 来源：E:\Share\github\MicroLink\MicroLink\microlink_app\src\swd_host\swd_host.c
 *      （ARM Limited, Apache-2.0；5301 同款芯片上已验证可用）
 *
 * 移植改动（仅裁掉与 SWD 访问无关的烧写/状态机部分，SWD 时序逻辑一字未改）：
 *   1. 去掉 #include "target_config.h"（连带 flash_blob.h / util.h 依赖链）；
 *   2. 裁掉 swd_flash_syscall_exec / swd_flash_syscall_verify_exec
 *      （依赖 program_syscall_t、flash_algo_return_t，属 flash 算法调用，RTT 桥用不到）；
 *   3. 裁掉 swd_set_target_state_hw / swd_set_target_state_sw / swd_wait_until_halted
 *      （依赖 target_state_t 与 target_family 状态机）；
 *   4. 新增 swd_host_port.c 提供 SWD_Transfer() / swd_set_target_reset() 两个平台胶水。
 *
 * 保留：swd_init / swd_init_debug / JTAG2SWD / swd_read_dp|ap|word|memory / 重试逻辑。
 * --------------------------------------------------------------------------- */
#ifndef TARGET_MCU_CORTEX_A
#include "DAP_config.h"
#include "swd_host.h"
#include "hpm_clock_drv.h"   /* clock_cpu_delay_ms() */
#include "stdio.h"

/* DAPLink 里本宏来自 flash_blob.h（已随 target_config.h 一起裁掉）；
 * 它是 AHB-AP 自动递增地址的页大小，Cortex-M 上恒为 1 KB。 */
#ifndef TARGET_AUTO_INCREMENT_PAGE_SIZE
#define TARGET_AUTO_INCREMENT_PAGE_SIZE (1024)
#endif
// Default NVIC and Core debug base addresses
// TODO: Read these addresses from ROM.



// AP CSW register, base value
#define CSW_VALUE (CSW_RESERVED | CSW_MSTRDBG | CSW_HPROT | CSW_DBGSTAT | CSW_SADDRINC)

/* 同上，但**地址不自增**。SWD 单字重复读的关键：AddrInc=1 时 DRW 读一次 AP.TAR
 * 就自己走到 addr+4，于是每次采样都得重写 TAR；换成不自增后 TAR 能一直复用，
 * 同一地址的连续读只剩 DRW + RDBUFF 两次传输（原来 3 次）。见 swd_read_word_held()。 */
#define CSW_VALUE_HOLD (CSW_VALUE & ~CSW_SADDRINC)

#define DCRDR 0xE000EDF8
#define DCRSR 0xE000EDF4
#define DHCSR 0xE000EDF0
#define REGWnR (1 << 16)

#define MAX_SWD_RETRY 100//10
#define MAX_TIMEOUT   1000000  // Timeout for syscalls on target

// Use the CMSIS-Core definition if available.
#if !defined(SCB_AIRCR_PRIGROUP_Pos)
#define SCB_AIRCR_PRIGROUP_Pos              8U                                            /*!< SCB AIRCR: PRIGROUP Position */
#define SCB_AIRCR_PRIGROUP_Msk             (7UL << SCB_AIRCR_PRIGROUP_Pos)                /*!< SCB AIRCR: PRIGROUP Mask */
#endif

typedef struct {
    uint32_t select;
    uint32_t csw;
    uint32_t tarp;      /* 我们上次写进 AP.TAR 的地址 */
    uint8_t  tarp_ok;   /* 1 = AP.TAR 现在确实还等于 tarp（没被自增或别的路径动过） */
} DAP_STATE;

typedef struct {
    uint32_t r[16];
    uint32_t xpsr;
} DEBUG_STATE;

static SWD_CONNECT_TYPE reset_connect = CONNECT_NORMAL;

static DAP_STATE dap_state;
static uint32_t  soft_reset = SYSRESETREQ;

/* 三个 AP/DP 影子寄存器的缓存全部作废（0xffffffff 是"未知"的约定值）。
 *
 * 🚨 主机自己碰过 DAP 之后必须调这个：CMSIS-DAP 主机通路走的是
 * DAP_SWD_Transfer → SWD_Read/SWD_Write，**完全绕过 swd_host**，所以我们这边
 * 记的 select / csw / tar 全都会失真。踩过的正是这一类：换了 SWD 档位却没生效、
 * 以及缓存说"CSW 还是自增"而硬件里已经被改成别的值。
 * 保守作废的代价只是多写一两次影子寄存器，判反了就是读到**别的地址**。 */
void swd_invalidate_ap_cache(void)
{
    dap_state.select  = 0xffffffff;
    dap_state.csw     = 0xffffffff;
    dap_state.tarp_ok = 0U;
}

static uint32_t swd_get_apsel(uint32_t adr)
{
    return adr & 0xff000000;
}

void swd_set_reset_connect(SWD_CONNECT_TYPE type)
{
    reset_connect = type;
}

void int2array(uint8_t *res, uint32_t data, uint8_t len)
{
    uint8_t i = 0;

    for (i = 0; i < len; i++) {
        res[i] = (data >> 8 * i) & 0xff;
    }
}

uint8_t swd_transfer_retry(uint32_t req, uint32_t *data)
{
    uint8_t i, ack;

    for (i = 0; i < MAX_SWD_RETRY; i++) {
        ack = SWD_Transfer(req, data);

        // if ack != WAIT
        if (ack != DAP_TRANSFER_WAIT) {
            return ack;
        }
    }

    return ack;
}

void swd_set_soft_reset(uint32_t soft_reset_type)
{
    soft_reset = soft_reset_type;
}

uint8_t swd_init(void)
{
    //TODO - DAP_Setup puts GPIO pins in a hi-z state which can
    //       cause problems on re-init.  This needs to be investigated
    //       and fixed.
    DAP_Setup();
    PORT_SWD_SETUP();
    DAP_Data.debug_port  = DAP_PORT_SWD;
    /* 初始化意味着目标/AP 可能已经被重置过，影子寄存器一律作废 */
    swd_invalidate_ap_cache();
    return 1;
}

uint8_t swd_off(void)
{
    PORT_OFF();
    swd_invalidate_ap_cache();
    return 1;
}

uint8_t swd_clear_errors(void)
{
    if (!swd_write_dp(DP_ABORT, STKCMPCLR | STKERRCLR | WDERRCLR | ORUNERRCLR)) {
        return 0;
    }
    return 1;
}

// Read debug port register.
uint8_t swd_read_dp(uint8_t adr, uint32_t *val)
{
    uint32_t tmp_in;
    uint8_t tmp_out[4];
    uint8_t ack;
    uint32_t tmp;
    tmp_in = SWD_REG_DP | SWD_REG_R | SWD_REG_ADR(adr);
    ack = swd_transfer_retry(tmp_in, (uint32_t *)tmp_out);
    *val = 0;
    tmp = tmp_out[3];
    *val |= (tmp << 24);
    tmp = tmp_out[2];
    *val |= (tmp << 16);
    tmp = tmp_out[1];
    *val |= (tmp << 8);
    tmp = tmp_out[0];
    *val |= (tmp << 0);
    return (ack == 0x01);
}

// Write debug port register
uint8_t swd_write_dp(uint8_t adr, uint32_t val)
{
    uint32_t req;
    uint8_t data[4];
    uint8_t ack;

    //check if the right bank is already selected
    if ((adr == DP_SELECT) && (dap_state.select == val)) {
        return 1;
    }

    req = SWD_REG_DP | SWD_REG_W | SWD_REG_ADR(adr);
    int2array(data, val, 4);
    ack = swd_transfer_retry(req, (uint32_t *)data);
    if ((ack == DAP_TRANSFER_OK) && (adr == DP_SELECT)) {
        dap_state.select = val;
    }
    return (ack == 0x01);
}

// Read access port register.
uint8_t swd_read_ap(uint32_t adr, uint32_t *val)
{
    uint8_t tmp_in, ack;
    uint8_t tmp_out[4];
    uint32_t tmp;
    uint32_t apsel = swd_get_apsel(adr);
    uint32_t bank_sel = adr & APBANKSEL;

    if (!swd_write_dp(DP_SELECT, apsel | bank_sel)) {
        return 0;
    }

    tmp_in = SWD_REG_AP | SWD_REG_R | SWD_REG_ADR(adr);
    // first dummy read
    swd_transfer_retry(tmp_in, (uint32_t *)tmp_out);
    ack = swd_transfer_retry(tmp_in, (uint32_t *)tmp_out);
    *val = 0;
    tmp = tmp_out[3];
    *val |= (tmp << 24);
    tmp = tmp_out[2];
    *val |= (tmp << 16);
    tmp = tmp_out[1];
    *val |= (tmp << 8);
    tmp = tmp_out[0];
    *val |= (tmp << 0);
    return (ack == 0x01);
}

// Write access port register
uint8_t swd_write_ap(uint32_t adr, uint32_t val)
{
    uint8_t data[4];
    uint8_t req, ack;
    uint32_t apsel = swd_get_apsel(adr);
    uint32_t bank_sel = adr & APBANKSEL;

    if (!swd_write_dp(DP_SELECT, apsel | bank_sel)) {
        return 0;
    }

    switch (adr) {
        case AP_CSW:
            if (dap_state.csw == val) {
                return 1;
            }

            dap_state.csw = val;
            break;

        case AP_TAR:
            /* 地址没变就把这一整趟省掉：AP 写是"写一次 + 一次 RDBUFF 收尾"，
             * 所以省下来的是**两次传输**。地址没变时 AP 里的值本来就一样。 */
            if (dap_state.tarp_ok && (dap_state.tarp == val)) {
                return 1;
            }

            dap_state.tarp = val;
            dap_state.tarp_ok = 1U;
            break;

        default:
            break;
    }

    req = SWD_REG_AP | SWD_REG_W | SWD_REG_ADR(adr);
    int2array(data, val, 4);

    if (swd_transfer_retry(req, (uint32_t *)data) != 0x01) {
        /* 这一趟没落地，硬件里还是旧值，可缓存在上面已经按"新值"记下了 ——
         * 不作废的话，下一次调用会拿"命中"的缓存跳过写入，而 AP.TAR 可能早被
         * 自增带跑，采样数据会静默错位（不报错、看着还正常）。 */
        swd_invalidate_ap_cache();
        return 0;
    }

    req = SWD_REG_DP | SWD_REG_R | SWD_REG_ADR(DP_RDBUFF);
    ack = swd_transfer_retry(req, NULL);
    if (ack != 0x01) {
        /* AP 写本身 ACK 了，但这笔 posted 写没能确认完成 —— 按"不确定"处理。 */
        swd_invalidate_ap_cache();
        return 0;
    }
    return 1;
}


// Write 32-bit word aligned values to target memory using address auto-increment.
// size is in bytes.
static uint8_t swd_write_block(uint32_t address, uint8_t *data, uint32_t size)
{
    uint8_t tmp_in[4], req;
    uint32_t size_in_words;
    uint32_t i, ack;

    if (size == 0) {
        return 0;
    }

    size_in_words = size / 4;

    // CSW register
    if (!swd_write_ap(AP_CSW, CSW_VALUE | CSW_SIZE32)) {
        return 0;
    }

    // TAR write
    req = SWD_REG_AP | SWD_REG_W | (1 << 2);
    int2array(tmp_in, address, 4);

    if (swd_transfer_retry(req, (uint32_t *)tmp_in) != 0x01) {
        return 0;
    }

    // DRW write
    req = SWD_REG_AP | SWD_REG_W | (3 << 2);

    /* 同上：写过 DRW 之后 AP.TAR 已被自增带跑（本轮 CSW 带 SADDRINC） */
    dap_state.tarp_ok = 0U;

    for (i = 0; i < size_in_words; i++) {
        if (swd_transfer_retry(req, (uint32_t *)data) != 0x01) {
            return 0;
        }

        data += 4;
    }

    // dummy read
    req = SWD_REG_DP | SWD_REG_R | SWD_REG_ADR(DP_RDBUFF);
    ack = swd_transfer_retry(req, NULL);
    return (ack == 0x01);
}

// Read 32-bit word aligned values from target memory using address auto-increment.
// size is in bytes.
static uint8_t swd_read_block(uint32_t address, uint8_t *data, uint32_t size)
{
    uint8_t tmp_in[4], req, ack;
    uint32_t size_in_words;
    uint32_t i;

    if (size == 0) {
        return 0;
    }

    size_in_words = size / 4;

    if (!swd_write_ap(AP_CSW, CSW_VALUE | CSW_SIZE32)) {
        return 0;
    }

    // TAR write
    req = SWD_REG_AP | SWD_REG_W | AP_TAR;
    int2array(tmp_in, address, 4);

    if (swd_transfer_retry(req, (uint32_t *)tmp_in) != DAP_TRANSFER_OK) {
        return 0;
    }

    // read data
    req = SWD_REG_AP | SWD_REG_R | AP_DRW;

    /* 发起 DRW 之前先把 TAR 缓存作废：本轮的 CSW 一定带 SADDRINC（上面刚写过），
     * 所以这一趟之后 AP.TAR 已经自己往前走了，缓存里的值不再成立。
     * 放在这里而不是循环里 —— 只要发起过 DRW，不管中途成败，TAR 都不可信。 */
    dap_state.tarp_ok = 0U;

    // initiate first read, data comes back in next read
    if (swd_transfer_retry(req, NULL) != 0x01) {
        return 0;
    }

    for (i = 0; i < (size_in_words - 1); i++) {
        if (swd_transfer_retry(req, (uint32_t *)data) != DAP_TRANSFER_OK) {
            return 0;
        }

        data += 4;
    }

    // read last word
    req = SWD_REG_DP | SWD_REG_R | SWD_REG_ADR(DP_RDBUFF);
    ack = swd_transfer_retry(req, (uint32_t *)data);
    return (ack == 0x01);
}

/* 对齐块读的快速路径（见 swd_host.h）：调用方保证 4 字节对齐、size 是 4 的倍数、
 * 不跨 1 KB 自增页 —— 于是直接进 swd_read_block，跳过 swd_read_memory() 的头尾字节
 * 处理与分页循环。scope 采样器每个样本要调 1~3 次，那两层的开销是量的出来的。 */
uint8_t swd_read_block4(uint32_t address, uint8_t *data, uint32_t size)
{
    if ((size == 0U) || ((address & 3U) != 0U) || ((size & 3U) != 0U))
    {
        return 0U;
    }
    if ((address & (TARGET_AUTO_INCREMENT_PAGE_SIZE - 1U)) + size > TARGET_AUTO_INCREMENT_PAGE_SIZE)
    {
        return 0U;      /* 跨页：让调用方退回 swd_read_memory() */
    }
    return swd_read_block(address, data, size);
}

/* ================= 单字"抱住地址"读：2 次传输 / 1 次传输 =================
 *
 * 背景：AHB-AP 的读是 **posted** 的 —— DRW 读回来的是"上一次 DRW 读"的结果，
 * 同时发起一次新的 AHB 读。所以同一地址连续读时：
 *
 *   · TAR 根本不用重写（前提是 CSW 里 AddrInc=0，见 CSW_VALUE_HOLD）；
 *   · 想要"当前值"，一次 DRW + 一次 RDBUFF 就够（swd_read_word_held 的老形态）；
 *   · 更快的是**把它当流水线**：每拍只发一次 DRW 读，它返回的是上一拍的值，
 *     调用方把那个值回填进上一拍自己的槽位 —— 每拍 **1 次传输**。
 *
 * 实测（F103ZE，60 MHz 档）：3 次 → 2 次把 4.503 µs 压到 2.681 µs；
 * 再上流水线只剩 1 次读，约 1.57 µs。
 *
 * ⚠️ 只能在**同一个地址**上连续读时用。地址一变（哪怕只是两个单字 span 交替），
 * 每次都要重写 TAR，而这里的 TAR 写走 swd_write_ap（写 + RDBUFF 收尾 = 2 次传输），
 * 比 swd_read_block 的裸 TAR 写（1 次）还贵 —— 那种配置反而更慢，实测两个远离的
 * 单字 span 从 8.6 µs 变成 11.2 µs。守卫必须是"**只有一个 span**"。
 * ======================================================================== */

/* 把 AP 准备成"抱住地址"的连续读：CSW 切成不自增、TAR 指向 address。
 * 已经是这个状态就是空操作（每拍调一次也没关系，它自己会纠偏）。 */
uint8_t swd_read_word_hold_prepare(uint32_t address)
{
    const uint32_t csw = CSW_VALUE_HOLD | CSW_SIZE32;

    if (dap_state.csw != csw)
    {
        if (!swd_write_ap(AP_CSW, csw)) { return 0; }
    }
    if (!dap_state.tarp_ok || (dap_state.tarp != address))
    {
        if (!swd_write_ap(AP_TAR, address)) { return 0; }
    }
    return 1;
}

/* 只发一次 DRW 读。**返回值是上一次 DRW 读的结果**，所以它单用没有意义：
 * 必须当流水线用 —— 第一次的结果丢掉，之后每次拿到的都是上一次的。
 * data 允许为 NULL（丢掉这次的结果）。 */
uint8_t swd_read_word_pipe(uint32_t *data)
{
    const uint8_t req = SWD_REG_AP | SWD_REG_R | AP_DRW;

    if (swd_transfer_retry(req, data) != 0x01) { return 0; }
    /* CSW 是不自增的，AP.TAR 还在原处，tarp_ok 保持有效 */
    return 1;
}

/* 一次性版本：DRW + RDBUFF = 2 次传输（给不想用流水线的调用方）。
 * 返回 1 = 成功。地址固定时每拍 2 次，比 swd_read_block 的 3 次省一次。 */
uint8_t swd_read_word_held(uint32_t address, uint8_t *data)
{
    uint8_t req, ack;

    if (!swd_read_word_hold_prepare(address)) { return 0; }

    /* prime：这一次读回的是上一次 DRW 读的结果，丢掉；RDBUFF 才是本次地址的值 */
    if (!swd_read_word_pipe(NULL)) { return 0; }

    req = SWD_REG_DP | SWD_REG_R | SWD_REG_ADR(DP_RDBUFF);
    ack = swd_transfer_retry(req, (uint32_t *)data);
    return (ack == 0x01) ? 1 : 0;
}

// Read target memory.
static uint8_t swd_read_data(uint32_t addr, uint32_t *val)
{
    uint8_t tmp_in[4];
    uint8_t tmp_out[4];
    uint8_t req, ack;
    uint32_t tmp;
    // put addr in TAR register
    int2array(tmp_in, addr, 4);
    req = SWD_REG_AP | SWD_REG_W | (1 << 2);

    if (swd_transfer_retry(req, (uint32_t *)tmp_in) != 0x01) {
        return 0;
    }

    // read data
    req = SWD_REG_AP | SWD_REG_R | (3 << 2);

    dap_state.tarp_ok = 0U;         /* 发过 DRW：AP.TAR 已被自增带跑 */

    if (swd_transfer_retry(req, (uint32_t *)tmp_out) != 0x01) {
        return 0;
    }

    // dummy read
    req = SWD_REG_DP | SWD_REG_R | SWD_REG_ADR(DP_RDBUFF);
    ack = swd_transfer_retry(req, (uint32_t *)tmp_out);
    *val = 0;
    tmp = tmp_out[3];
    *val |= (tmp << 24);
    tmp = tmp_out[2];
    *val |= (tmp << 16);
    tmp = tmp_out[1];
    *val |= (tmp << 8);
    tmp = tmp_out[0];
    *val |= (tmp << 0);
    return (ack == 0x01);
}

// Write target memory.
static uint8_t swd_write_data(uint32_t address, uint32_t data)
{
    uint8_t tmp_in[4];
    uint8_t req, ack;
    // put addr in TAR register
    int2array(tmp_in, address, 4);
    req = SWD_REG_AP | SWD_REG_W | (1 << 2);

    if (swd_transfer_retry(req, (uint32_t *)tmp_in) != 0x01) {
        return 0;
    }

    // write data
    int2array(tmp_in, data, 4);
    req = SWD_REG_AP | SWD_REG_W | (3 << 2);

    dap_state.tarp_ok = 0U;         /* 同上：发过 DRW 之后 AP.TAR 就会自增 */

    if (swd_transfer_retry(req, (uint32_t *)tmp_in) != 0x01) {
        return 0;
    }

    // dummy read
    req = SWD_REG_DP | SWD_REG_R | SWD_REG_ADR(DP_RDBUFF);
    ack = swd_transfer_retry(req, NULL);
    return (ack == 0x01) ? 1 : 0;
}

// Read 32-bit word from target memory.
uint8_t swd_read_word(uint32_t addr, uint32_t *val)
{
    if (!swd_write_ap(AP_CSW, CSW_VALUE | CSW_SIZE32)) {
        return 0;
    }

    if (!swd_read_data(addr, val)) {
        return 0;
    }

    return 1;
}

// Write 32-bit word to target memory.
uint8_t swd_write_word(uint32_t addr, uint32_t val)
{
    if (!swd_write_ap(AP_CSW, CSW_VALUE | CSW_SIZE32)) {
        return 0;
    }

    if (!swd_write_data(addr, val)) {
        return 0;
    }

    return 1;
}

// Read 8-bit byte from target memory.
uint8_t swd_read_byte(uint32_t addr, uint8_t *val)
{
    uint32_t tmp;

    if (!swd_write_ap(AP_CSW, CSW_VALUE | CSW_SIZE8)) {
        return 0;
    }

    if (!swd_read_data(addr, &tmp)) {
        return 0;
    }

    *val = (uint8_t)(tmp >> ((addr & 0x03) << 3));
    return 1;
}

// Write 8-bit byte to target memory.
uint8_t swd_write_byte(uint32_t addr, uint8_t val)
{
    uint32_t tmp;

    if (!swd_write_ap(AP_CSW, CSW_VALUE | CSW_SIZE8)) {
        return 0;
    }

    tmp = val << ((addr & 0x03) << 3);

    if (!swd_write_data(addr, tmp)) {
        return 0;
    }

    return 1;
}

// Read unaligned data from target memory.
// size is in bytes.
uint8_t swd_read_memory(uint32_t address, uint8_t *data, uint32_t size)
{
    uint32_t n;

    // Read bytes until word aligned
    while ((size > 0) && (address & 0x3)) {
        if (!swd_read_byte(address, data)) {
            return 0;
        }

        address++;
        data++;
        size--;
    }

    // Read word aligned blocks
    while (size > 3) {
        // Limit to auto increment page size
        n = TARGET_AUTO_INCREMENT_PAGE_SIZE - (address & (TARGET_AUTO_INCREMENT_PAGE_SIZE - 1));

        if (size < n) {
            n = size & 0xFFFFFFFC; // Only count complete words remaining
        }

        if (!swd_read_block(address, data, n)) {
            return 0;
        }

        address += n;
        data += n;
        size -= n;
    }

    // Read remaining bytes
    while (size > 0) {
        if (!swd_read_byte(address, data)) {
            return 0;
        }

        address++;
        data++;
        size--;
    }

    return 1;
}

// Write unaligned data to target memory.
// size is in bytes.
uint8_t swd_write_memory(uint32_t address, uint8_t *data, uint32_t size)
{
    uint32_t n = 0;

    // Write bytes until word aligned
    while ((size > 0) && (address & 0x3)) {
        if (!swd_write_byte(address, *data)) {
            return 0;
        }

        address++;
        data++;
        size--;
    }

    // Write word aligned blocks
    while (size > 3) {
        // Limit to auto increment page size
        n = TARGET_AUTO_INCREMENT_PAGE_SIZE - (address & (TARGET_AUTO_INCREMENT_PAGE_SIZE - 1));

        if (size < n) {
            n = size & 0xFFFFFFFC; // Only count complete words remaining
        }

        if (!swd_write_block(address, data, n)) {
            return 0;
        }

        address += n;
        data += n;
        size -= n;
    }

    // Write remaining bytes
    while (size > 0) {
        if (!swd_write_byte(address, *data)) {
            return 0;
        }

        address++;
        data++;
        size--;
    }

    return 1;
}

// Execute system call.
static uint8_t swd_write_debug_state(DEBUG_STATE *state)
{
    uint32_t i, status;

    if (!swd_write_dp(DP_SELECT, 0)) {
        return 0;
    }

    // R0, R1, R2, R3
    for (i = 0; i < 4; i++) {
        if (!swd_write_core_register(i, state->r[i])) {
            return 0;
        }
    }

    // R9
    if (!swd_write_core_register(9, state->r[9])) {
        return 0;
    }

    // R13, R14, R15
    for (i = 13; i < 16; i++) {
        if (!swd_write_core_register(i, state->r[i])) {
            return 0;
        }
    }

    // xPSR
    if (!swd_write_core_register(16, state->xpsr)) {
        return 0;
    }

    if (!swd_write_word(DBG_HCSR, DBGKEY | C_DEBUGEN | C_MASKINTS | C_HALT)) {
        return 0;
    }

    if (!swd_write_word(DBG_HCSR, DBGKEY | C_DEBUGEN | C_MASKINTS)) {
        return 0;
    }

    // check status
    if (!swd_read_dp(DP_CTRL_STAT, &status)) {
        return 0;
    }

    if (status & (STICKYERR | WDATAERR)) {
        return 0;
    }

    return 1;
}

uint8_t swd_read_core_register(uint32_t n, uint32_t *val)
{
    int i = 0, timeout = 100;

    if (!swd_write_word(DCRSR, n)) {
        return 0;
    }

    // wait for S_REGRDY
    for (i = 0; i < timeout; i++) {
        if (!swd_read_word(DHCSR, val)) {
            return 0;
        }

        if (*val & S_REGRDY) {
            break;
        }
    }

    if (i == timeout) {
        return 0;
    }

    if (!swd_read_word(DCRDR, val)) {
        return 0;
    }

    return 1;
}

uint8_t swd_write_core_register(uint32_t n, uint32_t val)
{
    int i = 0, timeout = 100;

    if (!swd_write_word(DCRDR, val)) {
        return 0;
    }

    if (!swd_write_word(DCRSR, n | REGWnR)) {
        return 0;
    }

    // wait for S_REGRDY
    for (i = 0; i < timeout; i++) {
        if (!swd_read_word(DHCSR, &val)) {
            return 0;
        }

        if (val & S_REGRDY) {
            return 1;
        }
    }

    return 0;
}


// SWD Reset
static uint8_t swd_reset(uint32_t count)
{
    uint8_t tmp_in[8];
    uint8_t i = 0;

    for (i = 0; i < 8; i++) {
        tmp_in[i] = 0xff;
    }

    SWJ_Sequence(count, tmp_in);
    return 1;
}

// SWD Switch
static uint8_t swd_switch(uint16_t val)
{
    uint8_t tmp_in[2];
    tmp_in[0] = val & 0xff;
    tmp_in[1] = (val >> 8) & 0xff;
    SWJ_Sequence(16, tmp_in);
    return 1;
}

// SWD Read ID
static uint8_t swd_read_idcode(uint32_t *id)
{
    uint8_t tmp_in[1];
    uint8_t tmp_out[4];
    tmp_in[0] = 0x00;
    SWJ_Sequence(8, tmp_in);

    if (swd_read_dp(0, (uint32_t *)tmp_out) != 0x01) {
        return 0;
    }

    *id = (tmp_out[3] << 24) | (tmp_out[2] << 16) | (tmp_out[1] << 8) | tmp_out[0];

    return 1;
}


uint8_t JTAG2SWD()
{
    uint32_t tmp = 0;

    if (!swd_reset(52)) {
        return 0;
    }
    if (!swd_switch(0xE79E)) {
        return 0;
    }
    if (!swd_reset(57)) {
        return 0;
    }

    if (!swd_read_idcode(&tmp)) {
        return 0;
    }

    return 1;
}


uint8_t swd_init_debug(void)
{
    uint32_t tmp = 0;
    int i = 0;
    int timeout = 100;
    // init dap state with fake values
    dap_state.select = 0xffffffff;
    dap_state.csw = 0xffffffff;
    dap_state.tarp_ok = 0U;

    int8_t retries = 4;
    int8_t do_abort = 0;
    do {
        if (do_abort) {
            //do an abort on stale target, then reset the device
            swd_write_dp(DP_ABORT, DAPABORT);
            swd_set_target_reset(1);
            clock_cpu_delay_ms(2);
            swd_set_target_reset(0);
            do_abort = 0;
        }
        swd_init();
        // call a target dependant function
        // this function can do several stuff before really
        // initing the debug
        //if (g_target_family && g_target_family->target_before_init_debug) {
        //    g_target_family->target_before_init_debug();
        //}

        if (!JTAG2SWD()) {
            do_abort = 1;
            continue;
        }

        if (!swd_clear_errors()) {
            do_abort = 1;
            continue;
        }

        if (!swd_write_dp(DP_SELECT, 0)) {
            do_abort = 1;
            continue;

        }

        // Power up
        if (!swd_write_dp(DP_CTRL_STAT, CSYSPWRUPREQ | CDBGPWRUPREQ)) {
            do_abort = 1;
            continue;
        }

        for (i = 0; i < timeout; i++) {
            if (!swd_read_dp(DP_CTRL_STAT, &tmp)) {
                do_abort = 1;
                break;
            }
            if ((tmp & (CDBGPWRUPACK | CSYSPWRUPACK)) == (CDBGPWRUPACK | CSYSPWRUPACK)) {
                // Break from loop if powerup is complete
                break;
            }
        }
        if ((i == timeout) || (do_abort == 1)) {
            // Unable to powerup DP
            do_abort = 1;
            continue;
        }

        if (!swd_write_dp(DP_CTRL_STAT, CSYSPWRUPREQ | CDBGPWRUPREQ | TRNNORMAL | MASKLANE)) {
            do_abort = 1;
            continue;
        }

        // call a target dependant function:
        // some target can enter in a lock state
        // this function can unlock these targets
        //if (g_target_family && g_target_family->target_unlock_sequence) {
        //    g_target_family->target_unlock_sequence();
        //}

        if (!swd_write_dp(DP_SELECT, 0)) {
            do_abort = 1;
            continue;
        }
        return 1;

    } while (--retries > 0);

    return 0;
}

#endif
