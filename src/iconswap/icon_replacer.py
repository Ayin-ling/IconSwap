"""批量替换程序图标的核心编排：扫描 → 分流 → 替换/降级 → 刷新缓存。

两套独立功能
------------
============  ==========================================================
功能           行为
============  ==========================================================
``exe``       只处理 ``.exe``：先 rcedit 内嵌图标（备份为
              ``原名_original.exe``）；**任何失败**都自动降级为
              “在桌面建 ``原名 - 快捷方式.lnk`` 并替换其图标”，原 exe 不动
``lnk``       只处理 ``.lnk``：不看、不校验 TargetPath，直接覆盖
              IconLocation；**绝不新建任何快捷方式**
============  ==========================================================

分流细则（``exe`` 模式）
------------------------
====================  =====================================================
情况                    动作
====================  =====================================================
无数字签名              备份 ``原名_original.exe`` → rcedit 改内嵌图标
                        → 同步桌面/开始菜单里指向它的旧快捷方式
有数字签名              策略 ``shortcut``（默认）：**绝不改 exe**，
                        在桌面建 ``原名 - 快捷方式.lnk`` 并替换其图标；
                        策略 ``skip``：跳过该文件
文件被占用 / rcedit 报错 /  PE 外挂数据      自动降级为桌面快捷方式
缺 rcedit.exe           整批中止（这是安装问题，不是单个文件问题）
====================  =====================================================

所有外部能力（签名检测 / 资源编辑 / 快捷方式 / 缓存刷新）都通过构造参数注入，
因此单测可以完全脱离 Windows 与第三方工具验证分流逻辑。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

from iconswap.pe_inspector import SignatureInfo, has_digital_signature, iter_executables
from iconswap.resource_tools import (
    ExeLockedError,
    ResourceToolError,
    ResourceToolMissing,
    UnsafeOverlayError,
    replace_icon,
)
from iconswap.shell_paths import desktop_dir
from iconswap.shortcut_tools import (
    ShortcutError,
    ShortcutTools,
    iter_shortcut_files,
    shortcut_name_for,
)
from iconswap.shell_cache import RefreshResult, refresh_icon_cache

__all__ = [
    "POLICY_SHORTCUT",
    "POLICY_SKIP",
    "KIND_EXE",
    "KIND_LNK",
    "KIND_OTHER",
    "MODE_ALL",
    "MODE_EXE",
    "MODE_LNK",
    "MODES",
    "PlannedFile",
    "FileResult",
    "BatchSummary",
    "BatchIconReplacer",
]

#: 签名文件的处理方式：创建带新图标的快捷方式
POLICY_SHORTCUT = "shortcut"
#: 签名文件的处理方式：直接跳过
POLICY_SKIP = "skip"

POLICIES = (POLICY_SHORTCUT, POLICY_SKIP)

#: 处理对象类型
KIND_EXE = "exe"
KIND_LNK = "lnk"
#: 既不是 exe 也不是 lnk（或文件不存在）：只记一行跳过，绝不中断整批
KIND_OTHER = "other"

#: 扫描模式
MODE_ALL = "all"   # exe + lnk
MODE_EXE = "exe"   # 只找可执行文件（功能一）
MODE_LNK = "lnk"   # 只找快捷方式（功能二）
MODES = (MODE_ALL, MODE_EXE, MODE_LNK)

# 结果动作
ACTION_REPLACED = "replaced"
ACTION_SHORTCUT = "shortcut"
ACTION_SHORTCUT_UPDATED = "shortcut_updated"
ACTION_SKIPPED_SIGNED = "skipped_signed"
ACTION_SKIPPED_LOCKED = "skipped_locked"
ACTION_SKIPPED_UNSAFE = "skipped_unsafe"
ACTION_SKIPPED = "skipped"
ACTION_FAILED = "failed"

_ACTION_LABELS = {
    ACTION_REPLACED: "已替换",
    ACTION_SHORTCUT: "已建快捷方式",
    ACTION_SHORTCUT_UPDATED: "快捷方式图标",
    ACTION_SKIPPED_SIGNED: "签名跳过",
    ACTION_SKIPPED_LOCKED: "占用跳过",
    ACTION_SKIPPED_UNSAFE: "结构跳过",
    ACTION_SKIPPED: "跳过",
    ACTION_FAILED: "失败",
}

#: 统一日志前缀（UI 日志区只有这几种行）
_ACTION_PREFIX = {
    ACTION_REPLACED: "[成功]",
    ACTION_SHORTCUT: "[降级]",
    ACTION_SHORTCUT_UPDATED: "[成功]",
    ACTION_SKIPPED_SIGNED: "[跳过]",
    ACTION_SKIPPED_LOCKED: "[跳过]",
    ACTION_SKIPPED_UNSAFE: "[跳过]",
    ACTION_SKIPPED: "[跳过]",
    ACTION_FAILED: "[失败]",
}

ProgressCallback = Callable[[int, int, "FileResult"], None]


@dataclass(frozen=True)
class PlannedFile:
    """扫描阶段得到的一个待处理文件（``.exe`` 或 ``.lnk``）。"""

    path: Path
    signed: bool
    signature: Optional[SignatureInfo] = None
    #: ``exe`` 或 ``lnk``
    kind: str = KIND_EXE
    #: 仅对 ``.lnk`` 有意义：快捷方式指向的目标程序
    target: Optional[Path] = None
    #: 扫描期发现的问题（例如快捷方式损坏），执行时据此跳过
    note: str = ""

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def is_shortcut(self) -> bool:
        return self.kind == KIND_LNK


@dataclass
class FileResult:
    """单个文件的处理结果。"""

    path: Path
    action: str
    message: str
    backup: Optional[Path] = None
    shortcut: Optional[Path] = None
    links_updated: int = 0
    #: 是否因为"无法安全改写 exe"而自动降级成快捷方式
    degraded: bool = False

    @property
    def label(self) -> str:
        return _ACTION_LABELS.get(self.action, self.action)

    def as_log_line(self) -> str:
        """UI 日志区的一行，例如：

        ``[成功] app.exe — 已替换内嵌图标``
        ``[降级] packed.exe -> 桌面快捷方式 — …``
        ``[失败] a.lnk — 修改快捷方式失败…``
        ``[跳过] readme.txt — 不是 .exe / .lnk，已跳过``
        """
        prefix = _ACTION_PREFIX.get(self.action, "[信息]")
        if self.action == ACTION_SHORTCUT:
            return f"{prefix} {self.path.name} -> 桌面快捷方式 — {self.message}"
        return f"{prefix} {self.path.name} — {self.message}"


@dataclass
class BatchSummary:
    """一批文件的处理汇总。"""

    folder: Path
    ico: Path
    results: list[FileResult] = field(default_factory=list)
    refresh: Optional[RefreshResult] = None
    scanned: int = 0
    aborted: bool = False

    # -- 计数 -------------------------------------------------------------
    def _count(self, action: str) -> int:
        return sum(1 for item in self.results if item.action == action)

    @property
    def replaced(self) -> int:
        return self._count(ACTION_REPLACED)

    @property
    def shortcuts_created(self) -> int:
        return self._count(ACTION_SHORTCUT)

    @property
    def degraded(self) -> int:
        """因结构保护而自动降级为快捷方式的 exe 数量。"""
        return sum(1 for item in self.results if item.degraded)

    @property
    def shortcuts_updated(self) -> int:
        """直接改写了图标位置的 .lnk 数量。"""
        return self._count(ACTION_SHORTCUT_UPDATED)

    @property
    def skipped_signed(self) -> int:
        return self._count(ACTION_SKIPPED_SIGNED)

    @property
    def skipped_locked(self) -> int:
        return self._count(ACTION_SKIPPED_LOCKED)

    @property
    def skipped_unsafe(self) -> int:
        return self._count(ACTION_SKIPPED_UNSAFE)

    @property
    def failed(self) -> int:
        return self._count(ACTION_FAILED)

    @property
    def links_updated(self) -> int:
        return sum(item.links_updated for item in self.results)

    def as_text(self) -> str:
        """人类可读汇总。

        只有真正改了 exe 内部资源才说"成功替换"；只建/改快捷方式时用对应说法，
        避免出现"成功替换 0 个"这种让人以为失败的文案。
        """
        parts: list[str] = []
        if self.replaced:
            parts.append(f"成功替换 {self.replaced} 个")
        if self.shortcuts_created:
            detail = f"已创建并替换快捷方式 {self.shortcuts_created} 个"
            if self.degraded:
                detail += f"（其中 {self.degraded} 个因 exe 改不了而自动降级）"
            parts.append(detail)
        if self.shortcuts_updated:
            parts.append(f"已更新快捷方式图标 {self.shortcuts_updated} 个")
        if self.skipped_signed:
            parts.append(f"因签名跳过 {self.skipped_signed} 个")
        if self.skipped_locked:
            parts.append(f"因占用跳过 {self.skipped_locked} 个")
        if self.skipped_unsafe:
            parts.append(f"因文件结构跳过 {self.skipped_unsafe} 个")
        if self.failed:
            parts.append(f"失败 {self.failed} 个")
        if self.links_updated:
            parts.append(f"同步旧快捷方式 {self.links_updated} 个")
        if self.refresh is not None:
            parts.append(f"缓存：{self.refresh.as_text()}")
        if not parts:
            return "没有需要处理的文件"
        return "，".join(parts)

    def to_dict(self) -> dict:
        return {
            "folder": str(self.folder),
            "ico": str(self.ico),
            "scanned": self.scanned,
            "replaced": self.replaced,
            "shortcuts_created": self.shortcuts_created,
            "degraded": self.degraded,
            "shortcuts_updated": self.shortcuts_updated,
            "skipped_signed": self.skipped_signed,
            "skipped_locked": self.skipped_locked,
            "skipped_unsafe": self.skipped_unsafe,
            "failed": self.failed,
            "links_updated": self.links_updated,
            "aborted": self.aborted,
            "summary": self.as_text(),
            "refresh": (
                {
                    "notified": self.refresh.notified,
                    "ie4uinit": self.refresh.ie4uinit_ok,
                    "paths": self.refresh.paths,
                    "text": self.refresh.as_text(),
                }
                if self.refresh is not None
                else None
            ),
            "results": [
                {
                    "path": str(item.path),
                    "name": item.path.name,
                    "kind": (
                        KIND_LNK
                        if item.path.suffix.lower() == ".lnk"
                        else KIND_EXE
                        if item.path.suffix.lower() == ".exe"
                        else KIND_OTHER
                    ),
                    "action": item.action,
                    "label": item.label,
                    "message": item.message,
                    "backup": str(item.backup) if item.backup else None,
                    "shortcut": str(item.shortcut) if item.shortcut else None,
                    "links_updated": item.links_updated,
                    "degraded": item.degraded,
                }
                for item in self.results
            ],
        }


class BatchIconReplacer:
    """批量替换图标。

    :param inspector: 签名检测函数 ``(path) -> SignatureInfo``。
    :param editor: 图标替换函数 ``(exe, ico) -> 备份路径``。
    :param shortcuts: :class:`ShortcutTools` 实例。
    :param cache: 缓存刷新函数 ``(paths) -> RefreshResult``。
    """

    def __init__(
        self,
        *,
        inspector: Callable[..., SignatureInfo] = has_digital_signature,
        editor: Callable[..., Path] = replace_icon,
        shortcuts: Optional[ShortcutTools] = None,
        cache: Callable[..., RefreshResult] = refresh_icon_cache,
        deep_signature: bool = False,
    ) -> None:
        self._inspector = inspector
        self._editor = editor
        self._shortcuts = shortcuts if shortcuts is not None else ShortcutTools()
        self._cache = cache
        self._deep = deep_signature

    # -- 扫描 -------------------------------------------------------------
    def scan(
        self,
        folder: "str | os.PathLike[str]",
        *,
        recursive: bool = True,
        exclude_self: bool = True,
        include_lnk: bool = True,
        include_exe: bool = True,
        mode: "str | None" = None,
    ) -> list[PlannedFile]:
        """扫描目录，返回按路径排序的待处理文件。

        :param include_lnk: 是否把 ``.lnk`` 快捷方式纳入范围。
        :param include_exe: 是否把 ``.exe`` 纳入范围。
        :param mode: ``"all"``（默认，exe+lnk）、``"exe"``（只找 exe）或
            ``"lnk"``（只找快捷方式），给出时覆盖上面两个开关。
        """
        if mode is not None:
            if mode not in MODES:
                raise ValueError(f"mode 必须是 {MODES} 之一，收到 {mode!r}")
            if mode == MODE_LNK:
                include_exe, include_lnk = False, True
            elif mode == MODE_EXE:
                include_exe, include_lnk = True, False
            else:  # MODE_ALL：尊重调用方传入的 include_lnk
                include_exe = True
        base = Path(os.fspath(folder))
        if not base.is_dir():
            raise NotADirectoryError(f"目标文件夹不存在：{base}")

        excluded = self._self_paths() if exclude_self else set()
        planned: list[PlannedFile] = []
        for path in (iter_executables(base, recursive=recursive) if include_exe else ()):
            # 单个文件定位失败（权限、路径过长等）不允许中断整次扫描
            try:
                if os.path.normcase(str(path.resolve())) in excluded:
                    continue
            except OSError:  # pragma: no cover - 无法解析路径时按普通文件处理
                pass

            info = self._inspect(path)
            planned.append(PlannedFile(path=path, signed=bool(info.signed), signature=info))

        if include_lnk:
            for lnk in iter_shortcut_files(base, recursive=recursive):
                planned.append(self._plan_shortcut(lnk))

        planned.sort(key=lambda item: str(item.path).lower())
        return planned

    def _plan_shortcut(self, lnk: Path) -> PlannedFile:
        """解析一个 .lnk：拿到目标程序，并顺带检测目标是否带签名。

        关键点（曾经的 bug）：**目标为空不代表不能改图标**。商店应用（UWP）、
        目标被删除、或仅仅是没有 TargetPath 的快捷方式，``IconLocation`` 照样
        可以覆盖。因此这里只在“连快捷方式本身都读不出来”时才记 ``note``，
        目标为空时 ``target`` 留 ``None``，执行阶段照改不误。
        """
        try:
            info = self._shortcuts.read(lnk)
        except Exception as exc:  # noqa: BLE001 - 损坏/被占用的快捷方式
            return PlannedFile(
                path=lnk,
                signed=False,
                kind=KIND_LNK,
                note=f"无法解析快捷方式（{type(exc).__name__}: {exc}）",
            )

        raw_target = (info.target or "").strip()
        target = Path(raw_target) if raw_target else None

        signed = False
        signature = None
        if target is not None:
            try:
                if target.is_file():
                    signature = self._inspect(target)
                    signed = bool(signature.signed)
            except OSError:  # pragma: no cover - 目标不可访问
                signed = False

        return PlannedFile(
            path=lnk,
            signed=signed,
            signature=signature,
            kind=KIND_LNK,
            target=target,
        )

    def _inspect(self, path: Path) -> SignatureInfo:
        """签名检测：任何异常都收敛成"无签名 + 错误信息"，绝不冒泡。"""
        try:
            return self._inspector(path, deep=self._deep)
        except TypeError:  # 注入的检测函数可能不接受 deep 关键字
            pass
        except Exception as exc:  # noqa: BLE001 - 损坏/加壳/权限异常
            return SignatureInfo(False, error=f"{type(exc).__name__}: {exc}")

        try:
            return self._inspector(path)
        except Exception as exc:  # noqa: BLE001
            return SignatureInfo(False, error=f"{type(exc).__name__}: {exc}")

    # -- 执行 -------------------------------------------------------------
    def plan_paths(
        self,
        paths: "Iterable[str | os.PathLike[str]]",
        *,
        exclude_self: bool = True,
    ) -> list[PlannedFile]:
        """把一份**显式文件清单**（用户在对话框里多选的结果）变成待处理列表。

        统一入口：用户选了什么就只有什么，不看目录。
        不存在的文件、非 ``.exe``/``.lnk`` 的文件也会保留为 ``KIND_OTHER``，
        这样执行阶段能在日志里明确写一行“跳过”，而不是悄悄消失。
        """
        excluded = self._self_paths() if exclude_self else set()
        planned: list[PlannedFile] = []
        for raw in paths:
            try:
                path = Path(os.fspath(raw))
            except (TypeError, ValueError):  # pragma: no cover - 非法输入
                continue
            try:
                if os.path.normcase(str(path.resolve())) in excluded:
                    continue
            except OSError:  # pragma: no cover
                pass

            if not path.is_file():
                planned.append(
                    PlannedFile(
                        path=path,
                        signed=False,
                        kind=KIND_OTHER,
                        note="文件不存在或无法访问",
                    )
                )
                continue

            suffix = path.suffix.lower()
            if suffix == ".lnk":
                planned.append(self._plan_shortcut(path))
            elif suffix == ".exe":
                info = self._inspect(path)
                planned.append(
                    PlannedFile(path=path, signed=bool(info.signed), signature=info)
                )
            else:
                planned.append(
                    PlannedFile(
                        path=path,
                        signed=False,
                        kind=KIND_OTHER,
                        note="不是 .exe / .lnk，已跳过",
                    )
                )
        planned.sort(key=lambda item: str(item.path).lower())
        return planned

    def run(
        self,
        folder: "str | os.PathLike[str] | None" = None,
        ico: "str | os.PathLike[str] | None" = None,
        *,
        policy: str = POLICY_SHORTCUT,
        recursive: bool = True,
        progress: Optional[ProgressCallback] = None,
        refresh: bool = True,
        shortcut_dir: "str | os.PathLike[str] | None" = None,
        exclude_self: bool = True,
        include_lnk: bool = True,
        include_exe: bool = True,
        mode: "str | None" = None,
        only_paths: "Iterable[str | os.PathLike[str]] | None" = None,
        planned: Optional[Iterable[PlannedFile]] = None,
    ) -> BatchSummary:
        """执行批量替换。

        :param folder: 目标文件夹；给 ``planned``（显式清单）时可以省略。
        :param ico: 新图标 ``.ico`` 路径。
        :param policy: 签名文件的处置方式，``shortcut`` 或 ``skip``。
        :param progress: 每处理完一个文件回调 ``(index, total, result)``。
        :param shortcut_dir: 快捷方式输出目录，默认 ``桌面``。
        :param include_lnk: 扫描时是否包含 ``.lnk``（复用 ``planned`` 时无效）。
        :param include_exe: 扫描时是否包含 ``.exe``。
        :param mode: ``"all"`` / ``"exe"`` / ``"lnk"``，给出时覆盖上面两个开关。
        :param only_paths: 只处理这些路径（UI 勾选结果）；``None`` 表示全部。
        :param planned: 复用已有扫描结果（避免重复扫描/重复检测签名）。
        """
        if policy not in POLICIES:
            raise ValueError(f"policy 必须是 {POLICIES} 之一，收到 {policy!r}")
        if ico is None:
            raise FileNotFoundError("没有指定图标文件")

        ico_path = Path(os.fspath(ico)).resolve()
        if not ico_path.is_file():
            raise FileNotFoundError(f"图标文件不存在：{ico_path}")

        files: list[PlannedFile]
        if planned is not None:
            files = list(planned)
            base = (
                Path(os.fspath(folder))
                if folder is not None
                else (files[0].path.parent if files else Path.cwd())
            )
        else:
            if folder is None:
                raise NotADirectoryError("没有指定目标文件夹")
            base = Path(os.fspath(folder))
            files = self.scan(
                base,
                recursive=recursive,
                exclude_self=exclude_self,
                include_lnk=include_lnk,
                include_exe=include_exe,
                mode=mode,
            )

        # UI 勾选控制：只保留被勾选的路径
        if only_paths is not None:
            wanted = {
                os.path.normcase(os.path.abspath(os.fspath(item)))
                for item in only_paths
            }
            files = [
                item
                for item in files
                if os.path.normcase(os.path.abspath(str(item.path))) in wanted
            ]
        summary = BatchSummary(folder=base, ico=ico_path, scanned=len(files))

        touched: list[str] = []
        total = len(files)
        for index, item in enumerate(files, start=1):
            try:
                result = self._handle_one(item, ico_path, policy, shortcut_dir)
            except ResourceToolMissing as exc:
                result = FileResult(item.path, ACTION_FAILED, str(exc))
                summary.results.append(result)
                summary.aborted = True
                if progress:
                    progress(index, total, result)
                break
            except Exception as exc:  # noqa: BLE001 - 单个文件失败不影响其余
                result = FileResult(item.path, ACTION_FAILED, f"{type(exc).__name__}: {exc}")

            summary.results.append(result)
            if result.action in (ACTION_REPLACED, ACTION_SHORTCUT, ACTION_SHORTCUT_UPDATED):
                touched.append(str(item.path))
            if progress:
                progress(index, total, result)

        if refresh:
            try:
                summary.refresh = self._cache(touched)
            except TypeError:  # 注入的刷新函数可能不收参数
                summary.refresh = self._cache()
            except Exception:  # noqa: BLE001 - 刷新失败不影响替换结果
                summary.refresh = None

        return summary

    # -- 单文件 -----------------------------------------------------------
    def _handle_one(
        self,
        item: PlannedFile,
        ico: Path,
        policy: str,
        shortcut_dir: "str | os.PathLike[str] | None",
    ) -> FileResult:
        # 0) 不支持的对象（非 exe/lnk、文件不存在）：记一行跳过，绝不抛出去
        if item.kind == KIND_OTHER:
            return FileResult(
                item.path,
                ACTION_SKIPPED,
                item.note or "不支持的文件类型，已跳过",
            )

        # 1) .lnk：只改快捷方式自身的图标位置，不碰它指向的 exe，
        #    因此不需要征求"是否创建快捷方式"的确认，也无需判断签名策略。
        if item.is_shortcut:
            return self._update_shortcut(item, ico)

        # 2) .exe：先直接改，改不了按策略降级/跳过
        if item.signed:
            if policy == POLICY_SKIP:
                return FileResult(
                    item.path,
                    ACTION_SKIPPED_SIGNED,
                    f"检测到 {item.path.name} 受数字签名保护，已跳过",
                )
            return self._create_shortcut(item.path, ico, shortcut_dir)

        return self._replace_icon(
            item.path, ico, policy=policy, shortcut_dir=shortcut_dir
        )

    def _update_shortcut(self, item: PlannedFile, ico: Path) -> FileResult:
        """把 ``.lnk`` 的 IconLocation 强制指向新 ico。

        这里**不读取、不校验 TargetPath**：目标为空（商店 / UWP 应用）、目标已
        被删除、目标不是 exe，都不影响改图标。只有“快捷方式本身读不出来”才跳过。
        """
        if item.note:
            return FileResult(item.path, ACTION_SKIPPED, f"{item.path.name}：{item.note}")

        try:
            self._shortcuts.set_icon(item.path, ico)
        except ShortcutError as exc:
            return FileResult(item.path, ACTION_FAILED, f"改快捷方式失败：{exc}")
        except PermissionError:
            return FileResult(
                item.path,
                ACTION_SKIPPED_LOCKED,
                "修改快捷方式失败，可能被占用或权限不足："
                f"{item.path.name} 正在被占用，已跳过",
            )
        except Exception as exc:  # noqa: BLE001 - 单个快捷方式失败不影响整批
            return FileResult(item.path, ACTION_FAILED, f"{type(exc).__name__}: {exc}")

        message = f"已成功修改快捷方式图标：{item.path.name}"
        if item.target is None:
            message += "（该快捷方式没有目标程序，按要求直接覆盖了图标）"
        hint = getattr(self._shortcuts, "last_hint", "")
        if hint:
            message += f"（提示：{hint}）"

        return FileResult(
            item.path,
            ACTION_SHORTCUT_UPDATED,
            message,
            shortcut=item.path,
        )

    def _replace_icon(
        self,
        exe: Path,
        ico: Path,
        *,
        policy: str = POLICY_SHORTCUT,
        shortcut_dir: "str | os.PathLike[str] | None" = None,
    ) -> FileResult:
        """直接改 exe 内嵌图标；**任何**单文件失败都按策略降级为快捷方式。

        降级而不是失败，是因为用户真正要的是“图标变了、还能双击打开”：
        被占用 / 自解压包 / 签名程序 / rcedit 报错，都可以用快捷方式达成目标。
        """
        try:
            backup = self._editor(exe, ico)
        except ResourceToolMissing:
            # 必须让"工具缺失"冒泡到 run()，否则整批会逐个失败却不停下来
            raise
        except ExeLockedError as exc:
            reason = str(exc) or f"{exe.name} 正在运行"
            action = ACTION_SKIPPED_LOCKED if policy == POLICY_SKIP else ACTION_SHORTCUT
            return self._degrade_to_shortcut(
                exe, ico, policy, shortcut_dir, reason, skip_action=action
            )
        except UnsafeOverlayError as exc:
            return self._degrade_to_shortcut(exe, ico, policy, shortcut_dir, str(exc))
        except (ResourceToolError, PermissionError, OSError) as exc:
            reason = str(exc) or type(exc).__name__
            action = ACTION_SKIPPED if policy == POLICY_SKIP else ACTION_SHORTCUT
            return self._degrade_to_shortcut(
                exe, ico, policy, shortcut_dir, reason, skip_action=action
            )

        links = self._sync_existing_shortcuts(exe)
        message = "已替换内嵌图标"
        if links:
            message += f"，同步 {links} 个旧快捷方式"
        return FileResult(
            exe,
            ACTION_REPLACED,
            message,
            backup=backup if backup != exe else None,
            links_updated=links,
        )

    def _create_shortcut(
        self,
        exe: Path,
        ico: Path,
        shortcut_dir: "str | os.PathLike[str] | None",
        *,
        degraded_reason: "str | None" = None,
    ) -> FileResult:
        """为 exe 建立带新图标的快捷方式。

        :param degraded_reason: 非空表示"无法安全改写 exe，因此降级"，用于日志文案。
        """
        # 默认放到**桌面**，用户一眼就能看到；显式传 shortcut_dir 时以调用方为准
        fallback_note = ""
        if shortcut_dir:
            directory = Path(os.fspath(shortcut_dir)).resolve()
        else:
            directory = desktop_dir()

        created: "Path | None" = None
        last_error: "Exception | None" = None

        for target_dir in self._shortcut_candidates(directory, exe.parent):
            try:
                created = self._shortcuts.create(
                    exe, target_dir / shortcut_name_for(exe), icon=ico
                )
                break
            except (ShortcutError, PermissionError, OSError) as exc:
                last_error = exc
                if target_dir != exe.parent:
                    fallback_note = f"（桌面不可写，已改为程序目录）"
                continue

        if created is None:
            prefix = "创建快捷方式失败" if not degraded_reason else "降级创建快捷方式失败"
            return FileResult(exe, ACTION_FAILED, f"{prefix}：{last_error}")

        where = str(created.parent)
        if degraded_reason:
            place = "桌面快捷方式" if created.parent == desktop_dir() else "快捷方式"
            message = (
                f"{exe.name} 无法直接修改（{degraded_reason}），"
                f"已创建{place}并替换图标（{created.name}，位置：{where}）{fallback_note}"
            )
        else:
            message = (
                f"受签名保护，已创建快捷方式 {created.name} 并指向新图标"
                f"（位置：{where}）{fallback_note}"
            )

        return FileResult(
            exe,
            ACTION_SHORTCUT,
            message,
            shortcut=created,
            degraded=bool(degraded_reason),
        )

    @staticmethod
    def _shortcut_candidates(preferred: Path, fallback: Path) -> list[Path]:
        """快捷方式候选目录：优先桌面，失败再退到 exe 同级。"""
        candidates = [preferred]
        if os.path.normcase(str(preferred)) != os.path.normcase(str(fallback)):
            candidates.append(fallback)
        return candidates

    def _degrade_to_shortcut(
        self,
        exe: Path,
        ico: Path,
        policy: str,
        shortcut_dir: "str | os.PathLike[str] | None",
        reason: str,
        *,
        skip_action: str = ACTION_SKIPPED_UNSAFE,
    ) -> FileResult:
        """直接改 exe 失败时的降级处理。

        - 策略 ``shortcut``（默认）：在桌面建快捷方式并替换其图标；
        - 策略 ``skip``：尊重用户选择，仅记录原因（动作由 ``skip_action`` 决定）。
        """
        if policy == POLICY_SKIP:
            return FileResult(
                exe,
                skip_action,
                f"{reason}；当前策略为“跳过”，未做处理",
            )
        return self._create_shortcut(
            exe, ico, shortcut_dir, degraded_reason=reason
        )

    def _sync_existing_shortcuts(self, exe: Path) -> int:
        try:
            updated = self._shortcuts.sync_targeting(exe, None)
        except ShortcutError:
            return 0
        except Exception:  # noqa: BLE001 - 扫描失败不影响替换结果
            return 0
        return len(updated)

    # -- 辅助 -------------------------------------------------------------
    @staticmethod
    def _self_paths() -> set[str]:
        """需要排除的路径（本程序自身）。"""
        paths: set[str] = set()
        try:
            exe = Path(sys.executable)
            if exe.suffix.lower() == ".exe":
                paths.add(os.path.normcase(str(exe.resolve())))
        except (OSError, ValueError):  # pragma: no cover
            pass

        try:
            import __main__

            main_file = getattr(__main__, "__file__", None)
            if main_file:
                paths.add(os.path.normcase(str(Path(main_file).resolve())))
        except Exception:  # noqa: BLE001  # pragma: no cover
            pass
        return paths
