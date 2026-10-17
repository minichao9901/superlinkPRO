import re
import sys
from pathlib import Path

sys.path.insert(0, r'E:\web-etm-trace\tools')
from tpiu_demux import demux, FSYNC, HSYNC          # noqa: E402

path = Path(sys.argv[1])
raw = path.read_bytes()
print(f'{path.name}: {len(raw)} bytes')
try:
    st, streams = demux(raw)
    print('OK frames=', st['frames'], st['stream_bytes'])
except Exception as exc:                             # noqa: BLE001
    print('FAIL:', exc)
    m = re.search(r'偏移 (\d+)', str(exc))
    if m:
        off = int(m.group(1))
        print('offset', off)
        print('  raw[off-32:off+48] =', raw[off - 32:off + 48].hex(' '))
        print('  FSYNC at', [i for i in range(max(0, off - 400), min(len(raw), off + 400))
                             if raw[i:i + 4] == FSYNC][:8])
        print('  HSYNC run before:', sum(1 for i in range(max(0, off - 200), off)
                                         if raw[i:i + 2] == HSYNC))
