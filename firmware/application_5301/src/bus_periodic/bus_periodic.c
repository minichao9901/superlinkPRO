/* SPDX-License-Identifier: Apache-2.0 */
#include <string.h>
#include "bus_periodic.h"
#include "i2c_bridge.h"
#include "spi_bridge.h"
#include "analog_bridge.h"
#include "scope_sampler.h"
#include "rtt_bridge.h"
#include "adc_stream.h"

typedef struct {
    uint8_t program[BP_PROGRAM];
    uint16_t size, pc;
    uint8_t bus, step, running, in_cycle, failed, armed;
    uint32_t epoch, period_ms, count, cycle, skipped, successes;
    uint64_t next, ready;
} bp_job_t;
typedef struct {
    uint32_t seq, epoch, cycle, time_ms, skipped;
    uint8_t slot, step, err, len, data[BP_DATA];
} bp_result_t;
static bp_job_t jobs[BP_JOBS];
static bp_result_t results[BP_QUEUE];
static uint8_t head, used, cleanup, fault, cursor;
static uint32_t sequence, epoch;
static volatile uint8_t dirty;

static uint16_t u16(const uint8_t *p) { return p[0] | ((uint16_t)p[1] << 8); }
static uint32_t u32(const uint8_t *p) {
    return p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}
