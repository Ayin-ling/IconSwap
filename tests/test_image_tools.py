"""去白边（背景转透明）的单元测试。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from iconswap.image_tools import (  # noqa: E402
    DEFAULT_TOLERANCE,
    estimate_background_color,
    make_background_transparent,
)


def _white_with_block(color=(220, 30, 30)) -> Image.Image:
    image = Image.new("RGB", (200, 200), (255, 255, 255))
    ImageDraw.Draw(image).rectangle((60, 60, 140, 140), fill=color)
    return image


# --------------------------------------------------------------------------- #
# 基本行为
# --------------------------------------------------------------------------- #
def test_white_background_becomes_transparent():
    result = make_background_transparent(_white_with_block())

    assert result.applied is True
    assert result.background == (255, 255, 255)
    image = result.image
    assert image.mode == "RGBA"
    assert image.getpixel((0, 0))[3] == 0        # 角落 → 透明
    assert image.getpixel((199, 199))[3] == 0
    assert image.getpixel((100, 100))[3] == 255  # 前景 → 不透明
    assert image.getpixel((100, 100))[:3] == (220, 30, 30)
    assert result.removed_ratio > 0.5


def test_estimate_background_uses_four_corners():
    image = Image.new("RGB", (10, 10), (255, 255, 255))
    ImageDraw.Draw(image).point((0, 0), fill=(255, 255, 255))
    ImageDraw.Draw(image).point((9, 0), fill=(240, 240, 240))
    ImageDraw.Draw(image).point((0, 9), fill=(230, 230, 230))
    ImageDraw.Draw(image).point((9, 9), fill=(255, 255, 255))

    background = estimate_background_color(image)

    # 四角排序后取中位数：230,240,255,255 → 255（比平均值更抗单点污染）
    assert background == (255, 255, 255)


def test_tolerance_boundary_is_inclusive():
    # 背景 255；像素 255-30=225 恰好等于容差 → 透明；224 → 保留
    image = Image.new("RGB", (40, 40), (255, 255, 255))
    ImageDraw.Draw(image).rectangle((10, 10, 19, 19), fill=(225, 225, 225))
    ImageDraw.Draw(image).rectangle((20, 20, 29, 29), fill=(224, 224, 224))

    result = make_background_transparent(image, tolerance=DEFAULT_TOLERANCE)

    assert result.image.getpixel((15, 15))[3] == 0    # 差值 30 → 透明
    assert result.image.getpixel((25, 25))[3] == 255  # 差值 31 → 保留


def test_rgba_transparency_is_preserved():
    image = Image.new("RGBA", (60, 60), (255, 255, 255, 255))
    ImageDraw.Draw(image).ellipse((10, 10, 50, 50), fill=(0, 128, 255, 255))
    image.putpixel((0, 0), (255, 255, 255, 40))  # 本来就半透明的背景像素

    result = make_background_transparent(image)

    assert result.image.getpixel((0, 0))[3] == 0     # 背景被清透明
    assert result.image.getpixel((30, 30))[3] == 255  # 前景 alpha 不变


# --------------------------------------------------------------------------- #
# 防御性：绝不产出全透明图
# --------------------------------------------------------------------------- #
def test_all_white_image_is_left_untouched():
    image = Image.new("RGB", (64, 64), (255, 255, 255))

    result = make_background_transparent(image)

    assert result.applied is False
    assert result.skipped_reason
    assert result.image.getpixel((10, 10))[3] == 255  # 原图 alpha 未动


def test_near_white_image_is_left_untouched():
    image = Image.new("RGB", (64, 64), (245, 245, 245))

    result = make_background_transparent(image, tolerance=30)

    assert result.applied is False


def test_invalid_arguments():
    with pytest.raises(TypeError):
        make_background_transparent("not an image")  # type: ignore[arg-type]

    with pytest.raises(ValueError):
        make_background_transparent(Image.new("RGB", (8, 8)), tolerance=-1)


def test_palette_image_is_supported():
    image = Image.new("P", (32, 32), 0)
    image.putpalette([255, 255, 255] + [0, 0, 0] * 255)

    result = make_background_transparent(image)

    assert result.image.mode == "RGBA"
    assert result.applied is False  # 整张都是白 → 拒绝

# --------------------------------------------------------------------------- #
# Bug 1 回归：只清除"与边缘连通"的背景，内部白色必须保留
# --------------------------------------------------------------------------- #
def test_interior_white_inside_outline_is_preserved():
    """黑框包住的白色（衣服/高光）不能被抠掉。"""
    image = Image.new("RGB", (300, 300), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    draw.rectangle((50, 50, 250, 250), fill=(0, 0, 0))          # 黑色轮廓
    draw.rectangle((60, 60, 240, 240), fill=(255, 255, 255))    # 内部白衣
    draw.ellipse((110, 110, 190, 190), fill=(220, 40, 40))      # 内部彩色

    result = make_background_transparent(image, tolerance=30)

    assert result.image.getpixel((150, 70))[3] == 255    # 内部白 → 保留（关键）
    assert result.image.getpixel((10, 10))[3] == 0       # 外部白 → 清除
    assert result.image.getpixel((55, 55))[3] == 255     # 黑框 → 保留
    assert result.image.getpixel((150, 150))[3] == 255


def test_background_reaching_inside_through_gap_is_removed():
    """轮廓右侧留缺口时，内部白与外部背景连通 → 应被清除。"""
    image = Image.new("RGB", (300, 300), (255, 255, 255))          # 白底
    draw = ImageDraw.Draw(image)
    draw.rectangle((60, 60, 240, 240), outline=(0, 0, 0), width=8)  # 黑色轮廓
    # 在右边框上开一个缺口，让外部白底流进内部
    draw.rectangle((228, 140, 252, 160), fill=(255, 255, 255))

    result = make_background_transparent(image, tolerance=30)

    assert result.image.getpixel((20, 20))[3] == 0      # 外部白 → 清除
    assert result.image.getpixel((150, 150))[3] == 0    # 内部白经缺口连通 → 清除
    assert result.image.getpixel((62, 150))[3] == 255   # 左边框本身 → 保留


def test_flood_fill_respects_connectivity():
    from iconswap.image_tools import flood_fill_from_edges

    # 5x5 全为候选背景，中心一圈设为前景 → 中心不应被填充
    width = height = 5
    candidate = bytearray(b"\xff" * (width * height))
    candidate[2 * width + 2] = 0  # 中心是前景

    filled = flood_fill_from_edges(bytes(candidate), width, height)

    assert filled[0] == 255                       # 边缘被填
    assert filled[2 * width + 2] == 0             # 中心未填


def test_flood_fill_handles_empty_candidate():
    from iconswap.image_tools import flood_fill_from_edges

    filled = flood_fill_from_edges(b"\x00" * 16, 4, 4)
    assert sum(filled) == 0
