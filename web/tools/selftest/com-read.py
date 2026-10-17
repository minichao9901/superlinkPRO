"""读一段 COM 口，量速率；可选把收到的字节原样写进文件。

    python tools/selftest/com-read.py <COM口> <秒数> <输出json> [--save=文件]

为什么单独一个进程：RTT 转发是**探针侧**在搬数据、往 CDC 灌；主机不把 COM 口的数据读走，
探针那条路会背压停下来 —— 那样测出来的"速率"是假的。所以测转发时必须真有一个读者。
读法用大块读（默认 64 KB），免得 Python 自己的循环开销成了瓶颈（小块读实测会掉到 ~2 MB/s）。
独立进程 + 结果写文件（不走 stdout 管道），避免和 CDP 脚本抢管道。
"""
import json
import os
import sys
import time

import serial

args = [a for a in sys.argv[1:] if not a.startswith('--')]
save = next((a.split('=', 1)[1] for a in sys.argv[1:] if a.startswith('--save=')), '')

port = args[0] if len(args) > 0 else 'COM5'
secs = float(args[1]) if len(args) > 1 else 8.0
out = args[2] if len(args) > 2 else 'tmp/com-read.json'
CHUNK = 256 * 1024

res = {'port': port, 'wantSeconds': secs, 'bytes': 0, 'seconds': 0, 'rate': 0, 'rateMB': 0,
       'head': '', 'tail': '', 'words': 0, 'saved': save, 'error': ''}
try:
    s = serial.Serial(port, 115200, timeout=0.1)
    fd = s.fileno()
    f = open(save, 'wb') if save else None
    t0 = time.time()
    n = 0
    head = b''
    tail = b''
    while time.time() - t0 < secs:
        # 直接用 os.read(fd)：pyserial 的 read() 每块都走一遍 Python 层的检查/拼接，
        # 实测只有 ~2 MB/s，而 CDC 满速是 ~3 MB/s —— 读者自己成了瓶颈就把速率测低了。
        # os.read 一次系统调用搬 256 KB（端口超时 0.1 s，没数据就返回空）。
        chunk = os.read(fd, CHUNK)
        if not chunk:
            continue
        n += len(chunk)
        if f:
            f.write(chunk)
        if len(head) < 240:
            head += chunk[:240 - len(head)]
        tail = (tail + chunk)[-200:]
    dt = time.time() - t0
    if f:
        f.close()
    s.close()
    res.update(bytes=n, seconds=round(dt, 3), rate=round(n / dt, 1) if dt else 0,
               rateMB=round(n / dt / 1048576, 3) if dt else 0,
               head=head.decode('utf-8', 'replace')[:200],
               tail=tail.decode('utf-8', 'replace')[-80:],
               words=tail.count(b'hello world!'))
except Exception as e:                                    # noqa: BLE001 —— 报障信息要全
    res['error'] = f'{type(e).__name__}: {e}'

with open(out, 'w', encoding='utf-8') as fo:
    json.dump(res, fo, ensure_ascii=False)
print(json.dumps(res, ensure_ascii=False))