static void put32(uint8_t *p, uint32_t v) {
    for (uint8_t i = 0; i < 4; i++) p[i] = (uint8_t)(v >> (8U * i));
}
uint8_t bus_periodic_owns(uint8_t bus) {
    if ((cleanup & (1U << bus)) != 0U) return 1U;
    for (uint8_t i = 0; i < BP_JOBS; i++)
        if (jobs[i].bus == bus && (jobs[i].running || jobs[i].in_cycle || jobs[i].armed)) return 1U;
    return 0U;
}
void bus_periodic_wake(void) {
    if (dirty && i2c_bridge_req_kind == 0U) i2c_bridge_req_kind = BP_WAKE;
}
void bus_periodic_irq(void) {
    uint32_t level = bp_lock();
    bp_timer_stop();
    dirty = 1U;
    bus_periodic_wake();
    bp_unlock(level);
}
static void stop_job(bp_job_t *j) {
    if (j->in_cycle) cleanup |= (uint8_t)(1U << j->bus);
    j->running = j->in_cycle = j->armed = 0U;
    j->epoch = ++epoch; /* Cancel in-flight publication before buffers are reused. */
}
static void finish_cycle(bp_job_t *j) {
    j->in_cycle = 0U;
    uint64_t now = bp_now(), period = (uint64_t)j->period_ms * 24000U;
    if (j->next <= now) {
        uint64_t missed = (now - j->next) / period + 1U;
        j->skipped += (uint32_t)missed; j->next += missed * period;
    }
    if (!j->failed) j->successes++;
    if (j->bus == BP_SPI) cleanup |= 1U << BP_SPI;
    if (j->count && (j->bus == BP_SPI ? j->successes : j->cycle) >= j->count) j->running = 0U;
}
void bus_periodic_reset(void) {
    uint32_t level = bp_lock();
    bp_timer_stop();
    for (uint8_t i = 0; i < BP_JOBS; i++) stop_job(&jobs[i]);
    head = used = fault = 0U;
    dirty = cleanup != 0U;
    bus_periodic_wake();
    bp_unlock(level);
}
static uint8_t validate(bp_job_t *j) {
    uint16_t off = 0U;
    uint8_t n = 0U;
    while (off < j->size) {
        if ((uint32_t)off + 4U > j->size || ++n > BP_STEPS) return BP_RANGE;
        const uint8_t *p = &j->program[off];
        uint16_t len = u16(p + 2);
        if (p[1] || (uint32_t)off + 4U + len > j->size) return BP_RANGE;
        if (p[0] == BP_DELAY) {
            if (len != 4U || u32(p + 4) > 60000000U) return BP_RANGE;
        } else if (p[0] != j->bus) return BP_RANGE;
        else if (j->bus == BP_I2C) {
            if (i2c_bridge_periodic_check(p + 4, len)) return BP_RANGE;
        } else if (j->bus == BP_ADC) {
            if (analog_periodic_check(p + 4, len)) return BP_RANGE;
        } else if (spi_bridge_periodic_check(p + 4, len)) return BP_RANGE;
        off = (uint16_t)(off + 4U + len);
    }
    return n ? BP_OK : BP_RANGE;
}
void bus_periodic_hid(uint8_t *req, uint8_t *res) {
    uint8_t action = req[3], rc = BP_OK, slot = req[4];
    uint8_t *out = res + 8;
    res[1] = 8U; res[2] = BP_CMD; res[3] = action;
    uint8_t minimum = action == BP_PUT ? 7U : action == BP_START ? 11U :
        action == BP_ACK ? 6U : (action == BP_STOP || action == BP_READ) ? 3U : 2U;
    if (req[1] < minimum) { put32(res + 4, BP_RANGE); return; }
    if (adc_stream_enabled() && action != BP_CAPS && action != BP_STATUS) {
        put32(res + 4, BP_BUSY); return;
    }
    uint32_t level = bp_lock();
    switch (action) {
    case BP_CAPS:
        memcpy(out, "BPT1", 4); out[4] = BP_JOBS;
        out[5] = (uint8_t)BP_PROGRAM; out[6] = BP_PROGRAM >> 8;
        out[7] = BP_STEPS; out[8] = BP_DATA; out[9] = BP_QUEUE;
        res[1] = 18U;
        break;
    case BP_CLEAR:
        /* One page owns this engine; never clear another running acquisition. */
        if (bus_periodic_owns(BP_I2C) || bus_periodic_owns(BP_SPI) || bus_periodic_owns(BP_ADC)) { rc = BP_BUSY; break; }
        memset(jobs, 0, sizeof(jobs)); head = used = fault = 0;
        break;
    case BP_PUT: {
        uint16_t off = u16(req + 6); uint8_t n = req[8];
        if (slot >= BP_JOBS || (req[5] != BP_I2C && req[5] != BP_SPI && req[5] != BP_ADC) || !n || n > 55U ||
            (uint32_t)off + n > BP_PROGRAM || (uint16_t)req[1] + 2U < 9U + n) { rc = BP_RANGE; break; }
        bp_job_t *j = &jobs[slot];
        if (j->running || j->in_cycle || j->armed || cleanup) { rc = BP_BUSY; break; }
        if (off == 0U) { j->size = 0U; j->bus = req[5]; }
        if (off != j->size || j->bus != req[5]) { rc = BP_STATE; break; }
        memcpy(j->program + off, req + 9, n); j->size += n;
        break;
    }
    case BP_START: {
        if (slot >= BP_JOBS || req[1] < 11U) { rc = BP_RANGE; break; }
        bp_job_t *j = &jobs[slot];
        uint32_t ms = u32(req + 5);
        if (j->running || j->in_cycle || j->armed || cleanup || fault) { rc = BP_BUSY; break; }
        if (!ms || ms > 60000U) { rc = BP_RANGE; break; }
        if ((j->bus != BP_ADC && bus_periodic_owns(BP_ADC)) ||
            bus_periodic_owns(j->bus == BP_SPI ? BP_I2C : BP_SPI) ||
            (j->bus == BP_ADC && bus_periodic_owns(BP_I2C)) ||
            (j->bus == BP_ADC ? !analog_periodic_ready() :
             j->bus == BP_SPI ? !spi_bridge_periodic_ready() : !i2c_bridge_periodic_ready())) {
            rc = BP_BUSY; break;
        }
        rc = validate(j); if (rc) break;
        j->period_ms = ms; j->count = u32(req + 9); j->cycle = j->skipped = j->successes = 0U;
        j->pc = j->step = 0U; j->epoch = ++epoch; j->armed = 1U;
        put32(out, j->epoch); res[1] = 12U;
        break;
    }
    case BP_RUN: {
        uint64_t now = bp_now(); uint8_t n = 0U;
        for (uint8_t i = 0; i < BP_JOBS; i++) if (jobs[i].armed) {
            jobs[i].armed = 0U; jobs[i].running = 1U; jobs[i].next = now; n++;
        }
        if (!n) { rc = BP_STATE; break; }
        dirty = 1U; bus_periodic_wake();
        break;
    }
    case BP_STOP:
        if (slot != 255U && slot >= BP_JOBS) { rc = BP_RANGE; break; }
        for (uint8_t i = 0; i < BP_JOBS; i++) if (slot == 255U || i == slot) stop_job(&jobs[i]);
        bp_timer_stop(); dirty = 1U; bus_periodic_wake();
        break;
    case BP_STATUS: {
        uint32_t mask = 0U;
        for (uint8_t i = 0; i < BP_JOBS; i++) if (jobs[i].running || jobs[i].in_cycle) mask |= 1U << i;
        put32(out, mask); put32(out + 4, used); put32(out + 8, fault);
        put32(out + 12, epoch); put32(out + 16, cleanup); res[1] = 28U;
        break;
    }
    case BP_READ: {
        if (!used) { rc = BP_EMPTY; break; }
        bp_result_t *r = &results[head]; uint8_t off = req[4];
        if (off > r->len) { rc = BP_RANGE; break; }
        put32(out, r->seq); put32(out + 4, r->epoch); put32(out + 8, r->cycle);
        put32(out + 12, r->time_ms); put32(out + 16, r->skipped);
        out[20] = r->slot; out[21] = r->step; out[22] = r->err; out[23] = r->len;
        uint8_t n = (uint8_t)(r->len - off); if (n > 32U) n = 32U;
        memcpy(out + 24, r->data + off, n); res[1] = (uint8_t)(32U + n);
        break;
    }
    case BP_ACK:
        if (!used || u32(req + 4) != results[head].seq) { rc = BP_STATE; break; }
        head = (uint8_t)((head + 1U) % BP_QUEUE); used--;
        break;
    default: rc = BP_RANGE; break;
    }
    put32(res + 4, rc);
    bp_unlock(level);
}

