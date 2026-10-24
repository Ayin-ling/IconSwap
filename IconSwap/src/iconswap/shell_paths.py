"""取用户桌面路径（快捷方式默认放这里，用户一眼能看到）。

优先用 Shell API ``SHGetFolderPathW(CSIDL_DESKTOPDIRECTORY)``，它会把
OneDrive 重定向过的桌面也解析正确；失败再依次回退到环境变量与
``~/Desktop``。全部失败时返回 ``~/Desktop``（调用方负责处理不可写）。
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from pathlib import Path
from typing import Optional

__all__ = ["desktop_dir", "desktop_shortcut_path", "desktop_dir_note"]

#: CSIDL_DESKTOPDIRECTORY —— 当前用户的桌面目录
_CSIDL_DESKTOPDIRECTORY = 0x0010
_MAX_PATH = 260


def _from_shell_api() -> Optional[Path]:
    if os.name != "nt":  # pragma: no cover - 非 Windows
        return None
    try:
        buffer = ctypes.create_unicode_buffer(_MAX_PATH)
        result = ctypes.windll.shell32.SHGetFolderPathW(
            wintypes.HWND(0),
            _CSIDL_DESKTOPDIRECTORY,
            wintypes.HANDLE(0),
            0,
            buffer,
        )
        if result == 0 and buffer.value:
            candidate = Path(buffer.value)
            if candidate.is_dir():
                return candidate
    except Exception:  # noqa: BLE001 - 没有 Shell API 就退回环境变量
        pass
    return None


def _from_environment() -> Optional[Path]:
    candidates: list[Path] = []
    userprofile = os.environ.get("USERPROFILE")
    if userprofile:
        candidates.append(Path(userprofile) / "Desktop")
    home = os.environ.get("HOME") or os.path.expanduser("~")
    if home:
        candidates.append(Path(home) / "Desktop")
    onedrive = os.environ.get("OneDrive") or os.environ.get("OneDriveConsumer")
    if onedrive:
        candidates.append(Path(onedrive) / "Desktop")

    for candidate in candidates:
        try:
            if candidate.is_dir():
                return candidate
        except OSError:  # pragma: no cover
            continue
    return None


def desktop_dir() -> Path:
    """返回当前用户桌面目录（尽量保证是真实存在的路径）。"""
    for resolver in (_from_shell_api, _from_environment):
        found = resolver()
        if found is not None:
            return found.resolve()

    # 兜底：即便不存在也返回约定路径，由调用方决定是否降级
    return Path(os.path.expanduser("~")) / "Desktop"


def desktop_dir_note() -> str:
    """桌面目录的来源说明（调试用）。"""
    if _from_shell_api() is not None:
        return "桌面路径来自 Shell API（支持 OneDrive 重定向）"
    if _from_environment() is not None:
        return "桌面路径来自环境变量"
    return "未找到桌面目录，回退到 ~/Desktop"


def desktop_shortcut_path(name: str) -> Path:
    """桌面上的快捷方式完整路径，自动补 ``.lnk`` 后缀。"""
    filename = name if name.lower().endswith(".lnk") else f"{name}.lnk"
    return desktop_dir() / filename
