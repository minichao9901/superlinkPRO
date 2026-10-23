/*
 * STM32H743 + CherryUSB CDC ACM 例程（etm-trace 目标固件）
 * ============================================================================
 * 目录定位：E:\web-etm-trace\firmware\stm32h743_cdc_trace
 * 目标板：正点原子阿波罗 STM32H743（HSE 25MHz）
 *
 * 时钟（对齐阿波罗 H743 的既有做法，见 E:\web-serial-rtt-tools\...\stm32h743_rtt_speed）：
 *   HSE 25MHz -> PLL1 M=5, N=192, P=2, Q=2, R=2
 *     VCO = 25/5*192 = 960MHz
 *     SYSCLK = 960/P=2 = 480MHz（H743 上限）
 *     AXI/HCLK = /2 = 240MHz，APB1/2/3 = /2 = 120MHz，APB4 = /4 = 60MHz
 *   USB 时钟 = HSI48（48MHz 内部 RC），不占用 PLL3
 *
 * USB：USB_OTG_FS @0x40080000，PA11/PA12，内嵌全速 PHY
 *   ⚠️ 阿波罗板上 USB HOST 与 USB SLAVE 共用 PA11/PA12，靠跳线帽选择 ——
 *      要当设备用，跳线帽必须把 PA11/PA12 接到 USB_SLAVE 座子。
 *   ⚠️ 板上没有 ULPI PHY，走不了高速。
 *
 * 关于 ETM：**固件不配置、不开启 ETM**。ETMv4/CSTF/ETF/TPIU 与 trace 引脚全部由
 *   外部 SWD 工具配置（与 stm32h7b0_spi_trace 同一约定），见 src/trace_hooks.h。
 *
 * CDC 命令：PING | INFO | MARK | ECHO on|off | ECHO <text>
 *
 * 构建：pwsh -NoProfile -File .\build.ps1
 */
#include "stm32h7xx_hal.h"
#include "usbd_core.h"
#include "usbd_cdc_acm.h"
#include "trace_hooks.h"
#include "SEGGER_RTT.h"

#include <string.h>

#ifndef H7_CDC_BANNER_INTERVAL_MS
#define H7_CDC_BANNER_INTERVAL_MS 0
#endif

/* PLL1 参数（build.ps1 通过 -D 传入；缺省是 480MHz 档）。
 *   VCO = HSE/M × N；SYSCLK = VCO/P；CPU = SYSCLK/D1CPRE；TRACECLK = VCO/R/2
 * 注意：物理 TRACECLK 是 pll1_r_ck 再二分频，所以 R=16 时 VCO 960MHz 得到 30MHz。 */
#ifndef H7_CDC_PLL_M
#define H7_CDC_PLL_M 5
#endif
#ifndef H7_CDC_PLL_N
#define H7_CDC_PLL_N 192
#endif
#ifndef H7_CDC_PLL_P
#define H7_CDC_PLL_P 2
#endif
#ifndef H7_CDC_PLL_R
#define H7_CDC_PLL_R 2
#endif
#ifndef H7_CDC_D1CPRE
#define H7_CDC_D1CPRE RCC_SYSCLK_DIV1
#endif
#ifndef H7_CDC_HPRE
#define H7_CDC_HPRE RCC_HCLK_DIV2
#endif
#ifndef H7_CDC_FLASH_LATENCY
#define H7_CDC_FLASH_LATENCY FLASH_LATENCY_4
#endif

uint32_t g_sysclk_hz = 0;
uint32_t g_clock_err = 0;

extern void probe_stage(uint32_t stage);

/* ======================= CherryUSB CDC ACM 描述符 ======================= */
#define CDC_IN_EP  0x81
#define CDC_OUT_EP 0x02
#define CDC_INT_EP 0x83

#define USBD_VID           0xF055
#define USBD_PID           0x7431
#define USBD_MAX_POWER     100
#define USBD_LANGID_STRING 1033

