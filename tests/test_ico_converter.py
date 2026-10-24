"""utils.ico_converter 的单元测试。

运行方式（项目根目录）::

    pytest tests\\test_ico_converter.py -v

说明：本套件不使用 pytest 内置的 ``tmp_path`` 夹具。``tmp_path`` 依赖
``tempfile.mkdtemp``（目录模式 0o700），在该沙箱环境下目录创建后内部无法
再写入文件，因此改用自带 ``workdir`` 夹具：在项目内 ``.pytest_tmp`` 下用
``Path.mkdir`` 建目录，用例结束自动清理。
"""

from __future__ import annotations

import shutil
import struct
import sys
import uuid
from pathlib import Path

import pytest
from PIL import Image

# 项目根目录不在 tests/ 下，pytest 的 prepend 导入模式只会把 tests/ 加进
# sys.path，这里显式把根目录补上，保证 `import iconswap.ico_converter` 可用。
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from iconswap.ico_converter import (  # noqa: E402  (需先设置 sys.path)
    DEFAULT_SIZES,
    MAX_ICON_SIZE,
    convert_to_ico,
    ico_bytes,
    normalize_sizes,
    to_square,
)

ICO_HEADER = b"\x00\x00\x01\x00"
TEMP_ROOT = ROOT / ".pytest_tmp"


# --------------------------------------------------------------------------- #
# 夹具与测试辅助
# --------------------------------------------------------------------------- #
@pytest.fixture()
def workdir():
    """每个用例独立的临时目录（项目内创建，用例结束删除）。"""
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    directory = TEMP_ROOT / f"case-{uuid.uuid4().hex[:12]}"
    directory.mkdir(parents=True, exist_ok=True)
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture(scope="session", autouse=True)
def _cleanup_temp_root():
    yield
    shutil.rmtree(TEMP_ROOT, ignore_errors=True)


def _make_png(path: Path, size: tuple[int, int], color="red", mode="RGBA") -> Path:
    """生成一张纯色测试 PNG 并返回路径。"""
    Image.new(mode, size, color).save(path, format="PNG")
    return path


def _ico_entries(data: bytes) -> list[dict]:
    """解析 ICO 目录，返回每个帧的宽/高/字节长度，顺带校验文件结构。"""
    assert data[:4] == ICO_HEADER, "ICO 文件头不正确"
    (count,) = struct.unpack("<H", data[4:6])
    assert count >= 1, "ICO 至少应包含一个帧"

    entries: list[dict] = []
    for index in range(count):
        start = 6 + index * 16
        assert start + 16 <= len(data), "ICO 目录项被截断"
        width, height, _colors, _reserved, _planes, _bpp, length, offset = (
            struct.unpack_from("<BBBBHHII", data, start)
        )
        assert length > 0, f"第 {index} 帧长度为 0"
        assert offset + length <= len(data), f"第 {index} 帧数据越界"
        entries.append(
            {"width": width or 256, "height": height or 256, "length": length}
        )
    return entries


def _ico_widths(data: bytes) -> list[int]:
    return [entry["width"] for entry in _ico_entries(data)]


# --------------------------------------------------------------------------- #
# normalize_sizes
# --------------------------------------------------------------------------- #
def test_default_sizes_are_used_when_none():
    assert normalize_sizes() == DEFAULT_SIZES
    assert normalize_sizes(None) == tuple(sorted(DEFAULT_SIZES))


def test_normalize_sizes_sorts_and_dedupes():
    assert normalize_sizes([32, 16, 16, 256]) == (16, 32, 256)
    assert normalize_sizes((48,)) == (48,)
    assert normalize_sizes([1, MAX_ICON_SIZE]) == (1, 256)


@pytest.mark.parametrize(
    "bad_sizes",
    [
        [],
        (),
        -1,
        0,
        257,
        3.5,
        "32",
        "16,32",
        True,
        32,  # 单个整数不是序列
        [16, 300],
    ],
)
def test_normalize_sizes_rejects_invalid_input(bad_sizes):
    with pytest.raises(ValueError):
        normalize_sizes(bad_sizes)


# --------------------------------------------------------------------------- #
# to_square
# --------------------------------------------------------------------------- #
def test_to_square_pads_wide_image_with_transparent_pixels():
    source = Image.new("RGBA", (200, 120), (255, 0, 0, 255))
    result = to_square(source)

    assert result.size == (200, 200)
    assert result.mode == "RGBA"
    # 上下各留 40px 透明边，内容位于 40..159 行
    assert result.getpixel((100, 0))[3] == 0
    assert result.getpixel((100, 39))[3] == 0
    assert result.getpixel((100, 40))[3] == 255
    assert result.getpixel((100, 159))[3] == 255
    assert result.getpixel((100, 199))[3] == 0


