"""app.py（Flask 入口）的接口测试。

运行方式（项目根目录）::

    pytest tests\\test_app.py -v

全部用例使用 Flask 自带的 ``test_client``，不启动真实服务器、不占用端口。
"""

from __future__ import annotations

import io
import struct
import sys
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
for _entry in (str(SRC), str(ROOT)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from iconswap.app import ALLOWED_EXTENSIONS, create_app  # noqa: E402
from iconswap.ico_converter import DEFAULT_SIZES  # noqa: E402

ICO_HEADER = b"\x00\x00\x01\x00"


# --------------------------------------------------------------------------- #
# 夹具与辅助
# --------------------------------------------------------------------------- #
@pytest.fixture()
def client():
    app = create_app({"TESTING": True})
    with app.test_client() as test_client:
        yield test_client


def _image_bytes(size=(256, 256), fmt="PNG", color="red", mode="RGBA") -> bytes:
    buffer = io.BytesIO()
    Image.new(mode, size, color).save(buffer, format=fmt)
    return buffer.getvalue()


def _ico_widths(data: bytes) -> list[int]:
    assert data[:4] == ICO_HEADER, "ICO 文件头不正确"
    (count,) = struct.unpack("<H", data[4:6])
    widths = []
    for index in range(count):
        start = 6 + index * 16
        width = struct.unpack_from("<B", data, start)[0]
        widths.append(width or 256)
    return widths


def _post(
    client,
    content: bytes,
    filename: str = "logo.png",
    field: str = "file",
    sizes: str | None = None,
):
    data: dict = {field: (io.BytesIO(content), filename)}
    if sizes is not None:
        data["sizes"] = sizes
    return client.post("/api/convert", data=data, content_type="multipart/form-data")


def _snapshot() -> set[Path]:
    """项目内所有文件的相对路径快照（忽略解释器缓存与测试临时目录）。"""
    files = set()
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        parts = set(path.relative_to(ROOT).parts)
        if "__pycache__" in parts or ".pytest_tmp" in parts or "_tmp" in parts:
            continue
        files.add(path.relative_to(ROOT))
    return files


# --------------------------------------------------------------------------- #
# 基础端点
# --------------------------------------------------------------------------- #
def test_index_serves_upload_page(client):
    response = client.get("/")

    assert response.status_code == 200
    assert response.mimetype == "text/html"
    body = response.data
    assert b"IconSwap" in body
    assert b'id="dropzone"' in body
    assert b'id="convert"' in body
    assert body.count(b'name="sizes"') == len(DEFAULT_SIZES)
    assert b'name="sizes"' in body
    assert "8.0 MB".encode() in body or b"MB" in body


def test_index_page_has_no_external_assets(client):
    body = client.get("/").data

    assert b"cdn." not in body
    assert b"googleapis" not in body
    assert b'src="http' not in body
    assert b'href="http' not in body


def test_static_assets_are_served(client):
    css = client.get("/static/css/style.css")
    js = client.get("/static/js/app.js")

    assert css.status_code == 200
    assert b"--accent" in css.data
    assert js.status_code == 200
    assert b"/api/convert" in js.data


def test_api_info_returns_service_info(client):
    response = client.get("/api")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["app"] == "IconSwap"
    assert "POST /api/convert" in payload["endpoints"]
    assert set(payload["supported_formats"]) == set(ALLOWED_EXTENSIONS)


def test_health(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


def test_sizes_info(client):
    response = client.get("/api/sizes")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["default"] == list(DEFAULT_SIZES)
    assert payload["min"] == 1
    assert payload["max"] == 256


# --------------------------------------------------------------------------- #
# 关于 / 第三方组件
# --------------------------------------------------------------------------- #
def test_about_endpoint_lists_components(client):
    response = client.get("/api/about")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["name"] == "IconSwap"
    assert payload["version"]
    assert payload["notice"]

    names = {item["name"] for item in payload["components"]}
    assert {"rcedit", "pywebview", "Flask", "Pillow", "pefile"}.issubset(names)
    rcedit = next(item for item in payload["components"] if item["name"] == "rcedit")
    assert rcedit["license"] == "MIT"
    assert rcedit["url"].startswith("https://github.com/electron/rcedit")


def test_page_has_about_entry(client):
    body = client.get("/").data

    assert b'id="about-open"' in body
    assert b'id="about-modal"' in body
    assert b"js/about.js" in body


def test_about_asset_is_served(client):
    response = client.get("/static/js/about.js")

    assert response.status_code == 200
    assert b"/api/about" in response.data


# --------------------------------------------------------------------------- #
# 转换成功路径
# --------------------------------------------------------------------------- #
def test_convert_with_default_sizes(client):
    response = _post(client, _image_bytes())

    assert response.status_code == 200
    assert response.mimetype == "image/x-icon"
    assert response.data[:4] == ICO_HEADER
    assert _ico_widths(response.data) == list(DEFAULT_SIZES)
    assert response.headers["X-Icon-Sizes"] == ",".join(str(s) for s in DEFAULT_SIZES)
    assert "attachment" in response.headers["Content-Disposition"]
    assert "logo.ico" in response.headers["Content-Disposition"]


def test_convert_with_custom_sizes(client):
    response = _post(client, _image_bytes(), sizes="16, 32,48")

    assert response.status_code == 200
    assert _ico_widths(response.data) == [16, 32, 48]
    assert response.headers["X-Icon-Sizes"] == "16,32,48"


def test_convert_accepts_other_formats(client):
    png = _image_bytes()
    jpeg = _image_bytes(fmt="JPEG", mode="RGB")
    bmp = _image_bytes(fmt="BMP", mode="RGB")

    for content, name in ((png, "a.png"), (jpeg, "b.jpg"), (bmp, "c.bmp")):
        response = _post(client, content, filename=name, sizes="16")
        assert response.status_code == 200, name
        assert response.data[:4] == ICO_HEADER, name


def test_convert_result_is_valid_ico(client):
    response = _post(client, _image_bytes(), sizes="16,32,48")

    with Image.open(io.BytesIO(response.data)) as icon:
        assert icon.format == "ICO"
        assert icon.size[0] in (16, 32, 48)


def test_convert_non_square_upload_produces_square_frames(client):
    response = _post(client, _image_bytes(size=(200, 120)), sizes="32")

    assert response.status_code == 200
    (width,) = _ico_widths(response.data)
    assert width == 32


def test_convert_sanitizes_download_name(client):
    response = _post(client, _image_bytes(), filename="..\\..\\evil.png", sizes="16")

    assert response.status_code == 200
    disposition = response.headers["Content-Disposition"]
    assert "evil.ico" in disposition
    assert ".." not in disposition


def test_convert_writes_nothing_to_disk(client):
    """转换只在内存里完成：不落 IconsLibrary，也不往项目目录写任何文件。"""
    before_project = _snapshot()

    response = _post(client, _image_bytes(), sizes="16,32")

    assert response.status_code == 200
    assert _snapshot() == before_project, "转换响应不应在磁盘上留下文件"
    assert "X-Icon-Path" not in response.headers, "图标库已移除，不应再返回落盘路径"


def test_convert_without_file_returns_400(client):
    response = client.post("/api/convert", data={}, content_type="multipart/form-data")

    assert response.status_code == 400
    assert "error" in response.get_json()


def test_convert_with_wrong_field_name_returns_400(client):
    response = _post(client, _image_bytes(), field="image")

    assert response.status_code == 400
    assert "file" in response.get_json()["error"]


def test_convert_empty_file_returns_400(client):
    response = _post(client, b"")

    assert response.status_code == 400
    assert "空" in response.get_json()["error"]


def test_convert_unsupported_extension_returns_415(client):
    response = _post(client, _image_bytes(), filename="note.txt")

    assert response.status_code == 415
    assert "不支持的图片格式" in response.get_json()["error"]


def test_convert_corrupt_image_returns_400(client):
    response = _post(client, b"this is not an image", filename="broken.png")

    assert response.status_code == 400
    assert "图片" in response.get_json()["error"]


@pytest.mark.parametrize("bad_sizes", ["abc", "16,abc", "0", "512", "-8", "1.5"])
def test_convert_invalid_sizes_returns_400(client, bad_sizes):
    response = _post(client, _image_bytes(), sizes=bad_sizes)

    assert response.status_code == 400
    assert "error" in response.get_json()


def test_convert_too_large_returns_413():
    app = create_app({"TESTING": True, "MAX_CONTENT_LENGTH": 1024})
    with app.test_client() as limited:
        response = _post(limited, _image_bytes(size=(512, 512)), sizes="16")

    assert response.status_code == 413
    assert "过大" in response.get_json()["error"]

# --------------------------------------------------------------------------- #
# 功能区 1：去白边（转换接口）
# --------------------------------------------------------------------------- #
def test_convert_removes_white_background(client):
    from PIL import ImageDraw

    image = Image.new("RGB", (128, 128), (255, 255, 255))
    ImageDraw.Draw(image).rectangle((40, 40, 88, 88), fill=(0, 128, 255))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")

    response = client.post(
        "/api/convert",
        data={
            "file": (io.BytesIO(buffer.getvalue()), "logo.png"),
            "sizes": "64,128",
            "remove_bg": "1",
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    assert response.headers["X-Background-Removed"] == "1"
    ico = Image.open(io.BytesIO(response.data))
    ico.load()
    rgba = ico.convert("RGBA")
    assert rgba.getpixel((0, 0))[3] == 0        # 白底角 → 透明
    assert rgba.getpixel((rgba.width // 2, rgba.height // 2))[3] == 255


def test_convert_without_remove_bg_keeps_opaque_corner(client):
    image = Image.new("RGB", (64, 64), (255, 255, 255))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")

    response = client.post(
        "/api/convert",
        data={"file": (io.BytesIO(buffer.getvalue()), "white.png")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    assert response.headers["X-Background-Removed"] == "0"


def test_convert_remove_bg_on_all_white_falls_back(client):
    """整张纯白：去背景应被拒绝，但仍要成功返回图标。"""
    image = Image.new("RGB", (64, 64), (255, 255, 255))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")

    response = client.post(
        "/api/convert",
        data={
            "file": (io.BytesIO(buffer.getvalue()), "white.png"),
            "remove_bg": "1",
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    assert response.headers["X-Background-Removed"] == "0"
    assert response.data[:4] == ICO_HEADER

def test_convert_debug_mode_dumps_before_after(client, monkeypatch):
    """ICONSWAP_DEBUG_BG=1 时应导出处理前后图片，便于肉眼对比。"""
    from urllib.parse import unquote

    from PIL import ImageDraw

    monkeypatch.setenv("ICONSWAP_DEBUG_BG", "1")
    image = Image.new("RGB", (64, 64), (255, 255, 255))
    ImageDraw.Draw(image).rectangle((16, 16, 48, 48), fill=(0, 0, 0))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")

    response = client.post(
        "/api/convert",
        data={"file": (io.BytesIO(buffer.getvalue()), "x.png"), "remove_bg": "1"},
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    header = response.headers.get("X-Debug-Files")
    assert header
    paths = unquote(header).split("|")
    assert len(paths) == 2
    for path in paths:
        assert Path(path).is_file()