/* Main loop only. One record per visit; no FIFO wait inside the timer ISR. */
void bus_periodic_poll(void) {
    uint8_t bytes[128], kind = 0U, slot = 255U, step = 0U;
    uint16_t len = 0U; uint32_t token = 0U;
    uint32_t level = bp_lock();
    if (i2c_bridge_req_kind == BP_WAKE) i2c_bridge_req_kind = 0U;
    dirty = 0U;
    uint8_t release = cleanup; cleanup = 0U;
    bp_unlock(level);
    if (release & (1U << BP_SPI)) spi_bridge_periodic_release();
    uint64_t now = bp_now();
    level = bp_lock();
    int8_t active = -1;
    for (uint8_t i = 0; i < BP_JOBS; i++) if (jobs[i].in_cycle) {
        if (jobs[i].pc == jobs[i].size && jobs[i].ready <= now) finish_cycle(&jobs[i]);
        else active = (int8_t)i;
    }
    for (uint8_t k = 0; k < BP_JOBS; k++) {
        uint8_t i = (uint8_t)((cursor + k) % BP_JOBS); bp_job_t *j = &jobs[i];
        /* Keep each sequence atomic with respect to other jobs (CS/repeated reads). */
        if (cleanup || (active >= 0 && i != (uint8_t)active)) continue;
        if (!j->running || (j->in_cycle ? j->ready : j->next) > now) continue;
        if (used == BP_QUEUE) {
            fault = BP_OVERFLOW;
            for (uint8_t z = 0; z < BP_JOBS; z++) stop_job(&jobs[z]);
            break; /* Do not perform a write whose result cannot be retained. */
        }
        if (!j->in_cycle) {
            uint64_t period = (uint64_t)j->period_ms * 24000U;
            uint64_t missed = (now - j->next) / period;
            j->skipped += (uint32_t)missed;
            j->next += (missed + 1U) * period;
            j->cycle++; j->in_cycle = 1U;
            j->pc = j->step = j->failed = 0U; j->ready = now;
        }
        const uint8_t *p = j->program + j->pc;
        kind = p[0]; len = u16(p + 2); step = j->step; token = j->epoch; slot = i;
        if (len > sizeof(bytes)) { fault = BP_RANGE; stop_job(j); slot = 255U; break; }
        memcpy(bytes, p + 4, len); cursor = (uint8_t)((i + 1U) % BP_JOBS);
        break;
    }
    bp_unlock(level);
    uint8_t data[BP_DATA] = {0}, n = 0U, err = 0U;
    if (slot != 255U && kind != BP_DELAY) {
        /* Auxiliary work yields to the core products; bounded retry after 1 ms. */
        if (scope_sampler_is_running() || rtt_bridge_is_running()) {
            level = bp_lock();
            if (jobs[slot].epoch == token) jobs[slot].ready = now + 480000U;
            bp_unlock(level); slot = 255U;
        } else if (kind == BP_I2C) err = i2c_bridge_periodic_exec(bytes, len, data, &n);
        else if (kind == BP_ADC) err = analog_periodic_exec(bytes, len, data, &n);
        else err = spi_bridge_periodic_exec(bytes, len, data, &n);
    }
    level = bp_lock();
    if (slot != 255U && jobs[slot].epoch == token && jobs[slot].running) {
        bp_job_t *j = &jobs[slot];
        if (err) j->failed = 1U;
        if (kind != BP_DELAY) {
            if (used == BP_QUEUE) {
                fault = BP_OVERFLOW;
                for (uint8_t i = 0; i < BP_JOBS; i++) stop_job(&jobs[i]);
            } else {
                bp_result_t *r = &results[(head + used) % BP_QUEUE];
                r->seq = ++sequence; r->epoch = token; r->cycle = j->cycle;
                r->time_ms = (uint32_t)(now / 24000U); r->skipped = j->skipped;
                r->slot = slot; r->step = step; r->err = err; r->len = n;
                memcpy(r->data, data, n); used++;
            }
        }
        if (j->running) {
            j->pc = (uint16_t)(j->pc + 4U + len); j->step++;
            j->ready = bp_now() + (kind == BP_DELAY ? (uint64_t)u32(bytes) * 24U : 0U);
            if (j->pc == j->size) {
                if (kind != BP_DELAY) finish_cycle(j);
            }
        }
    }
    now = bp_now(); uint64_t nearest = UINT64_MAX;
    active = -1;
    for (uint8_t i = 0; i < BP_JOBS; i++) if (jobs[i].in_cycle) active = (int8_t)i;
    for (uint8_t i = 0; i < BP_JOBS; i++) if (jobs[i].running &&
        (active < 0 || i == (uint8_t)active)) {
        uint64_t due = jobs[i].in_cycle ? jobs[i].ready : jobs[i].next;
        if (due < nearest) nearest = due;
    }
    if (cleanup || (nearest != UINT64_MAX && nearest <= now)) {
        dirty = 1U; bus_periodic_wake(); bp_timer_stop();
    } else if (nearest != UINT64_MAX) {
        if (bp_timer_arm(nearest - now)) {
            fault = BP_STATE;
            for (uint8_t i = 0; i < BP_JOBS; i++) stop_job(&jobs[i]);
            bp_timer_stop(); dirty = cleanup != 0U; bus_periodic_wake();
        }
    }
    else bp_timer_stop();
    bp_unlock(level);
}
