"""PE 文件检查：数字签名检测、可执行文件枚举、图标资源探测。

设计要点
--------
* **快**：签名检测只解析 PE 头部的 Security 数据目录（`fast_load=True` +
  `parse_data_directories([IMAGE_DIRECTORY_ENTRY_SECURITY])`），不解析资源、
  不加载整份文件，批量扫描几千个 exe 也很快。
* **准**：Security 目录非空只代表"文件里挂了一段证书"，还要校验
  `WIN_CERTIFICATE` 结构头才算"有签名"；需要更强保证时可开 ``deep=True``，
  走 Windows 的 `WinVerifyTrust` 做真实校验。
* **稳**：任何解析异常都收敛成 :class:`SignatureInfo`，不把异常抛给批量流程。

对外接口
--------
::

    info = has_digital_signature(r"C:/some/app.exe")      # 快速
    info = has_digital_signature(path, deep=True)         # WinVerifyTrust 校验
    for exe in iter_executables(folder, recursive=True):
        ...
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

try:  # pefile 是硬依赖，但导入失败时给出可操作的错误而不是 ImportError 栈
    import pefile
except ImportError:  # pragma: no cover - 依赖缺失分支
    pefile = None  # type: ignore[assignment]

__all__ = [
    "SignatureInfo",
    "PeInspectionError",
    "has_digital_signature",
    "iter_executables",
    "has_icon_resource",
    "resource_signature",
    "overlay_size",
    "winverifytrust",
    "IMAGE_DIRECTORY_ENTRY_SECURITY",
    "WIN_CERT_TYPE_PKCS_SIGNED_DATA",
]

IMAGE_DIRECTORY_ENTRY_SECURITY = 4
WIN_CERT_TYPE_PKCS_SIGNED_DATA = 0x0002
WIN_CERT_TYPE_X509 = 0x0001

_RT_ICON = 3
_RT_GROUP_ICON = 14


class PeInspectionError(RuntimeError):
    """PE 解析无法进行（依赖缺失、文件不是 PE 等）。"""


@dataclass(frozen=True)
class SignatureInfo:
    """一个 PE 文件的签名检查结果。"""

    signed: bool
    has_security_directory: bool = False
    cert_size: int = 0
    cert_type: Optional[int] = None
    verified: Optional[bool] = None  # None 表示没做 WinVerifyTrust
    error: Optional[str] = None

    @property
    def label(self) -> str:
        """给 UI / 日志用的一行描述。"""
        if not self.signed:
            return "无签名" if not self.error else f"无法读取（{self.error}）"
        if self.verified is True:
            return "已签名（校验通过）"
        if self.verified is False:
            return "有签名段（校验未通过）"
        return "已签名"


# --------------------------------------------------------------------------- #
# 依赖检查
# --------------------------------------------------------------------------- #
def _require_pefile():
    if pefile is None:  # pragma: no cover - 依赖缺失分支
        raise PeInspectionError("缺少 pefile，请先执行：pip install pefile")
    return pefile


# --------------------------------------------------------------------------- #
# 签名检测
# --------------------------------------------------------------------------- #
def has_digital_signature(
    path: "str | os.PathLike[str]",
    *,
    deep: bool = False,
) -> SignatureInfo:
    """检测 PE 文件是否带数字签名。

    :param path: exe / dll 路径。
    :param deep: 为 ``True`` 时额外调用 WinVerifyTrust 真实验签
        （稍慢，且需要 Windows；非 Windows 上自动跳过）。
    """
    file_path = Path(os.fspath(path))
    if not file_path.is_file():
        return SignatureInfo(False, error="文件不存在")

    module = _require_pefile()
    try:
        pe = module.PE(str(file_path), fast_load=True)
    except Exception as exc:  # noqa: BLE001 - 任何解析异常都视为"读不到"
        return SignatureInfo(False, error=str(exc).strip() or type(exc).__name__)

    try:
        entry = None
        # 只解析安全目录，避免遍历资源树带来的额外 IO
        try:
            pe.parse_data_directories(
                directories=[IMAGE_DIRECTORY_ENTRY_SECURITY]
            )
        except Exception:  # noqa: BLE001 - 某些加壳样本会在这里炸
            pass

        try:
            entry = pe.OPTIONAL_HEADER.DATA_DIRECTORY[IMAGE_DIRECTORY_ENTRY_SECURITY]
        except (IndexError, AttributeError):
            entry = None

        if entry is None or getattr(entry, "Size", 0) == 0:
            return SignatureInfo(False)

        # 安全目录的 VirtualAddress 实际上是**文件偏移**
        offset = int(entry.VirtualAddress)
        size = int(entry.Size)
        if size < 8 or offset <= 0:
            return SignatureInfo(True, has_security_directory=True, cert_size=size)

        try:
            file_size = file_path.stat().st_size
            if offset + 8 > file_size:
                return SignatureInfo(
                    True,
                    has_security_directory=True,
                    cert_size=size,
                    error="证书段越界",
                )
            with open(file_path, "rb") as handle:
                handle.seek(offset)
                header = handle.read(8)
        except OSError as exc:
            return SignatureInfo(True, has_security_directory=True, cert_size=size, error=str(exc))

        dw_length, _w_revision, w_type = struct.unpack("<IHH", header)
        plausible = dw_length >= 8 and (offset + dw_length) <= file_size
        info = SignatureInfo(
            signed=plausible,
            has_security_directory=True,
            cert_size=size,
            cert_type=w_type,
            error=None if plausible else "证书结构异常",
        )
    finally:
        try:
            pe.close()
        except Exception:  # noqa: BLE001
            pass

    if deep and info.signed:
        verified = winverifytrust(path)
        info = SignatureInfo(
            signed=info.signed,
            has_security_directory=info.has_security_directory,
            cert_size=info.cert_size,
            cert_type=info.cert_type,
            verified=verified,
            error=info.error,
        )
    return info


# --------------------------------------------------------------------------- #
# WinVerifyTrust 深度校验
# --------------------------------------------------------------------------- #
class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class _WINTRUST_FILE_INFO(ctypes.Structure):
    _fields_ = [
        ("cbStruct", ctypes.c_ulong),
        ("pcwszFilePath", ctypes.c_wchar_p),
        ("hFile", ctypes.c_void_p),
        ("pgKnownSubject", ctypes.c_void_p),
    ]


class _WINTRUST_DATA(ctypes.Structure):
    _fields_ = [
        ("cbStruct", ctypes.c_ulong),
        ("pPolicyCallbackData", ctypes.c_void_p),
        ("pSIPClientData", ctypes.c_void_p),
        ("dwUIChoice", ctypes.c_ulong),
        ("fdwRevocationChecks", ctypes.c_ulong),
        ("dwUnionChoice", ctypes.c_ulong),
        ("pFile", ctypes.c_void_p),
        ("dwStateAction", ctypes.c_ulong),
        ("hWVTStateData", ctypes.c_void_p),
        ("pwszURLReference", ctypes.c_wchar_p),
        ("dwProvFlags", ctypes.c_ulong),
        ("dwUIContext", ctypes.c_ulong),
    ]


_WTD_UI_NONE = 2
_WTD_REVOKE_NONE = 0
_WTD_CHOICE_FILE = 1
_WTD_STATEACTION_VERIFY = 1
_WTD_STATEACTION_CLOSE = 2
_WTD_SAFER_FLAG = 0x100
_WINTRUST_ACTION_GENERIC_VERIFY_V2 = _GUID(
    0x00AAC56B, 0xCD44, 0x11D0, (ctypes.c_ubyte * 8)(0x8C, 0xC2, 0x00, 0xC0, 0x4F, 0xC2, 0x95, 0xEE)
)


def winverifytrust(path: "str | os.PathLike[str]") -> Optional[bool]:
    """调用 `WinVerifyTrust` 做真实签名校验。

    :returns: ``True`` 校验通过、``False`` 校验失败、``None`` 当前平台不支持。
    """
    if os.name != "nt":  # pragma: no cover - 非 Windows
        return None

    try:
        wintrust = ctypes.WinDLL("wintrust.dll")
    except OSError:  # pragma: no cover
        return None

    file_info = _WINTRUST_FILE_INFO()
    file_info.cbStruct = ctypes.sizeof(_WINTRUST_FILE_INFO)
    file_info.pcwszFilePath = str(Path(os.fspath(path)).resolve())
    file_info.hFile = None
    file_info.pgKnownSubject = None

    trust_data = _WINTRUST_DATA()
    trust_data.cbStruct = ctypes.sizeof(_WINTRUST_DATA)
    trust_data.pPolicyCallbackData = None
    trust_data.pSIPClientData = None
    trust_data.dwUIChoice = _WTD_UI_NONE
    trust_data.fdwRevocationChecks = _WTD_REVOKE_NONE
    trust_data.dwUnionChoice = _WTD_CHOICE_FILE
    trust_data.pFile = ctypes.cast(ctypes.pointer(file_info), ctypes.c_void_p)
    trust_data.dwStateAction = _WTD_STATEACTION_VERIFY
    trust_data.hWVTStateData = None
    trust_data.pwszURLReference = None
    trust_data.dwProvFlags = _WTD_SAFER_FLAG
    trust_data.dwUIContext = 0

    action = _WINTRUST_ACTION_GENERIC_VERIFY_V2
    try:
        status = wintrust.WinVerifyTrust(
            None, ctypes.byref(action), ctypes.byref(trust_data)
        )
    except OSError:  # pragma: no cover
        return None
    finally:
        trust_data.dwStateAction = _WTD_STATEACTION_CLOSE
        try:
            wintrust.WinVerifyTrust(None, ctypes.byref(action), ctypes.byref(trust_data))
        except OSError:  # pragma: no cover
            pass

    return status == 0


# --------------------------------------------------------------------------- #
# 文件枚举与图标资源
# --------------------------------------------------------------------------- #
def iter_executables(
    root: "str | os.PathLike[str]",
    *,
    recursive: bool = True,
    skip_backups: bool = True,
    exclude: "str | os.PathLike[str] | None" = None,
) -> Iterator[Path]:
    """遍历目录下的 ``.exe`` 文件（按路径排序，结果稳定）。

    :param root: 目标目录。
    :param recursive: 是否包含子目录。
    :param skip_backups: 跳过本工具生成的 ``*_original.exe`` 备份。
    :param exclude: 需要排除的路径（通常传本程序自身）。
    :raises NotADirectoryError: ``root`` 不是目录。
    """
    base = Path(os.fspath(root))
    if not base.is_dir():
        raise NotADirectoryError(f"不是有效目录：{base}")

    excluded: set[str] = set()
    if exclude is not None:
        try:
            excluded.add(os.path.normcase(os.path.abspath(os.fspath(exclude))))
        except (TypeError, ValueError):  # pragma: no cover
            pass

    found: list[Path] = []
    for path in base.rglob("*") if recursive else base.glob("*"):
        try:
            if not path.is_file() or path.suffix.lower() != ".exe":
                continue
        except OSError:  # pragma: no cover - 权限异常的目录
            continue
        if skip_backups and path.stem.lower().endswith("_original"):
            continue
        if os.path.normcase(os.path.abspath(str(path))) in excluded:
            continue
        found.append(path)

    found.sort(key=lambda item: str(item).lower())
    yield from found


def resource_signature(path: "str | os.PathLike[str]") -> str:
    """计算 exe 中 ``.rsrc`` 段内容的 SHA-256，用于校验"图标是否真的被改写"。

    rcedit 有时会静默失败（被安全软件拦截、文件被占用等），退出码却是 0。
    替换前后各取一次签名，完全相同就说明资源没变，属于假成功。
    """
    pefile = _require_pefile()
    target = Path(os.fspath(path))
    if not target.is_file():
        raise PeInspectionError(f"文件不存在：{target}")

    try:
        pe = pefile.PE(str(target), fast_load=True)
    except Exception as exc:  # noqa: BLE001
        raise PeInspectionError(f"无法解析 PE：{target}（{exc}）") from exc

    try:
        payload = b""
        for section in getattr(pe, "sections", []) or []:
            name = bytes(section.Name).rstrip(b"\x00")
            if name == b".rsrc":
                try:
                    payload = section.get_data()
                except Exception:  # noqa: BLE001
                    payload = b""
                break
        return hashlib.sha256(payload).hexdigest()
    finally:
        try:
            pe.close()
        except Exception:  # noqa: BLE001
            pass


def overlay_size(path: "str | os.PathLike[str]") -> int:
    """返回"PE 结构之外"的尾部数据长度（字节）。

    PyInstaller 单文件包、部分 Electron / 自解压安装包会把归档或数据**追加**在
    最后一个节之后（overlay）。rcedit 这类改写资源的工具会重写整个文件，实测
    会把这段数据丢掉（本项目 27 MB 的 exe 被削成 300 KB），所以替换前必须检查。

    :returns: 尾部数据字节数；无法解析时返回 0。
    """
    file_path = Path(os.fspath(path))
    try:
        module = _require_pefile()
        pe = module.PE(str(file_path), fast_load=True)
    except Exception:  # noqa: BLE001
        return 0

    try:
        end = 0
        for section in getattr(pe, "sections", []) or []:
            end = max(end, int(section.PointerToRawData) + int(section.SizeOfRawData))
        if end <= 0:  # pragma: no cover - 异常 PE
            return 0
        try:
            total = file_path.stat().st_size
        except OSError:  # pragma: no cover
            return 0
        return max(0, total - end)
    finally:
        try:
            pe.close()
        except Exception:  # noqa: BLE001
            pass


def has_icon_resource(path: "str | os.PathLike[str]") -> bool:
    """PE 里是否存在图标资源（RT_GROUP_ICON，类型 14）。"""
    module = _require_pefile()
    try:
        pe = module.PE(str(Path(os.fspath(path))), fast_load=True)
    except Exception:  # noqa: BLE001
        return False
    try:
        try:
            pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_RESOURCE"]])
        except Exception:  # noqa: BLE001
            return False
        root = getattr(pe, "DIRECTORY_ENTRY_RESOURCE", None)
        if root is None:
            return False
        for entry in root.entries:
            if entry.id in (_RT_GROUP_ICON, _RT_ICON):
                return True
        return False
    finally:
        try:
            pe.close()
        except Exception:  # noqa: BLE001
            pass
