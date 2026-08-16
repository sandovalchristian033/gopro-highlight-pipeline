#!/usr/bin/env python3
"""Punto de entrada del pipeline. Corre `python run.py --help` para ver todo."""

import sys

from pov.cli import main

if __name__ == "__main__":
    sys.exit(main())
