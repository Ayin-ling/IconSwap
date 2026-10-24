"""多尺寸 ICO 图标转换工具（IconSwap 核心工具模块）。

职责
----
把一张位图（PNG / JPEG / BMP / WebP / GIF 等 Pillow 支持的格式）转换成包含
多个目标尺寸帧的 Windows ICO 图标文件。

约定
----
* 非正方形输入：不改变宽高比，先把图像居中放置到透明正方形画布上（四周补
  透明像素），再统一缩放。
* 目标尺寸：默认 ``(16, 24, 32, 48, 64, 128, 256)``，允许 1-256 之间的整数。
* 缩放策略：先保证底图不短于最大目标尺寸（源图过小时用 LANCZOS 放大），
  之后每个尺寸都由该底图缩小得到，保证 ICO 内每一帧都是真实的目标尺寸，
  而不是 Pillow ``thumbnail`` 的"不放大"结果。
* 输出路径：``None`` 时只返回内存中的 ICO 字节，不落盘；后缀不是 ``.ico``
  时自动补全或纠正为 ``.ico``。

本模块不依赖 Flask，可单独被 CLI / Web 层调用。
"""

from __future__ import annotations

import io
import os
import struct
from pathlib import Path
from typing import Any, Iterable, Union

from PIL import Image, UnidentifiedImageError

__all__ = [
    "DEFAULT_SIZES",
    "MAX_ICON_SIZE",
    "MIN_ICON_SIZE",
    "ICO_SUFFIX",
    "normalize_sizes",
    "to_square",
    "ico_bytes",
    "build_ico",
    "convert_to_ico",
]

#: 默认生成的多尺寸列表（Windows 图标常见规格）
DEFAULT_SIZES: tuple[int, ...] = (16, 24, 32, 48, 64, 128, 256)

#: ICO 规范允许的单边尺寸范围
MIN_ICON_SIZE = 1
MAX_ICON_SIZE = 256

ICO_SUFFIX = ".ico"

#: 源图最长边上限，避免一次性解码超大图占满内存
MAX_SOURCE_EDGE = 4096

_TRANSPARENT = (0, 0, 0, 0)
_RESAMPLE = Image.Resampling.LANCZOS


def normalize_sizes(sizes: Union[Iterable[int], None] = None) -> tuple[int, ...]:
    """校验并规范化目标尺寸列表。

    去重并按升序返回。``None`` 表示使用 :data:`DEFAULT_SIZES`。

    :raises ValueError: 传入空序列、非整数、布尔值或超出 1-256 的尺寸。
    """
    if sizes is None:
        return tuple(sorted(DEFAULT_SIZES))
    if isinstance(sizes, (str, bytes)):
        raise ValueError(f"sizes 必须是整数序列，收到字符串 {sizes!r}")

    try:
        raw = list(sizes)
    except TypeError as exc:  # 不可迭代
        raise ValueError(f"sizes 必须是整数序列，收到 {sizes!r}") from exc

    if not raw:
        raise ValueError("sizes 不能为空，至少需要一个目标尺寸")

    cleaned: list[int] = []
    for item in raw:
        if isinstance(item, bool) or not isinstance(item, int):
            raise ValueError(f"图标尺寸必须是整数，收到 {item!r}")
        if not MIN_ICON_SIZE <= item <= MAX_ICON_SIZE:
            raise ValueError(
                f"图标尺寸必须位于 {MIN_ICON_SIZE}-{MAX_ICON_SIZE} 之间，收到 {item}"
            )
        cleaned.append(item)
    return tuple(sorted(set(cleaned)))


