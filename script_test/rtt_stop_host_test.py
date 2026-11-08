"""Run the deferred STOP and DAP-idle logic with host stand-ins."""
from pathlib import Path
import os
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
text = (root / 'firmware/application_5301/src/rtt/rtt_bridge.c').read_text(encoding='utf-8')
start = text.index('    if (s_stop_pending)', text.index('static void rtt_bridge_service_requests'))
end = text.index('    if (s_raw_pending)', start)
branch = text[start:end]
start = text.index('int rtt_bridge_dap_recent(')
end = text.index('void rtt_bridge_set_swd_clock', start)
recent = text[start:end]
source = '''#include <assert.h>
#include <stdint.h>
#define RTT_IDLE_TICKS 480000U
#define HPM_MCHTMR 0
static uint64_t s_last_dap, test_now;
static unsigned full_timer_reads;
static uint64_t mchtmr_get_count(int timer){ (void)timer; full_timer_reads++; return test_now; }
''' + recent + '''
static int s_stop_pending, s_start_pending, s_start_rc, s_running, starts, flushes;
static void rtt_bridge_flush_pending_rd(void){ assert(s_running); flushes++; }
static void rtt_bridge_stop(void){ s_running = 0; }
static void service(void){
''' + branch + '''
  if(s_start_pending){ s_start_pending = 0; starts++; s_running = 1; }
}
int main(void){
  s_start_pending=1; s_start_rc=-100; s_stop_pending=1;
  service(); assert(starts==0 && !s_running && s_start_rc==0 && !s_stop_pending);
  s_start_pending=1; service(); assert(starts==1 && s_running);
  s_stop_pending=1; service(); assert(!s_running && flushes==1 && starts==1);
  s_running=1; s_start_pending=1; s_start_rc=-100; s_stop_pending=1;
  service(); assert(!s_running && !s_start_pending && s_start_rc==0 && flushes==2 && starts==1);
  assert(!rtt_bridge_dap_recent(0));
  s_last_dap = UINT64_C(0xfffff000);
  test_now = s_last_dap + RTT_IDLE_TICKS - 1U;
  assert(rtt_bridge_dap_recent((uint32_t)test_now));
  test_now++;
  const unsigned timer_reads_before_idle = full_timer_reads;
  assert(!rtt_bridge_dap_recent((uint32_t)test_now));
  assert(full_timer_reads == timer_reads_before_idle);
  test_now = s_last_dap + (UINT64_C(1) << 32) + RTT_IDLE_TICKS - 1U;
  assert(!rtt_bridge_dap_recent((uint32_t)test_now));
  return 0;
}
'''
with tempfile.TemporaryDirectory() as directory:
    path = Path(directory)
    (path / 'test.c').write_text(source)
    subprocess.run([os.environ.get('CC', 'gcc'), '-std=c11', '-Wall', '-Werror', str(path / 'test.c'), '-o', str(path / 'test')], check=True)
    subprocess.run([str(path / 'test')], check=True)
print('RTT STOP and DAP arbitration: queued START cancelled, RdOff flushed, timer-wrap alias rejected PASS')
