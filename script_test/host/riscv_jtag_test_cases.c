/* The model returns the previous request, latches BUSY/FAILED, and clears
 * them only after the real C engine emits dtmcs.dmireset through the TAP. */
static uint32_t memory[512], model_sbcs, model_addr, model_data;
static uint64_t pending;
static uint32_t sticky, scans, resets, writes, sbcs_reads;
static uint32_t fault_addr, fault_op, fault_skip, faults, fault_status;
static uint32_t tap_state = 1U, active_ir = IR_DMI, shifted, shift_bits;
static uint32_t hold_counter, dynamic_hold;
static const uint8_t next_state[16][2] = {
    {1,0},{1,2},{3,9},{4,5},{4,5},{6,8},{6,7},{4,8},
    {1,2},{10,0},{11,12},{11,12},{13,15},{13,14},{11,15},{1,2}
};

static void bus_read(void)
{
    if (model_addr < 0x1000U || model_addr >= 0x1800U) {
        model_sbcs |= 2U << 12;
        return;
    }
    model_data = dynamic_hold ? ++hold_counter : memory[(model_addr - 0x1000U) / 4U];
    if (model_sbcs & SBCS_SBAUTOINC) model_addr += 4U;
}

void JTAG_Sequence(uint32_t info, const uint8_t *tdi, uint8_t *tdo)
{
    uint32_t n = info & 63U, tms = (info >> 6) & 1U;
    if (!n) n = 64U;
    if (tdo) memset(tdo, 0, (n + 7U) / 8U);
    for (uint32_t bit = 0; bit < n; bit++) {
        uint32_t in = (tdi[bit / 8U] >> (bit % 8U)) & 1U;
        if (tap_state == 4U || tap_state == 11U) {
            if (shift_bits < 32U) shifted |= in << shift_bits;
            shift_bits++;
        }
        tap_state = next_state[tap_state][tms];
        if (tap_state == 3U || tap_state == 10U) { shifted = 0; shift_bits = 0; }
        if (tap_state == 15U) active_ir = shifted & 31U;
        if (tap_state == 8U && active_ir == IR_DTMCS && (shifted & (1U << 16))) {
            sticky = 0; pending = 0; resets++;
        }
    }
}

uint32_t JTAG_DMI_Scan(uint32_t idle, uint32_t lo, uint32_t mid,
                       uint32_t hi, uint32_t *tdo_hi)
{
    (void)idle;
    assert(active_ir == IR_DMI);
    uint64_t dr = lo | ((uint64_t)(mid & 255U) << 32) | ((uint64_t)(hi & 1U) << 40);
    uint32_t op = dr & 3U, addr = (dr >> 34) & 127U, data = dr >> 2;
    uint64_t response = sticky ? sticky : pending;
    scans++;
    if (!sticky) {
        if (faults && addr == fault_addr && op == fault_op) {
            if (fault_skip) fault_skip--;
            else { faults--; sticky = fault_status; pending = fault_status; }
        }
        if (!sticky) {
            uint32_t value = 0;
            if (op == DMI_OP_READ) {
                if (addr == DM_SBCS) { value = model_sbcs; sbcs_reads++; }
                if (addr == DM_DMSTATUS) value = 0x00400CA2U;
                if (addr == DM_SBDATA0) {
                    value = model_data;
                    if (!(model_sbcs & (SBCS_SBBUSYERROR | SBCS_SBERROR)) &&
                        (model_sbcs & SBCS_SBREADONDATA)) bus_read();
                }
            }
            if (op == DMI_OP_WRITE) {
                if (addr == DM_SBCS) {
                    uint32_t errors = model_sbcs & (SBCS_SBBUSYERROR | SBCS_SBERROR);
                    model_sbcs = (data & ~(SBCS_SBBUSYERROR | SBCS_SBERROR)) | (errors & ~data);
                }
                if (addr == DM_SBADDRESS0) {
                    model_addr = data;
                    if (model_sbcs & SBCS_SBREADONADDR) bus_read();
                }
                if (addr == DM_SBDATA0) {
                    assert(model_addr >= 0x1000U && model_addr < 0x1800U);
                    memory[(model_addr - 0x1000U) / 4U] = data;
                    writes++;
                    if (model_sbcs & SBCS_SBAUTOINC) model_addr += 4U;
                }
            }
            pending = ((uint64_t)value << 2) | ((uint64_t)addr << 34);
        }
    }
    *tdo_hi = response >> 32;
    return response;
}

static void setup(void)
{
    for (uint32_t i = 0; i < 512; i++) memory[i] = 0xAB120000U + i * 0x10203U;
    pending = sticky = scans = resets = writes = sbcs_reads = 0;
    faults = fault_skip = hold_counter = dynamic_hold = 0;
    tap_state = 1; active_ir = IR_DMI;
    model_sbcs = model_addr = model_data = test_clock = 0;
    s_open = 1; s_sbcs_valid = s_hold_ok = s_sba_failed = 0;
    s_delay = 8; s_dmi_stamp = 0; s_hold_reads = 0;
    s_sba_err_events = s_sba_err_retries = s_sba_err_recover = 0;
}

static void inject(uint32_t addr, uint32_t op, uint32_t skip, uint32_t status, uint32_t count)
{
    fault_addr = addr; fault_op = op; fault_skip = skip; fault_status = status; faults = count;
}

