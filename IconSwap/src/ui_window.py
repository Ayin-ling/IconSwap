"""IconSwap 桌面启动器：后台跑 Flask，前台开 pywebview 独立窗口。

用法
----
::

    python src/main.py                 # 开发模式
    dist\\IconSwap.exe                # 打包后双击

流程
----
1. 用 werkzeug 的 :func:`make_server` 在 ``127.0.0.1`` 上绑定 **端口 0**，由
   系统分配空闲端口（不写死 5000，避免端口占用冲突，也没有"先探测再绑定"
   的竞态）；
2. 后台线程 ``serve_forever()`` 运行 Flask，直接复用 :func:`app.create_app`
   的工厂函数，**不复制、不改写任何路由**；
3. 主线程交给 pywebview，创建 900×720、可缩放的独立桌面窗口并加载
   ``http://127.0.0.1:<port>/``；
4. 窗口关闭（或启动异常、Ctrl+C）时关闭 Flask 服务并回收线程，干净退出。

打包
----
``build_exe.bat`` 会把 ``templates`` 与 ``static`` 一并放进 ``_MEIPASS``；
:func:`_point_app_at_resources` 在冻结环境下把 Flask 的模板/静态目录指到
``_MEIPASS``，因此 ``--onefile`` 单文件 exe 也能正常加载资源。
"""

from __future__ import annotations

import atexit
import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from werkzeug.serving import make_server

from iconswap import paths
from iconswap.app import create_app
from iconswap.risk_consent import ensure_risk_consent

__all__ = [
    "FlaskServer",
    "main",
    "WINDOW_TITLE",
    "WINDOW_WIDTH",
    "WINDOW_HEIGHT",
    "STAGED_ENV_FLAG",
]

#: 仅监听本机回环地址，不对外暴露
HOST = "127.0.0.1"

WINDOW_TITLE = "IconSwap"
WINDOW_WIDTH = 900
WINDOW_HEIGHT = 720
MIN_WINDOW_SIZE = (720, 560)

#: 子进程标记：已从纯英文目录启动过，避免无限重启
STAGED_ENV_FLAG = "ICONSWAP_STAGED"

#: onefile 解压/运行时临时目录（必须是纯英文且无需管理员即可创建）。
#: 与 build_exe.bat 里的 ``--runtime-tmpdir`` 保持一致；可用环境变量覆盖。
DEFAULT_RUNTIME_TMPDIR = Path(os.environ.get("ICONSWAP_TMPDIR", r"C:\Users\Public\IconSwapTemp"))

_LOGGER = logging.getLogger("iconswap.desktop")


# --------------------------------------------------------------------------- #
# 基础设施
# --------------------------------------------------------------------------- #
#: 日志文件名（只写临时目录，绝不写 exe 同级目录）
LOG_FILENAME = "IconSwap.log"


def _resolve_log_path() -> "Path | None":
    """挑一个可写的日志文件路径，**绝不在程序目录/当前目录生成文件**。

    优先级：
    1. ``ICONSWAP_LOG_DIR`` 环境变量指定的目录
    2. ``%TEMP%\\IconSwap\\IconSwap.log``（按用户隔离，最常见）
    3. 运行时临时目录 ``C:\\Users\\Public\\IconSwapTemp\\IconSwap.log``
    4. ``%LOCALAPPDATA%\\IconSwap\\IconSwap.log``

    全部不可写时返回 ``None``，此时只保留控制台日志（``--windowed`` 下等于静默），
    这是有意的：宁可没有日志文件，也不能往用户目录里丢垃圾。
    """
    override = os.environ.get("ICONSWAP_LOG_DIR")
    candidates: list[Path] = []
    if override:
        candidates.append(Path(override) / LOG_FILENAME)

    temp_value = os.environ.get("TEMP") or os.environ.get("TMP")
    if temp_value:
        candidates.append(Path(temp_value) / "IconSwap" / LOG_FILENAME)

    candidates.append(DEFAULT_RUNTIME_TMPDIR / LOG_FILENAME)

    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "IconSwap" / LOG_FILENAME)

    for candidate in candidates:
        try:
            candidate.parent.mkdir(parents=True, exist_ok=True)
            with open(candidate, "a", encoding="utf-8"):
                pass  # 只验证可写，不写内容
        except OSError:
            continue
        return candidate
    return None


