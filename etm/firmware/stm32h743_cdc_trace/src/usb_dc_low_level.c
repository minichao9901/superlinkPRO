/*
 * USB 低级初始化 + 中断分发表 —— STM32H743 / USB_OTG_FS（PA11/PA12，内嵌全速 PHY）
 *
 * 型号差异备忘（H743 vs H7B0，两份工程都靠这段代码兼容）：
 *   H743/H750：两个 USB 实例
 *     USB_OTG_FS（= USB2_OTG_FS）@ 0x40080000，中断 OTG_FS_IRQn(=101)
 *     USB_OTG_HS（= USB1_OTG_HS）@ 0x40040000，中断 OTG_HS_IRQn(=77)
 *   H7B0/H7A3/H7B3：只有一个 USB1_OTG_HS @ 0x40040000，中断 OTG_HS_IRQn
 *   → 所以这里的逻辑**按 reg_base 判断**，两个系列都能用；MspInit 里两个实例都判。
 *
 * 阿波罗 H743 注意：板上 USB HOST 与 USB SLAVE 共用 PA11/PA12，需要跳线帽选中 SLAVE；
 * 板上无 ULPI PHY，走不了高速。
 */
#include "stm32h7xx_hal.h"
#include "usbd_core.h"
#include "usb_dwc2_param.h"

#define USB_OTG_HS_BASE_ADDR 0x40040000UL
#define USB_OTG_FS_BASE_ADDR 0x40080000UL

/* 中断分发表（定义在 cherryusb/port/dwc2/usb_glue_st_h7b0.c） */
extern void (*g_usb_dwc2_irq[2])(uint8_t busid);
extern uint8_t g_usb_dwc2_busid[2];

static PCD_HandleTypeDef s_pcd;

void usb_dc_low_level_init(uint8_t busid)
{
    uint32_t base = (uint32_t)g_usbdev_bus[busid].reg_base;

    if (base == USB_OTG_HS_BASE_ADDR) {
        g_usb_dwc2_busid[1] = busid;
        g_usb_dwc2_irq[1] = USBD_IRQHandler;
    } else {
        g_usb_dwc2_busid[0] = busid;
        g_usb_dwc2_irq[0] = USBD_IRQHandler;
    }

    s_pcd.Instance = (USB_OTG_GlobalTypeDef *)base;
    HAL_PCD_MspInit(&s_pcd);
}

void usb_dc_low_level_deinit(uint8_t busid)
{
    uint32_t base = (uint32_t)g_usbdev_bus[busid].reg_base;

    if (base == USB_OTG_HS_BASE_ADDR) {
        g_usb_dwc2_busid[1] = 0;
        g_usb_dwc2_irq[1] = NULL;
    } else {
        g_usb_dwc2_busid[0] = 0;
        g_usb_dwc2_irq[0] = NULL;
    }

    s_pcd.Instance = (USB_OTG_GlobalTypeDef *)base;
    HAL_PCD_MspDeInit(&s_pcd);
}

/* ---------------- HAL_PCD_MspInit / DeInit（ST 惯例的板级钩子） ---------------- */
void HAL_PCD_MspInit(PCD_HandleTypeDef *hpcd)
{
    GPIO_InitTypeDef io = { 0 };

    if (hpcd->Instance == USB_OTG_FS) {
        /* PA11 = DM, PA12 = DP，AF10（H743 上是 OTG_FS 复用） */
        __HAL_RCC_GPIOA_CLK_ENABLE();
        io.Pin = GPIO_PIN_11 | GPIO_PIN_12;
        io.Mode = GPIO_MODE_AF_PP;
        io.Pull = GPIO_NOPULL;
        io.Speed = GPIO_SPEED_FREQ_VERY_HIGH;
#ifdef GPIO_AF10_OTG2_FS
        io.Alternate = GPIO_AF10_OTG2_FS;
#elif defined(GPIO_AF10_OTG_FS)
        io.Alternate = GPIO_AF10_OTG_FS;
#else
        io.Alternate = GPIO_AF10_OTG1_FS;
#endif
        HAL_GPIO_Init(GPIOA, &io);

        __HAL_RCC_USB_OTG_FS_CLK_ENABLE();
        /* 复位：清掉上一次运行/软复位遗留的 DWC2 状态（CherryUSB 自己管协议，不走 HAL_PCD_Init，
         * 所以这一步不会有人替我们做）。 */
        __HAL_RCC_USB_OTG_FS_FORCE_RESET();
        __HAL_RCC_USB_OTG_FS_RELEASE_RESET();

        HAL_NVIC_SetPriority(OTG_FS_IRQn, 3, 0);
        HAL_NVIC_ClearPendingIRQ(OTG_FS_IRQn);
        HAL_NVIC_EnableIRQ(OTG_FS_IRQn);
    } else if (hpcd->Instance == USB_OTG_HS) {
        /* PB14/PB15（或 PA11/PA12 备用）走 HS 口 —— 本工程默认不用，
         * 保留这段是为了将来换 ULPI 高速口时不用重写。 */
        __HAL_RCC_GPIOB_CLK_ENABLE();
        io.Pin = GPIO_PIN_14 | GPIO_PIN_15;
        io.Mode = GPIO_MODE_AF_PP;
        io.Pull = GPIO_NOPULL;
        io.Speed = GPIO_SPEED_FREQ_VERY_HIGH;
        io.Alternate = GPIO_AF12_OTG2_FS; /* H743 上 HS 口的 FS 复用 */
        HAL_GPIO_Init(GPIOB, &io);

        __HAL_RCC_USB1_OTG_HS_CLK_ENABLE();
        __HAL_RCC_USB1_OTG_HS_FORCE_RESET();
        __HAL_RCC_USB1_OTG_HS_RELEASE_RESET();

        HAL_NVIC_SetPriority(OTG_HS_IRQn, 3, 0);
        HAL_NVIC_ClearPendingIRQ(OTG_HS_IRQn);
        HAL_NVIC_EnableIRQ(OTG_HS_IRQn);
    }
}

void HAL_PCD_MspDeInit(PCD_HandleTypeDef *hpcd)
{
    if (hpcd->Instance == USB_OTG_FS) {
        __HAL_RCC_USB_OTG_FS_CLK_DISABLE();
        HAL_NVIC_DisableIRQ(OTG_FS_IRQn);
        HAL_NVIC_ClearPendingIRQ(OTG_FS_IRQn);
        HAL_GPIO_DeInit(GPIOA, GPIO_PIN_11 | GPIO_PIN_12);
    } else if (hpcd->Instance == USB_OTG_HS) {
        __HAL_RCC_USB1_OTG_HS_CLK_DISABLE();
        HAL_NVIC_DisableIRQ(OTG_HS_IRQn);
        HAL_NVIC_ClearPendingIRQ(OTG_HS_IRQn);
        HAL_GPIO_DeInit(GPIOB, GPIO_PIN_14 | GPIO_PIN_15);
    }
}