def to_square(image: Image.Image, *, max_edge: int = MAX_SOURCE_EDGE) -> Image.Image:
    """把任意位图转换成 RGBA 正方形图，非正方形部分补透明像素。

    :param image: 任意 Pillow 图像。
    :param max_edge: 最长边上限，超出时先等比缩小。
    :returns: 新的正方形 RGBA 图像，原图不被修改。
    :raises TypeError: ``image`` 不是 :class:`PIL.Image.Image`。
    :raises ValueError: 图像尺寸非法。
    """
    if not isinstance(image, Image.Image):
        raise TypeError(f"image 必须是 PIL.Image.Image，收到 {type(image).__name__}")

    img = image.convert("RGBA")

    width, height = img.size
    if width <= 0 or height <= 0:
        raise ValueError(f"图像尺寸非法: {width}x{height}")

    edge = max(width, height)
    if edge > max_edge:
        ratio = max_edge / float(edge)
        img = img.resize(
            (max(1, round(width * ratio)), max(1, round(height * ratio))), _RESAMPLE
        )
        width, height = img.size
        edge = max(width, height)

    if width == height:
        return img

    canvas = Image.new("RGBA", (edge, edge), _TRANSPARENT)
    canvas.paste(img, ((edge - width) // 2, (edge - height) // 2))
    return canvas


def _load_image(source: Any) -> Image.Image:
    """从路径或 Pillow 图像载入一份独立的 RGBA 源图副本。"""
    if isinstance(source, Image.Image):
        return source.copy()

    try:
        path = Path(os.fspath(source))
    except TypeError as exc:
        raise TypeError(
            f"source 必须是路径(str/pathlib.Path)或 PIL.Image.Image，"
            f"收到 {type(source).__name__}"
        ) from exc

    if not path.exists():
        raise FileNotFoundError(f"源图片不存在: {path}")
    if not path.is_file():
        raise ValueError(f"源路径不是文件: {path}")

    try:
        with Image.open(path) as im:
            im.load()
            return im.copy()
    except UnidentifiedImageError as exc:
        raise ValueError(f"无法识别的图片格式: {path}") from exc
    except OSError as exc:
        raise ValueError(f"图片读取失败: {path} ({exc})") from exc


#: 超过该尺寸的帧用 PNG 压缩（体积小、Windows 支持），其它尺寸必须用 DIB，
#: 否则资源管理器会忽略该帧、回退显示默认图标。
PNG_FRAME_MIN_SIZE = 256


def _dib_frame(image: Image.Image, size: int) -> bytes:
    """把图像编码成 ICO 内嵌的 DIB（BMP）帧。

    Windows 外壳在 16/32/48 这类小尺寸上**只认 DIB**；Pillow 对带 alpha 的图会
    一律写成 PNG，导致快捷方式图标显示成默认图标。这里手动按规范编码：

    ``BITMAPINFOHEADER``（高度为 2*size，含 XOR + AND 掩码）+ 自下而上的
    32 位 BGRA 像素 + 1 位 AND 掩码（每行按 4 字节对齐）。
    """
    frame = image if image.size == (size, size) else image.resize((size, size), _RESAMPLE)
    rgba = frame.convert("RGBA")
    pixels = rgba.load()

    mask_row = ((size + 31) // 32) * 4
    mask_size = mask_row * size
    xor_size = size * size * 4

    header = struct.pack(
        "<IiiHHIIiiII",
        40,          # biSize
        size,        # biWidth
        size * 2,    # biHeight（XOR + AND）
        1,           # biPlanes
        32,          # biBitCount
        0,           # biCompression = BI_RGB
        xor_size + mask_size,
        0, 0, 0, 0,
    )

    xor = bytearray(xor_size)
    mask = bytearray(mask_size)
    for y in range(size):
        src_y = size - 1 - y          # BMP 自下而上存储
        row = y * size * 4
        mask_base = y * mask_row
        for x in range(size):
            red, green, blue, alpha = pixels[x, src_y]
            index = row + x * 4
            xor[index] = blue
            xor[index + 1] = green
            xor[index + 2] = red
            xor[index + 3] = alpha
            if alpha == 0:
                mask[mask_base + (x >> 3)] |= 0x80 >> (x & 7)
    return header + bytes(xor) + bytes(mask)


def _png_frame(image: Image.Image, size: int) -> bytes:
    """大尺寸帧用 PNG（256x256 的 DIB 会让文件膨胀到 260KB）。"""
    frame = image if image.size == (size, size) else image.resize((size, size), _RESAMPLE)
    buffer = io.BytesIO()
    frame.convert("RGBA").save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def build_ico(image: Image.Image, sizes: Iterable[int]) -> bytes:
    """按 Windows 兼容性要求封装多尺寸 ICO。

    * 小于 :data:`PNG_FRAME_MIN_SIZE` 的帧用 DIB（外壳必须能读）
    * 256 帧用 PNG（体积小，外壳同样支持）
    """
    ordered = sorted(set(sizes))
    frames: list[tuple[int, bytes]] = []
    for size in ordered:
        payload = (
            _png_frame(image, size) if size >= PNG_FRAME_MIN_SIZE else _dib_frame(image, size)
        )
        frames.append((size, payload))

    header = struct.pack("<HHH", 0, 1, len(frames))
    offset = 6 + 16 * len(frames)
    entries = bytearray()
    payloads = bytearray()
    for size, payload in frames:
        width = 0 if size >= 256 else size   # 256 在目录项里记 0
        entries += struct.pack(
            "<BBBBHHII", width, width, 0, 0, 1, 32, len(payload), offset
        )
        payloads += payload
        offset += len(payload)
    return header + bytes(entries) + bytes(payloads)


def ico_bytes(source: Any, *, sizes: Union[Iterable[int], None] = None) -> bytes:
    """把源图编码成多尺寸 ICO 字节串（不落盘）。

    :param source: 图片路径或 :class:`PIL.Image.Image`。
    :param sizes: 目标尺寸序列，缺省用 :data:`DEFAULT_SIZES`。
    :raises FileNotFoundError: 源文件不存在。
    :raises ValueError: 源文件不是可识别图片、尺寸非法。
    """
    target_sizes = normalize_sizes(sizes)
    base = to_square(_load_image(source))

    largest = max(target_sizes)
    if base.width < largest:
        base = base.resize((largest, largest), _RESAMPLE)

    data = build_ico(base, target_sizes)
    if not data:  # pragma: no cover
        raise ValueError("ICO 编码结果为空")
    return data


def _resolve_output_path(output: Any) -> Path:
    """规范化输出路径：拒绝已存在的目录，后缀统一为 ``.ico``。"""
    try:
        path = Path(os.fspath(output))
    except TypeError as exc:
        raise TypeError(
            f"output 必须是路径(str/pathlib.Path)，收到 {type(output).__name__}"
        ) from exc

    if path.is_dir():
        raise ValueError(f"output 必须是文件路径，不能是已存在的目录: {path}")
    if path.suffix.lower() != ICO_SUFFIX:
        path = path.with_suffix(ICO_SUFFIX)
    return path


def convert_to_ico(
    source: Any,
    output: Any = None,
    *,
    sizes: Union[Iterable[int], None] = None,
    overwrite: bool = True,
) -> Union[Path, bytes]:
    """把图片转换成多尺寸 ICO。

    :param source: 图片路径或 :class:`PIL.Image.Image`。
    :param output: 输出文件路径；``None`` 时只返回 ICO 字节，不写磁盘。
    :param sizes: 目标尺寸序列，缺省用 :data:`DEFAULT_SIZES`。
    :param overwrite: ``False`` 且目标文件已存在时抛 :class:`FileExistsError`。
    :returns: 写入成功返回输出路径 :class:`pathlib.Path`；``output=None``
        时返回 ICO 字节串。
    :raises FileNotFoundError: 源文件不存在。
    :raises FileExistsError: ``overwrite=False`` 且目标已存在。
    :raises ValueError: 图片无法识别、尺寸非法、输出路径非法。
    """
    target_sizes = normalize_sizes(sizes)
    data = ico_bytes(source, sizes=target_sizes)

    if output is None:
        return data

    out_path = _resolve_output_path(output)
    if out_path.exists() and not overwrite:
        raise FileExistsError(f"目标文件已存在: {out_path}")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(data)
    return out_path