def _setup_logging() -> None:
    """配置日志。

    ``--windowed`` 打包后 ``sys.stderr`` 为 ``None``，此时只剩文件日志；
    日志文件一律落在临时目录（见 :func:`_resolve_log_path`），
    **不会**在 exe 同级目录或当前工作目录产生任何文件。
    """
    level = logging.DEBUG if os.environ.get("ICONS_DEBUG_GUI") else logging.INFO
    handlers: list[logging.Handler] = []

    if sys.stderr is not None:
        handlers.append(logging.StreamHandler(sys.stderr))

    log_path = _resolve_log_path()
    if log_path is not None:
        try:
            handlers.append(logging.FileHandler(log_path, encoding="utf-8"))
        except OSError:
            pass  # 只读目录等情况下放弃文件日志，不影响启动

    logging.basicConfig(
        level=level,
        format="[%(asctime)s] %(levelname)s %(message)s",
        handlers=handlers,
        force=True,
    )


def _bundle_dir() -> Path:
    """资源根目录：打包后在 ``_MEIPASS``，开发时是 ``src/iconswap``。"""
    return paths.package_dir()


def ensure_runtime_tmpdir(path: "str | os.PathLike[str] | None" = None) -> "Path | None":
    """确保 onefile 的运行时临时目录存在，返回可用路径（失败返回 ``None``）。

    注意顺序：PyInstaller 的引导程序在**任何 Python 代码之前**就要用这个目录，
    所以这里创建只能服务于"下一次启动"以及从暂存目录重启的子进程。
    首次运行仍然依赖 ``--runtime-tmpdir`` 指定的路径可被创建——这也是为什么
    默认选 ``C:\\Users\\Public\\IconSwapTemp``（普通用户可写）而不是 ``C:\\IconSwapTemp``
    （C 盘根目录通常需要管理员权限）。
    """
    target = Path(os.fspath(path)) if path is not None else DEFAULT_RUNTIME_TMPDIR
    try:
        target.mkdir(parents=True, exist_ok=True)
        # 顺手验证可写，避免只是"存在但不能写"
        probe = target / ".iconswap_write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return target
    except OSError as exc:
        _LOGGER.warning("运行时临时目录不可用：%s（%s）", target, exc)
        return None


def _point_app_at_resources(flask_app) -> None:
    """把模板/静态目录显式指到资源根目录（兼容单文件打包）。

    源码运行时 Flask 自己就能按包路径找到 ``src/iconswap/templates``；
    打包后模块在 PYZ 里，必须指到 ``_MEIPASS/iconswap/...``。
    """
    base = _bundle_dir()
    templates = base / "templates"
    static = base / "static"
    if templates.is_dir():
        flask_app.template_folder = str(templates)
    if static.is_dir():
        flask_app.static_folder = str(static)


# --------------------------------------------------------------------------- #
# 自搬运：中文路径下先复制到纯英文目录再重启
# --------------------------------------------------------------------------- #
def _is_ascii_path(path: "str | Path") -> bool:
    """路径是否全部由 ASCII 字符组成。"""
    return all(ord(char) < 128 for char in str(path))


def _staging_candidates() -> "list[Path]":
    """可用的暂存目录候选（按优先级）。

    可用环境变量 ``ICONSWAP_STAGE_DIR`` 指定首选目录（例如放到某个固定盘符），
    其余为常见可写位置。
    """
    candidates = []

    override = os.environ.get("ICONSWAP_STAGE_DIR")
    if override:
        candidates.append(Path(override))

    for key in ("LOCALAPPDATA", "PUBLIC", "PROGRAMDATA"):
        value = os.environ.get(key)
        if value:
            candidates.append(Path(value) / "IconSwap" / "runtime")

    candidates.append(Path(r"C:\Users\Public\IconSwap\runtime"))
    candidates.append(Path(r"C:\ProgramData\IconSwap\runtime"))

    unique = []
    for candidate in candidates:
        if _is_ascii_path(candidate) and candidate not in unique:
            unique.append(candidate)
    return unique


def _copy_if_needed(source: Path, target: Path) -> None:
    """仅在目标缺失或与源不同（大小/修改时间）时复制，避免每次启动拷贝。"""
    if target.exists():
        try:
            source_stat = source.stat()
            target_stat = target.stat()
            if (
                source_stat.st_size == target_stat.st_size
                and int(source_stat.st_mtime) == int(target_stat.st_mtime)
            ):
                return
        except OSError:
            pass
    shutil.copy2(source, target)