#define USB_CONFIG_SIZE (9 + CDC_ACM_DESCRIPTOR_LEN)
#define CDC_MAX_MPS 64
#define CDC_BUF_SIZE 2048

static const uint8_t device_descriptor[] = {
    USB_DEVICE_DESCRIPTOR_INIT(USB_2_0, 0xEF, 0x02, 0x01, USBD_VID, USBD_PID, 0x0100, 0x01)
};

static const uint8_t config_descriptor[] = {
    USB_CONFIG_DESCRIPTOR_INIT(USB_CONFIG_SIZE, 0x02, 0x01, USB_CONFIG_BUS_POWERED, USBD_MAX_POWER),
    CDC_ACM_DESCRIPTOR_INIT(0x00, CDC_INT_EP, CDC_OUT_EP, CDC_IN_EP, CDC_MAX_MPS, 0x02)
};

static const uint8_t device_quality_descriptor[] = {
    0x0a, USB_DESCRIPTOR_TYPE_DEVICE_QUALIFIER,
    0x00, 0x02, 0x00, 0x00, 0x00, 0x40, 0x00, 0x00,
};

static const char *string_descriptors[] = {
    (const char[]){ 0x09, 0x04 },
    "web-etm-trace",
    "STM32H743 CDC ACM (CherryUSB)",
    "H743CDC0001",
};

static const uint8_t *device_descriptor_callback(uint8_t speed)
{
    (void)speed;
    return device_descriptor;
}

static const uint8_t *config_descriptor_callback(uint8_t speed)
{
    (void)speed;
    return config_descriptor;
}

static const uint8_t *device_quality_descriptor_callback(uint8_t speed)
{
    (void)speed;
    return device_quality_descriptor;
}

static const char *string_descriptor_callback(uint8_t speed, uint8_t index)
{
    (void)speed;
    if (index >= (sizeof(string_descriptors) / sizeof(char *))) {
        return NULL;
    }
    return string_descriptors[index];
}

static const struct usb_descriptor cdc_descriptor = {
    .device_descriptor_callback = device_descriptor_callback,
    .config_descriptor_callback = config_descriptor_callback,
    .device_quality_descriptor_callback = device_quality_descriptor_callback,
    .string_descriptor_callback = string_descriptor_callback
};

/* ======================= 端点缓冲（非缓存 + 32B 对齐） ======================= */
static USB_NOCACHE_RAM_SECTION USB_MEM_ALIGNX uint8_t read_buffer[CDC_BUF_SIZE];
static USB_NOCACHE_RAM_SECTION USB_MEM_ALIGNX uint8_t write_buffer[CDC_BUF_SIZE];
static uint8_t snapshot[CDC_BUF_SIZE];

static volatile uint32_t rx_len;
static volatile uint32_t rx_total;
static volatile uint32_t tx_total;
static volatile uint8_t ep_tx_busy_flag;
static volatile uint8_t dtr_enable;
static volatile uint8_t echo_enable = 1;

static const char banner[] =
    "\r\n=== STM32H743 CherryUSB CDC ACM (etm-trace target) ===\r\n"
    "commands: PING | INFO | MARK | ECHO on|off | ECHO <text>\r\n";

/* ======================= 事件回调 ======================= */
static void usbd_event_handler(uint8_t busid, uint8_t event)
{
    switch (event) {
        case USBD_EVENT_RESET:
            USB_LOG_RAW("[usb] reset\r\n");
            break;
        case USBD_EVENT_CONNECTED:
            USB_LOG_RAW("[usb] connected\r\n");
            break;
        case USBD_EVENT_DISCONNECTED:
            USB_LOG_RAW("[usb] disconnected\r\n");
            break;
        case USBD_EVENT_RESUME:
        case USBD_EVENT_SUSPEND:
            break;
        case USBD_EVENT_CONFIGURED:
            USB_LOG_RAW("[usb] configured\r\n");
            ep_tx_busy_flag = 0;
            usbd_ep_start_read(busid, CDC_OUT_EP, read_buffer, CDC_BUF_SIZE);
            break;
        case USBD_EVENT_SET_REMOTE_WAKEUP:
        case USBD_EVENT_CLR_REMOTE_WAKEUP:
            break;
        default:
            break;
    }
}

