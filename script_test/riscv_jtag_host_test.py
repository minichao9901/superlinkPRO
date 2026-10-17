"""Compile the complete production JTAG C engine against a posted DMI/TAP model.
No HPM SDK or board required; this does not validate GPIO timing.
"""
from pathlib import Path
import os
import re
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
src = root / 'firmware/application_5301/src/riscv/riscv_jtag.c'
source = src.read_text(encoding='utf-8')
source = re.sub(r'^#include "(?!riscv_jtag.h)[^"]+"', '', source, flags=re.M)
source = source.replace('return *(volatile uint32_t *)(HPM_MCHTMR_BASE + 0x00);',
                        'return ++test_clock;')
with tempfile.TemporaryDirectory(prefix='jtag-host-') as directory:
    path = Path(directory)
    (path / 'riscv_jtag.h').write_text(src.with_suffix('.h').read_text(encoding='utf-8'), encoding='utf-8')
    support = (root / 'script_test/host/riscv_jtag_test_support.h').read_text(encoding='utf-8')
    cases = (root / 'script_test/host/riscv_jtag_test_cases.c').read_text(encoding='utf-8')
    (path / 'test.c').write_text(support + '\n' + source + '\n' + cases, encoding='utf-8')
    extra = ['-fsanitize=address,undefined', '-fno-omit-frame-pointer'] if os.environ.get('JTAG_HOST_SANITIZE') == '1' else []
    subprocess.run([os.environ.get('CC', 'gcc'), '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror',
                    '-Wno-unused-function', *extra, str(path / 'test.c'), '-o', str(path / 'test')], check=True)
    subprocess.run([str(path / 'test')], check=True)
