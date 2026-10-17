/**
 * @file    swd_host.h
 * @brief   Host driver for accessing the DAP
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

#ifndef SWDHOST_CM_H
#define SWDHOST_CM_H

/* 移植版：去掉 flash_blob.h / target_family.h 依赖（仅保留 SWD 访问 API）。
 * 原始文件：ARM DAPLink swd_host.h（Apache-2.0）。 */
#ifdef TARGET_MCU_CORTEX_A
#include "debug_ca.h"
#else
#define NVIC_Addr    (0xe000e000)
#define DBG_Addr     (0xe000edf0)
#include "debug_cm.h"
#endif

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    CONNECT_NORMAL,
    CONNECT_UNDER_RESET,
} SWD_CONNECT_TYPE;


uint8_t swd_init(void);
uint8_t swd_off(void);
uint8_t swd_init_debug(void);
uint8_t swd_clear_errors(void);
uint8_t swd_read_dp(uint8_t adr, uint32_t *val);
uint8_t swd_write_dp(uint8_t adr, uint32_t val);
uint8_t swd_read_ap(uint32_t adr, uint32_t *val);
uint8_t swd_write_ap(uint32_t adr, uint32_t val);
uint8_t swd_read_word(uint32_t addr, uint32_t *val);
uint8_t swd_write_word(uint32_t addr, uint32_t val);
uint8_t swd_read_byte(uint32_t addr, uint8_t *val);
uint8_t swd_write_byte(uint32_t addr, uint8_t val);
uint8_t swd_read_memory(uint32_t address, uint8_t *data, uint32_t size);

/* 对齐块读的快速路径（给 scope 采样器用）：调用方保证 address 4 字节对齐、size 是 4 的
 * 倍数、且整段不跨 1 KB 自增页 —— 于是可以跳过 swd_read_memory() 里的头尾字节处理与
 * 分页循环。实测每次采样的框架开销里有相当一部分就花在那两层包装上。 */
uint8_t swd_read_block4(uint32_t address, uint8_t *data, uint32_t size);

/* 同一个字反复读的快速路径：CSW 切成不自增 + 缓存 TAR。
 *
 * 两个层次，见 swd_host.c 里的长注释：
 *   - swd_read_word_held()：一次调用完成，**2 次传输**（DRW + RDBUFF），原路径是 3 次；
 *   - swd_read_word_hold_prepare() + swd_read_word_pipe()：把它当流水线用，
 *     每拍**只发 1 次 DRW 读**，返回值是上一拍的结果，由调用方回填进上一拍的槽位。
 *
 * 🚨 只能在**同一个地址**上连续读时用（调用方保证只有一个 span）。地址一变就要重写
 * TAR，而这里的 TAR 写带 RDBUFF 收尾（2 次传输），比 swd_read_block 的裸写（1 次）还贵
 * —— 实测两个远离的单字 span 会从 8.6 µs 变成 11.2 µs。
 * 返回 1 = 成功，0 = 失败（与 swd_read_block4 同约定）。 */
uint8_t swd_read_word_held(uint32_t address, uint8_t *data);
uint8_t swd_read_word_hold_prepare(uint32_t address);
uint8_t swd_read_word_pipe(uint32_t *data);

/* 清掉 swd_host 对 AP/DP 的影子寄存器缓存（select / CSW / TAR）。
 * 主机自己碰过 DAP（走 DAP_SWD_Transfer 那条路，绕过 swd_host）之后必须调，
 * 否则缓存会失真 —— 轻则多写几次寄存器，重则读到**别的地址**。 */
void swd_invalidate_ap_cache(void);
uint8_t swd_write_memory(uint32_t address, uint8_t *data, uint32_t size);
uint8_t swd_read_core_register(uint32_t n, uint32_t *val);
uint8_t swd_write_core_register(uint32_t n, uint32_t val);
uint8_t swd_transfer_retry(uint32_t req, uint32_t *data);
void int2array(uint8_t *res, uint32_t data, uint8_t len);
void swd_set_reset_connect(SWD_CONNECT_TYPE type);
void swd_set_soft_reset(uint32_t soft_reset_type);
uint8_t JTAG2SWD(void);

/* 平台胶水（swd_host_port.c 提供，官方分别来自 DAP 层与 target_reset.c） */
uint8_t SWD_Transfer(uint32_t request, uint32_t *data);
void swd_set_target_reset(uint8_t asserted);

#ifdef __cplusplus
}
#endif

#endif