def test_to_square_pads_tall_image_horizontally():
    # 100x121 时正方形边长为 121，左右各补 10/11 像素
    source = Image.new("RGBA", (100, 121), (0, 0, 0, 255))
    result = to_square(source)

    assert result.size == (121, 121)
    assert result.getpixel((0, 60))[3] == 0
    assert result.getpixel((9, 60))[3] == 0
    assert result.getpixel((10, 60))[3] == 255
    assert result.getpixel((109, 60))[3] == 255
    assert result.getpixel((110, 60))[3] == 0
    assert result.getpixel((120, 60))[3] == 0


def test_to_square_keeps_square_image_and_does_not_mutate_source():
    source = Image.new("RGB", (64, 64), (0, 128, 255))
    result = to_square(source)

    assert result.size == (64, 64)
    assert result.mode == "RGBA"
    assert result is not source
    assert result.getpixel((10, 10))[:3] == (0, 128, 255)
    assert source.mode == "RGB"  # 原图未被转换


def test_to_square_rejects_non_image():
    with pytest.raises(TypeError):
        to_square("not-an-image")


# --------------------------------------------------------------------------- #
# ico_bytes
# --------------------------------------------------------------------------- #
def test_ico_bytes_contains_exactly_requested_sizes(workdir):
    source = _make_png(workdir / "logo.png", (256, 256))
    data = ico_bytes(source, sizes=(16, 32, 48, 256))

    assert data[:4] == ICO_HEADER
    assert _ico_widths(data) == [16, 32, 48, 256]
    for entry in _ico_entries(data):
        assert entry["height"] == entry["width"]


def test_ico_bytes_from_small_source_upscales_to_every_size(workdir):
    source = _make_png(workdir / "tiny.png", (32, 32))
    data = ico_bytes(source, sizes=(16, 32, 64))

    assert _ico_widths(data) == [16, 32, 64]


def test_ico_bytes_non_square_source_produces_square_frames(workdir):
    source = _make_png(workdir / "wide.png", (200, 120))
    data = ico_bytes(source, sizes=(16, 32))

    for entry in _ico_entries(data):
        assert entry["width"] == entry["height"]


def test_ico_bytes_accepts_pil_image():
    data = ico_bytes(Image.new("RGB", (50, 80), "blue"), sizes=(16, 32))
    assert _ico_widths(data) == [16, 32]


def test_ico_bytes_missing_source_raises(workdir):
    with pytest.raises(FileNotFoundError):
        ico_bytes(workdir / "nope.png")


def test_ico_bytes_non_image_file_raises_value_error(workdir):
    bogus = workdir / "notes.png"
    bogus.write_text("这不是图片", encoding="utf-8")
    with pytest.raises(ValueError):
        ico_bytes(bogus)


def test_ico_bytes_rejects_directory_as_source(workdir):
    folder = workdir / "folder.png"
    folder.mkdir()
    with pytest.raises(ValueError):
        ico_bytes(folder)


def test_ico_bytes_rejects_unsupported_source_type():
    with pytest.raises(TypeError):
        ico_bytes(12345)


# --------------------------------------------------------------------------- #
# convert_to_ico
# --------------------------------------------------------------------------- #
def test_convert_to_ico_writes_file_matching_ico_bytes(workdir):
    source = _make_png(workdir / "icon.png", (300, 300))
    target = workdir / "out" / "icon.ico"

    result = convert_to_ico(source, target, sizes=(16, 48, 256))

    assert isinstance(result, Path)
    assert result == target
    assert target.is_file()
    assert target.stat().st_size > 0
    data = target.read_bytes()
    assert data == ico_bytes(source, sizes=(16, 48, 256))
    assert _ico_widths(data) == [16, 48, 256]


def test_convert_to_ico_creates_missing_directories(workdir):
    source = _make_png(workdir / "a.png", (64, 64))
    target = workdir / "deep" / "nested" / "dir" / "a.ico"

    result = convert_to_ico(source, target, sizes=(16, 32))

    assert target.parent.is_dir()
    assert result.exists()


