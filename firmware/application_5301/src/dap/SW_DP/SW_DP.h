#ifndef SW_DP_H
#define SW_DP_H

#include <stdint.h>

uint32_t swd_speed_calc(uint32_t xq);

void SWJ_Sequence_GPIO_Slow(uint32_t count, const uint8_t *data);
void SWD_Sequence_GPIO_Slow(uint32_t info, const uint8_t *swdo, uint8_t *swdi);
uint8_t SWD_Write_GPIO_Slow(uint8_t header, uint32_t *data);
uint8_t SWD_Read_GPIO_Slow(uint8_t header, uint32_t *data);

void SWJ_Sequence_GPIO_ASM_20M(uint32_t count, const uint8_t *data, uint32_t delay);
uint8_t SWD_Write_GPIO_ASM_20M(uint8_t header, uint32_t *data, uint32_t delay);
uint8_t SWD_Read_GPIO_ASM_20M(uint8_t header, uint32_t *data, uint32_t delay);

void SWJ_Sequence_GPIO_ASM_30M(uint32_t count, const uint8_t *data, uint32_t delay);
uint8_t SWD_Write_GPIO_ASM_30M(uint8_t header, uint32_t *data, uint32_t delay);
uint8_t SWD_Read_GPIO_ASM_30M(uint8_t header, uint32_t *data, uint32_t delay);

void SWJ_Sequence_GPIO_ASM_36M(uint32_t count, const uint8_t *data, uint32_t delay);
uint8_t SWD_Write_GPIO_ASM_36M(uint8_t header, uint32_t *data, uint32_t delay);
uint8_t SWD_Read_GPIO_ASM_36M(uint8_t header, uint32_t *data, uint32_t delay);

void SWJ_Sequence_GPIO_ASM_45M(uint32_t count, const uint8_t *data, uint32_t delay);
uint8_t SWD_Write_GPIO_ASM_45M(uint8_t header, uint32_t *data, uint32_t delay);
uint8_t SWD_Read_GPIO_ASM_45M(uint8_t header, uint32_t *data, uint32_t delay);

void SWJ_Sequence_GPIO_ASM_60M(uint32_t count, const uint8_t *data, uint32_t delay);
uint8_t SWD_Write_GPIO_ASM_60M(uint8_t header, uint32_t *data, uint32_t delay);
uint8_t SWD_Read_GPIO_ASM_60M(uint8_t header, uint32_t *data, uint32_t delay);

void SWJ_Sequence_GPIO_ASM_SLOW(uint32_t count, const uint8_t *data, uint32_t delay);
uint8_t SWD_Write_GPIO_ASM_SLOW(uint8_t header, uint32_t *data, uint32_t delay);
uint8_t SWD_Read_GPIO_ASM_SLOW(uint8_t header, uint32_t *data, uint32_t delay);

void SWD_DynamicLoad_Slow(void);
void SWD_DynamicLoad_60M(void);
void SWD_DynamicLoad_45M(void);
void SWD_DynamicLoad_36M(void);
void SWD_DynamicLoad_30M(void);
void SWD_DynamicLoad_20M(void);

/* 诊断：当前装载的读 blob 在 swd_ops 里的偏移，以及传给它的 clock_delay。
 * 这是"SWJ_Clock 到底换挡了没有"的唯一可靠证据 —— 光看状态字里的请求频率没用，
 * 那个值在链路还没起来时也会被记下来（rtt_bridge_set_swd_clock 的 else 分支）。
 * 返回值对应关系（见 swd_blob_evklite.h 的 SWD_READ_OFFSET_*）：
 *   0x53C=60M  0x60C=45M  0x6E0=36M  0x7C4=30M  0xA54=20M  0x620=SLOW
 *   0xFFFFFFFF = 还没装载过任何 blob（read_func 为 NULL）。 */
uint32_t swd_blob_read_offset(void);
uint32_t swd_blob_clock_delay(void);

#endif