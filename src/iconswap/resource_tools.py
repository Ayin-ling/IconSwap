"""调用 rcedit.exe 替换 exe 内嵌图标，带备份、锁定检测与失败回滚。

为什么用 rcedit
--------------
rcedit 是 Electron 官方在用的轻量工具，改图标不需要重新链接资源，
命令行极简：

::

    rcedit.exe "目标.exe" --set-icon "新图标.ico"

它只做资源段重写，不会碰代码段；但仍**不能**用于带数字签名的 exe
（改完签名立即失效，系统会判定文件损坏），这条约束由上层
:mod:`iconswap.icon_replacer` 的分流逻辑保证。

工具位置（按优先级查找）
----------------------
1. 显式传入 ``rcedit=...``
2. 环境变量 ``ICONSWAP_RCEDIT``
3. 仓库 / 包内的 ``bin\\rcedit.exe``（打包时随包带上；见 README.md）
4. 兼容旧的 ``tools\\rcedit.exe`` 与 ``_MEIPASS\\bin\\rcedit.exe``
5. 系统 ``PATH``

路径含空格（例如 ``C:\\Program Files\\...``）时无需自己加引号：
这里用参数列表调用 ``subprocess``，不会经过 shell，空格与中文都原样传递；
日志里打印的命令行会做展示用引号处理。
"""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from iconswap import paths

LOGGER = logging.getLogger("iconswap.rcedit")
from iconswap.pe_inspector import (
    PeInspectionError,
    has_icon_resource,
    overlay_size,
    resource_signature,
)

__all__ = [
    "ResourceToolError",
    "ResourceToolMissing",
    "ExeLockedError",
    "UnsafeOverlayError",
    "backup_path_for",
    "backup_exe",
    "locate_rcedit",
    "replace_icon",
    "MAX_OVERLAY_BYTES",
]

#: rcedit 在 Windows 上通常的候选名
_TOOL_NAMES = ("rcedit.exe", "rcedit-x64.exe", "rcedit-x86.exe")

_CREATE_NO_WINDOW = 0x08000000
_DEFAULT_TIMEOUT = 90

#: 允许的 PE 尾部附加数据上限：超过就认为会破坏程序，拒绝替换
MAX_OVERLAY_BYTES = 1024 * 1024


class ResourceToolError(RuntimeError):
    """资源替换失败（工具报错、文件损坏等）。"""


class ResourceToolMissing(ResourceToolError):
    """找不到 rcedit.exe。"""


class ExeLockedError(ResourceToolError):
    """目标 exe 正被占用（正在运行），无法写入。"""


class UnsafeOverlayError(ResourceToolError):
    """目标 exe 在 PE 结构之外带大量附加数据，替换图标会破坏程序。"""


def _bundle_dir() -> Path:
    """程序根目录（打包后是 _MEIPASS）。"""
    return paths.bundle_root()


def locate_rcedit(explicit: "str | os.PathLike[str] | None" = None) -> Path:
    """定位 rcedit.exe（``bin/`` 优先，见 :mod:`iconswap.paths`）。

    :raises ResourceToolMissing: 所有候选位置都不存在。
    """
    candidates: list[Path] = []

    if explicit:
        candidates.append(Path(os.fspath(explicit)))

    env_value = os.environ.get("ICONSWAP_RCEDIT")
    if env_value:
        candidates.append(Path(env_value))

    candidates.extend(paths.rcedit_candidates())

    for name in _TOOL_NAMES:
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))

    for candidate in candidates:
        if candidate.is_file():
            return candidate

    raise ResourceToolMissing(
        "找不到 rcedit.exe。请把 rcedit.exe 放到项目的 bin\\ 目录"
        f"（当前查找位置：{paths.bin_dir()}），"
        "或设置环境变量 ICONSWAP_RCEDIT 指向它。"
    )


def backup_path_for(exe: "str | os.PathLike[str]") -> Path:
    """备份文件路径：``原名.exe`` → ``原名_original.exe``。"""
    path = Path(os.fspath(exe))
    return path.with_name(f"{path.stem}_original{path.suffix}")


def backup_exe(exe: "str | os.PathLike[str]", *, overwrite: bool = False) -> Path:
    """备份 exe；已存在旧备份时默认保留旧备份（它才是真正的原件）。

    :returns: 备份文件路径。
    """
    source = Path(os.fspath(exe))
    target = backup_path_for(source)
    if target.exists() and not overwrite:
        return target
    shutil.copy2(source, target)
    return target


def _ensure_not_locked(exe: Path) -> None:
    """用独占写打开的方式探测文件是否被占用。

    Windows 上运行中的 exe 允许读取、拒绝写入，因此尝试 ``rb+`` 打开即可判断。
    """
    try:
        with open(exe, "rb+"):
            pass
    except PermissionError as exc:
        raise ExeLockedError(f"{exe.name} 正在运行，已跳过") from exc
    except OSError as exc:
        raise ResourceToolError(f"无法写入 {exe.name}：{exc}") from exc


