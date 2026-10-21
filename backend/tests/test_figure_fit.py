"""配图装填（`services/figure_fit.py`）：**内容要在卡片里居中**。

这个文件守的是用户的直接反馈「视频中的图片要左右居中」。
论文原图四周烘进来的白边是**不对称**的（实测 a1ed76d0 的 Fig.2：左边 20px、右边 154px），
所以「把整张图居中」和「把图里的内容居中」差得很远 —— 前者让画面上的图整体偏左 67px。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services import figure_fit
from app.services import htmlpage
from app.services.design import PORTRAIT
from app.services.video_render import _fit_figure
from app.services.video_core import Scene


def make_figure(tmp_path: Path, *, w: int = 800, h: int = 600) -> Path:
    """白底图，内容只画在**偏右上**的一块（模拟论文图周围不对称的白边）。

    注意：`Pixmap.copy(其他 pixmap, irect)` 在 1.28 上**不会粘上去**（实测粘完还是白的），
    所以这里直接按原始像素造 —— 测试要造的是「左边 470px 白、右边 30px 白」这种真图。
    """
    pymupdf = pytest.importorskip("pymupdf")
    buf = bytearray([255] * (w * h * 3))
    for y in range(60, 260):
        row = y * w * 3
        for x in range(470, 770):
            index = row + x * 3
            buf[index] = buf[index + 1] = buf[index + 2] = 60
    pix = pymupdf.Pixmap(pymupdf.csRGB, w, h, bytes(buf), 0)
    path = tmp_path / "figure.png"
    pix.save(str(path))
    return path


class TestInkBox:
    def test_finds_the_content_and_skips_the_white_margin(self, tmp_path):
        path = make_figure(tmp_path)
        box = figure_fit.ink_bbox_of_file(str(path))
        assert box == (470, 60, 770, 260), f"墨迹包围盒量错了：{box}"

    def test_a_blank_image_has_no_ink(self, tmp_path):
        pymupdf = pytest.importorskip("pymupdf")
        pix = pymupdf.Pixmap(pymupdf.csRGB, 200, 200, bytes(bytearray([255] * (200 * 200 * 3))), 0)
        path = tmp_path / "blank.png"
        pix.save(str(path))
        assert figure_fit.ink_bbox_of_file(str(path)) is None


class TestContentIsCentred:
    def test_the_content_not_the_sheet_is_centred(self):
        """内容框左右留白相等 —— 这就是「图片左右居中」。"""
        card_w, card_h = PORTRAIT.image_box_w, PORTRAIT.image_box_h
        fit = figure_fit.fit_figure(card_w, card_h, 800, 600, (470, 60, 770, 260))
        left = fit.ink_x
        right = card_w - (fit.ink_x + fit.ink_w)
        assert abs(left - right) <= 1.0, f"内容没居中：左 {left:.1f} / 右 {right:.1f}"
        assert fit.ink_w > card_w * 0.5, "内容太小了（装填没吃满可用空间）"

    def test_caption_sits_on_the_content_not_on_the_sheet(self):
        """图注贴的是**内容**下沿（`fitted_figure_height` 用的就是 ink_h）。"""
        fit = figure_fit.fit_figure(912, 742, 800, 600, (470, 60, 770, 260))
        assert fit.ink_h < fit.img_h, "这张图的上下也有白边，内容应当比整张图矮"

    def test_without_ink_it_falls_back_to_the_whole_sheet(self):
        fit = figure_fit.fit_figure(912, 742, 800, 600, None)
        assert abs(fit.ink_x - (912 - fit.img_w) / 2) <= 0.5
        assert fit.ink_w == fit.img_w


class TestOverhangIsCapped:
    def test_a_tiny_content_box_does_not_blow_the_sheet_up(self):
        """内容只有 40×30、四周全是白：不能把整张纸放大到糊。"""
        card_w, card_h = 912, 742
        fit = figure_fit.fit_figure(card_w, card_h, 1600, 1200, (700, 500, 740, 530))
        assert fit.img_w <= card_w * (1 + 2 * figure_fit.CARD_OVERHANG_RATIO) + 1
        assert fit.img_h <= card_h * (1 + 2 * figure_fit.CARD_OVERHANG_RATIO) + 1
        assert fit.scale <= figure_fit.MAX_FIT_SCALE
        # 内容仍然居中
        assert abs(fit.ink_x - (card_w - fit.ink_x - fit.ink_w)) <= 1.0


class TestBothRenderPathsAgree:
    """两条渲染路径（HTML 走 Chrome / SVG 走 resvg）必须摆出**同一个位置**。

    原来 HTML 那条路把缩放交给浏览器的 `max-width/max-height`，于是它居中整张图、
    SVG 那条路居中内容 —— 同一张图在两条路上差 60~70px，而只有换机器才看得出来。
    """

    def test_the_html_markup_uses_the_same_numbers(self, tmp_path):
        path = make_figure(tmp_path)
        scene = Scene(start=0.0, end=3.0, image=path, kind="figure", caption="")
        markup = htmlpage._plain_image_block(scene, PORTRAIT)
        fit, _ = figure_fit.fit_for_file(path, PORTRAIT.image_box_w, PORTRAIT.image_box_h)
        assert f"width:{fit.ink_w:.1f}px" in markup, "框的宽度要等于内容宽度"
        assert f"height:{fit.ink_h:.1f}px" in markup, "框的高度要等于内容高度"
        assert f"left:{-(fit.ink_x - fit.img_x):.1f}px" in markup, "图要按内容偏移平移"
        assert f"top:{-(fit.ink_y - fit.img_y):.1f}px" in markup

    def test_the_svg_path_uses_the_same_numbers(self, tmp_path):
        path = make_figure(tmp_path)
        scene = Scene(start=0.0, end=3.0, image=path, kind="figure", caption="")
        fitted = _fit_figure(scene, PORTRAIT)
        fit, _ = figure_fit.fit_for_file(path, PORTRAIT.image_box_w, PORTRAIT.image_box_h)
        assert fitted.x == pytest.approx(fit.img_x, abs=0.5)
        assert fitted.y == pytest.approx(fit.img_y, abs=0.5)
        assert fitted.frame == pytest.approx(fit.frame, abs=0.5)
        assert fitted.ink_h == pytest.approx(fit.ink_h, abs=1.0)
