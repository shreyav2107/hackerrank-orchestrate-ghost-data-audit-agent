"""Launcher for the deterministic pipeline bundled in this code directory."""

import sys
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parent
ROOT_DIR = CODE_DIR.parent
if str(CODE_DIR) not in sys.path:
    sys.path.insert(0, str(CODE_DIR))
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(1, str(ROOT_DIR))

from pipeline import main

if __name__ == "__main__":
    main()
