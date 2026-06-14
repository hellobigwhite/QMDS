"""QMDS 项目启动入口"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from qmds.cli import main

if __name__ == "__main__":
    sys.exit(main())
