"""首次使用风险确认：一次性弹窗 + 本地配置文件记忆。

行为
----
1. 配置文件：``pathlib.Path.home() / ".iconswap_config.json"``
2. 启动时读取，若 ``risk_accepted`` 不为 ``true``，弹出系统确认框：
   “本工具会修改 exe 文件图标，存在被安全软件拦截或文件损坏的风险。
     请确保已备份重要数据。是否继续使用？”
3. 选择“是/确定” → 写入 ``{"risk_accepted": true, "version": 1}`` 并继续；
   选择“否/取消” → 调用方退出程序。
4. 之后启动检测到 ``risk_accepted: true`` 就直接进主程序。

说明
----
* GUI 框架是 pywebview，它本身没有 messagebox，因此这里用 Windows 原生
  ``user32.MessageBoxW``（就是系统对话框，和 pywebview 窗口同一套 UI 体系）。
  这样在 ``--windowed`` 打包、没有控制台的环境下也能正常显示。
* 弹窗必须发生在 ``webview.start()`` 之前，且在主线程上调用：
  ``MessageBoxW`` 是模态阻塞调用，返回后主线程才继续创建窗口，因此不会与
  pywebview 的事件循环冲突。
* 自动化/测试场景可用环境变量 ``ICONSWAP_SKIP_RISK_PROMPT=1`` 跳过弹窗
  （仍会正常读写配置）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable, Optional

__all__ = [
    "CONFIG_NAME",
    "CONFIG_VERSION",
    "RISK_MESSAGE",
    "RISK_TITLE",
    "SKIP_ENV",
    "accept_risk",
    "config_path",
    "ensure_risk_consent",
    "is_risk_accepted",
    "load_config",
    "native_confirm",
    "reset_risk",
]

CONFIG_NAME = ".iconswap_config.json"
CONFIG_VERSION = 1

RISK_TITLE = "IconSwap 使用确认"
RISK_MESSAGE = (
    "本工具会修改 exe 文件图标，存在被安全软件拦截或文件损坏的风险。"
    "请确保已备份重要数据。是否继续使用？"
)

#: 跳过弹窗（仅用于自动化测试与批处理）
SKIP_ENV = "ICONSWAP_SKIP_RISK_PROMPT"

#: MessageBoxW 常量
_MB_YESNO = 0x00000004
_MB_ICONWARNING = 0x00000030
_MB_DEFBUTTON2 = 0x00000100  # 默认选中“否”，避免误触
_MB_TOPMOST = 0x00040000
_MB_SETFOREGROUND = 0x00010000
_IDYES = 6


def config_path() -> Path:
    """配置文件路径：用户主目录下的 ``.iconswap_config.json``。"""
    return Path.home() / CONFIG_NAME


def load_config(path: "str | os.PathLike[str] | None" = None) -> dict:
    """读取配置；文件缺失/损坏/非字典时返回空字典（绝不抛异常）。"""
    target = Path(os.fspath(path)) if path is not None else config_path()
    try:
        with open(target, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def is_risk_accepted(path: "str | os.PathLike[str] | None" = None) -> bool:
    """是否已确认过风险（``risk_accepted`` 严格等于 ``True``）。"""
    return load_config(path).get("risk_accepted") is True


def accept_risk(path: "str | os.PathLike[str] | None" = None) -> Path:
    """写入 ``{"risk_accepted": true, "version": 1}`` 并返回配置文件路径。

    采用“临时文件 + 替换”，避免写一半断电留下坏文件。
    """
    target = Path(os.fspath(path)) if path is not None else config_path()
    payload = {"risk_accepted": True, "version": CONFIG_VERSION}

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".tmp")
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except OSError:
        # 写不进去也不能挡着用户用：下次启动会再问一次
        pass
    return target


def reset_risk(path: "str | os.PathLike[str] | None" = None) -> bool:
    """删除配置文件（下次启动重新弹窗）；返回是否真的删掉了。"""
    target = Path(os.fspath(path)) if path is not None else config_path()
    try:
        target.unlink()
        return True
    except OSError:
        return False


def native_confirm(message: str = RISK_MESSAGE, title: str = RISK_TITLE) -> bool:
    """弹出 Windows 确认框；用户点“是”返回 ``True``。

    非 Windows 平台或调用失败时返回 ``False``（安全优先：不确认就不继续）。
    """
    if os.name != "nt":  # pragma: no cover - 非 Windows
        return False

    try:
        import ctypes

        flags = (
            _MB_YESNO
            | _MB_ICONWARNING
            | _MB_DEFBUTTON2
            | _MB_TOPMOST
            | _MB_SETFOREGROUND
        )
        result = ctypes.windll.user32.MessageBoxW(None, message, title, flags)
        return result == _IDYES
    except Exception:  # noqa: BLE001 - 弹窗失败按“未确认”处理
        return False


def ensure_risk_consent(
    *,
    path: "str | os.PathLike[str] | None" = None,
    asker: Optional[Callable[[], bool]] = None,
    message: str = RISK_MESSAGE,
    title: str = RISK_TITLE,
) -> bool:
    """确保用户已确认风险。

    :param path: 配置文件路径（默认主目录），测试可注入。
    :param asker: 询问函数，默认弹系统对话框；测试可注入假实现。
    :returns: ``True`` 表示可以继续启动；``False`` 表示用户拒绝，调用方应退出。
    """
    if os.environ.get(SKIP_ENV) == "1":
        return True

    if is_risk_accepted(path):
        return True

    ask = asker if asker is not None else (lambda: native_confirm(message, title))
    if not ask():
        return False

    accept_risk(path)
    return True