void usbd_cdc_acm_bulk_out(uint8_t busid, uint8_t ep, uint32_t nbytes)
{
    (void)ep;
    rx_len = nbytes;
    rx_total += nbytes;
    g_trace.usb_rx = rx_total;
    usbd_ep_start_read(busid, CDC_OUT_EP, read_buffer, CDC_BUF_SIZE);
}

void usbd_cdc_acm_bulk_in(uint8_t busid, uint8_t ep, uint32_t nbytes)
{
    tx_total += nbytes;
    g_trace.usb_tx = tx_total;

    if ((nbytes % usbd_get_ep_mps(busid, ep)) == 0 && nbytes) {
        usbd_ep_start_write(busid, CDC_IN_EP, NULL, 0);
    } else {
        ep_tx_busy_flag = 0;
    }
}

static struct usbd_endpoint cdc_out_ep = {
    .ep_addr = CDC_OUT_EP,
    .ep_cb = usbd_cdc_acm_bulk_out
};

static struct usbd_endpoint cdc_in_ep = {
    .ep_addr = CDC_IN_EP,
    .ep_cb = usbd_cdc_acm_bulk_in
};

static struct usbd_interface intf0;
static struct usbd_interface intf1;

void usbd_cdc_acm_set_dtr(uint8_t busid, uint8_t intf, bool dtr)
{
    (void)busid;
    (void)intf;
    dtr_enable = dtr ? 1 : 0;
}

void usbd_cdc_acm_set_rts(uint8_t busid, uint8_t intf, bool rts)
{
    (void)busid;
    (void)intf;
    (void)rts;
}

int cdc_acm_init(uint8_t busid, uintptr_t reg_base)
{
    usbd_desc_register(busid, &cdc_descriptor);

    usbd_add_interface(busid, usbd_cdc_acm_init_intf(busid, &intf0));
    usbd_add_interface(busid, usbd_cdc_acm_init_intf(busid, &intf1));
    usbd_add_endpoint(busid, &cdc_out_ep);
    usbd_add_endpoint(busid, &cdc_in_ep);

    return usbd_initialize(busid, reg_base, usbd_event_handler);
}

/* ======================= 发送 ======================= */
static void cdc_write(const uint8_t *data, uint32_t len)
{
    if (!dtr_enable || len == 0 || len > CDC_BUF_SIZE) {
        return;
    }
    if (ep_tx_busy_flag) {
        return;
    }
    memcpy(write_buffer, data, len);
    ep_tx_busy_flag = 1;
    if (usbd_ep_start_write(0, CDC_IN_EP, write_buffer, len) != 0) {
        ep_tx_busy_flag = 0;
    }
}

static void cdc_puts(const char *s)
{
    cdc_write((const uint8_t *)s, (uint32_t)strlen(s));
}

/* ======================= RTT 日志 ======================= */
int trace_log_printf(const char *fmt, ...)
{
    char buf[192];
    va_list ap;
    int n;

    va_start(ap, fmt);
    n = vsnprintf(buf, sizeof(buf), fmt, ap);
    va_end(ap);

    if (n > 0) {
        uint32_t len = ((uint32_t)n < sizeof(buf)) ? (uint32_t)n : (uint32_t)(sizeof(buf) - 1u);
        SEGGER_RTT_Write(0, buf, len);
    }
    return n;
}

