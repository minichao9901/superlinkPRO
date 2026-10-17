import time
from pyocd.core.helpers import ConnectHelper
from pyocd.core.target import Target

session = ConnectHelper.session_with_chosen_probe(
    target_override='cortex_m',
    options={'frequency': 1000000, 'connect_mode': 'attach',
             'auto_unlock': False, 'resume_on_disconnect': True})
session.open()
t = session.target
print('API:', [x for x in dir(t) if 'reset' in x.lower() or 'break' in x.lower()])
t.halt()
print('halted at', hex(t.read_core_register('pc')))
try:
    print('breakpoints:', t.get_breakpoints() if hasattr(t, 'get_breakpoints') else 'n/a')
except Exception as e:
    print('bp list err', e)
addr = 0x08000B4C
try:
    t.set_breakpoint(addr)
    print('set_breakpoint ok ->', t.get_breakpoints() if hasattr(t, 'get_breakpoints') else '')
except Exception as e:
    print('set_breakpoint failed:', e)
try:
    t.reset()
    print('after reset: state', t.get_state(), 'pc', hex(t.read_core_register('pc')))
except Exception as e:
    print('reset failed:', e)
try:
    t.resume()
    print('resumed')
except Exception as e:
    print('resume failed:', e)
t0 = time.time()
while time.time() - t0 < 5:
    st = t.get_state()
    if st == Target.State.HALTED:
        print(f'halted after {time.time()-t0:.3f}s at', hex(t.read_core_register('pc')))
        break
    time.sleep(0.01)
else:
    print('still', t.get_state(), 'pc', hex(t.read_core_register('pc')))
try:
    print('fpb ctrl', hex(t.read32(0xE0002000)))
    for i in range(8):
        print(' comp', i, hex(t.read32(0xE0002008 + i * 4)))
except Exception as e:
    print('fpb read failed', e)
t.resume()
session.close()
