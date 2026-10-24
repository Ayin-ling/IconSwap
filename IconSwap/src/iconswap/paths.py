"""项目内的路径约定：源码运行与 PyInstaller 打包后都能找到资源。

源码布局::

    IconSwap/
    ├── bin/rcedit.exe
    ├── src/iconswap/{app.py, templates/, static/}
    └── src/main.py

打包后 ``bundle_root()`` 指向 ``sys._MEIPASS``，其中 ``bin/`` 与
``iconswap/`` 由 PyInstaller 的 ``--add-data`` 原样带进来。
"""

from __future__ import annotations

import sys
from pathlib import Path

__all__ = ["is_frozen", "bundle_root", "bin_dir", "package_dir", "rcedit_candidates"]


def is_frozen() -> bool:
    """是否运行在 PyInstaller 打包出的 exe 里。"""
    return bool(getattr(sys, "frozen", False))


def bundle_root() -> Path:
    """只读资源根目录：打包后是 ``_MEIPASS``，源码运行为仓库根目录。"""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    # src/iconswap/paths.py -> src/iconswap -> src -> 仓库根
    return Path(__file__).resolve().parents[2]


def bin_dir() -> Path:
    """外部工具目录（``bin/rcedit.exe``）。"""
    return bundle_root() / "bin"


def package_dir() -> Path:
    """``iconswap`` 包目录（模板与静态资源所在处）。"""
    if is_frozen():
        return bundle_root() / "iconswap"
    return Path(__file__).resolve().parent


def rcedit_candidates() -> list[Path]:
    """``bin/`` 优先，其次兼容旧的 ``tools/`` 与仓库根目录。"""
    names = ("rcedit.exe", "rcedit-x64.exe", "rcedit-x86.exe")
    roots = (bin_dir(), bundle_root() / "tools", bundle_root())
    return [root / name for root in roots for name in names]