/* ======================= CDC 命令行 ======================= */
static void handle_command(const uint8_t *buf, uint32_t len)
{
    char line[128];
    uint32_t n = (len < sizeof(line) - 1u) ? len : (sizeof(line) - 1u);

    memcpy(line, buf, n);
    line[n] = '\0';
    while (n > 0u && (line[n - 1u] == '\r' || line[n - 1u] == '\n')) {
        line[--n] = '\0';
    }

    if (strcmp(line, "PING") == 0) {
        cdc_puts("PONG\r\n");
    } else if (strcmp(line, "INFO") == 0) {
        char out[192];
        snprintf(out, sizeof(out),
                 "chip=STM32H743 sysclk=%luHz hclk=%luHz clk_err=%lu loops=%lu marks=%lu rx=%lu tx=%lu\r\n",
                 (unsigned long)HAL_RCC_GetSysClockFreq(), (unsigned long)HAL_RCC_GetHCLKFreq(),
                 (unsigned long)g_clock_err,
                 (unsigned long)g_trace.loops, (unsigned long)g_trace.marks,
                 (unsigned long)rx_total, (unsigned long)tx_total);
        cdc_puts(out);
    } else if (strcmp(line, "MARK") == 0) {
        trace_hook_mark();
        cdc_puts("OK MARK\r\n");
    } else if (strcmp(line, "ECHO on") == 0) {
        echo_enable = 1;
        cdc_puts("OK ECHO on\r\n");
    } else if (strcmp(line, "ECHO off") == 0) {
        echo_enable = 0;
        cdc_puts("OK ECHO off\r\n");
    } else if (strncmp(line, "ECHO ", 5) == 0) {
        cdc_write((const uint8_t *)(line + 5), n - 5u);
        cdc_puts("\r\n");
    } else if (n == 0u) {
        /* 空行忽略 */
    } else {
        /* 普通文本已由主循环原样回显，不追加命令错误。 */
    }
}

