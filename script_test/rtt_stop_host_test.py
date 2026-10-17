"""Run the production deferred STOP branch with host stand-ins (no timing claim)."""
from pathlib import Path
import os
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
text = (root / 'firmware/application_5301/src/rtt/rtt_bridge.c').read_text(encoding='utf-8')
start = text.index('    if (s_stop_pending)', text.index('static void rtt_bridge_service_requests'))
end = text.index('    if (s_raw_pending)', start)
branch = text[start:end]
source = '''#include <assert.h>
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
  return 0;
}
'''
with tempfile.TemporaryDirectory() as directory:
    path = Path(directory)
    (path / 'test.c').write_text(source)
    subprocess.run([os.environ.get('CC', 'gcc'), '-std=c11', '-Wall', '-Werror', str(path / 'test.c'), '-o', str(path / 'test')], check=True)
    subprocess.run([str(path / 'test')], check=True)
print('RTT STOP: queued START cancelled, active RdOff flushed before stopping, completion published PASS')