def replace_icon(
    exe: "str | os.PathLike[str]",
    ico: "str | os.PathLike[str]",
    *,
    rcedit: "str | os.PathLike[str] | None" = None,
    make_backup: bool = True,
    timeout: int = _DEFAULT_TIMEOUT,
    allow_overlay: bool = False,
) -> Path:
    """用 rcedit 把 ``ico`` 写入 ``exe`` 的图标资源。

    流程：锁定检测 → 尾部数据检查 → 备份 → 调用 rcedit → 校验资源 → 失败回滚。

    :param allow_overlay: 默认 ``False``：目标 exe 若带超过 1 MB 的 PE 尾部数据
        （PyInstaller 单文件包、自解压包等），rcedit 会把这些数据整体丢弃、
        直接破坏程序，因此拒绝处理并抛 :class:`UnsafeOverlayError`。
        确认目标格式安全时再传 ``True``。
    :returns: 备份文件路径（未备份时返回原始 exe 路径）。
    :raises ExeLockedError: 文件被占用。
    :raises UnsafeOverlayError: 存在大量尾部数据，拒绝处理。
    :raises ResourceToolMissing: 找不到 rcedit.exe。
    :raises ResourceToolError: 替换失败（已尝试回滚）。
    """
    exe_path = Path(os.fspath(exe))
    ico_path = Path(os.fspath(ico))

    if not exe_path.is_file():
        raise ResourceToolError(f"目标文件不存在：{exe_path}")
    if not ico_path.is_file():
        raise ResourceToolError(f"图标文件不存在：{ico_path}")
    if ico_path.suffix.lower() != ".ico":
        raise ResourceToolError(f"图标必须是 .ico 文件：{ico_path}")

    tool = locate_rcedit(rcedit)
    _ensure_not_locked(exe_path)

    overlay = overlay_size(exe_path)
    if overlay > MAX_OVERLAY_BYTES and not allow_overlay:
        raise UnsafeOverlayError(
            f"{exe_path.name} 在 PE 结构外附带 {overlay / 1024 / 1024:.1f} MB 数据"
            "（常见于 PyInstaller / 自解压单文件包），改图标会把这段数据丢掉并破坏程序，已跳过"
        )

    # 记录替换前的资源签名，用于识别"rcedit 假成功"
    signature_before = None
    try:
        signature_before = resource_signature(exe_path)
    except PeInspectionError:  # pefile 不可用时跳过该校验
        signature_before = None

    backup = exe_path
    if make_backup:
        backup = backup_exe(exe_path)

    command = [str(tool), str(exe_path), "--set-icon", str(ico_path)]
    # 展示用命令行：含空格/中文的路径在这里给双引号，方便用户复制排错。
    # 实际调用用的是参数列表，不经过 shell，所以空格与中文都安全。
    LOGGER.info("rcedit 调用：%s", shlex.join(command))
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            timeout=timeout,
            creationflags=_CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except subprocess.TimeoutExpired as exc:
        _rollback(exe_path, backup)
        raise ResourceToolError(f"rcedit 超时（{timeout}s）：{exe_path.name}") from exc
    except OSError as exc:
        _rollback(exe_path, backup)
        raise ResourceToolError(f"无法启动 rcedit：{exc}") from exc

    detail = _decode_output(completed.stdout) + _decode_output(completed.stderr)
    detail = detail.strip()
    if completed.returncode != 0:
        _rollback(exe_path, backup)
        raise ResourceToolError(
            f"rcedit 失败（退出码 {completed.returncode}）：{exe_path.name}"
            + (f" | {detail[:300]}" if detail else " | 无输出")
        )
    if "fatal" in detail.lower():
        _rollback(exe_path, backup)
        raise ResourceToolError(f"rcedit 报致命错误：{exe_path.name} | {detail[:300]}")

    # 校验一：替换后应当能读到图标资源
    try:
        if not has_icon_resource(exe_path):
            _rollback(exe_path, backup)
            raise ResourceToolError(f"替换后仍未检测到图标资源：{exe_path.name}")
    except PeInspectionError:  # pragma: no cover - pefile 缺失时跳过校验
        pass

    # 校验二：资源内容必须真的变了（识别"退出码 0 但其实没改"）
    if signature_before is not None:
        try:
            signature_after = resource_signature(exe_path)
        except PeInspectionError:  # pragma: no cover
            signature_after = None
        if signature_after is not None and signature_after == signature_before:
            _rollback(exe_path, backup)
            raise ResourceToolError(
                f"替换未生效：{exe_path.name} 的图标资源没有变化"
                "（rcedit 可能被安全软件拦截或文件被占用）"
            )

    LOGGER.info("rcedit 校验通过：%s（资源签名变化）", exe_path.name)
    return backup


def _decode_output(data: "bytes | None") -> str:
    """rcedit 输出可能被中文系统写成 GBK/ANSI，这里逐个编码尝试。"""
    if not data:
        return ""
    for encoding in ("utf-8", "gbk", "mbcs"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", "replace")


def _rollback(exe: Path, backup: Path) -> None:
    """用备份覆盖回原文件（备份就是原件时不做任何事）。"""
    if backup == exe or not backup.exists():
        return
    try:
        shutil.copy2(backup, exe)
    except OSError:  # pragma: no cover - 回滚失败只能记录
        pass


def find_tool_hint() -> str:
    """给 UI/文档用的获取 rcedit 的提示文案。"""
    return (
        "未找到 rcedit.exe。请从 https://github.com/electron/rcedit/releases "
        "下载 rcedit-x64.exe，改名为 rcedit.exe 后放到项目 tools\\ 目录，"
        "或设置环境变量 ICONSWAP_RCEDIT 指向该文件。"
    )