/* ======================= 时钟：480MHz ======================= */
static void system_clock_init(void)
{
    RCC_OscInitTypeDef osc = { 0 };
    RCC_ClkInitTypeDef clk = { 0 };
    RCC_PeriphCLKInitTypeDef periph = { 0 };

    /* ① H743 跑 480MHz 必须 VOS0，且写 VOS 之前要先清 PWR_CR3.SCUEN
     *   （官方 RTT 测速固件里记过这个坑：SCUEN 不清，VOS 写不进去，
     *    表现为永远等不到 VOSRDY） */
    PWR->CR3 &= ~PWR_CR3_SCUEN;
    HAL_PWREx_ConfigSupply(PWR_LDO_SUPPLY);
    __HAL_PWR_VOLTAGESCALING_CONFIG(PWR_REGULATOR_VOLTAGE_SCALE0);
    while (!__HAL_PWR_GET_FLAG(PWR_FLAG_VOSRDY)) {
    }

    /* ② Flash 等待周期 */
    __HAL_FLASH_SET_LATENCY(H7_CDC_FLASH_LATENCY);

    /* ③ PLL1: 25/5*192 = 960MHz VCO -> SYSCLK 480MHz
     *   （H7 的 PLLN 字段是"值-1"，HAL 里写 192 会编码成 191） */
    osc.OscillatorType = RCC_OSCILLATORTYPE_HSE;
    osc.HSEState = RCC_HSE_ON;
    osc.PLL.PLLState = RCC_PLL_ON;
    osc.PLL.PLLSource = RCC_PLLSOURCE_HSE;
    osc.PLL.PLLM = H7_CDC_PLL_M;
    osc.PLL.PLLN = H7_CDC_PLL_N;
    osc.PLL.PLLP = H7_CDC_PLL_P;
    osc.PLL.PLLQ = 2;
    osc.PLL.PLLR = H7_CDC_PLL_R;
    osc.PLL.PLLRGE = RCC_PLL1VCIRANGE_2; /* 4~8MHz 参考 */
    osc.PLL.PLLVCOSEL = RCC_PLL1VCOWIDE;
    osc.PLL.PLLFRACN = 0;
    if (HAL_RCC_OscConfig(&osc) != HAL_OK) {
        g_clock_err |= 1u;
    }

    /* ④ AXI/HCLK /2 = 240MHz，APB1/2/3 /2 = 120MHz，APB4 /4 = 60MHz */
    clk.ClockType = RCC_CLOCKTYPE_HCLK | RCC_CLOCKTYPE_SYSCLK |
                    RCC_CLOCKTYPE_PCLK1 | RCC_CLOCKTYPE_PCLK2 | RCC_CLOCKTYPE_D1PCLK1 | RCC_CLOCKTYPE_D3PCLK1;
    clk.SYSCLKSource = RCC_SYSCLKSOURCE_PLLCLK;
    clk.SYSCLKDivider = H7_CDC_D1CPRE;
    clk.AHBCLKDivider = H7_CDC_HPRE;
    clk.APB3CLKDivider = RCC_APB3_DIV2;
    clk.APB1CLKDivider = RCC_APB1_DIV2;
    clk.APB2CLKDivider = RCC_APB2_DIV2;
    clk.APB4CLKDivider = RCC_APB4_DIV4;
    if (HAL_RCC_ClockConfig(&clk, H7_CDC_FLASH_LATENCY) != HAL_OK) {
        g_clock_err |= 2u;
    }

    /* ⑤ USB 内核时钟 = PLL3Q 48MHz（由 HSE 25MHz 派生）
     *   依据两处参考实现（都用 PLL3Q，不用 HSI48）：
     *     · STM32Cube_FW_H7_V1.11.2  Projects/STM32H743I-EVAL/Applications/
     *       USB_Device/CDC_Standalone（USB2_OTG_FS + PA11/PA12）
     *     · 本站已验证的 firmware/stm32h7b0_cdc_trace
     *   ⚠️ 原来这里用 HSI48：实机 USBSEL 确实读回 3(=HSI48)、HSI48RDY 也置位，
     *      但 USB2_OTG_FS 的核软复位（GRSTCTL.CSRST）**永不完成**，于是
     *      dwc2_set_mode 死等 GINTSTS.CMOD（GINTSTS 对强制 HOST/DEVICE 完全无响应）。
     *      换成 PLL3Q 后核复位与枚举才正常。
     *   PLL3: 25/25 = 1MHz 参考 ×336 = 336MHz VCO，Q=7 → 48MHz。 */
    periph.PeriphClockSelection = RCC_PERIPHCLK_USB;
    periph.UsbClockSelection = RCC_USBCLKSOURCE_PLL3;
    periph.PLL3.PLL3M = 25;
    periph.PLL3.PLL3N = 336;
    periph.PLL3.PLL3P = 2;
    periph.PLL3.PLL3Q = 7;
    periph.PLL3.PLL3R = 2;
    periph.PLL3.PLL3RGE = RCC_PLL3VCIRANGE_0;
    periph.PLL3.PLL3VCOSEL = RCC_PLL3VCOMEDIUM;
    periph.PLL3.PLL3FRACN = 0;
    if (HAL_RCCEx_PeriphCLKConfig(&periph) != HAL_OK) {
        g_clock_err |= 4u;
    }

    /* ⑥ USB 供电 —— H7 必须显式处理，漏了就是"能枚举但设备描述符请求失败"。
     *   （H7B0 工程有这一段，H743 移植时整段漏掉了。）
     *   PWR_CR3.USB33DEN   : VDD33USB 电压检测器，外部供 3.3V 也必须开。
     *   PWR_CR3.USBREGEN   : 片内 USB 稳压器，只在板子不给 VDD33USB 时才开。
     */
    HAL_PWREx_EnableUSBVoltageDetector();
#if H7_CDC_USB_INTERNAL_REGULATOR
    if (HAL_PWREx_EnableUSBReg() != HAL_OK) {
        g_clock_err |= 8u;
    }
#else
    CLEAR_BIT(PWR->CR3, PWR_CR3_USBREGEN);
#endif

    g_sysclk_hz = HAL_RCC_GetSysClockFreq();
}

