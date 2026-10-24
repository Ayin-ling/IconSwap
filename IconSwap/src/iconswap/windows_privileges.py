"""Windows 权限与受保护目录判定。

用途
----
* 修改 ``.lnk`` 时先判断"有没有可能写进去"，给出**可操作**的提示，
  而不是让 COM 静默失败。
* 区分"文件被占用"与"权限不足"，两者处理方式不同。
"""

from __future__ import annotations

import ctypes
import os
from pathlib import Path
from typing import Optional

__all__ = [
    "is_admin",
    "is_protected_path",
    "describe_com_error",
    "permission_hint",
]

#: 视为"受保护"的目录前缀（小写、正斜杠归一化后比较）
_PROTECTED_ROOTS = (
    r"c:/program files",
    r"c:/program files (x86)",
    r"c:/programdata",
    r"c:/windows",
    r"c:/windows/system32",
)


def is_admin() -> bool:
    """当前进程是否具备管理员权限。"""
    if os.name != "nt":  # pragma: no cover - 非 Windows
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001  # pragma: no cover
        return False


def _normalized(path: "str | os.PathLike[str]") -> str:
    text = os.path.expandvars(str(path)).replace("\\", "/").lower()
    return os.path.normpath(text).replace("\\", "/")


def is_protected_path(path: "str | os.PathLike[str]") -> bool:
    """目标是否位于通常需要管理员权限的目录。"""
    target = _normalized(path)
    for root in _PROTECTED_ROOTS:
        if target == root or target.startswith(root + "/"):
            return True
    # 盘符根目录（C:/xxx 这种一层）也算敏感
    parts = [part for part in Path(target).parts if part]
    return len(parts) <= 1


def permission_hint(path: "str | os.PathLike[str]") -> str:
    """给用户看的权限提示（不为空表示"大概率写不进去"）。"""
    target = Path(os.fspath(path))
    if not is_protected_path(target):
        return ""
    if is_admin():
        return "目标位于受保护目录，但你以管理员身份运行，通常可以写入"
    return (
        "目标位于受保护目录（Program Files / Windows / ProgramData 等），"
        "当前不是管理员，写入很可能被拒绝。请用「以管理员身份运行」重试，"
        "或把快捷方式复制到桌面后再改图标"
    )


def describe_com_error(error: BaseException) -> str:
    """把 ``pywintypes.com_error`` 之类翻译成可读文字。"""
    text = str(error).strip()
    try:
        import pywintypes  # noqa: PLC0415 - 惰性导入

        if isinstance(error, pywintypes.com_error):
            hresult: Optional[int] = None
            detail = ""
            if error.args:
                if isinstance(error.args[0], int):
                    hresult = error.args[0]
                for arg in error.args[1:]:
                    if isinstance(arg, str) and arg.strip():
                        detail = arg.strip()
                        break
            code = f"0x{hresult & 0xFFFFFFFF:08X}" if isinstance(hresult, int) else "?"
            message = detail or text or "COM 调用失败"
            if isinstance(hresult, int) and (hresult & 0xFFFFFFFF) == 0x80070005:
                return f"{message}（拒绝访问 0x80070005，通常是权限不足或文件被占用）"
            return f"{message}（HRESULT {code}）"
    except Exception:  # noqa: BLE001 - 没有 pywin32 时退回原始文本
        pass
    return text or type(error).__name__
