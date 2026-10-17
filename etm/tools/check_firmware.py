#!/usr/bin/env python3
"""离线检查 ELF 的启动向量、Flash/RAM 布局、DWARF 与测试调用链。"""
import argparse
import hashlib
import json
import struct
from pathlib import Path

from elftools.elf.elffile import ELFFile


def inspect(path):
    with path.open('rb') as stream:
        elf = ELFFile(stream)
        assert elf['e_machine'] == 'EM_ARM' and elf.elfclass == 32
        symtab = elf.get_section_by_name('.symtab')
        symbols = {s.name: {'address': s['st_value'], 'size': s['st_size']}
                   for s in symtab.iter_symbols() if s.name}
        vector = elf.get_section_by_name('.isr_vector')
        stack, reset = struct.unpack_from('<II', vector.data())
        assert vector['sh_addr'] == 0x08000000
        assert stack == 0x20020000
        assert reset & 1 and (reset & ~1) == (symbols['Reset_Handler']['address'] & ~1)
        names = ['g_trace', 'g_error', 'g_round', 'g_result', 'g_sysclk_hz', 'g_systick_count']
        for name in names:
            assert 0x24000000 <= symbols[name]['address'] < 0x24100000, name
        functions = ['trace_even', 'trace_odd', 'trace_workload', 'main', 'trace_init',
                     'SysTick_Handler', 'HAL_IncTick']
        for name in functions:
            assert 0x08000000 <= (symbols[name]['address'] & ~1) < 0x08020000, name
        assert elf.has_dwarf_info()
        dwarf = elf.get_dwarf_info()
        units = list(dwarf.iter_CUs())
        assert units and all(cu['version'] == 4 for cu in units)
        rows = []
        for cu in units:
            program = dwarf.line_program_for_CU(cu)
            if program:
                rows.extend(e.state for e in program.get_entries()
                            if e.state and not e.state.end_sequence)
        for name in functions:
            start = symbols[name]['address'] & ~1
            end = start + symbols[name]['size']
            assert any(start <= r.address < end and r.line for r in rows), name
        flash_end = max(s['sh_addr'] + s['sh_size'] for s in elf.iter_sections()
                        if s['sh_flags'] & 2 and 0x08000000 <= s['sh_addr'] < 0x08020000)
        assert flash_end <= 0x08020000
    disasm = path.parent / 'fw.disasm.txt'
    if disasm.exists():
        text = disasm.read_text(encoding='utf-8-sig')
        for name in ('trace_even', 'trace_odd', 'trace_workload'):
            assert any('bl' in line and '<' + name + '>' in line
                       for line in text.splitlines()), name
    return {
        'elf': str(path.resolve()),
        'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
        'checks': 'ELF/static checks only; hardware output not verified',
        'nominal': {'cpu_hz': 8000000, 'traceclk_hz': 4000000,
                    'port_width': 4, 'protocol': 'ETMv4.0', 'trace_id': 2,
                    'etm_config': 0, 'etm_victl': 0x201},
        'symbols': {n: symbols[n] for n in names + functions},
        'flash_code_bytes': flash_end - 0x08000000,
        'dwarf_version': 4,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    root = Path(__file__).resolve().parents[1]
    parser.add_argument('elf', nargs='?', type=Path,
                        default=root / 'firmware/stm32h7b0_etm/build/fw.elf')
    args = parser.parse_args()
    result = inspect(args.elf)
    out = args.elf.parent / 'firmware-info.json'
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print('PASS: 启动向量、Flash/AXI SRAM、DWARF 4、源码行、真实函数调用。')
    for name, symbol in result['symbols'].items():
        print(f"  {name:20s} 0x{symbol['address']:08X}")
    print(f'产物信息：{out}')
    print('实际 CPU/trace 时钟与波形仍需上板检查。')


if __name__ == '__main__':
    main()
