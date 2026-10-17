/*
 * CherryUSB DWC2 glue —— STM32H7B0（也可直接给 H743/H750 用，见文末说明）
 *
 * 为什么不用官方的 port/dwc2/usb_glue_st.c：
 *   官方那份**同时**包含 device 与 host 两套代码，开头就 `#include "usbh_core.h"`，
 *   于是即使只做 device，也会被迫把整个 USB host 栈（usbh_core.c、usbh_hub.c…）
 *   拖进工程。裸机小工程里这是纯负担，也更容易碰编译依赖。
 *
 * 本文件把官方 glue 里 device 侧真正需要的三件事照抄下来（参数逐字对齐官方 H7 段）：
 *   1) param_pa11_pa12 / param_pb14_pb15 —— DWC2 的 FIFO、PHY、GCCFG 参数
 *   2) dwc2_get_user_params()            —— 按 reg_base 挑参数（0x40040000 = HS 口）
 *   3) OTG_FS_IRQHandler / OTG_HS_IRQHandler + usbd_dwc2_delay_ms / get_system_clock
 *
 * ⚠️ 型号差异：H7B0/H7A3/H7B3 只有 **USB1_OTG_HS @0x40040000**（PA11/PA12 内嵌全速 PHY），
 *    没有独立的 USB_OTG_FS；H743/H750 两个口都有，FS 口在 0x40080000。
 *    本文件两个口都支持，按 reg_base 自动选参数，所以 H743 那份工程可以原样复用。
 *
 * ⚠️ PA11/PA12 的 FS 口**不支持 DMA**（只有 PB14/PB15 的 HS 口才有），
 *    device_dma_enable 必须保持 false。
 */
#include "usbd_core.h"
#include "usb_dwc2_param.h"
#include "stm32h7xx_hal.h"

/* PA11/PA12：内嵌全速 PHY，64 字节包 */
const struct dwc2_user_params param_pa11_pa12 = {
    .phy_type = DWC2_PHY_TYPE_PARAM_FS, // 要 ULPI 高速就换成 DWC2_PHY_TYPE_PARAM_UTMI
#ifdef CONFIG_USB_DWC2_DMA_ENABLE
    .device_dma_enable = true,
#else
    .device_dma_enable = false,
#endif
    .device_dma_desc_enable = false,
    .device_rx_fifo_size = (952 - 16 - 256 - 128 - 128 - 128 - 128),
    .device_tx_fifo_size = {
        [0] = 16,  // 64 byte
        [1] = 256, // 1024 byte
        [2] = 128, // 512 byte
        [3] = 128, // 512 byte
        [4] = 128, // 512 byte
        [5] = 128, // 512 byte
        [6] = 0,
        [7] = 0,
        [8] = 0,
        [9] = 0,
        [10] = 0,
        [11] = 0,
        [12] = 0,
        [13] = 0,
        [14] = 0,
        [15] = 0 },

    .host_dma_desc_enable = false,
    .host_rx_fifo_size = 568,
    .host_nperio_tx_fifo_size = 128, // 512 byte
    .host_perio_tx_fifo_size = 256,  // 1024 byte

    .device_gccfg = (1 << 16), // fs: USB_OTG_GCCFG_PWRDWN hs:0
    .host_gccfg = (1 << 16),   // fs: USB_OTG_GCCFG_PWRDWN hs:0

    .b_session_valid_override = true
};

/* PB14/PB15：高速口（要外接 ULPI PHY，或用内嵌 FS PHY 跑全速） */
const struct dwc2_user_params param_pb14_pb15 = {
#ifdef CONFIG_USB_HS
    .phy_type = DWC2_PHY_TYPE_PARAM_UTMI,
#else
    .phy_type = DWC2_PHY_TYPE_PARAM_FS,
#endif
#ifdef CONFIG_USB_DWC2_DMA_ENABLE
    .device_dma_enable = true,
#else
    .device_dma_enable = false,
#endif
    .device_dma_desc_enable = false,
    .device_rx_fifo_size = (952 - 16 - 256 - 128 - 128 - 128 - 128),
    .device_tx_fifo_size = {
        [0] = 16,
        [1] = 256,
        [2] = 128,
        [3] = 128,
        [4] = 128,
        [5] = 128,
        [6] = 0,
        [7] = 0,
        [8] = 0,
        [9] = 0,
        [10] = 0,
        [11] = 0,
        [12] = 0,
        [13] = 0,
        [14] = 0,
        [15] = 0 },

    .host_dma_desc_enable = false,
    .host_rx_fifo_size = 568,
    .host_nperio_tx_fifo_size = 128,
    .host_perio_tx_fifo_size = 256,

#ifdef CONFIG_USB_HS
    .device_gccfg = 0,
    .host_gccfg = 0,
#else
    .device_gccfg = (1 << 16),
    .host_gccfg = (1 << 16),
#endif
    .b_session_valid_override = true
};

/* H7 的两个 USB 口基址 */
#define USB_OTG_HS_BASE_ADDR 0x40040000UL
#define USB_OTG_FS_BASE_ADDR 0x40080000UL

void dwc2_get_user_params(uint32_t reg_base, struct dwc2_user_params *params)
{
    if (reg_base == USB_OTG_HS_BASE_ADDR) {
        memcpy(params, &param_pb14_pb15, sizeof(struct dwc2_user_params));
    } else {
        memcpy(params, &param_pa11_pa12, sizeof(struct dwc2_user_params));
    }
}

extern uint32_t SystemCoreClock;

void usbd_dwc2_delay_ms(uint8_t ms)
{
    uint32_t count = SystemCoreClock / 1000 * ms;
    while (count--) {
        __asm volatile("nop");
    }
}

uint32_t usbd_dwc2_get_system_clock(void)
{
    return SystemCoreClock;
}

/* 中断分发表：由 src/usb_dc_low_level.c 的 usb_dc_low_level_init() 填好。
 * H7B0 用 OTG_HS_IRQHandler；H743 若用 FS 口则走 OTG_FS_IRQHandler（两个都留着）。 */
void (*g_usb_dwc2_irq[2])(uint8_t busid);
uint8_t g_usb_dwc2_busid[2] = { 0, 0 };

void OTG_FS_IRQHandler(void)
{
    g_usb_dwc2_irq[0](g_usb_dwc2_busid[0]);
}

void OTG_HS_IRQHandler(void)
{
    g_usb_dwc2_irq[1](g_usb_dwc2_busid[1]);
}

#ifdef CONFIG_USB_DCACHE_ENABLE
void usb_dcache_clean(uintptr_t addr, size_t size)
{
    SCB_CleanDCache_by_Addr((void *)addr, size);
}

void usb_dcache_invalidate(uintptr_t addr, size_t size)
{
    SCB_InvalidateDCache_by_Addr((void *)addr, size);
}

void usb_dcache_flush(uintptr_t addr, size_t size)
{
    SCB_CleanInvalidateDCache_by_Addr((void *)addr, size);
}
#endif
