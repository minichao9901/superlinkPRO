#!/usr/bin/env python3
"""Configure or restore STM32F103ZE ETMv3.5, 4-bit parallel trace.

Target PLL/CPU clocks are never changed.  `enable` saves the prior CoreSight
register values so `restore` can return the target to its original trace mode.
"""
import argparse
import hashlib
import json
import time
from pathlib import Path

from elftools.elf.elffile import ELFFile
from pyocd.core.helpers import ConnectHelper
from pyocd.core.target import Target

ROOT = Path(__file__).resolve().parents[1]
ELF = ROOT / 'firmware/stm32f103_cdc_trace/fw-ze.elf'
SNAPSHOT = ROOT / 'captures/pxlogic-f103ze/target-before.json'
REG = {
    'demcr': 0xE000EDFC, 'dbg_cr': 0xE0042004,
    'etmcr': 0xE0041000, 'etmtrigger': 0xE0041008,
    'etmteevr': 0xE0041020, 'etmtecr1': 0xE0041024,
    'etmfflr': 0xE004102C, 'etmtraceidr': 0xE0041200,
    'tpiu_cspsr': 0xE0040004, 'tpiu_sppr': 0xE00400F0,
    'tpiu_ffcr': 0xE0040304,
    'itm_tcr': 0xE0000E80, 'itm_ter': 0xE0000E00,
    'dwt_ctrl': 0xE0001000,
}
ETM = 0xE0041000
TPIU = 0xE0040000


def read(target, name):
    return target.read32(REG[name])


def write(target, name, value, mask=0xFFFFFFFF):
    target.write32(REG[name], value)
    target.flush()
    actual = read(target, name)
    if ((actual ^ value) & mask) != 0:
        raise RuntimeError(f'{name} readback {actual:#010x}, expected {value:#010x}')


def wait_bit(target, address, mask, expected, what):
    for _ in range(100):
        value = target.read32(address)
        if value & mask == expected:
            return value
        time.sleep(.005)
    raise RuntimeError(f'{what} timed out, final {value:#010x}')


def unlock(target, base):
    status = target.read32(base + 0xFB4)
    if status & 3 == 3:
        target.write32(base + 0xFB0, 0xC5ACCE55)
        target.flush()
        if target.read32(base + 0xFB4) & 2:
            raise RuntimeError(f'CoreSight unlock failed: {base:#x}')
    return status


def verify_image(target):
    count = 0
    with ELF.open('rb') as stream:
        elf = ELFFile(stream)
        for segment in elf.iter_segments():
            if segment['p_type'] != 'PT_LOAD' or not segment['p_filesz']:
                continue
            base = segment['p_paddr']
            if not 0x08000000 <= base < 0x08080000:
                continue
            data = segment.data()
            for offset in range(0, len(data), 512):
                expected = data[offset:offset + 512]
                actual = bytes(target.read_memory_block8(base + offset, len(expected)))
                if actual != expected:
                    raise RuntimeError(f'board Flash differs from fw-ze.elf at {base + offset:#010x}')
                count += len(expected)
    if not count:
        raise RuntimeError('ELF has no Flash load segment')
    return count


def snapshot(target):
    return {name: read(target, name) for name in REG}


def configure(target):
    if SNAPSHOT.exists():
        raise RuntimeError(f'Existing snapshot: {SNAPSHOT}; restore it before a new enable')
    cpuid = target.read32(0xE000ED00)
    device = target.read32(0xE0042000) & 0xFFF
    if (cpuid >> 4) & 0xFFF != 0xC23 or device != 0x414:
        raise RuntimeError(f'expected F103ZE/M3, got CPUID {cpuid:#x} DEV {device:#x}')
    flash_bytes = verify_image(target)
    rcc_cr = target.read32(0x40021000)
    rcc_cfgr = target.read32(0x40021004)
    if (rcc_cfgr >> 2) & 3 != 2 or not rcc_cr & (1 << 25):
        raise RuntimeError(f'PLL clock not active: RCC_CR {rcc_cr:#x}, CFGR {rcc_cfgr:#x}')
    if not target.read32(TPIU) & 8:
        raise RuntimeError('TPIU does not advertise the 8-bit input used for 4-bit DDR pins')
    was_halted = target.get_state() == Target.State.HALTED
    if not was_halted:
        target.halt()
    before = snapshot(target)
    locks = {'etm': target.read32(ETM + 0xFB4), 'tpiu': target.read32(TPIU + 0xFB4)}
    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    data = {'cpuid': cpuid, 'device': device, 'flash_bytes_verified': flash_bytes,
            'elf_sha256': hashlib.sha256(ELF.read_bytes()).hexdigest(),
            'was_halted': was_halted, 'before': before, 'locks': locks}
    SNAPSHOT.write_text(json.dumps(data, indent=2), encoding='utf-8')
    try:
        write(target, 'demcr', before['demcr'] | 0x01000000)
        unlock(target, ETM)
        unlock(target, TPIU)
        # Clear power-down, then enter the ETM programming state.
        write(target, 'etmcr', before['etmcr'] & ~1, mask=0x801)
        write(target, 'etmcr', (read(target, 'etmcr') | 0x400) & ~1, mask=0xC01)
        wait_bit(target, ETM + 0x10, 2, 2, 'ETM programming state')
        write(target, 'etmcr', (read(target, 'etmcr') | 0x800) & ~0x301,
              mask=0xF01)  # ETMEN=1; no branch broadcast/debug request/power-down.
        write(target, 'etmtrigger', 0)
        write(target, 'etmteevr', 0x6F)  # Always-true trace-enable event.
        write(target, 'etmtecr1', 0)  # Cortex-M3 implements only the start/stop bit.
        write(target, 'etmfflr', 0)
        write(target, 'etmtraceidr', 2, mask=0x7F)
        write(target, 'tpiu_cspsr', 8, mask=0xF)  # 8-bit input -> four DDR data pins.
        write(target, 'tpiu_sppr', 0, mask=0x3)  # Parallel synchronous port.
        write(target, 'tpiu_ffcr', before['tpiu_ffcr'] | 2, mask=0x102)
        write(target, 'dbg_cr', (before['dbg_cr'] & ~0xE0) | 0xE0,
              mask=0xE0)  # TRACE_IOEN=1, 4-bit TRACE_MODE.
        write(target, 'etmcr', read(target, 'etmcr') & ~0x400, mask=0xC01)
        wait_bit(target, ETM + 0x10, 2, 0, 'ETM leaving programming state')
        after = snapshot(target)
        data['after'] = after
        data['etmsr'] = target.read32(ETM + 0x10)
        data['etmpdsr'] = target.read32(ETM + 0x314)
        SNAPSHOT.write_text(json.dumps(data, indent=2), encoding='utf-8')
    except Exception:
        try:
            restore(target, data)
        except Exception as cleanup:
            print(f'ETM cleanup failed, saved snapshot retained: {cleanup}')
        raise
    finally:
        if not was_halted:
            target.resume()
    return data


