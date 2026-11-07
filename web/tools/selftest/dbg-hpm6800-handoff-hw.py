"""Check HPM6800EVK probe handoffs with the XIP dbgstress image already running."""

import argparse
import base64
import json
import struct
import sys
import time
from pathlib import Path

import serial

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
import toolbox

LOCK = ROOT.parent / 'web-etm-trace-coordination/hardware.lock'
ELF = ROOT / 'web/tools/target-firmware/hpm6800evk_dbgstress/fw.elf'
CFG = 'hpm6800_all_in_one.cfg'
CFG_FILES = [
    CFG, 'probes/cmsis_dap.cfg', 'soc/hpm6880.cfg', 'soc/hpm_common.cfg',
    'soc/hpm_reset.cfg', 'soc/hpm_common_csr.cfg', 'boards/hpm6800evk.cfg',
]
# Unused ILM in the XIP dbgstress image. Save it before seeding the RTT ring.
SCRATCH = 0x18000
SIZE = 0x900
PAYLOAD = b'HPM6800 Wasm to RTT CDC handoff\r\n' * 12


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('runtime', type=Path)
    parser.add_argument('sdk_openocd', type=Path)
    parser.add_argument('cdc_port')
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    require(not args.output.exists(), 'output must be a new file')
    client = toolbox.Client(args.runtime, 45)
    evidence = {}
    saved = {}
    original = None
    unresolved = False
    owned = False

    def call(page, op, **params):
        nonlocal unresolved
        try:
            response = client.request({'action': 'workbench', 'page': page, 'op': op, **params})
        except (RuntimeError, TimeoutError) as error:
            unresolved |= isinstance(error, TimeoutError) or '操作可能仍在执行' in str(error) or '是否已排队未知' in str(error)
            raise
        if 'job' in response:
            client.http('ack/' + response['job'], {})
        require(response.get('ok'), response.get('error'))
        return response['value']

    def inv(page, method, *values, target='view'):
        response = call(page, 'invoke', target=target, method=method, args=list(values))
        require(not response['errors'], response['errors'])
        return response['value']

    def dbg(method, *values):
        return inv('dbg', method, *values, target='session')

    def hid(method, *values):
        return inv('rttcdc', method, *values, target='hid')

    def scope(method, *values):
        return inv('scope', method, *values)

    def read(page, selector):
        return call(page, 'read', selector=selector)

    def set_value(page, selector, value):
        call(page, 'set', selector=selector, value=value)

    def click(page, selector):
        call(page, 'click', selector=selector)

    def wait(probe, pred, message, timeout=7):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = probe()
            if pred(value):
                return value
            time.sleep(.1)
        raise RuntimeError(message)

    def debug_state():
        return call('dbg', 'state')['summary']

    def connect_debug():
        require(inv('dbg', 'connect') is True, 'Wasm connect failed')
        state = debug_state()
        require(state['connected'] and state['backend'] == 'OpenOCD Wasm / WebUSB' and
                state['clockKhz'] == 8000, state)
        return {key: state[key] for key in ('backend', 'clockKhz', 'halted')}

    def read_scratch():
        return bytes(dbg('memRead', SCRATCH, SIZE)['$bytes'])

    def write_scratch(data):
        dbg('memWrite', SCRATCH, {'$base64': base64.b64encode(data).decode(), 'kind': 'bytes'})

    def target_adopt(page, prefix):
        selector = f'#{prefix}-use-session-target'
        require(any(c['selector'] == selector for c in call(page, 'describe')['controls']), 'refresh the workbench build')
        click(page, selector)
        wait(lambda: read(page, f'#{prefix}-target')['value'], lambda v: v == 'riscv', 'HPM target not adopted')
        require(debug_state()['connected'], 'configuration adoption released Wasm early')

    def start_rtt_and_read(result):
        def rtt_status():
            # Poll explicitly because the page suppresses UI refresh when hidden.
            hid('refresh')
            status = hid('summary')
            result.update({key: status[key] for key in ('cbAddr', 'moved', 'rdErr', 'startRc', 'running', 'state')})
            return status

        with serial.Serial(args.cdc_port, 115200, timeout=.15) as port:
            port.reset_input_buffer()
            hid('start')
            status = wait(rtt_status, lambda s: s['running'] and s['moved'] >= len(PAYLOAD),
                          'RTT did not forward seeded payload')
            require(int(status['cbAddr'], 16) == SCRATCH and not status['rdErr'], status['state'])
            require(not debug_state()['connected'], 'Wasm did not release the probe for RTT CDC')
            received = bytearray()
            deadline = time.monotonic() + 3
            while len(received) < len(PAYLOAD) and time.monotonic() < deadline:
                received.extend(port.read(len(PAYLOAD) - len(received)))
            require(received == PAYLOAD, f'CDC payload mismatch: received {len(received)} B')
            result.update(payloadBytes=len(received), payloadExact=True, debugReleased=True)

    def start_scope_and_sample():
        scope('start')
        snap = wait(lambda: scope('summary'), lambda s: s['running'] and s['receivedSamples'] >= 300,
                    'Scope did not capture real samples')
        require(snap['mode'] == 'real' and not debug_state()['connected'] and
                'RISC-V/JTAG' in read('scope', '#sc-backend')['text'], snap)
        require(not snap['lostGap'] and not snap['readErrors'], snap)
        return {key: snap[key] for key in ('rateHz', 'receivedSamples', 'lostGap', 'readErrors', 'vars')}

    def scope_stopped():
        snap = scope('summary')
        require(not snap['running'] and snap['transport'] is None, snap)
        return {key: snap[key] for key in ('receivedSamples', 'tailCountDelta', 'metricsError')}

    def check_read_cursor():
        require(struct.unpack_from('<I', read_scratch(), 40)[0] == len(PAYLOAD),
                'target RTT read cursor did not advance')

    LOCK.mkdir()
    try:
        require(not debug_state()['connected'] and not hid('summary')['running'] and not scope('summary')['running'], 'probe in use')
        require(not inv('rttcdc', 'summary')['open'], 'browser serial port already in use')
        owned = True
        controls_to_restore = {
            'rttcdc': ['#h-target', '#h-addr', '#h-size', '#h-chan'],
            'scope': ['#sc-target', '#sc-search', '#sc-period', '#sc-seconds', '#sc-cdcoff'],
        }
        for page, controls in controls_to_restore.items():
            for selector in controls:
                saved[(page, selector)] = read(page, selector)['value']
        desc = call('dbg', 'describe')
        require(any(c['selector'] == '#d-backend' for c in desc['controls']), 'debugger not ready')
        set_value('dbg', '#d-backend', 'riscv')
        cfg_files = [toolbox.pack_file(args.sdk_openocd / name, 'openocd/' + name) for name in CFG_FILES]
        call('dbg', 'upload', selector='#d-cfg-dir', files=cfg_files)
        wait(lambda: read('dbg', '#d-cfg-entry')['options'], lambda options: any(o['value'] == CFG for o in options),
             'CFG list not loaded')
        set_value('dbg', '#d-cfg-entry', CFG)
        wait(lambda: read('dbg', '#d-cfg-info')['text'], lambda t: CFG in t and '0x1000563d' in t,
             'wrong HPM CFG')
        call('dbg', 'upload', selector='#d-elf-file', files=[toolbox.pack_file(ELF)])
        wait(debug_state, lambda s: bool(s['elf']), 'ELF not loaded')
        evidence['wasmInitial'] = connect_debug()
        require(not evidence['wasmInitial']['halted'], 'start with the XIP dbgstress image running')

        target_adopt('rttcdc', 'h')
        dbg('halt')
        pc = dbg('readReg','pc')
        require(0x80003000 <= pc < 0x8000e000, f'unexpected test firmware PC {pc:#x}')
        original = read_scratch()
        require(len(original) == SIZE and read_scratch() == original, 'scratch backup unstable')
        ring = bytearray(SIZE)
        ring[:10] = b'SEGGER RTT'
        struct.pack_into('<II', ring, 16, 1, 0)
        struct.pack_into('<6I', ring, 24, 0, SCRATCH + 0x100, 1024, len(PAYLOAD), 0, 0)
        ring[0x100:0x100 + len(PAYLOAD)] = PAYLOAD
        write_scratch(ring)
        require(read_scratch() == ring, 'scratch readback failed')
        dbg('run')
        for selector, value in [('#h-addr', hex(SCRATCH)), ('#h-size', '0x1000'), ('#h-chan', '0')]:
            set_value('rttcdc', selector, value)
        evidence['rtt'] = {}
        start_rtt_and_read(evidence['rtt'])
        print('Wasm -> RTT CDC passed', flush=True)
        evidence['wasmAfterRtt'] = connect_debug()
        rtt = hid('summary')
        require(not rtt['running'] and not rtt['connected'], rtt)
        dbg('halt')
        check_read_cursor()
        dbg('run')

        target_adopt('scope', 'sc')
        wait(lambda: read('scope', '#sc-reuse-elf'), lambda x: not x['disabled'],
             'Scope could not reuse ELF while Wasm owned probe')
        click('scope', '#sc-reuse-elf')
        wait(lambda: scope('summary'), lambda s: bool(s['elf']), 'Scope ELF reuse did not finish')
        for name in ('g_ticks', 'g_stage'):
            set_value('scope', '#sc-search', name)
            choices = [c for c in call('scope', 'describe')['controls']
                       if c.get('type') == 'checkbox' and c.get('label', '').startswith(name)]
            require(len(choices) == 1, choices)
            set_value('scope', choices[0]['selector'], True)
        for selector, value in [('#sc-period', '1000'), ('#sc-seconds', '5'), ('#sc-cdcoff', False)]:
            set_value('scope', selector, value)
        evidence['scope'] = start_scope_and_sample()
        print('Wasm -> J-Scope passed', flush=True)
        evidence['wasmAfterScope'] = connect_debug()
        evidence['scope']['afterHandoff'] = scope_stopped()

        # Re-arm the fixture, then leave Wasm disconnected for both independent starts.
        dbg('halt')
        write_scratch(ring)
        require(read_scratch() == ring, 'scratch readback failed')
        dbg('run')
        inv('dbg', 'disconnect')
        require(not debug_state()['connected'], 'Wasm still connected before standalone RTT start')
        evidence['rttStandalone'] = {}
        start_rtt_and_read(evidence['rttStandalone'])
        require(hid('disconnect') is True, 'standalone RTT disconnect failed')
        print('Standalone RTT CDC passed', flush=True)
        require(not debug_state()['connected'] and not hid('summary')['running'], 'probe not idle before standalone Scope start')
        evidence['scopeStandalone'] = start_scope_and_sample()
        scope('disconnect')
        evidence['scopeStandalone']['afterDisconnect'] = scope_stopped()
        print('Standalone J-Scope passed', flush=True)

        evidence['wasmFinal'] = connect_debug()
        dbg('halt')
        check_read_cursor()
        write_scratch(original)
        require(read_scratch() == original, 'scratch restore mismatch')
        original = None
        evidence['scratchRestored'] = True
        dbg('run')
        evidence['handoffPassed'] = True
    except Exception as error:
        evidence['error'] = str(error)
    finally:
        if owned and not unresolved:
            try:
                if hid('summary')['connected']:
                    hid('disconnect')
                sampler = scope('summary')
                if sampler['transport'] is not None or sampler['running']:
                    scope('disconnect')
                if original is not None:
                    if not debug_state()['connected']:
                        connect_debug()
                    dbg('halt')
                    write_scratch(original)
                    require(read_scratch() == original, 'scratch restore mismatch')
                    evidence['scratchRestored'] = True
                state = debug_state()
                if state['connected']:
                    if state['halted'] and not evidence.get('wasmInitial', {}).get('halted'):
                        dbg('run')
                    inv('dbg', 'disconnect')
                require(not debug_state()['connected'] and not hid('summary')['running'] and
                        not scope('summary')['running'], 'probe still active')
                for (page, selector), value in saved.items():
                    set_value(page, selector, value)
                evidence['released'] = True
            except Exception as error:
                evidence['cleanupError'] = str(error)
        if not unresolved and 'cleanupError' not in evidence:
            LOCK.rmdir()
        with args.output.open('x', encoding='utf-8') as out:
            json.dump(evidence, out, ensure_ascii=False, indent=2)
            out.write('\n')
        print(json.dumps(evidence, ensure_ascii=False), flush=True)
    return 0 if evidence.get('handoffPassed') and evidence.get('released') else 1


if __name__ == '__main__':
    sys.exit(main())
