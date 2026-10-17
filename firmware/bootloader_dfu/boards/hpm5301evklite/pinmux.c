/*
 * Copyright (c) 2023 HPMicro
 *
 * SPDX-License-Identifier: BSD-3-Clause
 *
 */

/*
 * Note:
 * PY and PZ IOs: if any SOC pin function needs to be routed to these IOs,
 * besides of IOC, PIOC/BIOC needs to be configured SOC_GPIO_X_xx, so that
 * expected SoC function can be enabled on these IOs.
 *
 */
#include "board.h"
#include "pinmux.h"
#include "hpm_gpio_drv.h"
#include "hpm_gpiom_drv.h"

// Miscellaneous
#define DFU_PIN IOC_PAD_PA03

// LED (single board LED, active low)
#define LED1_PIN IOC_PAD_PA10
#define LED2_PIN IOC_PAD_PA10

// UART0
#define ISP_TX_PIN IOC_PAD_PA00
#define ISP_RX_PIN IOC_PAD_PA01

// UART3
#define AUX_RX_PIN IOC_PAD_PB14
#define AUX_TX_PIN IOC_PAD_PB15

// UART2 (CDC VCOM of the APP, parked as input here)
#define UART2_TXD_PIN IOC_PAD_PB08
#define UART2_RXD_PIN IOC_PAD_PB09

// USB
#define USB_DP_PIN IOC_PAD_PA24
#define USB_DN_PIN IOC_PAD_PA25

void init_py_pins_as_pgpio(void)
{
    /* Set PY00-PY01 default function to PGPIO */
    HPM_PIOC->PAD[IOC_PAD_PY00].FUNC_CTL = PIOC_PY00_FUNC_CTL_PGPIO_Y_00;
    HPM_PIOC->PAD[IOC_PAD_PY01].FUNC_CTL = PIOC_PY01_FUNC_CTL_PGPIO_Y_01;
}

/*
 * The DAP target pins (PA04-PA08, wired to the J5 header) are the SoC's own
 * JTAG port. The bootloader must NOT reconfigure them: while the board sits
 * in DFU/upgrade mode, J5 still works as the chip's JTAG debug port, which is
 * the recovery/self-debug path for this board.
 */
void init_gpio_swj_pins(void)
{
}

/**
 * @brief Init UART pins
 * @param ptr UART_Type instance
 */
void init_uart_pins(UART_Type *ptr)
{
    if (ptr == HPM_UART0)
    {
        HPM_IOC->PAD[ISP_TX_PIN].FUNC_CTL = IOC_PA00_FUNC_CTL_UART0_TXD;
        HPM_IOC->PAD[ISP_RX_PIN].FUNC_CTL = IOC_PA01_FUNC_CTL_UART0_RXD;
    }
    else if (ptr == HPM_UART2)
    {
        HPM_IOC->PAD[UART2_TXD_PIN].FUNC_CTL = IOC_PB08_FUNC_CTL_UART2_TXD;
        HPM_IOC->PAD[UART2_RXD_PIN].FUNC_CTL = IOC_PB09_FUNC_CTL_UART2_RXD;
    }
    else if (ptr == HPM_UART3)
    {
        HPM_IOC->PAD[AUX_TX_PIN].FUNC_CTL = IOC_PB15_FUNC_CTL_UART3_TXD;
        HPM_IOC->PAD[AUX_RX_PIN].FUNC_CTL = IOC_PB14_FUNC_CTL_UART3_RXD;
    }
    else
    {
        ;
    }
}

/**
 * @brief Init LED pins (PA10, active low, default off = high)
 * @param None
 */
void init_led_pins(void)
{
    // set alternate function as gpio
    HPM_IOC->PAD[LED1_PIN].FUNC_CTL = IOC_PAD_FUNC_CTL_ALT_SELECT_SET(0);

    // set pad parameter
    HPM_IOC->PAD[LED1_PIN].PAD_CTL = IOC_PAD_PAD_CTL_HYS_SET(0) |
                                     IOC_PAD_PAD_CTL_PRS_SET(0) |
                                     IOC_PAD_PAD_CTL_PS_SET(0) |
                                     IOC_PAD_PAD_CTL_PE_SET(0) |
                                     IOC_PAD_PAD_CTL_KE_SET(0) |
                                     IOC_PAD_PAD_CTL_OD_SET(0) |
                                     IOC_PAD_PAD_CTL_SR_SET(0) |
                                     IOC_PAD_PAD_CTL_SPD_SET(0) |
                                     IOC_PAD_PAD_CTL_DS_SET(0);

    // set pin controller
    gpiom_set_pin_controller(HPM_GPIOM, GPIO_GET_PORT_INDEX(LED1_PIN), GPIO_GET_PIN_INDEX(LED1_PIN), gpiom_soc_gpio0);

    // set output/input mode
    gpio_set_pin_output(HPM_GPIO0, GPIO_GET_PORT_INDEX(LED1_PIN), GPIO_GET_PIN_INDEX(LED1_PIN));

    // set pin default state (LED active low: off = high)
    gpio_write_pin(HPM_GPIO0, GPIO_GET_PORT_INDEX(LED1_PIN), GPIO_GET_PIN_INDEX(LED1_PIN), 1);
}

/**
 * @brief Power control pins: none on this board (no-op, kept for API parity)
 * @param None
 */
void init_power_pins(void)
{
}

void init_model_sel_pins(void)
{
}

void init_dfu_pins(void)
{
    uint32_t pad_ctl = IOC_PAD_PAD_CTL_HYS_SET(1) | // hyster enable
                       IOC_PAD_PAD_CTL_PRS_SET(0) | // pull res 100k
                       IOC_PAD_PAD_CTL_PS_SET(0) |  // pull down
                       IOC_PAD_PAD_CTL_PE_SET(1) |  // pull enable
                       IOC_PAD_PAD_CTL_KE_SET(0) |
                       IOC_PAD_PAD_CTL_OD_SET(0) |
                       IOC_PAD_PAD_CTL_SR_SET(0) |
                       IOC_PAD_PAD_CTL_SPD_SET(0) |
                       IOC_PAD_PAD_CTL_DS_SET(0);

    HPM_IOC->PAD[DFU_PIN].FUNC_CTL = IOC_PAD_FUNC_CTL_ALT_SELECT_SET(0);
    HPM_IOC->PAD[DFU_PIN].PAD_CTL = pad_ctl;

    gpiom_set_pin_controller(HPM_GPIOM, GPIO_GET_PORT_INDEX(DFU_PIN), GPIO_GET_PIN_INDEX(DFU_PIN), gpiom_soc_gpio0);
    gpio_set_pin_input(HPM_GPIO0, GPIO_GET_PORT_INDEX(DFU_PIN), GPIO_GET_PIN_INDEX(DFU_PIN));
}

void init_usb_pins(USB_Type *ptr)
{
    if (ptr == HPM_USB0)
    {
        HPM_IOC->PAD[USB_DP_PIN].FUNC_CTL = IOC_PAD_FUNC_CTL_ANALOG_MASK;
        HPM_IOC->PAD[USB_DN_PIN].FUNC_CTL = IOC_PAD_FUNC_CTL_ANALOG_MASK;
    }
}
