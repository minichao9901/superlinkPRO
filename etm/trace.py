#!/usr/bin/env python3
"""AI Trace CLI: python etm/trace.py --help."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / 'tools'))
from trace_cli import main

if __name__ == '__main__':
    sys.exit(main())
