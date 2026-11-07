static uint32_t get32(const uint8_t *p)
{ return (uint32_t)p[0] | (uint32_t)p[1]<<8 | (uint32_t)p[2]<<16 | (uint32_t)p[3]<<24; }
static void reset_test(void)
{
    scope_sampler_stop(1);
    scope_usb_reset_apply();
    scope_sampler_poll();
    test_clock = test_previous = test_calls = test_last_dap = 0;
    test_fail_read = 0; test_stop_call = 0; test_cdc = 1; test_read_cost = 36;
    test_fail_call = 0; test_tx_count = 0; test_zlp_count = 0;
}
int main(void)
{
    scope_var_t one = { 0x20001044U, 4, 4, 0 };
    uint32_t status_words[12];
    reset_test();
    assert(scope_sampler_configure(3, SCOPE_FLAG_DISCARD, 1, &one) == 0);
    assert(s_period_ticks == 72 && s_time_step == 3 && s_time_version == 1);
    assert(scope_start_now() == 0);
    test_clock = s_next_tick;
    scope_sampler_poll();
    assert(s_produced == 1 && s_t_time == 3);
    assert(s_pkt[s_fill_buf][2] == 0 || s_pkt[s_fill_buf][2] == 1);
    assert(scope_sampler_configure_ticks(60, SCOPE_FLAG_DISCARD, 1, &one) == 0);
    assert(!s_running && s_period_ticks == 60 && s_time_step == 60 && s_time_version == 2);
    assert(scope_start_now() == 0);
    uint8_t data_buf = s_fill_buf;
    for (unsigned i = 0; i < 124; i++) {
        test_clock = s_next_tick;
        scope_sampler_poll();
    }
    assert(s_produced == 124 && s_pkts == 2 && s_t_time == 7440);
    assert(s_pkt[data_buf][2] == 2 && get32(s_pkt[data_buf] + 8) == 0);
    for (unsigned i = 1; i < 124; i++)
        assert(get32(s_pkt[data_buf]+16+i*4) == get32(s_pkt[data_buf]+16+(i-1)*4)+1);
    scope_sampler_status(status_words, 12);
    assert(status_words[0] & (1U<<2));
    assert((status_words[11] & 0xffff) == 60 && (status_words[11] & (1U<<17)));
    assert(scope_sampler_configure_ticks(1, 0, 1, &one) == 0 && s_period_ticks == 48);
    assert(scope_sampler_configure_ticks(UINT32_MAX, 0, 1, &one) == 0 && s_period_ticks == 24000000);
    reset_test();
    assert(scope_sampler_configure_ticks(48, SCOPE_FLAG_DISCARD | SCOPE_FLAG_FAST_BATCH, 1, &one) == 0);
    assert(scope_start_now() == 0);
    test_clock = s_next_tick;
    scope_sampler_poll();
    assert(s_produced == 64 && s_t_time == 64 * 48 && s_dropped == 0);
    for (unsigned i = 0; i < 1; i++) { test_clock = s_next_tick; scope_sampler_poll(); }
    assert(s_produced == 128 && s_pkts == 2 && s_fill_n == 4);
    /* Crossing a packet boundary must publish the old packet fully flushed,
     * and place subsequent posted reads only in the new filling buffer. */
    uint8_t batch_buf = 0;
    while (s_pkt[batch_buf][3] != SCOPE_KIND_DATA) { batch_buf++; assert(batch_buf < SCOPE_TX_BUFS); }
    for (unsigned i = 1; i < 124; i++)
        assert(get32(s_pkt[batch_buf]+16+i*4) == get32(s_pkt[batch_buf]+16+(i-1)*4)+1);
    test_clock = s_next_tick;
    test_stop_call = test_calls + 3;
    scope_sampler_poll();
    assert(!s_running && s_produced == 131);
    reset_test();
    assert(scope_sampler_configure_ticks(48, SCOPE_FLAG_DISCARD | SCOPE_FLAG_FAST_BATCH, 1, &one) == 0);
    assert(scope_start_now() == 0);
    test_fail_read = 1; test_clock = s_next_tick;
    scope_sampler_poll();
    assert(s_swd_err == 1 && s_produced == 0 && s_pipe_dst == NULL);
    /* Timer rollover must not stall the bounded wait. */
    test_clock = s_next_tick = UINT32_MAX - 100;
    scope_sampler_poll();
    assert(s_produced == 64);
    /* A slow period always returns after one sample. */
    assert(scope_sampler_configure_ticks(96, SCOPE_FLAG_DISCARD | SCOPE_FLAG_FAST_BATCH, 1, &one) == 0);
    assert(scope_start_now() == 0); test_clock = s_next_tick; scope_sampler_poll();
    assert(s_produced == 1);
    /* Keep the proven normal-period service cadence; larger bursts only help
     * the tight deadline range. Normal batches still return at packet edges. */
    assert(scope_sampler_configure_ticks(60, SCOPE_FLAG_DISCARD | SCOPE_FLAG_FAST_BATCH, 1, &one) == 0);
    assert(scope_start_now() == 0); test_clock = s_next_tick; scope_sampler_poll();
    assert(s_produced == 16);
    for (unsigned i = 0; i < 7; i++) { test_clock = s_next_tick; scope_sampler_poll(); }
    assert(s_produced == 124 && s_fill_n == 0);
    /* Slow consumer: queued/in-flight packets are immutable even when all buffers fill. */
    reset_test();
    assert(scope_sampler_configure_ticks(48, SCOPE_FLAG_FAST_BATCH | SCOPE_FLAG_CDC_OFF, 1, &one) == 0);
    assert(scope_start_now() == 0 && !test_cdc);
    for (unsigned i = 0; i < 100 && s_fill_buf < SCOPE_TX_BUFS; i++) {
        test_clock = s_next_tick; scope_sampler_poll();
    }
    assert(s_fill_buf >= SCOPE_TX_BUFS && s_if_count == SCOPE_TX_BUFS && s_tx_active);
    uint8_t frozen[SCOPE_TX_BUFS][SCOPE_PACKET];
    memcpy(frozen, s_pkt, sizeof frozen);
    uint32_t produced_before = s_produced, reads_before = test_calls;
    for (unsigned i = 0; i < 3; i++) { test_clock = s_next_tick; scope_sampler_poll(); }
    assert(s_usb_drop == 3 && s_produced == produced_before && test_calls == reads_before);
    assert(memcmp(frozen, s_pkt, sizeof frozen) == 0 && s_pipe_dst == NULL);
    /* M0 while running is rejected without reads, even with a full TX pool. */
    scope_sampler_request_bench(20); scope_sampler_poll();
    assert(s_bench_err == -5 && test_calls == reads_before);
    scope_sampler_tx_complete();
    assert(s_fill_buf < SCOPE_TX_BUFS);
    test_clock = s_next_tick; scope_sampler_poll();
    assert(s_produced == produced_before + 64);
    scope_sampler_stop(1);
    scope_sampler_poll();
    assert(!test_cdc && !test_zlp_count && s_if_count == SCOPE_TX_BUFS);
    scope_sampler_request_bench(20);
    for (unsigned i = 0; i < SCOPE_TX_BUFS; i++) {
        assert(s_if_count == SCOPE_TX_BUFS - i && !test_zlp_count);
        scope_sampler_tx_complete();
    }
    assert(test_zlp_count == 1 && s_tx_zlp && !test_cdc);
    scope_sampler_tx_complete(); scope_sampler_poll();
    assert(test_cdc && !scope_sampler_is_running() && s_bench_err == 0 && s_bench_iters == 20);
    /* Recent DAP traffic still prevents entry into a batch. */
    reset_test();
    assert(scope_sampler_configure_ticks(48, SCOPE_FLAG_FAST_BATCH | SCOPE_FLAG_DISCARD, 1, &one) == 0);
    assert(scope_start_now() == 0);
    test_last_dap = s_next_tick - 1; test_clock = s_next_tick;
    scope_sampler_poll();
    assert(s_yield == 1 && s_produced == 0 && test_calls == 0);
    /* Multi-span plans never spin for 64 samples. */
    scope_var_t two[2] = { one, { 0x20002044U, 4, 4, 0 } };
    assert(scope_sampler_configure_ticks(48, SCOPE_FLAG_FAST_BATCH | SCOPE_FLAG_DISCARD, 2, two) == 0);
    assert(scope_start_now() == 0); test_last_dap = 0;
    test_clock = s_next_tick; scope_sampler_poll();
    assert(!s_pipe_ok && s_nspans == 2 && s_produced == 1);
    assert(scope_sampler_configure_ticks(96, SCOPE_FLAG_DISCARD, 1, &one) == 0);
    s_produced = 100000; s_dropped = 80000; s_usb_drop = 70000;
    s_swd_err = 90000; s_yield = 65536; test_clock = UINT32_MAX - 10;
    assert(scope_sampler_metrics(status_words, 11) == 0);
    assert(scope_sampler_metrics(status_words, 12) == 12);
    assert(status_words[0] == 0x31535348 && status_words[1] == UINT32_MAX - 10);
    assert(status_words[2] == 24000000 && status_words[3] == 100000);
    assert(status_words[4] == 80000 && status_words[5] == 70000 && status_words[6] == 90000);
    assert(status_words[7] == 65536 && status_words[10] == 96);
    /* JTAG held reads retain the posted values across bounded bursts and
     * packet flushes. Slower periods/multi-span plans must stay scalar. */
    reset_test(); test_read_cost = 70;
    assert(scope_sampler_configure_ticks(96, SCOPE_FLAG_RISCV | SCOPE_FLAG_DISCARD | SCOPE_FLAG_FAST_BATCH, 1, &one) == 0);
    assert(scope_start_now() == 0); test_clock = s_next_tick; scope_sampler_poll();
    assert(s_backend == SCOPE_BE_RISCV && s_produced == 16 && s_dropped == 0);
    for (unsigned i = 0; i < 7; i++) { test_clock = s_next_tick; scope_sampler_poll(); }
    assert(s_produced == 124 && s_fill_n == 0);
    batch_buf = 0;
    while (s_pkt[batch_buf][3] != SCOPE_KIND_DATA) { batch_buf++; assert(batch_buf < SCOPE_TX_BUFS); }
    for (unsigned i = 1; i < 124; i++)
        assert(get32(s_pkt[batch_buf]+16+i*4) == get32(s_pkt[batch_buf]+16+(i-1)*4)+1);
    test_clock = s_next_tick; test_stop_call = test_calls + 3; scope_sampler_poll();
    assert(!s_running && s_produced == 127);
    reset_test(); test_read_cost = 70;
    assert(scope_sampler_configure_ticks(144, SCOPE_FLAG_RISCV | SCOPE_FLAG_DISCARD | SCOPE_FLAG_FAST_BATCH, 1, &one) == 0);
    assert(scope_start_now() == 0); test_clock = UINT32_MAX - 100; s_next_tick = test_clock;
    scope_sampler_poll(); assert(s_produced == 16);
    test_fail_read = 1; test_clock = s_next_tick; scope_sampler_poll();
    assert(s_swd_err == 1 && s_produced == 16 && s_pipe_dst == NULL);
    assert(scope_sampler_configure_ticks(145, SCOPE_FLAG_RISCV | SCOPE_FLAG_DISCARD | SCOPE_FLAG_FAST_BATCH, 1, &one) == 0);
    assert(scope_start_now() == 0); test_clock = s_next_tick; scope_sampler_poll(); assert(s_produced == 1);
    assert(scope_sampler_configure_ticks(96, SCOPE_FLAG_RISCV | SCOPE_FLAG_DISCARD | SCOPE_FLAG_FAST_BATCH, 2, two) == 0);
    assert(scope_start_now() == 0); test_clock = s_next_tick; scope_sampler_poll(); assert(s_produced == 1 && !s_pipe_ok);

    /* STOP arrived in the read, while DEF is still in flight. The last
     * accepted value belongs in the tail and can only be fetched in poll. */
    reset_test();
    assert(scope_sampler_configure_ticks(48, SCOPE_FLAG_FAST_BATCH | SCOPE_FLAG_CDC_OFF, 1, &one) == 0);
    assert(scope_start_now() == 0 && !test_cdc && test_tx_count == 1);
    test_stop_call = 3; test_clock = s_next_tick; scope_sampler_poll();
    assert(s_stopping && s_stop_req && test_calls == 3 && s_fill_n == 3);
    assert(scope_sampler_is_running() && !s_running);
    assert(scope_sampler_needs_service()); /* production main loop must service STOP */
    scope_sampler_status(status_words, 12);
    assert((status_words[0] & 0xe1) == 0x60);
    scope_sampler_request_start();
    assert(!s_start_req && scope_sampler_start_result() == -7);
    assert(scope_sampler_configure_ticks(96, 0, 1, &one) == -7);
    scope_sampler_poll(); scope_sampler_stop(1); scope_sampler_poll();
    assert(test_calls == 4 && s_if_count == 2 && test_tx_count == 1 && !test_zlp_count);
    scope_sampler_tx_complete();
    assert(test_tx_count == 2 && test_tx_last[3] == SCOPE_KIND_DATA);
    assert(get32(test_tx_last+12) == (12U<<16 | 3U));
    assert(get32(test_tx_last+16) == 1 && get32(test_tx_last+20) == 2 && get32(test_tx_last+24) == 3);
    uint32_t seq_before = s_seq, done_before = s_tx_done;
    scope_sampler_tx_complete();
    assert(test_zlp_count == 1 && s_tx_zlp && !test_cdc && s_seq == seq_before && s_tx_done == done_before + 1);
    scope_sampler_stop(1); scope_sampler_tx_complete(); scope_sampler_poll();
    assert(test_cdc && !scope_sampler_is_running() && s_tx_done == done_before + 1);
    scope_sampler_stop(1); scope_sampler_poll(); assert(test_zlp_count == 1);
    assert(scope_sampler_retire_reads(33) == -6 && scope_sampler_retire_reads(2) == 0);
    assert(scope_sampler_needs_service()); /* also wake for EOF's outstanding reads */
    scope_sampler_poll();
    assert(test_zlp_count == 2 && s_stopping && s_tx_zlp);
    scope_sampler_tx_complete();
    assert(test_zlp_count == 3 && s_stopping && s_tx_zlp);
    scope_sampler_tx_complete(); scope_sampler_poll();
    assert(!s_stopping && s_tx_done == done_before + 1 && s_seq == seq_before);
    assert(scope_sampler_retire_reads(1) == -7);

    /* Last posted read fails at either STOP or the full-packet boundary. */
    for (unsigned full = 0; full < 2; full++) {
        reset_test();
        assert(scope_sampler_configure_ticks(96, 0, 1, &one) == 0);
        assert(scope_start_now() == 0); scope_sampler_tx_complete();
        unsigned n = full ? 124 : 3;
        test_fail_call = n + 1;
        for (unsigned i = 0; i < n; i++) { test_clock = s_next_tick; scope_sampler_poll(); }
        scope_sampler_stop(1); scope_sampler_poll();
        assert(s_produced == n && s_swd_err == 1 && s_invalid_samples == 1);
        assert(test_tx_count == 2 && get32(test_tx_last+12) == ((n-1)*4U<<16 | (n-1)));
        assert(get32(test_tx_last+16+(n-2)*4U) == n-1);
        uint32_t m[13];
        assert(scope_sampler_metrics(m, 13) == 13 && m[12] == 1 && (m[11] & (1U<<9)));
        scope_sampler_tx_complete();
        assert(test_zlp_count == 1 && !s_if_count);
        scope_sampler_tx_complete(); scope_sampler_poll();
    }

    /* Non-pipelined, 8-variable tail. Reset aborts queued data instead of
     * reporting a clean stop; no fabricated EOF is sent after reset. */
    reset_test();
    scope_var_t eight[8];
    for (unsigned i = 0; i < 8; i++) { eight[i] = one; eight[i].addr += i * 4; }
    assert(scope_sampler_configure_ticks(480, SCOPE_FLAG_CDC_OFF, 8, eight) == 0);
    assert(scope_start_now() == 0); scope_sampler_tx_complete();
    for (unsigned i = 0; i < 2; i++) { test_clock = s_next_tick; scope_sampler_poll(); }
    scope_sampler_stop(1); scope_sampler_poll();
    assert(test_tx_count == 2 && get32(test_tx_last+12) == (64U<<16 | 2U));
    scope_sampler_usb_reset(); scope_sampler_poll();
    scope_sampler_status(status_words, 12);
    assert((status_words[0] & 0xe1) == 0xa0 && !test_zlp_count && test_cdc);
    assert(!scope_sampler_is_running() && !s_if_count);

    /* A queued START canceled before the first sample still completes
     * the browser's pending bulk read, and never starts the sampler. */
    reset_test();
    scope_sampler_request_start(); scope_sampler_stop(1); scope_sampler_poll();
    assert(test_tx_count == 0 && test_zlp_count == 1 && s_stopping);
    scope_sampler_tx_complete(); scope_sampler_poll();
    assert(!scope_sampler_is_running());

    /* Old clients send a bare STOP and never read an EOF. */
    reset_test();
    assert(scope_sampler_configure_ticks(96, SCOPE_FLAG_CDC_OFF, 1, &one) == 0);
    assert(scope_start_now() == 0); scope_sampler_tx_complete();
    test_clock = s_next_tick; scope_sampler_poll();
    scope_sampler_stop(0); scope_sampler_poll();
    assert(!scope_sampler_is_running() && test_cdc && !test_zlp_count && test_tx_count == 1);
    scope_sampler_request_start(); scope_sampler_stop(1); scope_sampler_poll();
    assert(test_zlp_count == 1 && test_tx_count == 1); /* no previous partial DATA */
    scope_sampler_tx_complete(); scope_sampler_poll();

    one.size = 255;
    assert(scope_sampler_configure_ticks(60, 0, 1, &one) == -6 && s_nvars == 0);
    puts("scope host tests: protocol, batches, SWD/JTAG pipeline, partial/full stop, ZLP, invalid tail, USB reset, ownership PASS");
    return 0;
}
