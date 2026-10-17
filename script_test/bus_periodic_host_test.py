"""Compile the production periodic engine with a virtual timer and bus adapters."""
from pathlib import Path
import os
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
src = root / 'firmware/application_5301/src'
with tempfile.TemporaryDirectory() as folder:
    exe = Path(folder) / 'test'
    flags = ['-fsanitize=address,undefined', '-fno-omit-frame-pointer'] if os.getenv('BUS_HOST_SANITIZE') else []
    subprocess.run([os.getenv('CC', 'gcc'), '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror', *flags,
        *[f'-I{src / p}' for p in ['api', 'bus_periodic', 'i2c_bridge', 'spi_bridge', 'scope', 'rtt', 'analog_bridge']],
        str(src / 'bus_periodic/bus_periodic.c'), str(root / 'script_test/host/bus_periodic_test.c'),
        '-o', str(exe)], check=True)
    subprocess.run([str(exe)], check=True)
print('Periodic production engine: MCU cadence, groups, delays, overflow, stop/reset, stale ACK, priority yield PASS')