int main(void)
{
    uint32_t value = 0, out[8];
    setup(); inject(DM_DMSTATUS, DMI_OP_READ, 0, 3, 1);
    assert(dmi_read(DM_DMSTATUS, &value) == 0);
    assert(value == 0x00400CA2U && resets == 1 && s_delay == 10);
    setup(); inject(DM_DMSTATUS, DMI_OP_READ, 0, 1, 1);
    assert(dmi_read(DM_DMSTATUS, &value) < 0 && resets == 0);
    setup(); inject(DM_DMSTATUS, DMI_OP_READ, 0, 3, 100);
    assert(dmi_read(DM_DMSTATUS, &value) < 0 && resets == 8);
    setup(); riscv_jtag_set_delay(UINT32_MAX);
    assert(s_delay == 255 && DAP_Data.clock_delay == 255);
    inject(DM_DMSTATUS, DMI_OP_READ, 0, 3, 1);
    assert(dmi_read(DM_DMSTATUS, &value) == 0 && s_delay == 255);
    setup(); inject(DM_SBCS, DMI_OP_READ, 0, 2, 1);
    assert(sba_check_errors() < 0 && s_last_sbcs == UINT32_MAX && resets == 1);
    setup(); model_sbcs = SBCS_SBBUSYERROR;
    inject(DM_SBCS, DMI_OP_WRITE, 0, 2, 1);
    assert(sba_clear_errors() == UINT32_MAX && !s_sbcs_valid);
    setup(); inject(DM_SBDATA0, DMI_OP_WRITE, 0, 3, 1);
    assert(riscv_jtag_write(0x1000U, (uint8_t *)memory, 32) < 0);
    assert(resets == 1 && writes == 0); /* no replay of an uncertain write */
    setup(); inject(DM_SBDATA0, DMI_OP_WRITE, 7, 2, 1);
    assert(riscv_jtag_write(0x1000U, (uint8_t *)memory, 32) < 0);
    assert(resets == 1 && writes == 7); /* last write response is checked */
    setup(); inject(DM_SBDATA0, DMI_OP_READ, 3, 3, 1);
    assert(riscv_jtag_read(0x1000U, (uint8_t *)out, sizeof(out)) == 0);
    assert(memcmp(out, memory, sizeof(out)) == 0 && resets == 1);
    setup(); dynamic_hold = 1;
    assert(riscv_jtag_hold_prepare(0x1000U) == 0);
    for (uint32_t i = 1; i <= 96; i++) {
        assert(riscv_jtag_hold_read(&value) == 0 && value == i);
    }
    setup(); assert(riscv_jtag_hold_prepare(0x1000U) == 0);
    s_hold_reads = 31; value = 0xBADBADU;
    inject(DM_SBCS, DMI_OP_READ, 0, 3, 1);
    assert(riscv_jtag_hold_read(&value) < 0 && value == 0xBADBADU && !s_hold_ok);
    /* Both target and host pointers can be unaligned. Guard bytes ensure the
     * last partial word never writes beyond the requested destination. */
    for (uint32_t head = 0; head < 4; head++) {
        for (uint32_t host = 0; host < 4; host++) {
            for (uint32_t len = 1; len <= 67; len++) {
                uint8_t storage[80]; setup(); memset(storage, 0xCD, sizeof(storage));
                uint8_t *dst = storage + 4 + host;
                assert(riscv_jtag_read_once(0x1000U + head, dst, len) == 0);
                assert(memcmp(dst, (uint8_t *)memory + head, len) == 0);
                for (uint32_t k = 0; k < 4 + host; k++) assert(storage[k] == 0xCD);
                for (uint32_t k = 4 + host + len; k < sizeof(storage); k++) assert(storage[k] == 0xCD);
                uint32_t words = (head + len + 3U) / 4U;
                assert(scans == words + 6U); /* includes first SBCS configuration */
                assert(sbcs_reads == 1U);
            }
        }
    }
    for (uint32_t n = 1; n <= 256; n++) {
        uint32_t block[256]; setup();
        assert(riscv_jtag_read_once(0x1000U, (uint8_t *)block, 4) == 0);
        uint32_t before = scans, checks = sbcs_reads;
        assert(riscv_jtag_read_once(0x1000U, (uint8_t *)block, n * 4) == 0);
        assert(memcmp(block, memory, n * 4) == 0);
        assert(scans - before == n + 4U && sbcs_reads - checks == 1U);
    }
    for (uint32_t status = 1; status <= 3; status++) {
        setup(); inject(DM_SBCS, DMI_OP_READ, 0, status, 1);
        assert(riscv_jtag_read_once(0x1000U, (uint8_t *)out, sizeof(out)) < 0);
        assert(s_sba_failed && s_last_sbcs == UINT32_MAX);
    }
    setup(); assert(riscv_jtag_read_once(0x1000U, (uint8_t *)out, sizeof(out)) == 0);
    model_sbcs |= SBCS_SBBUSYERROR;
    assert(riscv_jtag_read_once(0x1000U, (uint8_t *)out, sizeof(out)) < 0);
    assert(s_sba_failed && !(model_sbcs & SBCS_SBBUSYERROR) && s_sba_err_events == 1);
    assert(riscv_jtag_read(0x1000U, (uint8_t *)out, sizeof(out)) == 0);
    assert(memcmp(out, memory, sizeof(out)) == 0);
    setup(); uint32_t before = scans;
    assert(riscv_jtag_read_once(UINT32_MAX - 1U, (uint8_t *)out, 8) < 0);
    assert(riscv_jtag_read_once(0x1000U, NULL, 8) < 0 && scans == before);
    puts("JTAG block pipeline: 1072 target/host alignment and guarded-length cases, 256 cached scan-count cases, N+4 scans, final SBCS errors rejected PASS");
    puts("JTAG production C: sticky DTM recovery, no write replay, unknown SBCS rejected, block restart and 32-sample pipeline boundaries PASS");
    return 0;
}
