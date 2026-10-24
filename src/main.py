#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IconSwap 启动入口。

直接运行::

    python src/main.py
    python -m iconswap        # 等价（见 src/iconswap/__main__.py）

打包入口（PyInstaller）也用这个文件：``src/main.py``。
"""

from __future__ import annotations

import sys
from pathlib import Path


def _ensure_src_on_path() -> None:
    """让 ``python src/main.py`` 与 ``python -m iconswap`` 都能找到包。"""
    src_dir = str(Path(__file__).resolve().parent)
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)


def main() -> int:
    _ensure_src_on_path()
    from ui_window import main as run_app

    return run_app()


if __name__ == "__main__":
    raise SystemExit(main())
