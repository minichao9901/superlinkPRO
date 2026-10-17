import sys
import time
sys.path.insert(0, r'E:\web-etm-trace\tools')
from pyocd.core.helpers import ConnectHelper
from h743_etm import halt_at_symbol, elf_symbol

addr = elf_symbol(r'E:\web-etm-trace\firmware\stm32h743_cdc_trace\build\fw.elf', 'trace_hook_entry')
print('trace_hook_entry =', hex(addr))
session = ConnectHelper.session_with_chosen_probe(
    target_override='cortex_m',
    options={'frequency': 1000000, 'connect_mode': 'attach',
             'auto_unlock': False, 'resume_on_disconnect': True})
session.open()
try:
    pc = halt_at_symbol(session, addr)
    print('OK halted at', hex(pc))
    t = session.target
    print('fpb ctrl', hex(t.read32(0xE0002000)))
    for i in range(8):
        print('  comp', i, hex(t.read32(0xE0002008 + i * 4)))
    print('boot_stage probe_stage value @0x24000014 =', hex(t.read32(0x24000014)))
    t.resume()
finally:
    session.close()
