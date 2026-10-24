"""IconSwap —— 桌面图标转换应用的 Flask 入口。

接口
----
=====================  =======================================================
方法 / 路径             说明
=====================  =======================================================
``GET  /``             上传页面（templates/index.html）
``GET  /api``          服务信息与接口清单（JSON）
``GET  /api/about``    应用版本与第三方组件清单
``GET  /health``       健康检查
``GET  /api/sizes``    允许的图标尺寸范围与默认尺寸
``POST /api/convert``  上传图片，返回多尺寸 ICO 文件（attachment 下载）
=====================  =======================================================

``POST /api/convert`` 的请求约定：

* ``multipart/form-data``，图片字段名 ``file``（支持 png/jpg/jpeg/bmp/webp/
  gif/tif/tiff）；
* 可选字段 ``remove_bg=1``：去掉纯色背景（默认白色）并转为透明；
  可选字段 ``tolerance``：去背景容差（0–255，默认 30）；
* 可选字段 ``sizes``：逗号或空格分隔的目标尺寸，例如 ``"16,32,48,256"``；
  缺省时使用 :data:`iconswap.ico_converter.DEFAULT_SIZES`；
* 成功返回 ``200``，``Content-Type: image/x-icon``，附件名为 ``<原文件名>.ico``，
  并在 ``X-Icon-Sizes`` 响应头回显实际生成的尺寸；
* 失败返回 JSON：``{"error": "..."}``，400（参数/图片问题）、413（文件过大）、
  415（扩展名不支持）、500（未知错误）。

不落盘原则
----------
上传内容只在内存中解码（``io.BytesIO`` + Pillow），转换结果以字节流直接返回，
不写任何临时文件，因此不会在服务器上留下用户图片或中间产物。

运行
----
::

    python src/main.py                 # 桌面窗口（pywebview，见 src/ui_window.py）
    flask --app iconswap.app run --port 5000   # 用浏览器调试
    set ICONS_MAX_UPLOAD_BYTES=...    # 可选：覆盖 8 MB 上传上限
"""

from __future__ import annotations

# --------------------------------------------------------------------------- #
# 运行时临时目录兜底（必须在任何其它模块导入/使用 tempfile 之前执行）
#
# 说明（重要）：
#   1. PyInstaller onefile 的引导程序在**任何 Python 代码之前**就要解压，
#      那一步使用的是编译期 --runtime-tmpdir（见 IconSwap.spec），
#      这里的代码只能影响"解压之后"进程内的 TEMP（任务图标、临时文件等）。
#   2. 因此这里绝不能让它抛异常：标准账户无法在 C:\ 根目录建目录，
#      直接 os.makedirs(r"C:\IconSwapTemp") 会 PermissionError 导致启动即崩。
#   3. 候选顺序：C:\IconSwapTemp → C:\Users\Public\IconSwapTemp，
#      逐个尝试并做写入探针，全部失败则保留系统原值。
# --------------------------------------------------------------------------- #
import os as _os_boot

RUNTIME_TEMP_DIR: "str | None" = None


def _bootstrap_runtime_temp() -> "str | None":
    """确保存在一个可写的纯英文临时目录，并把它设为 TEMP/TMP。"""
    if _os_boot.name != "nt":  # 只有 Windows 需要
        return None

    for candidate in (r"C:\IconSwapTemp", r"C:\Users\Public\IconSwapTemp"):
        probe = _os_boot.path.join(candidate, ".iconswap_write_probe")
        try:
            _os_boot.makedirs(candidate, exist_ok=True)
            with open(probe, "w", encoding="utf-8") as handle:
                handle.write("ok")
            _os_boot.remove(probe)
        except OSError:
            continue

        _os_boot.environ["TEMP"] = candidate
        _os_boot.environ["TMP"] = candidate
        return candidate

    return None


try:
    RUNTIME_TEMP_DIR = _bootstrap_runtime_temp()
except Exception:  # noqa: BLE001 - 兜底逻辑自身也绝不允许阻断启动
    RUNTIME_TEMP_DIR = None


import io
import os
import re
import tempfile
import uuid
from pathlib import Path
from typing import Any

from flask import (
    Blueprint,
    Flask,
    current_app,
    jsonify,
    render_template,
    request,
    send_file,
)
from PIL import Image, UnidentifiedImageError
from werkzeug.exceptions import RequestEntityTooLarge

from iconswap.about import app_info
from iconswap.image_tools import DEFAULT_TOLERANCE, make_background_transparent
from iconswap.batch_api import bp as batch_bp
from iconswap.ico_converter import (
    DEFAULT_SIZES,
    MAX_ICON_SIZE,
    MIN_ICON_SIZE,
    convert_to_ico,
    normalize_sizes,
)

__all__ = ["ALLOWED_EXTENSIONS", "create_app", "app"]

#: 允许上传的图片扩展名（小写，不含点）
ALLOWED_EXTENSIONS: frozenset[str] = frozenset(
    {"png", "jpg", "jpeg", "bmp", "webp", "gif", "tif", "tiff"}
)