def stage_and_relaunch() -> bool:
    """冻结环境下若路径含非 ASCII 字符，则复制到纯英文目录并重启自身。

    背景：pywebview 的 pythonnet 后端在 .NET 侧解析 `Python.Runtime.dll` 时，
    若 exe 所在目录或临时解压目录含非 ASCII 字符，会抛
    `Failed to resolve Python.Runtime.Loader.Initialize`，窗口无法创建。

    会依次尝试 :func:`_staging_candidates` 里的目录，任一可用即搬运并重启。

    :returns: ``True`` 表示已启动新进程，当前进程应当立刻退出。
    """
    if not getattr(sys, "frozen", False):
        return False  # 开发模式不受该限制，直接原地启动
    if os.environ.get(STAGED_ENV_FLAG) == "1":
        return False  # 已经是从暂存目录启动的，防止无限重启

    exe = Path(sys.executable)
    meipass = Path(getattr(sys, "_MEIPASS", exe.parent))
    if _is_ascii_path(exe) and _is_ascii_path(meipass):
        return False  # 路径本来就干净，正常启动

    for target_dir in _staging_candidates():
        staged_exe = target_dir / exe.name
        temp_dir = target_dir / "tmp"
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            temp_dir.mkdir(parents=True, exist_ok=True)
            _copy_if_needed(exe, staged_exe)
        except OSError as exc:
            _LOGGER.warning("暂存目录 %s 不可用：%s", target_dir, exc)
            continue

        child_env = dict(os.environ)
        child_env[STAGED_ENV_FLAG] = "1"
        child_env["TEMP"] = str(temp_dir)  # 解压目录也必须是纯英文
        child_env["TMP"] = str(temp_dir)

        _LOGGER.info(
            "当前路径含非 ASCII 字符（%s），已暂存到 %s 并从那里重启",
            exe.parent,
            target_dir,
        )
        try:
            subprocess.Popen(
                [str(staged_exe)],
                cwd=str(target_dir),
                env=child_env,
                close_fds=True,
            )
        except OSError:
            _LOGGER.exception("重启暂存副本失败：%s", staged_exe)
            continue
        return True

    _LOGGER.error("所有暂存目录都不可用，仍在原路径启动（窗口可能打不开）")
    return False


class NativeBridge:
    """暴露给前端的原生能力（``window.pywebview.api.*``）。

    * ``choose_folder()`` —— 选目录（功能一的“浏览…”、功能二的“扫描文件夹”）；
    * ``choose_files()`` —— 多选 ``.lnk`` 文件（功能二的“选择 .lnk 文件…”）。

    浏览器里没有 ``pywebview``，前端会自动降级成手动输入路径 / 扫描文件夹。
    """

    def choose_folder(self) -> str:
        try:
            import webview  # 局部导入：模块本身不依赖 GUI
        except ImportError:  # pragma: no cover
            return ""

        try:
            windows = getattr(webview, "windows", None) or []
            window = windows[0] if windows else None
            if window is None:  # pragma: no cover
                return ""
            result = window.create_file_dialog(webview.FOLDER_DIALOG)
            if not result:
                return ""
            return str(result[0] if isinstance(result, (list, tuple)) else result)
        except Exception:  # pragma: no cover - 用户取消/平台不支持
            _LOGGER.debug("文件夹选择框调用失败", exc_info=True)
            return ""

    def choose_files(self) -> list:
        """多选文件，返回绝对路径列表（用户取消时返回空列表）。

        只用于“仅替换选中的快捷方式”：用户选了什么，就只改什么。
        """
        try:
            import webview  # 局部导入
        except ImportError:  # pragma: no cover
            return []

        try:
            windows = getattr(webview, "windows", None) or []
            window = windows[0] if windows else None
            if window is None:  # pragma: no cover
                return []
            result = window.create_file_dialog(
                webview.OPEN_DIALOG,
                allow_multiple=True,
                file_types=(
                    "程序与快捷方式 (*.exe;*.lnk)",
                    "可执行文件 (*.exe)",
                    "快捷方式 (*.lnk)",
                    "所有文件 (*.*)",
                ),
            )
            if not result:
                return []
            if isinstance(result, str):  # 老版本会直接返回字符串
                return [result]
            return [str(item) for item in result]
        except Exception:  # pragma: no cover - 用户取消/平台不支持
            _LOGGER.debug("文件选择框调用失败", exc_info=True)
            return []

    def choose_icon(self) -> str:
        """选一个 ``.ico``，返回它的**真实绝对路径**（用户取消返回空串）。

        批量替换只认磁盘上的真实文件：不复制、不缓存、不落临时目录，
        这样快捷方式的 ``IconLocation`` 才不会在程序退出后失效。
        """
        try:
            import webview  # 局部导入
        except ImportError:  # pragma: no cover
            return ""

        try:
            windows = getattr(webview, "windows", None) or []
            window = windows[0] if windows else None
            if window is None:  # pragma: no cover
                return ""
            result = window.create_file_dialog(
                webview.OPEN_DIALOG,
                allow_multiple=False,
                file_types=("图标文件 (*.ico)", "所有文件 (*.*)"),
            )
            if not result:
                return ""
            if isinstance(result, (list, tuple)):
                return str(result[0]) if result else ""
            return str(result)
        except Exception:  # pragma: no cover - 用户取消/平台不支持
            _LOGGER.debug("图标选择框调用失败", exc_info=True)
            return ""


