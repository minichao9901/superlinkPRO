#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "bus_periodic.h"
#include "i2c_bridge.h"
#include "spi_bridge.h"
#ifndef ADC_STREAM_HOST
uint8_t adc_stream_enabled(void) { return 0U; }
void adc_stream_started(void) {}
void adc_stream_poll(void) {}
#endif
uint8_t analog_periodic_ready(void) { return 1U; }
uint8_t analog_periodic_check(const uint8_t *p, uint16_t len) {
    return len != 2U || p[0] != 3U || (p[1] != 8U && p[1] != 10U && p[1] != 12U && p[1] != 16U);
}
uint8_t analog_periodic_exec(const uint8_t *p, uint16_t len, uint8_t *data, uint8_t *n) {
    assert(!analog_periodic_check(p, len)); data[0] = 0x34; data[1] = 0x12; *n = 2; return 0;
}
volatile uint8_t i2c_bridge_req_kind;
static uint64_t now, timer;
static unsigned reads, releases, fail, core, reset_on_exec, clock_failure;
static uint32_t depth;
uint64_t bp_now(void) { return now; }
uint32_t bp_lock(void) { return depth++; }
void bp_unlock(uint32_t level) { assert(depth == level + 1U); depth = level; }
uint8_t bp_timer_arm(uint64_t ticks) { timer = now + ticks; return (uint8_t)clock_failure; }
void bp_timer_stop(void) { timer = 0U; }
int scope_sampler_is_running(void) { return (int)core; }
int rtt_bridge_is_running(void) { return 0; }
uint8_t i2c_bridge_periodic_ready(void) { return 1; }
uint8_t spi_bridge_periodic_ready(void) { return 1; }
#ifndef BP_SERVER
uint8_t i2c_bridge_periodic_check(const uint8_t *p, uint16_t len) { return len != 1 || p[0] != 42; }
uint8_t spi_bridge_periodic_check(const uint8_t *p, uint16_t len) { return i2c_bridge_periodic_check(p, len); }
#endif
uint8_t i2c_bridge_periodic_exec(const uint8_t *p, uint16_t len, uint8_t *data, uint8_t *n) {
    assert(!depth);
#ifndef BP_SERVER
    assert(!i2c_bridge_periodic_check(p, len));
#else
    (void)p; (void)len;
#endif
    reads++; *n = BP_DATA;
    for (unsigned i = 0; i < *n; i++) data[i] = (uint8_t)(reads + i);
    if (reset_on_exec) bus_periodic_reset();
    return (uint8_t)fail;
}
uint8_t spi_bridge_periodic_exec(const uint8_t *p, uint16_t len, uint8_t *data, uint8_t *n) {
    return i2c_bridge_periodic_exec(p, len, data, n);
}
void spi_bridge_periodic_release(void) { assert(!depth); releases++; }
static uint8_t req[64], res[64];
static void w32(uint8_t *p, uint32_t v) { for (unsigned i = 0; i < 4; i++) p[i] = (uint8_t)(v >> (8*i)); }
static uint32_t r32(const uint8_t *p) { return p[0] | ((uint32_t)p[1]<<8) | ((uint32_t)p[2]<<16) | ((uint32_t)p[3]<<24); }
static uint8_t command(unsigned action) {
#ifndef BP_SERVER
    if (req[1] < 2U && action != BP_PUT) req[1] = 6U;
#endif
    req[2] = BP_CMD; req[3] = (uint8_t)action; memset(res, 0, sizeof(res));
    bus_periodic_hid(req, res); assert(res[2] == BP_CMD && res[3] == action && !depth);
    return (uint8_t)r32(res+4);
}
static void poll(void) {
    for (unsigned n = 0; i2c_bridge_req_kind == BP_WAKE; n++) {
        assert(n < 100); bus_periodic_poll();
    }
}
static void advance(unsigned ms) {
    uint64_t end = now + (uint64_t)ms * 24000U;
    while (timer && timer <= end) { now = timer; bus_periodic_irq(); poll(); }
    now = end;
}
static void reset(void) {
    bus_periodic_reset(); poll(); memset(req,0,sizeof(req)); req[1] = 2U;
    assert(command(BP_CLEAR) == BP_OK); reads = releases = fail = core = reset_on_exec = clock_failure = 0;
}
static void upload(unsigned slot, unsigned bus, unsigned trailing_delay) {
    uint8_t bytes[13] = {(uint8_t)bus,0,1,0,42,BP_DELAY,0,4,0,0,0,0,0};
    w32(bytes+9, trailing_delay);
    memset(req, 0, sizeof(req)); req[4]=(uint8_t)slot; req[5]=(uint8_t)bus;
    req[8]=(uint8_t)(trailing_delay ? 13 : 5); memcpy(req+9,bytes,req[8]); req[1]=7+req[8];
    assert(command(BP_PUT) == BP_OK);
}
static void start(unsigned slot,unsigned ms,unsigned count) {
    memset(req,0,sizeof(req)); req[1]=11; req[4]=(uint8_t)slot;
    w32(req+5,ms);w32(req+9,count); assert(command(BP_START)==BP_OK);
}
static void run(void) { assert(command(BP_RUN)==BP_OK); poll(); }
static uint32_t status(void) { assert(command(BP_STATUS)==BP_OK); return r32(res+8); }
static unsigned queued(void) { status(); return r32(res+12); }
static void ack(void) {
    req[4]=0; assert(command(BP_READ)==BP_OK); uint32_t seq=r32(res+8);
    assert(res[31] == BP_DATA && res[1] == 64);
    uint8_t first=res[32]; req[4]=32; assert(command(BP_READ)==BP_OK);
    assert(r32(res+8)==seq && res[1]==54 && res[32]==(uint8_t)(first+32));
    w32(req+4,seq);assert(command(BP_ACK)==BP_OK);
    assert(command(BP_ACK)==BP_STATE);
}
#ifdef BP_SERVER
int main(void) {
    char line[512];
    while (fgets(line,sizeof(line),stdin)) {
        if (line[0]=='H') {
            assert(strlen(line)>=131);
            for (unsigned i=0;i<64;i++) { unsigned v; assert(sscanf(line+2+i*2,"%2x",&v)==1);req[i]=(uint8_t)v; }
            command(req[3]);
            for (unsigned i=0;i<64;i++) printf("%02x",res[i]);
            puts("");
        } else if (line[0]=='T') {
            unsigned ms;assert(sscanf(line+2,"%u",&ms)==1);advance(ms);puts("OK");
        } else if (line[0]=='P') { poll();puts("OK"); }
        else if (line[0]=='R') { reset();puts("OK"); }
        else if (line[0]=='C') { core=line[2]=='1';puts("OK"); }
        fflush(stdout);
    }
    return 0;
}
#else
int main(void) {
    reset(); command(BP_CAPS);assert(!memcmp(res+8,"BPT1",4));
    upload(0,BP_I2C,0);start(0,10,3);assert(!reads && !timer);run();
    assert(reads==1 && timer==now+240000);advance(25);assert(reads==3 && !status() && !timer);
    assert(queued()==3);ack();ack();ack();assert(!queued());
    req[4]=0;assert(command(BP_READ)==BP_EMPTY);
    reset();for(unsigned i=0;i<8;i++){upload(i,BP_SPI,0);start(i,10,2);}run();advance(10);
    assert(reads==16 && !status() && !timer && !bus_periodic_owns(BP_SPI));
    reset();upload(0,BP_I2C,5000);start(0,10,1);run();assert(reads==1 && status());
    advance(4);assert(status());advance(1);assert(!status() && !timer);
    reset();upload(0,BP_I2C,0);start(0,10,0);run();
    /* Main loop was delayed 55 ms: keep one due sample, mark four skipped slots. */
    now+=55*24000U;bus_periodic_irq();poll();assert(reads==2);
    req[4]=0;command(BP_READ);w32(req+4,r32(res+8));command(BP_ACK);
    req[4]=0;command(BP_READ);assert(r32(res+24)==4);
    reset();upload(0,BP_I2C,0);start(0,1,0);run();advance(40);
    assert(reads==BP_QUEUE && queued()==BP_QUEUE && !status() && !timer);
    assert(r32(res+16)==BP_OVERFLOW);
    reset();upload(0,BP_SPI,0);start(0,1,1);fail=1;run();advance(2);assert(reads==3 && status());
    fail=0;advance(1);assert(reads==4 && !status());
    reset();upload(0,BP_I2C,0);start(0,1,0);core=1;run();advance(20);assert(!reads);
    core=0;advance(20);assert(reads==1);
    reset();upload(0,BP_SPI,0);start(0,1,0);reset_on_exec=1;run();assert(!queued() && !status() && !timer);
    reset();upload(0,BP_I2C,0);start(0,1,0);req[4]=255;command(BP_STOP);poll();advance(100);assert(!reads && !timer);
    reset();upload(0,BP_I2C,0);start(0,1,0);clock_failure=1;run();assert(!status() && !timer && r32(res+16)==BP_STATE);
    reset();now=UINT32_MAX-24000ULL;upload(0,BP_I2C,0);start(0,1,3);run();advance(3);assert(reads==3 && !status());
    reset();req[4]=0;req[5]=BP_I2C;req[8]=55;req[1]=1;assert(command(BP_PUT)==BP_RANGE);
    req[8]=1;req[1]=8;req[6]=1;assert(command(BP_PUT)==BP_STATE);
    puts("periodic virtual timer and protocol boundaries PASS");
}
#endif
