"""Build a host wire server from the production scheduler and bridge validators.
Usage: python3 script_test/bus_periodic_wire_server.py OUTPUT_EXECUTABLE
Used by the paired web repository's bus-periodic test for a real C/JS ABI check.
"""
from pathlib import Path
import os
import re
import subprocess
import sys
import tempfile
root = Path(__file__).resolve().parents[1]
src = root / 'firmware/application_5301/src'
def function(text, name):
    m = re.search(r'(?:static )?uint8_t ' + name + r'\([^;]*?\)\s*\{', text)
    if not m: raise ValueError(name)
    start = m.start(); pos = m.end(); depth = 1
    while depth:
        if text[pos] == '{': depth += 1
        elif text[pos] == '}': depth -= 1
        pos += 1
    return text[start:pos]
i2c = (src / 'i2c_bridge/i2c_bridge.c').read_text(encoding='utf-8')
spi = (src / 'spi_bridge/spi_bridge.c').read_text(encoding='utf-8')
# Ignore the unsupported-board stubs, keep the actual production validators.
i2c = i2c[i2c.index('#else /* BOARD_HAS_I2C_BRIDGE */'):]
spi = spi[spi.index('#else /* BOARD_HAS_SPI_BRIDGE */'):]
code = '''#include <stdint.h>
#include "bus_periodic.h"
#include "i2c_bridge_proto.h"
#include "spi_bridge_proto.h"
#define IB_REQ_BYTES 60U
static uint16_t rd_u16(const uint8_t *p){return p[0]|((uint16_t)p[1]<<8);}
'''
code += function(i2c, 'ib_xfer_check') + '\n'
code += function(i2c, 'i2c_bridge_periodic_check') + '\n'
code += function(spi, 'spi_bridge_periodic_check') + '\n'
with tempfile.TemporaryDirectory() as folder:
    checks = Path(folder) / 'validators.c'; checks.write_text(code)
    subprocess.run([os.getenv('CC', 'gcc'), '-std=c11', '-O2', '-Wall', '-Wextra',
        '-Wno-unused-function', '-Werror', '-DBP_SERVER',
        *[f'-I{src / p}' for p in ['bus_periodic','i2c_bridge','spi_bridge','scope','rtt','analog_bridge']],
        str(src / 'bus_periodic/bus_periodic.c'), str(root / 'script_test/host/bus_periodic_test.c'),
        str(checks), '-o', sys.argv[1]], check=True)
