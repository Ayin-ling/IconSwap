"""快捷方式（.lnk）创建与同步，基于 pywin32 的 ``WScript.Shell``。

两类用途
--------
1. **给受签名保护的 exe 建替身**：不动原文件，只在旁边（或桌面）放一个
   ``原名 - 快捷方式.lnk``，把它的 ``IconLocation`` 指到新 ico。
2. **同步旧快捷方式**：策略 A 改掉了 exe 内嵌图标后，桌面 / 公共桌面 / 开始菜单
   / 任务栏固定项里那些指向该 exe 的旧 ``.lnk`` 需要一起更新，否则用户看到的
   还是旧图标。

pywin32 是可选依赖：模块导入时不加载，真正用到才 import，缺库时抛
:class:`ShortcutError` 而不是 ImportError，方便上层降级。
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Optional

from iconswap.windows_privileges import describe_com_error, is_admin, permission_hint

LOGGER = logging.getLogger("iconswap.shortcuts")

__all__ = [
    "ShortcutError",
    "ShortcutInfo",
    "ShortcutTools",
    "default_shortcut_roots",
    "iter_shortcut_files",
    "shortcut_name_for",
    "icon_signature",
    "icon_location_for",
    "ensure_com",
]

_SHELL = None
_COM_STATE = threading.local()


class ShortcutError(RuntimeError):
    """快捷方式操作失败（缺 pywin32、COM 报错、目标不合法等）。"""


def ensure_com() -> None:
    """确保**当前线程**已初始化 COM（每个线程只初始化一次）。

    ``WScript.Shell`` 是 COM 对象：Flask 的请求线程与批量任务的后台线程都是
    全新线程，没有这一步会得到 ``CO_E_NOTINITIALIZED``（0x800401F0），
    表现为“改快捷方式失败”但其实什么都没写。
    """
    if getattr(_COM_STATE, "ready", False):
        return
    try:
        import pythoncom  # noqa: PLC0415 - 惰性导入
    except ImportError:  # pragma: no cover - 缺 pywin32
        return
    try:
        pythoncom.CoInitialize()
    except Exception:  # pragma: no cover - 已初始化等情况
        return
    _COM_STATE.ready = True


def _shell():
    """惰性获取 WScript.Shell COM 对象。"""
    global _SHELL
    if _SHELL is not None:
        return _SHELL
    try:
        import win32com.client  # noqa: PLC0415 - 惰性导入
    except ImportError as exc:  # pragma: no cover - 缺依赖分支
        raise ShortcutError(
            "缺少 pywin32，无法处理快捷方式。请执行：pip install pywin32"
        ) from exc
    ensure_com()
    try:
        _SHELL = win32com.client.Dispatch("WScript.Shell")
    except Exception as exc:  # noqa: BLE001 - COM 注册表异常等
        raise ShortcutError(f"无法创建 WScript.Shell 对象：{exc}") from exc
    return _SHELL


def shortcut_name_for(exe: "str | os.PathLike[str]", suffix: str = " - 快捷方式") -> str:
    """``Demo.exe`` → ``Demo - 快捷方式.lnk``。"""
    return f"{Path(os.fspath(exe)).stem}{suffix}.lnk"


def default_shortcut_roots() -> list[Path]:
    """需要扫描的快捷方式根目录（当前用户桌面、公共桌面、开始菜单、任务栏固定项）。"""
    roots: list[Path] = []

    user_profile = os.environ.get("USERPROFILE")
    if user_profile:
        roots.append(Path(user_profile) / "Desktop")
        roots.append(
            Path(user_profile)
            / "AppData"
            / "Roaming"
            / "Microsoft"
            / "Windows"
            / "Start Menu"
        )
        roots.append(
            Path(user_profile)
            / "AppData"
            / "Roaming"
            / "Microsoft"
            / "Internet Explorer"
            / "Quick Launch"
            / "User Pinned"
            / "TaskBar"
        )

    public = os.environ.get("PUBLIC", r"C:\Users\Public")
    roots.append(Path(public) / "Desktop")

    program_data = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
    roots.append(Path(program_data) / "Microsoft" / "Windows" / "Start Menu")

    # 去重 + 只保留真实存在的目录
    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = _norm(root)
        if key in seen:
            continue
        seen.add(key)
        if root.is_dir():
            unique.append(root)
    return unique


@dataclass(frozen=True)
class ShortcutInfo:
    """一个已存在快捷方式的关键属性。"""

    path: Path
    target: str
    icon: str


def iter_shortcut_files(
    root: "str | os.PathLike[str]",
    *,
    recursive: bool = True,
) -> Iterator[Path]:
    """遍历目录下的 ``.lnk`` 文件（按路径排序，结果稳定）。

    :raises NotADirectoryError: ``root`` 不是目录。
    """
    base = Path(os.fspath(root))
    if not base.is_dir():
        raise NotADirectoryError(f"不是有效目录：{base}")

    found: list[Path] = []
    for path in base.rglob("*.lnk") if recursive else base.glob("*.lnk"):
        try:
            if path.is_file():
                found.append(path)
        except OSError:  # pragma: no cover - 权限异常目录
            continue

    found.sort(key=lambda item: str(item).lower())
    yield from found


class ShortcutTools:
    """快捷方式读写工具。

    所有方法都是显式操作：创建/更新后立刻 ``Save()``，失败抛 :class:`ShortcutError`。
    """

    def __init__(self, shell=None) -> None:
        self._shell_obj = shell
        #: 最近一次操作产生的“非致命提示”（如受保护目录建议管理员运行），
        #: 供上层把信息带进 UI 日志。
        self.last_hint: str = ""

    # -- 内部 -------------------------------------------------------------
    def _get_shell(self):
        return self._shell_obj if self._shell_obj is not None else _shell()

    def _load(self, lnk_path: Path):
        try:
            return self._get_shell().CreateShortCut(str(lnk_path))
        except Exception as exc:  # noqa: BLE001
            raise ShortcutError(f"读取快捷方式失败：{lnk_path}（{exc}）") from exc

    # -- 读 ---------------------------------------------------------------
    def read(self, lnk_path: "str | os.PathLike[str]") -> ShortcutInfo:
        """读取快捷方式的目标与图标位置。"""
        path = Path(os.fspath(lnk_path))
        shortcut = self._load(path)
        try:
            return ShortcutInfo(
                path=path,
                target=str(shortcut.TargetPath or ""),
                icon=str(shortcut.IconLocation or ""),
            )
        except Exception as exc:  # noqa: BLE001
            raise ShortcutError(f"解析快捷方式失败：{path}（{exc}）") from exc

    # -- 写 ---------------------------------------------------------------
    def create(
        self,
        target_exe: "str | os.PathLike[str]",
        lnk_path: "str | os.PathLike[str]",
        *,
        icon: "str | os.PathLike[str] | None" = None,
        arguments: Optional[str] = None,
        description: Optional[str] = None,
        working_dir: "str | os.PathLike[str] | None" = None,
    ) -> Path:
        """创建（或覆盖）一个指向 ``target_exe`` 的快捷方式。

        路径一律转成绝对路径再写入：``WScript.Shell`` 会把相对路径按当前工作
        目录解析，写进去就会变成错误的目标（实测会落到盘符根目录下）。
        """
        exe = Path(os.fspath(target_exe)).resolve()
        if not exe.is_file():
            raise ShortcutError(f"目标程序不存在：{exe}")

        lnk = Path(os.fspath(lnk_path)).resolve()
        if lnk.suffix.lower() != ".lnk":
            lnk = lnk.with_suffix(".lnk")
        lnk.parent.mkdir(parents=True, exist_ok=True)

        self.last_hint = permission_hint(lnk) or ""

        shortcut = self._load(lnk)
        try:
            shortcut.TargetPath = str(exe)
            shortcut.WorkingDirectory = str(
                Path(working_dir).resolve() if working_dir else exe.parent
            )
            if arguments:
                shortcut.Arguments = arguments
            shortcut.Description = description or f"{exe.stem} 的快捷方式"
            icon_path = _icon_location(icon, exe)
            if icon_path:
                shortcut.IconLocation = icon_path
            shortcut.Save()
        except Exception as exc:  # noqa: BLE001 - pywintypes.com_error 等
            raise ShortcutError(
                f"创建快捷方式失败：{lnk.name}（{describe_com_error(exc)}）"
            ) from exc

        # 写后校验：重新加载快捷方式，目标与图标都必须真的落地
        info = self.read(lnk)
        if _norm(info.target) != _norm(exe):
            raise ShortcutError(
                f"快捷方式目标不正确：{lnk.name} 指向 {info.target or '空'}（期望 {exe}）"
            )
        if icon_path and icon_parts(info.icon) != icon_parts(icon_path):
            raise ShortcutError(
                "快捷方式图标未生效："
                f"{lnk.name} 的 IconLocation 读回为 {info.icon or '空'}（期望 {icon_path}）"
            )
        LOGGER.info("快捷方式已校验：%s -> %s（图标 %s）", lnk.name, info.target, info.icon)
        return lnk

    def set_icon(
        self,
        lnk_path: "str | os.PathLike[str]",
        icon: "str | os.PathLike[str]",
        *,
        index: int = 0,
    ) -> Path:
        """把已存在快捷方式的 ``IconLocation`` 强制改成 ``icon,index``。

        **不看 TargetPath**：目标为空（商店 / UWP 应用）、目标已被删除、目标根本
        不是程序，都照改不误——这正是之前 “快捷方式没有目标程序 → 跳过” 的 bug。

        三层防御，专治"COM 不报错但也没写进去"的静默失败：

        1. **写前检查**：文件是否存在、是否可写（只读位会被清掉）；
           受保护目录 + 非管理员时把提示记到 :attr:`last_hint`。
        2. **写入 + 写后校验**：``Save()`` 之后**必须**重新读取 ``IconLocation``
           并比对；不一致先换 ``IShellLinkW`` 再写一次，仍不一致就抛
           :class:`ShortcutError`，绝不假装成功。
        3. **显式捕获** ``pywintypes.com_error``，把 HRESULT 翻译成人话。
        """
        lnk = Path(os.fspath(lnk_path)).resolve()
        if not lnk.is_file():
            raise ShortcutError(f"快捷方式不存在：{lnk}")

        resolved = Path(os.fspath(icon)).resolve()
        if not resolved.is_file():
            raise ShortcutError(f"图标文件不存在：{resolved}")

        # 1) 写前检查（绝对路径 + 显式索引，格式固定为 "D:\..\x.ico,0"）
        self._ensure_writable(lnk)
        expected = icon_location_for(resolved, index)

        errors: list[str] = []

        # 2) 主方案：WScript.Shell
        try:
            shortcut = self._load(lnk)
            shortcut.IconLocation = expected
            shortcut.Save()
        except Exception as exc:  # noqa: BLE001 - pywintypes.com_error 等
            errors.append(describe_com_error(exc))
        else:
            actual = str(self.read(lnk).icon or "")
            if icon_parts(actual) == icon_parts(expected):
                return lnk
            errors.append(f"WScript.Shell 读回 {actual or '空'}")

        # 3) 备用方案：IShellLinkW + IPersistFile（同样只改图标，不新建文件）
        try:
            _write_icon_via_ishelllink(lnk, resolved, int(index))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"IShellLinkW 写入失败：{describe_com_error(exc)}")
        else:
            actual = str(self.read(lnk).icon or "")
            if icon_parts(actual) == icon_parts(expected):
                return lnk
            errors.append(f"IShellLinkW 读回 {actual or '空'}")

        raise ShortcutError(
            "修改快捷方式失败，可能被占用或权限不足："
            f"{lnk.name} 的 IconLocation 未能写成 {expected}（{'；'.join(errors)}）"
        )

    def _ensure_writable(self, lnk: Path) -> None:
        """确认 .lnk 可写；只读属性会尝试清除，仍不可写则给出明确原因。

        受保护目录 + 非管理员时不算硬失败（有的环境仍可写），但会把
        "建议以管理员身份运行"记到 :attr:`last_hint`，由上层显示到 UI 日志。
        """
        self.last_hint = ""

        try:
            if not os.access(lnk, os.W_OK):
                try:
                    import stat

                    os.chmod(lnk, stat.S_IWRITE | stat.S_IREAD)
                except OSError:
                    pass
        except OSError:  # pragma: no cover
            pass

        hint = permission_hint(lnk)
        if hint:
            self.last_hint = hint
            LOGGER.warning("%s：%s", lnk.name, hint)

        if not os.access(lnk, os.W_OK):
            raise ShortcutError(
                "修改快捷方式失败，可能被占用或权限不足："
                f"{lnk}"
                + (f"（{hint}）" if hint else "（可能被其它程序独占）")
            )

    # -- 扫描 -------------------------------------------------------------
    def iter_shortcuts(self, roots: Optional[Iterable[Path]] = None) -> Iterator[Path]:
        """遍历根目录下的所有 ``.lnk``（递归）。"""
        for root in roots if roots is not None else default_shortcut_roots():
            try:
                base = Path(root)
                if not base.is_dir():
                    continue
                for path in base.rglob("*.lnk"):
                    if path.is_file():
                        yield path
            except OSError:  # pragma: no cover - 不可读目录
                continue

    def find_targeting(
        self,
        exe: "str | os.PathLike[str]",
        roots: Optional[Iterable[Path]] = None,
    ) -> list[ShortcutInfo]:
        """找出所有指向 ``exe`` 的快捷方式。"""
        wanted = _norm(exe)
        matches: list[ShortcutInfo] = []
        for lnk in self.iter_shortcuts(roots):
            try:
                info = self.read(lnk)
            except ShortcutError:
                continue
            if not info.target:
                continue
            if _norm(info.target) == wanted:
                matches.append(info)
        return matches

    def sync_targeting(
        self,
        exe: "str | os.PathLike[str]",
        icon: "str | os.PathLike[str] | None" = None,
        roots: Optional[Iterable[Path]] = None,
    ) -> list[Path]:
        """把指向 ``exe`` 的旧快捷方式的图标统一刷新。

        :param icon: 新图标路径；``None`` 表示指向 exe 自身（策略 A 改完内嵌图标后
            正是这个用法）。
        :returns: 实际被更新的快捷方式列表。
        """
        exe_path = Path(os.fspath(exe)).resolve()
        icon_path = Path(os.fspath(icon)).resolve() if icon else exe_path
        updated: list[Path] = []
        for info in self.find_targeting(exe_path, roots):
            try:
                self.set_icon(info.path, icon_path)
            except ShortcutError:
                continue
            updated.append(info.path)
        return updated


def icon_location_for(icon: "str | os.PathLike[str]", index: int = 0) -> str:
    r"""把图标路径规范成 Windows 资源管理器能正确渲染的 ``IconLocation``。

    规则（实测得出，勿改）：

    * 必须是**绝对路径**；
    * 必须带 ``,<索引>`` 后缀；
    * **绝对不要加双引号**。``"C:\a b\x.ico",0`` 这种写法虽然能被读回，
      但资源管理器会解析失败、直接显示成空白图标（实测 Windows 11：
      带引号的 exe/ico 快捷方式全部白板，去掉引号立刻正常）。
      空格、中文、逗号都不需要引号——外壳按“最后一个逗号 + 数字”切分。
    """
    path = os.path.abspath(os.fspath(icon))
    return f"{path},{int(index)}"


def _icon_location(icon: "str | os.PathLike[str] | None", exe: Path) -> str:
    """把 icon 参数规范成 ``绝对路径,0`` 形式；``None`` 时退回 exe 自身。"""
    if icon is None:
        return icon_location_for(exe)
    text = str(icon)
    if "," in text:  # 已经是完整的 "路径,索引"
        return text
    return icon_location_for(icon)


def _write_icon_via_ishelllink(lnk: Path, ico: Path, index: int = 0) -> None:
    r"""备用写入路径：``IShellLinkW`` + ``IPersistFile``。

    ``WScript.Shell`` 偶尔会遇到“Save 不报错但 IconLocation 没落地”的情况
    （老版本 wshom、被别的进程同时持有等）。这里直接走底层 COM 接口，
    改写的是同一个 ``.lnk`` 文件，不会新建任何快捷方式。
    """
    import pythoncom  # noqa: PLC0415 - 惰性导入，缺 pywin32 时由调用方处理
    from win32com.shell import shell as winshell  # noqa: PLC0415

    ensure_com()
    link = pythoncom.CoCreateInstance(
        winshell.CLSID_ShellLink,
        None,
        pythoncom.CLSCTX_INPROC_SERVER,
        winshell.IID_IShellLinkW,
    )
    persist = link.QueryInterface(pythoncom.IID_IPersistFile)
    persist.Load(str(lnk), 0)  # STGM_READ
    link.SetIconLocation(str(ico), int(index))
    persist.Save(str(lnk), True)


def icon_parts(text: "str | os.PathLike[str] | None") -> "tuple[str, int]":
    r"""把 ``路径,索引`` 拆成 (归一化路径, 索引)，用于严格比对。

    兼容两种写法：``C:\a.ico,0`` 与 ``"C:\a b\a.ico",0``（引号在索引之前）。
    """
    raw = str(text or "").strip()
    if not raw:
        return "", 0
    head, sep, tail = raw.rpartition(",")
    if sep and tail.strip().lstrip("-").isdigit():
        path, index = head, int(tail.strip())
    else:
        path, index = raw, 0
    path = path.strip().strip('"').strip()
    return _norm(path), index

def icon_signature(text: "str | os.PathLike[str] | None") -> str:
    r"""把 ``路径,序号`` 归一化成可比较的签名（只保留路径部分）。

    处理 ``"C:\x\a.ico,0"``、``"C:\x\a.ico"``、带引号、正反斜杠混用等情况。
    """
    raw = str(text or "").strip()
    if not raw:
        return ""
    head, sep, tail = raw.rpartition(",")
    if sep and tail.strip().lstrip("-").isdigit():
        raw = head
    # 去掉可能包裹路径的引号（有的程序写 "C:\x\a.ico",0 这种格式）
    raw = raw.strip().strip('"').strip()
    return _norm(raw)


def _norm(path: "str | os.PathLike[str] | None") -> str:
    r"""展开环境变量 + 绝对化 + 大小写不敏感 的归一化。"""
    text = os.path.expandvars(str(path or ""))
    if not text:
        return ""
    try:
        text = os.path.abspath(text)
    except (OSError, ValueError):  # pragma: no cover
        pass
    return os.path.normcase(os.path.normpath(text))
