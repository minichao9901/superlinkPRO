"""Exercise production SPI drain helper/HID branch on host; no hardware timing claim."""
from pathlib import Path
import os
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
text = (root / 'firmware/application_5301/src/spi_bridge/spi_bridge.c').read_text(encoding='utf-8')
start = text.index('static void sb_drain_pending_reads(void)')
end = text.index('void spi_bridge_poll(void)', start)
helper = text[start:end]
start = text.index('    case SB_ACT_DRAIN:')
end = text.index('    case SB_ACT_ABORT:', start)
branch = text[start:end]
poll_start = text.index('void spi_bridge_poll(void)', text.index(helper))
start = text.index('    if (s_drain_reads != 0U)', poll_start)
end = text.index('    if (s_hw_req', start)
poll = text[start:end]
source = '''#include <stdint.h>
#include <stddef.h>
#include <assert.h>
#define SB_R_EVT 0x82
#define SB_OK 0
#define SB_ACT_DRAIN 9
static volatile uint8_t s_drain_reads;
static uint8_t req_hid[16], res_hid[16], slot[8];
static int full, responses, enabled_work;
static uint8_t s_enabled;
static uint8_t s_hw_req, s_cs_asserted;
static void sb_cs_release(void){s_cs_asserted=0;}
static uint8_t *sb_in_alloc(void){ return full ? NULL : slot; }
static void sb_rsp_init(uint8_t *p,uint8_t type,uint8_t status,uint16_t seq){ assert(p==slot && type==SB_R_EVT && status==SB_OK && seq==0xFFFF); }
static void sb_in_commit(uint16_t n){ assert(n==0); responses++; }
static uint32_t sb_status_word(void){ return s_enabled; }
static void wr_u32(uint8_t *p,uint32_t v){ for(int i=0;i<4;i++)p[i]=(uint8_t)(v>>(8*i)); }
''' + helper + '''
static void hid(void){ switch(9){
''' + branch + '''} }
static void poll(void){
''' + poll + ''' enabled_work++; }
int main(void){
 req_hid[4]=255;hid();assert(s_drain_reads==16 && res_hid[1]==12 && res_hid[8]=='D' && res_hid[11]=='1');
 full=1;poll();assert(s_drain_reads==16 && !responses);
 full=0;for(int i=0;i<16;i++)poll();assert(responses==16 && !s_drain_reads && !enabled_work);
 poll();assert(responses==16);
 s_enabled=1;req_hid[4]=4;hid();for(int i=0;i<4;i++)poll();assert(responses==20 && enabled_work==4);
 req_hid[4]=0;hid();poll();assert(responses==20 && !s_drain_reads);
 /* A closed bridge has no hardware to reconfigure: the guard must drop any
  * hardware-reinit request instead of carrying it across the disabled state. */
 s_enabled=0;s_hw_req=1;s_cs_asserted=1;poll();assert(!s_hw_req && !s_cs_asserted && enabled_work==5);
 return 0;
}
'''
with tempfile.TemporaryDirectory() as directory:
    path = Path(directory)
    (path / 'test.c').write_text(source)
    subprocess.run([os.environ.get('CC', 'gcc'), '-std=c11', '-Wall', '-Werror', str(path / 'test.c'), '-o', str(path / 'test')], check=True)
    subprocess.run([str(path / 'test')], check=True)
print('SPI drain: bounded short responses, ring backpressure, disabled bridge drops pending hw reinit, capability signature PASS')