/* ======================= main ======================= */
int main(void)
{
    probe_stage(1);

    HAL_Init();
    probe_stage(2);

    system_clock_init();
    probe_stage(3);

    SystemCoreClockUpdate();
    trace_log_printf("\r\n=== STM32H743 CDC ACM (CherryUSB %d.%d.%d) ===\r\n", 1, 6, 1);
    trace_log_printf("SYSCLK=%lu Hz, HCLK=%lu Hz, clock_err=%lu\r\n",
                     (unsigned long)HAL_RCC_GetSysClockFreq(),
                     (unsigned long)HAL_RCC_GetHCLKFreq(),
                     (unsigned long)g_clock_err);
    probe_stage(4);

    /* I-Cache 开、D-Cache 关：USB 缓冲在 .noncacheable；关掉 D-Cache 后
     * SWD/ETM 工具读到的内存与 CPU 视图一致，排查省事。 */
    SCB_EnableICache();
    probe_stage(5);

    SEGGER_RTT_Init();
    /* ETM/CDC 可独立使用：未连接 RTT 读取器时，调试日志满不能卡住 echo 主循环。 */
    SEGGER_RTT_SetFlagsUpBuffer(0, SEGGER_RTT_MODE_NO_BLOCK_SKIP);
    trace_hook_init();
    probe_stage(6);

    trace_hook_entry();
    if (cdc_acm_init(0, USB_OTG_FS_PERIPH_BASE) != 0) {
        g_clock_err |= 16u;
        trace_log_printf("USB initialization failed; trace ready remains zero\r\n");
        for (;;) { __WFI(); }
    }
    trace_log_printf("usb initialized (USB_OTG_FS_PERIPH_BASE=0x%08lx)\r\n",
                     (unsigned long)USB_OTG_FS_PERIPH_BASE);
    probe_stage(7);

    trace_hook_ready();
    probe_stage(8);

    for (;;) {
        g_trace.loops++;

        if (rx_len
#if H7_CDC_BANNER_INTERVAL_MS > 0
            /* 周期 banner 占用 IN 端点时保留待回显数据，不交给忙即丢弃的发送函数。 */
            && !ep_tx_busy_flag
#endif
        ) {
            uint32_t n = rx_len;

            memcpy(snapshot, read_buffer, n);
            rx_len = 0;

            if (echo_enable) {
                cdc_write(snapshot, n);
            }
            handle_command(snapshot, n);
            trace_log_printf("[cdc] rx %lu bytes\r\n", (unsigned long)n);
        }

        if ((g_trace.loops & 0xFFFFFu) == 0u) {
            trace_hook_mark();
        }

        static uint8_t banner_sent = 0;
#if H7_CDC_BANNER_INTERVAL_MS > 0
        static uint32_t banner_last_ms;
        uint32_t banner_now_ms = HAL_GetTick();
#endif
        if (dtr_enable && !ep_tx_busy_flag &&
            (!banner_sent
#if H7_CDC_BANNER_INTERVAL_MS > 0
             || (uint32_t)(banner_now_ms - banner_last_ms) >= H7_CDC_BANNER_INTERVAL_MS
#endif
            )) {
            cdc_puts(banner);
            banner_sent = 1;
#if H7_CDC_BANNER_INTERVAL_MS > 0
            banner_last_ms = banner_now_ms;
#endif
        }
    }
}

/* ======================= 中断 ======================= */
/* OTG_FS_IRQHandler / OTG_HS_IRQHandler 由 cherryusb/port/dwc2/usb_glue_st_h7b0.c 提供 */

void SysTick_Handler(void)
{
    HAL_IncTick();
}

void HardFault_Handler(void)
{
    __asm volatile("bkpt #0");
    for (;;) {
    }
}

void NMI_Handler(void)
{
    for (;;) {
    }
}

void MemManage_Handler(void)
{
    for (;;) {
    }
}

void BusFault_Handler(void)
{
    for (;;) {
    }
}

void UsageFault_Handler(void)
{
    for (;;) {
    }
}

void SVC_Handler(void)
{
    for (;;) {
    }
}

void DebugMon_Handler(void)
{
    for (;;) {
    }
}

void PendSV_Handler(void)
{
    for (;;) {
    }
}
