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

/* 读引擎在 swd_ops 内的偏移和当前延时。UINT32_MAX 表示未加载。 */
uint32_t swd_blob_read_offset(void);
uint32_t swd_blob_clock_delay(void);
/* Nominal rate of the selected fixed timing. Zero for variable timing. */
uint32_t swd_blob_nominal_clock_hz(void);

#endif
