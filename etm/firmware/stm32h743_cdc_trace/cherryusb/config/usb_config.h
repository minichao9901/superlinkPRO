/*
 * CherryUSB 配置 —— STM32H7B0（dwc2 port, USB_OTG_FS, PA11/PA12, 全速）
 *
 * 与 F103 那份的差别：
 *   - port 换 dwc2（H7 是 Synopsys DWC2 OTG，不是 F1 的老 USB FS device）
 *   - USB 时钟用 **HSI48**（H7 内部 48MHz RC，省下 PLL3），不需要 PLL 凑 48MHz
 *   - 不能用 DMA：PA11/PA12 的 FS 模式没有 DMA 支持（只有 PB14/PB15 的 HS 口有），
 *     所以 CONFIG_USB_DWC2_DMA_ENABLE 必须**保持注释**。
 *   - H7 有 D-Cache：USB 缓冲必须放"非缓存区"（见链接脚本的 .noncacheable）
 */
#ifndef CHERRYUSB_CONFIG_H
#define CHERRYUSB_CONFIG_H

/* ================ USB common Configuration ================ */

#include <stdio.h>
#include "trace_hooks.h"

#define CONFIG_USB_PRINTF(...) trace_log_printf(__VA_ARGS__)

#ifndef CONFIG_USB_DBG_LEVEL
#define CONFIG_USB_DBG_LEVEL USB_DBG_INFO
#endif

/* 对齐按 D-Cache 行（H7 的 cache line = 32 字节），缓冲放 .noncacheable */
#define CONFIG_USB_DCACHE_ENABLE
#define CONFIG_USB_ALIGN_SIZE 32

#define USB_NOCACHE_RAM_SECTION __attribute__((section(".noncacheable"), aligned(CONFIG_USB_ALIGN_SIZE)))
#ifndef USB_MEM_ALIGNX
#define USB_MEM_ALIGNX __attribute__((aligned(CONFIG_USB_ALIGN_SIZE)))
#endif

/* ================= USB Device Stack Configuration ================ */

#ifndef CONFIG_USBDEV_REQUEST_BUFFER_LEN
#define CONFIG_USBDEV_REQUEST_BUFFER_LEN 512
#endif

#define CONFIG_USBDEV_ADVANCE_DESC
#define CONFIG_USBDEV_MAX_BUS 1

/* ================ USB Device Port Configuration ================*/

/* ---------------- DWC2 Configuration ---------------- */
/* ⚠️ 别打开：PA11/PA12 的 FS 口不支持 DMA。要开就得换 PB14/PB15 的 HS 口。 */
// #define CONFIG_USB_DWC2_DMA_ENABLE

/* ⚠️ 也别定义 CONFIG_USB_HS：本工程走内部 FS PHY（全速，12Mbps） */
// #define CONFIG_USB_HS

#endif /* CHERRYUSB_CONFIG_H */
