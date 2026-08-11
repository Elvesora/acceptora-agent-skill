#!/usr/bin/env python3
"""Compatibility wrapper for the installer's non-mutating legacy preview."""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS_ROOT = Path(__file__).resolve().parent
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from install import preview_main


if __name__ == "__main__":
    raise SystemExit(preview_main())