#: 上传表单里的图片字段名
UPLOAD_FIELD = "file"

ICO_MIMETYPE = "image/x-icon"

#: 默认上传上限：8 MB
DEFAULT_MAX_UPLOAD_BYTES = 8 * 1024 * 1024

_SIZE_SPLITTER = re.compile(r"[,\s]+")


def _error(message: str, status: int):
    """构造统一的 JSON 错误响应。"""
    return jsonify({"error": message}), status


def _parse_flag(raw: Any, default: bool = False) -> bool:
    """解析布尔型表单字段（``1/true/yes/on`` 为真）。"""
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in {"1", "true", "yes", "on", "y"}


def _debug_enabled() -> bool:
    """是否开启抠图调试（``ICONSWAP_DEBUG_BG=1``）。"""
    return str(os.environ.get("ICONSWAP_DEBUG_BG", "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _dump_debug_images(before, after) -> list:
    """把处理前后的图片写到临时目录，返回文件路径列表（失败返回空）。"""
    try:
        directory = Path(tempfile.gettempdir()) / "IconSwap" / "debug"
        directory.mkdir(parents=True, exist_ok=True)
        tag = uuid.uuid4().hex[:8]
        paths = []
        for label, image in (("before", before), ("after", after)):
            target = directory / f"bg_{tag}_{label}.png"
            image.save(target, format="PNG")
            paths.append(str(target))
        return paths
    except Exception:  # noqa: BLE001 - 调试功能绝不影响主流程
        return []


def _parse_tolerance(raw: Any, default: int = DEFAULT_TOLERANCE) -> int:
    """解析去背景容差，限制在 0–255。"""
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return max(0, min(255, value))


def _parse_sizes(raw: Any) -> tuple[int, ...]:
    """解析 ``sizes`` 表单字段；空值回落到默认尺寸。

    :raises ValueError: 字段内容不是合法的尺寸列表。
    """
    if raw is None:
        return normalize_sizes(None)
    if not isinstance(raw, str):
        raise ValueError("sizes 必须是逗号分隔的整数列表")
    text = raw.strip()
    if not text:
        return normalize_sizes(None)

    raw_items = [item for item in _SIZE_SPLITTER.split(text) if item]
    try:
        parsed = [int(item) for item in raw_items]
    except ValueError as exc:
        raise ValueError(f"sizes 含非整数项: {raw!r}") from exc
    if not parsed:  # pragma: no cover - 上面的空串判断已覆盖
        return normalize_sizes(None)
    return normalize_sizes(parsed)


def _safe_stem(filename: str) -> str:
    """从上传文件名提取安全的下载名主干（去掉路径与非法字符）。"""
    stem = Path(filename).stem.strip()
    cleaned = "".join(ch for ch in stem if ch.isalnum() or ch in "-_")
    return cleaned or "icon"


def _decode_image(raw: bytes) -> Image.Image:
    """把上传字节解码成独立的 Pillow 图像。

    :raises ValueError: 内容不是可识别的图片，或像素数过大。
    """
    try:
        with Image.open(io.BytesIO(raw)) as image:
            image.load()
            return image.copy()
    except UnidentifiedImageError as exc:
        raise ValueError("无法识别的图片内容，请上传有效的图片文件") from exc
    except Image.DecompressionBombError as exc:
        raise ValueError("图片像素数量超限，已拒绝处理") from exc
    except OSError as exc:
        raise ValueError(f"图片解码失败: {exc}") from exc


bp = Blueprint("iconswap", __name__)


@bp.get("/")
def index():
    """上传页面：选图 → 勾选尺寸 → 生成并下载 ICO。"""
    limit = int(current_app.config.get("MAX_CONTENT_LENGTH") or DEFAULT_MAX_UPLOAD_BYTES)
    return render_template(
        "index.html",
        sizes=DEFAULT_SIZES,
        max_bytes=limit,
        max_mb=round(limit / (1024 * 1024), 2),
    )


@bp.get("/api")
def api_info():
    """服务信息与接口清单（JSON）。"""
    return jsonify(
        {
            "app": "IconSwap",
            "description": "桌面图标转换服务：把图片转换成多尺寸 ICO",
            "endpoints": {
                "GET /": "上传页面",
                "GET /api": "服务信息",
                "GET /health": "健康检查",
                "GET /api/sizes": "允许的尺寸范围与默认尺寸",
                "POST /api/convert": (
                    f"上传图片（multipart 字段 {UPLOAD_FIELD}，可选 sizes），返回 .ico"
                ),
            },
            "supported_formats": sorted(ALLOWED_EXTENSIONS),
        }
    )


@bp.get("/health")
def health():
    """健康检查。"""
    return jsonify({"status": "ok"})


@bp.get("/api/about")
def about():
    """应用与第三方组件信息（供「关于」界面使用）。"""
    return jsonify(app_info())


@bp.get("/api/sizes")
def sizes_info():
    """返回允许的尺寸范围与默认尺寸。"""
    return jsonify(
        {
            "default": list(DEFAULT_SIZES),
            "min": MIN_ICON_SIZE,
            "max": MAX_ICON_SIZE,
        }
    )


@bp.post("/api/convert")
def convert():
    """接收上传图片，返回多尺寸 ICO 附件。"""
    upload = request.files.get(UPLOAD_FIELD)
    if upload is None or not upload.filename:
        return _error(f"缺少上传文件字段 {UPLOAD_FIELD!r}", 400)

    suffix = Path(upload.filename).suffix.lower().lstrip(".")
    if suffix not in ALLOWED_EXTENSIONS:
        supported = ", ".join(sorted(ALLOWED_EXTENSIONS))
        return _error(f"不支持的图片格式: .{suffix}；支持: {supported}", 415)

    try:
        target_sizes = _parse_sizes(request.form.get("sizes"))
    except ValueError as exc:
        return _error(str(exc), 400)

    raw = upload.read()
    if not raw:
        return _error("上传文件为空", 400)

    try:
        image = _decode_image(raw)
    except ValueError as exc:
        return _error(str(exc), 400)

    # 可选：去除纯色背景（默认白色）→ 透明
    removed_ratio = 0.0
    background_note = ""
    debug_files: list = []
    if _parse_flag(request.form.get("remove_bg")):
        try:
            debug_before = image.copy() if _debug_enabled() else None
            cleanup = make_background_transparent(
                image, tolerance=_parse_tolerance(request.form.get("tolerance"))
            )
            image = cleanup.image
            # 只有真正生效时才标记"已去除背景"
            removed_ratio = cleanup.removed_ratio if cleanup.applied else 0.0
            if cleanup.skipped_reason:
                background_note = cleanup.skipped_reason
                current_app.logger.info("去背景未生效：%s", cleanup.skipped_reason)
            if debug_before is not None:
                debug_files = _dump_debug_images(debug_before, cleanup.image)
                if debug_files:
                    current_app.logger.info("去背景调试文件：%s", " | ".join(debug_files))
        except Exception:  # noqa: BLE001 - 抠图失败绝不阻断转换
            background_note = "去背景失败，已使用原图"
            current_app.logger.exception("去背景处理失败，回退原图")

    try:
        payload = convert_to_ico(image, None, sizes=target_sizes)
    except ValueError as exc:
        return _error(str(exc), 400)
    except Exception:  # pragma: no cover - 防御性兜底
        current_app.logger.exception("ICO 转换失败")
        return _error("服务器内部错误，转换失败", 500)

    response = send_file(
        io.BytesIO(payload),
        mimetype=ICO_MIMETYPE,
        as_attachment=True,
        download_name=f"{_safe_stem(upload.filename)}.ico",
        max_age=0,
    )
    response.headers["X-Icon-Sizes"] = ",".join(str(size) for size in target_sizes)
    # 不落盘：ICO 只以字节流返回，由浏览器/用户决定存到哪（不再写 IconsLibrary）
    response.headers["X-Background-Removed"] = "1" if removed_ratio > 0 else "0"
    response.headers["X-Background-Ratio"] = f"{removed_ratio:.3f}"
    if debug_files:
        try:
            from urllib.parse import quote

            response.headers["X-Debug-Files"] = quote("|".join(debug_files))
        except Exception:  # pragma: no cover
            pass
    if background_note:
        # Header 只能是 ASCII，用 URL 编码，前端按需 decodeURIComponent
        try:
            from urllib.parse import quote

            response.headers["X-Background-Note"] = quote(background_note)
        except Exception:  # pragma: no cover
            pass
    return response


def create_app(config: dict[str, Any] | None = None) -> Flask:
    """应用工厂：创建并配置 Flask 实例。

    :param config: 可选的配置覆盖（如测试里缩小 ``MAX_CONTENT_LENGTH``）。
    """
    app = Flask(__name__)
    app.json.ensure_ascii = False
    app.config.update(
        MAX_CONTENT_LENGTH=int(
            os.environ.get("ICONS_MAX_UPLOAD_BYTES", DEFAULT_MAX_UPLOAD_BYTES)
        ),
    )
    if config:
        app.config.update(config)

    app.register_blueprint(bp)
    app.register_blueprint(batch_bp)

    @app.errorhandler(RequestEntityTooLarge)
    def _handle_too_large(_exc):  # pragma: no cover - 由 Flask 内部调用
        limit = app.config.get("MAX_CONTENT_LENGTH")
        return _error(f"上传文件过大，上限 {limit} 字节", 413)

    return app


app = create_app()


if __name__ == "__main__":  # pragma: no cover - 手动启动入口
    # 桌面模式：后台线程跑 Flask + pywebview 独立窗口，实现在 src/ui_window.py。
    # 仅想用浏览器调试时，可执行： flask --app iconswap.app run --port 5000
    import sys

    from ui_window import main as _desktop_main

    sys.exit(_desktop_main())