def test_convert_to_ico_fixes_wrong_suffix(workdir):
    source = _make_png(workdir / "banner.png", (128, 128))
    requested = workdir / "banner.out.png"

    result = convert_to_ico(source, requested, sizes=(32,))

    assert result.suffix == ".ico"
    assert result.name == "banner.out.ico"
    assert result.is_file()
    assert not requested.exists()


def test_convert_to_ico_without_output_returns_bytes(workdir):
    source = _make_png(workdir / "mem.png", (48, 48))
    payload = convert_to_ico(source, None, sizes=(16, 32))

    assert isinstance(payload, bytes)
    assert payload[:4] == ICO_HEADER
    assert not (workdir / "mem.ico").exists()


def test_convert_to_ico_overwrite_false_raises(workdir):
    source = _make_png(workdir / "c.png", (64, 64))
    target = workdir / "c.ico"
    convert_to_ico(source, target, sizes=(16,))

    with pytest.raises(FileExistsError):
        convert_to_ico(source, target, sizes=(16,), overwrite=False)


def test_convert_to_ico_overwrite_true_replaces_file(workdir):
    source = _make_png(workdir / "d.png", (64, 64))
    target = workdir / "d.ico"

    convert_to_ico(source, target, sizes=(16,))
    first = target.read_bytes()
    convert_to_ico(source, target, sizes=(16, 32))
    second = target.read_bytes()

    assert first != second
    assert _ico_widths(second) == [16, 32]


def test_convert_to_ico_rejects_directory_output(workdir):
    source = _make_png(workdir / "e.png", (64, 64))
    folder = workdir / "as-dir.ico"
    folder.mkdir()

    with pytest.raises(ValueError):
        convert_to_ico(source, folder, sizes=(16,))


def test_convert_to_ico_result_is_openable_by_pillow(workdir):
    source = _make_png(workdir / "f.png", (256, 256))
    target = convert_to_ico(source, workdir / "f.ico", sizes=(16, 32, 48))

    with Image.open(target) as icon:
        assert icon.format == "ICO"
        assert icon.size[0] in (16, 32, 48)


def test_convert_to_ico_preserves_source_file_untouched(workdir):
    source = _make_png(workdir / "g.png", (100, 100))
    before = source.read_bytes()

    convert_to_ico(source, workdir / "g.ico", sizes=(32,))

    assert source.read_bytes() == before

# --------------------------------------------------------------------------- #
# ICO 帧格式：小尺寸必须是 DIB（否则资源管理器显示默认图标）
# --------------------------------------------------------------------------- #
def _frame_kinds(data: bytes) -> list[str]:
    import struct

    count = struct.unpack("<H", data[4:6])[0]
    offset = 6
    kinds = []
    for _ in range(count):
        _w, _h, _c, _r, _p, _b, size, start = struct.unpack("<BBBBHHII", data[offset:offset + 16])
        offset += 16
        frame = data[start:start + size]
        if frame[:8] == b"\x89PNG\r\n\x1a\n":
            kinds.append("PNG")
        elif frame[:4] == b"\x28\x00\x00\x00":
            kinds.append("DIB")
        else:
            kinds.append("?")
    return kinds


def test_small_frames_use_dib_and_large_uses_png():
    from PIL import Image

    data = ico_bytes(Image.new("RGBA", (256, 256), (249, 115, 22, 255)), sizes=(16, 48, 256))

    kinds = _frame_kinds(data)
    assert kinds[0] == "DIB"     # 16
    assert kinds[1] == "DIB"     # 48
    assert kinds[2] == "PNG"     # 256


def test_dib_frame_preserves_transparency():
    import io

    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    ImageDraw.Draw(image).ellipse((8, 8, 56, 56), fill=(0, 128, 255, 255))
    data = ico_bytes(image, sizes=(32,))

    decoded = Image.open(io.BytesIO(data))
    decoded.load()
    rgba = decoded.convert("RGBA")
    assert rgba.getpixel((0, 0))[3] == 0        # 透明角
    assert rgba.getpixel((16, 16))[3] == 255    # 实心圆


def test_icon_parts_parses_path_and_index():
    from iconswap.shortcut_tools import icon_parts

    expected = icon_parts(r"C:\tools\a.ico, 0")
    assert expected[1] == 0
    assert icon_parts(r"C:\tools\a.ico")[0] == expected[0]
    assert icon_parts(r"c:/tools/a.ico,3")[1] == 3
    assert icon_parts("") == ("", 0)
