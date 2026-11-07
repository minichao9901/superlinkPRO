"""Verify the HPM6800EVK RAM fixture through the authorized workbench CLI page."""

import argparse
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[3]
ADDRESS = 0x18000
PAYLOAD = bytes.fromhex('13 05 00 00 6f 00 40 00 13 05 15 00 13 05 15 00 6f 00 00 00')
BACKEND = 'OpenOCD Wasm / WebUSB'
LOCK = ROOT.parent / 'web-etm-trace-coordination/hardware.lock'


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def fixture_digest(elf):
    data = elf.read_bytes()
    ident, kind, machine, _, entry, table, _, _, _, stride, count, *_ = struct.unpack_from(
        '<16sHHIIIIIHHHHHH', data)
    require(ident.startswith(b'\x7fELF\x01\x01') and (kind, machine, entry, stride, count) ==
            (2, 243, ADDRESS, 32, 1), 'unexpected test ELF header')
    ptype, offset, vaddr, paddr, length, extent, flags, _ = struct.unpack_from('<8I', data, table)
    require((ptype, vaddr, paddr, length, extent, flags) == (1, ADDRESS, ADDRESS, len(PAYLOAD), len(PAYLOAD), 5)
            and data[offset:offset + length] == PAYLOAD, 'unexpected test ELF payload')
    return hashlib.sha256(data).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('runtime', type=Path)
    parser.add_argument('elf', type=Path)
    parser.add_argument('out', type=Path, help='new evidence directory; the fixture remains in ILM')
    parser.add_argument('--lock-held', action='store_true', help='use the lock held by this same operator')
    args = parser.parse_args()
    elf_digest = fixture_digest(args.elf)
    args.out.mkdir(parents=True, exist_ok=False)
    base = [sys.executable, '-X', 'utf8', str(ROOT / 'toolbox.py'), '--runtime',
            str(args.runtime.resolve()), '--timeout', '25', 'page', 'dbg']

    def cli(*words):
        p = subprocess.run([*base, *words], cwd=ROOT, capture_output=True, encoding='utf-8', timeout=35)
        result = json.loads(p.stdout)
        require(result.get('ok'), result.get('error', str(result)))
        return result['value']

    def invoke(method, *values, target='session'):
        result = cli('invoke', '--target', target, '--args', json.dumps(values), method)
        require(not result['errors'], str(result['errors']))
        return result['value']

    def status():
        return cli('state')['summary']

    def read_ilm():
        return bytes(invoke('memRead', ADDRESS, len(PAYLOAD))['$bytes'])

    def snapshot(pc, line, a0):
        s = status()
        require(s['backend'] == BACKEND and s['connected'] and s['halted'], str(s))
        actual = (s['pc'], s['src']['cur']['line'], invoke('readReg', 'a0'))
        require(actual == (pc, line, a0) and s['src']['shown'] > 0, f'unexpected source stop {actual}')
        return dict(pc=pc, line=line, a0=a0, source=s['src']['cur']['file'])

    def wait_until(predicate):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            value = status()
            if predicate(value):
                return value
            time.sleep(.2)
        raise RuntimeError(cli('read', '#d-out')['text'][-1600:])

    report = {'elfSha256': elf_digest, 'ilmRange': [ADDRESS, ADDRESS + len(PAYLOAD)],
              'wasmSha256': hashlib.sha256((ROOT / 'web/app/core/openocd-runtime/openocd-core.wasm').read_bytes()).hexdigest()}
    owned = entered = False
    if args.lock_held:
        require(LOCK.is_dir(), 'the hardware lock is missing')
    else:
        LOCK.mkdir()
        owned = True
    try:
        controls = {x['selector'] for x in cli('describe')['controls']}
        methods = {x['name'] for x in cli('describe', '--target', 'session')['methods']}
        require({'#d-ram-elf', '#d-step-over'} <= controls and {'readReg', 'memRead'} <= methods,
                'unexpected workbench CLI')
        require('command' not in methods, 'this RAM session exposes a raw OpenOCD command')
        original = status()
        report['connected'] = original
        require(original['connected'] and original['backend'] == BACKEND and original['clockKhz'] == 8000
                and original['src']['ready'] and not original['bps'], 'connect the HPM CFG with no breakpoints first')
        entered = True
        cli('upload', '#d-ram-elf', str(args.elf.resolve()))
        wait_until(lambda s: s['elf'] and s['halted'] and s['pc'] == 0x18008 and s['src']['cur'])
        require(read_ilm() == PAYLOAD, 'ELF ILM readback mismatch')
        report['ilmReadback'] = PAYLOAD.hex()
        report['misa'] = invoke('readReg', 'misa')
        require(report['misa'] >> 30 == 1, 'target is not RV32')
        report['main'] = snapshot(0x18008, 14, 0)
        report['steps'] = []
        for pc, line, a0 in ((0x1800c, 16, 1), (0x18010, 18, 2)):
            cli('click', '#d-step-over')
            wait_until(lambda s: s['halted'] and s['pc'] == pc and (s['src']['cur'] or {}).get('line') == line)
            report['steps'].append(snapshot(pc, line, a0))
    except Exception as error:
        report['error'] = str(error)
    finally:
        try:
            if entered:
                invoke('disconnect', target='view')
                require(not status()['connected'], 'disconnect not confirmed')
                report['disconnected'] = True
        except Exception as error:
            report['cleanupError'] = str(error)
        if owned and 'cleanupError' not in report:
            LOCK.rmdir()
        report['passed'] = 'error' not in report and 'cleanupError' not in report
        (args.out / 'hardware.json').write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
