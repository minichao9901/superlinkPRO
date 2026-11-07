"""Program and boot the repository's HPM6800EVK scope fixture through the workbench CLI."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[3]
FIXTURE = ROOT / 'web/tools/target-firmware/hpm6800evk_scope/fw.elf'
CFG = 'hpm6800_all_in_one.cfg'
LOCK = ROOT.parent / 'web-etm-trace-coordination/hardware.lock'
FIXTURE_HASH = 'e5124ca35a872c2c54f210df99c5cfc3ca7a0d8f6a64f1cfa29a907aeb9b73a9'


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('runtime', type=Path)
    parser.add_argument('cfg_directory', type=Path, help='HPM SDK 1.11.0 boards/openocd')
    parser.add_argument('output', type=Path, help='new evidence directory')
    parser.add_argument('--lock-held', action='store_true', help='continue the connection started by this same operator')
    parser.add_argument('--verify-existing', action='store_true', help='recheck and boot an already programmed fixture')
    args = parser.parse_args()
    image = FIXTURE.read_bytes()
    require(hashlib.sha256(image).hexdigest() == FIXTURE_HASH, 'unexpected fixture digest')
    args.output.mkdir(parents=True, exist_ok=False)
    args_file = args.output.resolve() / 'image-argument.json'
    args_file.write_text(json.dumps([{'$file': str(FIXTURE), 'kind': 'buffer'}]), encoding='ascii')
    cli = [sys.executable, '-X', 'utf8', str(ROOT / 'toolbox.py'), '--runtime', str(args.runtime.resolve())]
    report = {'cfg': CFG, 'imageSha256': FIXTURE_HASH,
              'wasmSha256': hashlib.sha256((ROOT / 'web/app/core/openocd-runtime/openocd-core.wasm').read_bytes()).hexdigest()}
    connected = False
    unresolved_job = False

    def call(*words, timeout=30):
        nonlocal unresolved_job
        try:
            process = subprocess.run([*cli, '--timeout', str(timeout), *words], cwd=ROOT,
                                     capture_output=True, encoding='utf-8', timeout=timeout + 20)
        except subprocess.TimeoutExpired:
            unresolved_job = True
            raise
        result = json.loads(process.stdout)
        error = result.get('error', '')
        if not result.get('ok') and not result.get('job') and (
                '是否已排队未知' in error or re.search(r'job [a-f0-9]{32}', error)):
            unresolved_job = True
        require(result.get('ok'), json.dumps(result, ensure_ascii=False))
        return result['value']

    def dbg(*words, **options):
        return call('page', 'dbg', *words, **options)

    def invoke(method, *values, target='session', argfile=None, timeout=30):
        arguments = ['--args-file', str(argfile)] if argfile else ['--args', json.dumps(values)]
        result = dbg('invoke', '--target', target, *arguments, method, timeout=timeout)
        require(not result['errors'], str(result['errors']))
        return result['value']

    def status():
        return dbg('state')['summary']

    def word(address):
        return int.from_bytes(bytes(invoke('memRead', address, 4)['$bytes']), 'little')

    def app_sample():
        invoke('halt')
        try:
            return {'pc': invoke('readReg', 'pc'), 'magic': word(0x01240030), 'tick': word(0x01240000)}
        finally:
            invoke('run')

    if args.lock_held:
        require(LOCK.is_dir(), 'missing hardware coordination lock')
    else:
        LOCK.mkdir()
    try:
        call('ping')
        require(any(page['id'] == 'dbg' for page in call('catalog')['pages']), 'debug page is unavailable')
        desc = dbg('describe')
        controls = {control['selector'] for control in desc['controls']}
        require({'#d-backend', '#d-cfg-dir', '#d-cfg-entry'} <= controls, 'unexpected workbench controls')
        already_connected = status()['connected']
        require(not already_connected or args.lock_held, 'the workbench already has a debug session')
        if not already_connected:
            dbg('set', '#d-backend', json.dumps('openocd'))
        methods = {method['name'] for method in dbg('describe', '--target', 'session')['methods']}
        require({'programFlashElf', 'bootVerifiedFlash', 'memRead'} <= methods, 'reload the current workbench code')
        if not already_connected:
            dbg('upload', '--directory', '#d-cfg-dir', str(args.cfg_directory.resolve()))
            deadline = time.monotonic() + 10
            while CFG not in {option['value'] for option in dbg('read', '#d-cfg-entry')['options']}:
                require(time.monotonic() < deadline, 'the official CFG entry is missing')
                time.sleep(.2)
            dbg('set', '#d-cfg-entry', json.dumps(CFG))
        cfg_info = dbg('wait', '#d-cfg-info', '--contains', 'hpm6880.cpu0', timeout=10)['text']
        require(CFG in cfg_info and 'hpm6880.cpu0' in cfg_info and '0x1000563d' in cfg_info, cfg_info)
        report['cfgInfo'] = cfg_info
        connected = already_connected or invoke('connect', target='view', timeout=45) is True
        require(connected, 'Wasm connection failed; see workbench log')
        summary = status()
        require(summary['connected'] and summary['backend'] == 'OpenOCD Wasm / WebUSB' and
                summary['clockKhz'] == 8000, str(summary))
        report['connected'] = summary

        print('OpenOCD Wasm + WebUSB connected at 8 MHz.', flush=True)
        if args.verify_existing:
            report['reverified'] = invoke('verifyFlashElf', argfile=args_file, timeout=450)
            require(report['reverified']['bytes'] == 47856, 'unexpected ELF readback length')
        else:
            report['program'] = invoke('programFlashElf', argfile=args_file, timeout=900)
            require([(r['first'], r['last'], r['bytes']) for r in report['program']['verified']] ==
                    [(0, 1, 8192), (3, 13, 45056)], 'unexpected verified sector ranges')
        print('Flash readback verified; resetting into the programmed image.', flush=True)
        report['boot'] = invoke('bootVerifiedFlash', timeout=75)
        require(report['boot']['running'], 'the target did not run after reset')
        first = app_sample()
        time.sleep(.2)
        second = app_sample()
        require(all(s['magic'] == 0x53434f50 and 0x80003000 <= s['pc'] < 0x8000dc88 for s in (first, second)) and
                0 < (second['tick'] - first['tick']) % (1 << 32) < 1000000,
                f'application marker/tick mismatch: {first}, {second}')
        report['application'] = [first, second]
    except Exception as error:
        report['error'] = str(error)
        try:
            log = dbg('read', '#d-out')['text'][-4000:]
            report['logTail'] = re.sub(r'serial=[\w-]+', 'serial=[redacted]', log)
        except Exception:
            pass
    finally:
        if unresolved_job:
            report['cleanupError'] = 'CLI job must be reconciled before disconnecting or releasing the lock'
        elif connected:
            try:
                invoke('disconnect', target='view')
                require(not status()['connected'], 'disconnect was not confirmed')
                report['disconnected'] = True
            except Exception as error:
                report['cleanupError'] = str(error)
        if 'cleanupError' not in report:
            LOCK.rmdir()
        report['passed'] = 'error' not in report and 'cleanupError' not in report
        (args.output / 'hardware.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
