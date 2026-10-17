"""Compile the production sampler with host hardware substitutes (no HPM SDK needed).
Runs scheduler/packet tests; does not validate target instruction timing or board wiring.
Usage: python script_test/scope_host_test.py
"""
from pathlib import Path
import os
import re
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
src = root / 'firmware/application_5301/src/scope/scope_sampler.c'
source = src.read_text(encoding='utf-8')
# Replace SDK includes and the one MMIO timer read; all sampler logic stays intact.
source = re.sub(r'^#include "(?!scope_sampler.h)[^"]+"', '', source, flags=re.M)
source = source.replace('return *(volatile uint32_t *)(HPM_MCHTMR_BASE + 0x00);',
                        'return test_clock_read();')
with tempfile.TemporaryDirectory(prefix='scope-host-') as d:
    path = Path(d)
    (path / 'scope_sampler.h').write_text(src.with_suffix('.h').read_text(encoding='utf-8'), encoding='utf-8')
    unit = (root / 'script_test/host/scope_test_support.h').read_text(encoding='utf-8') + '\n' + source
    unit += '\n' + (root / 'script_test/host/scope_test_cases.c').read_text(encoding='utf-8')
    (path / 'test.c').write_text(unit, encoding='utf-8')
    subprocess.run([os.environ.get('CC', 'gcc'), '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror',
                    f'-I{root / "firmware/application_5301/src/api"}', '-Wno-unused-function', '-Wno-unused-variable', str(path / 'test.c'),
                    '-o', str(path / 'test')], check=True)
    subprocess.run([str(path / 'test')], check=True)
