"""``python -m iconswap`` 入口（等价于 ``python src/main.py``）。"""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    src_dir = str(Path(__file__).resolve().parents[1])
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)
    from ui_window import main as run_app

    return run_app()


if __name__ == "__main__":
    raise SystemExit(main())