class FlaskServer:
    """在后台线程里运行 Flask 的最小封装，可重复调用 :meth:`stop`。"""

    def __init__(self, flask_app, host: str = HOST, port: int = 0) -> None:
        # threaded=True：pywebview 的内嵌浏览器会并发请求静态资源
        self._server = make_server(host, port, flask_app, threaded=True)
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._stopped = threading.Event()

    # -- 属性 --------------------------------------------------------------
    @property
    def host(self) -> str:
        return HOST

    @property
    def port(self) -> int:
        return int(self._server.server_port)

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    @property
    def thread(self) -> Optional[threading.Thread]:
        return self._thread

    # -- 生命周期 ----------------------------------------------------------
    def start(self) -> "FlaskServer":
        """启动服务线程（幂等）。"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self
            self._stopped.clear()
            self._thread = threading.Thread(
                target=self._serve, name="iconswap-flask", daemon=True
            )
            self._thread.start()
        return self

    def _serve(self) -> None:
        try:
            self._server.serve_forever()
        except Exception:  # pragma: no cover - 线程内兜底
            _LOGGER.exception("Flask 服务线程异常退出")
        finally:
            self._stopped.set()

    def wait_ready(self, timeout: float = 5.0) -> bool:
        """轮询直到端口可连接（用于窗口创建前的健康等待）。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with socket.create_connection((self.host, self.port), timeout=0.2):
                    return True
            except OSError:
                time.sleep(0.05)
        return False

    def stop(self, timeout: float = 5.0) -> None:
        """停止服务线程并释放监听端口（幂等）。"""
        try:
            self._server.shutdown()
        except Exception:  # pragma: no cover - 重复 shutdown 时的防御
            _LOGGER.debug("shutdown 时出错", exc_info=True)

        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)

        try:
            self._server.server_close()
        except Exception:  # pragma: no cover
            _LOGGER.debug("server_close 时出错", exc_info=True)

        self._stopped.set()


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def main() -> int:
    """启动桌面窗口；返回进程退出码。"""
    _setup_logging()

    # 0) 运行时临时目录：不存在就建（服务"下一次启动"与暂存子进程）
    runtime_tmpdir = ensure_runtime_tmpdir()
    if runtime_tmpdir is not None:
        _LOGGER.debug("运行时临时目录：%s", runtime_tmpdir)

    # 1) 首次使用风险确认：未确认过则弹原生对话框，拒绝即退出
    if not ensure_risk_consent():
        _LOGGER.info("用户未确认风险提示，程序退出")
        return 0

    # 2) 中文路径下先自搬运到纯英文目录并从那里重启（见 stage_and_relaunch）
    if stage_and_relaunch():
        return 0

    try:
        import webview
    except ImportError:
        _LOGGER.error(
            "缺少 pywebview，无法打开桌面窗口。请先安装："
            "pip install -r requirements-desktop.txt"
        )
        return 1

    flask_app = create_app()
    _point_app_at_resources(flask_app)

    server = FlaskServer(flask_app).start()
    atexit.register(server.stop)

    if not server.wait_ready(timeout=5.0):
        _LOGGER.error("Flask 未能在 5 秒内就绪，退出")
        server.stop()
        return 1
    _LOGGER.info("Flask 已就绪：%s", server.url)

    window = webview.create_window(
        WINDOW_TITLE,
        server.url,
        width=WINDOW_WIDTH,
        height=WINDOW_HEIGHT,
        min_size=MIN_WINDOW_SIZE,
        resizable=True,
        text_select=True,
        js_api=NativeBridge(),
    )

    def _on_closed() -> None:
        _LOGGER.info("窗口已关闭，正在停止 Flask 服务")
        server.stop()

    try:
        window.events.closed += _on_closed
    except Exception:  # pragma: no cover - 老版本 pywebview 无该事件
        _LOGGER.debug("绑定窗口关闭事件失败", exc_info=True)

    try:
        webview.start(
            debug=bool(os.environ.get("ICONS_DEBUG_GUI")),
            private_mode=False,
        )
    except KeyboardInterrupt:  # pragma: no cover - 交互式中断
        _LOGGER.info("收到中断，准备退出")
    except Exception:  # pragma: no cover - 无 GUI 环境等
        _LOGGER.exception("桌面窗口启动失败")
        return 1
    finally:
        server.stop()
        _LOGGER.info("已退出")

    return 0


if __name__ == "__main__":
    sys.exit(main())
