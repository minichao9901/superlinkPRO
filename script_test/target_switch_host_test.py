"""Exercise the production TARGET action guard with host stand-ins; no hardware timing claim."""
from pathlib import Path
import subprocess, tempfile, os
root=Path(__file__).resolve().parents[1]
s=(root/'firmware/application_5301/src/api/api_param.c').read_text(encoding='utf-8')
case=s[s.index('        case RTT_ACT_TARGET:'):s.index('        case RTT_ACT_STATUS:',s.index('        case RTT_ACT_TARGET:'))]
source='''#include <assert.h>
#define RTT_ACT_TARGET 10
static int bridge, scope, changes, target;
static int rtt_bridge_is_running(void){return bridge;}
static int scope_sampler_is_running(void){return scope;}
static void rtt_bridge_set_target(unsigned kind){changes++;target=kind;}
static int command(void){unsigned char req_hid[5]={0,0,0,10,1};int rc=0;switch(req_hid[3]){\n'''+case+'''}return rc;}
int main(void){bridge=1;assert(command()==-7);assert(changes==0);bridge=0;scope=1;assert(command()==-7);assert(changes==0);scope=0;assert(command()==0);assert(changes==1&&target==1);return 0;}
'''
with tempfile.TemporaryDirectory() as d:
 p=Path(d);(p/'test.c').write_text(source)
 subprocess.run([os.environ.get('CC','gcc'),'-std=c11','-Wall','-Werror',str(p/'test.c'),'-o',str(p/'test')],check=True)
 subprocess.run([str(p/'test')],check=True)
print('TARGET guard: live RTT/scope rejected, idle switch accepted PASS')