def restore(target, data):
    before = data['before']
    was_running = target.get_state() != Target.State.HALTED
    if was_running:
        target.halt()
    try:
        unlock(target, ETM)
        unlock(target, TPIU)
        write(target, 'demcr', read(target, 'demcr') | 0x01000000)
        write(target, 'etmcr', (read(target, 'etmcr') | 0x400) & ~1, mask=0xC01)
        try:
            wait_bit(target, ETM + 0x10, 2, 2, 'restore programming state')
        except RuntimeError:
            # ETMv3.2+ cannot acknowledge programming until its FIFO drains.
            # Power-down is always writable and clears pending overflow/FIFO.
            write(target, 'etmcr', read(target, 'etmcr') | 1, mask=1)
            wait_bit(target, ETM + 0x10, 2, 2, 'ETM programming after FIFO clear')
            write(target, 'etmcr', read(target, 'etmcr') & ~1, mask=1)
        for name in ('etmtrigger', 'etmteevr', 'etmtecr1', 'etmfflr', 'etmtraceidr'):
            write(target, name, before[name], mask=0x7F if name == 'etmtraceidr' else 0xFFFFFFFF)
        for name in ('tpiu_ffcr', 'tpiu_cspsr', 'tpiu_sppr'):
            write(target, name, before[name])
        write(target, 'etmcr', before['etmcr'], mask=0xC01)
        for name, value in data.get('sync_before', {}).items():
            if name in ('itm_tcr', 'itm_ter', 'dwt_ctrl'):
                write(target, name, value)
        write(target, 'dbg_cr', before['dbg_cr'])
        if data['locks']['etm'] & 2:
            target.write32(ETM + 0xFB0, 0)
        if data['locks']['tpiu'] & 2:
            target.write32(TPIU + 0xFB0, 0)
        target.flush()
        write(target, 'demcr', before['demcr'])
    finally:
        if was_running:
            target.resume()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=('enable', 'sync', 'finish', 'status', 'restore'))
    args = p.parse_args()
    session = ConnectHelper.session_with_chosen_probe(
        target_override='cortex_m',
        options={'frequency': 1000000, 'connect_mode': 'attach',
                 'auto_unlock': False, 'resume_on_disconnect': True})
    with session:
        target = session.target
        if args.action == 'enable':
            result = configure(target)
        elif args.action == 'sync':
            result = json.loads(SNAPSHOT.read_text(encoding='utf-8'))
            if 'sync_before' not in result:
                result['sync_before'] = {name: read(target, name)
                                         for name in ('itm_tcr', 'itm_ter', 'dwt_ctrl')}
                SNAPSHOT.write_text(json.dumps(result, indent=2), encoding='utf-8')
            unlock(target, 0xE0000000)
            unlock(target, ETM)
            write(target, 'itm_tcr', 0x0001000D)
            write(target, 'dwt_ctrl', read(target, 'dwt_ctrl') | 1, mask=1)
            write(target, 'dbg_cr', (read(target, 'dbg_cr') & ~0xE0) | 0xE0, mask=0xE0)
            write(target, 'etmcr', read(target, 'etmcr') & ~1, mask=1)
            time.sleep(.4)
            result = {'registers': snapshot(target), 'etmsr': target.read32(ETM + 0x10),
                      'running': target.get_state() != Target.State.HALTED}
        elif args.action == 'finish':
            if not SNAPSHOT.exists():
                raise RuntimeError('No trace snapshot; run enable before finish')
            unlock(target, ETM)
            wait_bit(target, ETM + 0x10, 2, 2, 'ETM programming state')
            write(target, 'etmtecr1', 0)
            write(target, 'etmcr', read(target, 'etmcr') & ~0x400, mask=0xC01)
            wait_bit(target, ETM + 0x10, 2, 0, 'ETM trace state')
            result = {'registers': snapshot(target), 'etmsr': target.read32(ETM + 0x10),
                      'running': target.get_state() != Target.State.HALTED}
        elif args.action == 'status':
            result = {'registers': snapshot(target),
                      'etmsr': target.read32(ETM + 0x10),
                      'etmpdsr': target.read32(ETM + 0x314),
                      'running': target.get_state() != Target.State.HALTED}
        else:
            result = json.loads(SNAPSHOT.read_text(encoding='utf-8'))
            restore(target, result)
            SNAPSHOT.unlink()
            result = {'restored': True, 'registers': snapshot(target)}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
