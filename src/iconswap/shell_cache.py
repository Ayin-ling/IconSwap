"""强制刷新 Windows 图标缓存。

两步组合（缺一不可）
------------------
1. ``SHChangeNotify(SHCNE_ASSOCCHANGED, SHCNF_IDLIST, None, None)``
   —— 通知资源管理器"关联/图标变了"，让它重新读取；
2. ``ie4uinit.exe -show``
   —— IE/Shell 的图标缓存重建入口，能把 ``iconcache_*.db`` 真正刷掉。

额外可选：``SHChangeNotify(SHCNE_UPDATEITEM, SHCNF_PATHW, path, None)`` 可以对
单个文件做定点刷新，比整机刷新更温和（这里作为 :func:`notify_paths` 提供）。
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from typing import Iterable, Optional

__all__ = [
    "SHCNE_ASSOCCHANGED",
    "SHCNF_IDLIST",
    "RefreshResult",
    "notify_shell",
    "notify_paths",
    "run_ie4uinit",
    "refresh_icon_cache",
]

SHCNE_ASSOCCHANGED = 0x08000000
SHCNE_UPDATEITEM = 0x00002000
SHCNF_IDLIST = 0x0000
SHCNF_PATHW = 0x0005

_CREATE_NO_WINDOW = 0x08000000


def _shell32():
    if os.name != "nt":  # pragma: no cover - 非 Windows
        return None
    try:
        return ctypes.windll.shell32
    except (AttributeError, OSError):  # pragma: no cover
        return None


def notify_shell(event: int = SHCNE_ASSOCCHANGED, flags: int = SHCNF_IDLIST, item1=None, item2=None) -> bool:
    """调用 ``SHChangeNotify``；成功返回 ``True``。

    非 Windows 平台直接返回 ``False``，便于测试。
    """
    shell32 = _shell32()
    if shell32 is None:  # pragma: no cover
        return False
    try:
        shell32.SHChangeNotify(ctypes.c_long(event), ctypes.c_uint(flags), item1, item2)
        return True
    except Exception:  # noqa: BLE001 - 通知失败不应中断主流程
        return False


def notify_paths(paths: Iterable[str], *, timeout: float = 0.0) -> int:
    """对一批文件做定点刷新，返回成功次数。"""
    count = 0
    for raw in paths:
        try:
            buffer = ctypes.c_wchar_p(str(raw))
            if notify_shell(SHCNE_UPDATEITEM, SHCNF_PATHW, buffer, None):
                count += 1
        except Exception:  # noqa: BLE001
            continue
    return count


def run_ie4uinit(*args: str, timeout: int = 20) -> bool:
    """执行 ``ie4uinit.exe``，默认 ``-show``；成功返回 ``True``。"""
    if os.name != "nt":  # pragma: no cover
        return False
    command = ["ie4uinit.exe", *(args or ("-show",))]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            timeout=timeout,
            creationflags=_CREATE_NO_WINDOW,
        )
        return completed.returncode == 0
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - 精简系统可能没有该 exe
        return False


class RefreshResult:
    """刷新结果，供 UI/日志展示。"""

    __slots__ = ("notified", "ie4uinit_ok", "paths", "errors")

    def __init__(self, notified: bool, ie4uinit_ok: bool, paths: int, errors: Optional[list[str]] = None) -> None:
        self.notified = notified
        self.ie4uinit_ok = ie4uinit_ok
        self.paths = paths
        self.errors = errors or []

    @property
    def ok(self) -> bool:
        return self.notified or self.ie4uinit_ok or self.paths > 0

    def as_text(self) -> str:
        parts = []
        parts.append("已通知资源管理器" if self.notified else "资源管理器通知失败")
        parts.append("图标缓存已重建" if self.ie4uinit_ok else "图标缓存重建未执行")
        if self.paths:
            parts.append(f"定点刷新 {self.paths} 个文件")
        return "，".join(parts)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<RefreshResult notified={self.notified} ie4uinit={self.ie4uinit_ok} paths={self.paths}>"


def refresh_icon_cache(
    paths: Optional[Iterable[str]] = None,
    *,
    run_rebuild: bool = True,
    timeout: int = 20,
) -> RefreshResult:
    """刷新图标缓存。

    :param paths: 需要定点刷新的文件（可选，通常是刚改过的 exe）。
    :param run_rebuild: 是否执行 ``ie4uinit.exe -show``。
    """
    errors: list[str] = []

    if os.name != "nt":  # pragma: no cover - 非 Windows 直接返回
        return RefreshResult(False, False, 0, ["当前平台不支持"])

    notified = notify_shell(SHCNE_ASSOCCHANGED, SHCNF_IDLIST, None, None)
    if not notified:
        errors.append("SHChangeNotify 调用失败")

    path_count = notify_paths(paths or []) if paths else 0

    rebuild_ok = run_ie4uinit("-show", timeout=timeout) if run_rebuild else False
    if run_rebuild and not rebuild_ok:
        errors.append("ie4uinit.exe -show 未成功")

    if not (notified or rebuild_ok or path_count) and not sys.platform.startswith("win"):  # pragma: no cover
        errors.append("非 Windows 平台")

    return RefreshResult(notified, rebuild_ok, path_count, errors)
