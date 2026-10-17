#ifndef TRACE_H
#define TRACE_H
#include <stdint.h>

/* Hardware readbacks in AXI SRAM, readable through a debugger. */
typedef struct {
    uint32_t magic, stage, error, nominal_traceclk_hz;
    uint32_t dbgmcu_idcode, dbgmcu_cr;
    uint32_t etm_idr[6];
    uint32_t etm_auth, etm_status, etm_power, etm_config;
    uint32_t etm_traceid, etm_victl, etm_syncp, etm_prgctl;
    uint32_t funnel_ctrl, etf_ctrl, etf_mode, etf_status, etf_ffcr;
    uint32_t tpiu_port_size, tpiu_ffcr, tpiu_ffsr;
    uint32_t gpioe_moder, gpioe_afrl;
} TraceDiagnostics;

extern volatile TraceDiagnostics g_trace;
int trace_init(void);
#endif
