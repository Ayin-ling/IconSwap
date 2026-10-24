"""应用信息与第三方组件清单（供「关于」界面使用）。

版本号优先从已安装包的元数据里读，读不到就退回写死的值，保证在未安装依赖
的环境（比如只看源码）里也不会崩。
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as _pkg_version
from typing import Any

__all__ = ["APP_NAME", "APP_VERSION", "COMPONENTS", "app_info"]

APP_NAME = "IconSwap"
APP_VERSION = "1.2.0"


def _v(package: str, fallback: str = "—") -> str:
    try:
        return _pkg_version(package)
    except PackageNotFoundError:  # pragma: no cover - 未安装时
        return fallback


#: 第三方组件：名称、版本、许可证、用途、主页
COMPONENTS: list[dict[str, str]] = [
    {
        "name": "rcedit",
        "version": "2.0.0",
        "license": "MIT",
        "author": "Electron",
        "purpose": "替换 exe 内嵌图标资源",
        "url": "https://github.com/electron/rcedit",
    },
    {
        "name": "pywebview",
        "version": _v("pywebview"),
        "license": "BSD-3-Clause",
        "author": "Roman Sirokov",
        "purpose": "桌面窗口（WebView2 宿主）",
        "url": "https://github.com/r0x0r/pywebview",
    },
    {
        "name": "pythonnet",
        "version": _v("pythonnet"),
        "license": "MIT",
        "author": "pythonnet contributors",
        "purpose": "pywebview 的 .NET 互操作",
        "url": "https://github.com/pythonnet/pythonnet",
    },
    {
        "name": "Flask",
        "version": _v("flask"),
        "license": "BSD-3-Clause",
        "author": "Pallets",
        "purpose": "本地 HTTP 服务与路由",
        "url": "https://github.com/pallets/flask",
    },
    {
        "name": "Werkzeug",
        "version": _v("werkzeug"),
        "license": "BSD-3-Clause",
        "author": "Pallets",
        "purpose": "WSGI 服务器",
        "url": "https://github.com/pallets/werkzeug",
    },
    {
        "name": "Pillow",
        "version": _v("pillow"),
        "license": "MIT-CMU (HPND)",
        "author": "Pillow contributors",
        "purpose": "图片解码与 ICO 编码",
        "url": "https://python-pillow.org",
    },
    {
        "name": "pefile",
        "version": _v("pefile"),
        "license": "MIT",
        "author": "Ero Carrera",
        "purpose": "PE 结构解析与数字签名检测",
        "url": "https://github.com/erocarrera/pefile",
    },
    {
        "name": "pywin32",
        "version": _v("pywin32"),
        "license": "PSF-2.0",
        "author": "Mark Hammond",
        "purpose": "快捷方式(.lnk)读写、Windows API",
        "url": "https://github.com/mhammond/pywin32",
    },
    {
        "name": "PyInstaller",
        "version": _v("pyinstaller"),
        "license": "GPL-2.0-or-later WITH bootloader-exception",
        "author": "PyInstaller Development Team",
        "purpose": "打包成单文件 exe（仅构建期使用）",
        "url": "https://pyinstaller.org",
    },
]

#: 许可与免责声明要点（UI 里直接展示）
NOTICE = (
    "IconSwap 自身代码可自由使用；随附的第三方组件遵循各自许可证。"
    "rcedit 以 MIT 许可证分发（© Electron），再分发时请保留其版权与许可声明。"
    "图标替换会改动可执行文件，请务必保留程序生成的 *_original.exe 备份。"
)


def app_info() -> dict[str, Any]:
    """返回「关于」界面需要的全部信息。"""
    return {
        "name": APP_NAME,
        "version": APP_VERSION,
        "platform": "Windows 10/11 (x64)",
        "components": COMPONENTS,
        "notice": NOTICE,
    }
