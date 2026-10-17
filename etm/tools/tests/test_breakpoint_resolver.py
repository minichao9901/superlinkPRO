import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools'))
from decode_trace import Image
from breakpoint_resolver import resolve_breakpoint


class BreakpointResolverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.image = Image(ROOT / 'firmware/stm32h7b0_spi_trace/stable-70-35-v1/fw.elf')

    def test_function_and_thumb_instruction_address(self):
        items = resolve_breakpoint(self.image, 'b HAL_SPI_Transmit')
        self.assertEqual(items[0]['address'], 0x08003c1c)
        self.assertEqual(resolve_breakpoint(self.image, 'b *0x08003c1c')[0]['address'], items[0]['address'])
        with self.assertRaisesRegex(ValueError, '半字对齐'):
            resolve_breakpoint(self.image, 'b *0x08003c1d')
        with self.assertRaisesRegex(ValueError, '指令起点'):
            resolve_breakpoint(self.image, 'b *0x08003c1e')  # middle of a 32-bit instruction

    def test_file_and_function_use_absolute_source_line(self):
        address, _, _ = next(f for f in self.image.functions if f[2] == 'main')
        _, file, line = self.image.location(address)
        a = resolve_breakpoint(self.image, f'b main:{line}')
        b = resolve_breakpoint(self.image, f'b {Path(file).name}:{line}')
        self.assertEqual(a, b)
        self.assertTrue(all(r['line'] == line for r in a))

    def test_missing_line_and_symbols_never_guess(self):
        for command in ('b main.c:999999', 'b not_a_function', 'b main:0'):
            with self.assertRaises(ValueError):
                resolve_breakpoint(self.image, command)

    def test_missing_debug_info_is_explicit(self):
        image = Image(ROOT / 'firmware/stm32h7b0_spi_trace/stable-70-35-v1/fw.elf')
        image.lines = {}
        image.line_starts = []
        with self.assertRaisesRegex(ValueError, '没有 DWARF'):
            resolve_breakpoint(image, 'b main:199')
        self.assertTrue(resolve_breakpoint(image, 'b main'))

    def test_literal_pool_is_not_an_instruction_breakpoint(self):
        data = next(s['st_value'] for s in self.image.elf.get_section_by_name('.symtab').iter_symbols()
                    if s.name == '$d' and 0x08000000 <= s['st_value'] < 0x08020000)
        with self.assertRaisesRegex(ValueError, '指令起点'):
            resolve_breakpoint(self.image, f'b *0x{data:x}')
