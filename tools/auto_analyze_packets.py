# -*- coding: utf-8 -*-
"""CLI shim: prefer `python -m packet.auto_analyze`. @author by ak"""
from packet.auto_analyze import main

if __name__ == "__main__":
    raise SystemExit(main())
