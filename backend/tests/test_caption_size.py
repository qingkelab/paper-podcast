"""字幕字号：**整集一个号**（用户：「字幕不用这样分段，文字大小要统一」）。

原来字号是**逐句**由浏览器 `fitText()` 量的：短句 38px、长句 26px —— 于是相邻两句
的字号差一档，看着像「字幕在跳」，也像被切成了几块。现在由
`design.caption_font_size()` 一次算好、挂在 `Layout.subtitle_font` 上，两条渲染路径都读它。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.design import (
    CAPTION_FONT_MAX,
    CAPTION_FONT_MIN,
    LANDSCAPE,
    PORTRAIT,
    caption_font_size,
    wrap_units,
)

SHORT = "那这近似会不会让结果不可信？"
MEDIUM = "6 比特下状态内存压缩了 5 倍多，Qwen 从 144 兆降到 28 兆左右。"
LONG = (
    "哥伦比亚和普林斯顿的团队想了个办法：不重新训练模型，而是在模型外面动手术，"
    "用最少的物理试错把性能拉回来。"
)


class TestOneSizeForTheWholeEpisode:
    def test_the_longest_sentence_sets_the_size(self):
        short_only = caption_font_size([SHORT], PORTRAIT)
        with_long = caption_font_size([SHORT, LONG], PORTRAIT)
        assert with_long <= short_only, "多一句长句只可能让整集降一档"
        assert short_only == CAPTION_FONT_MAX
        assert with_long >= CAPTION_FONT_MIN

    def test_every_sentence_fits_in_two_lines_at_that_size(self):
        size = caption_font_size([SHORT, MEDIUM, LONG], PORTRAIT)
        for text in (SHORT, MEDIUM, LONG):
            lines = wrap_units(text, PORTRAIT.subtitle_width / size)
            assert len(lines) <= 2, f"「{text[:12]}…」在 {size:.0f}px 下占了 {len(lines)} 行"

    def test_the_size_never_goes_below_the_floor(self):
        """极端长句也不会把整集的字号拉到不可读 —— 宁可它多占一行。"""
        huge = "很长的一句话，" * 40
        assert caption_font_size([huge], PORTRAIT) == CAPTION_FONT_MIN

    def test_no_texts_means_the_maximum(self):
        assert caption_font_size([], PORTRAIT) == CAPTION_FONT_MAX
        assert caption_font_size(["   ", ""], PORTRAIT) == CAPTION_FONT_MAX

    def test_landscape_is_measured_against_its_own_width(self):
        size = caption_font_size([LONG], LANDSCAPE)
        assert size >= CAPTION_FONT_MIN
        assert len(wrap_units(LONG, LANDSCAPE.subtitle_width / size)) <= 2


class TestEveryBandUsesThatSize:
    """两条渲染路径都要认这个号 —— 只改一条的话，换机器出片字号又变回去。"""

    def test_the_html_band_pins_the_font(self):
        from dataclasses import replace

        from app.services.htmlpage import caption_band_page

        layout = replace(PORTRAIT, subtitle_font=31.0)
        page = caption_band_page(SHORT, layout)
        assert "font-size: 31px" in page.html
        # 上下限都钉在同一个值上：`fitText()` 只能选这一个字号
        assert 'data-fit-max="31"' in page.html and 'data-fit-min="31"' in page.html

    def test_the_svg_band_uses_the_same_size(self, tmp_path):
        from dataclasses import replace

        from app.services.video_render import _band_layout

        layout = replace(PORTRAIT, subtitle_font=31.0)
        for text in (SHORT, MEDIUM, LONG):
            size, lines, _, _, _ = _band_layout(text, layout)
            assert size == 31.0, f"「{text[:10]}…」的字号没跟版式走"
            assert len(lines) <= 2

    def test_two_different_sentences_get_the_same_font_size(self, tmp_path):
        """这是用户看到的那个现象本身：相邻两句的字号必须一样。"""
        from dataclasses import replace

        from app.services.video_render import render_caption_band

        size = caption_font_size([SHORT, LONG], PORTRAIT)
        layout = replace(PORTRAIT, subtitle_font=size)
        a = render_caption_band(SHORT, tmp_path / "a.png", layout=layout)
        b = render_caption_band(LONG, tmp_path / "b.png", layout=layout)
        for png in (a, b):
            assert Path(png).exists()
        # 两张带子的**字高**一致：量各自第一行字的像素高度（长句会占两行，行数不同是正常的）。
        # 留 3px 余量：墨高取决于那几个字有没有出头的笔画，不完全是字号。
        height_a, height_b = _first_line_height(a), _first_line_height(b)
        assert abs(height_a - height_b) <= 3, f"两句字高差太多：{height_a} vs {height_b}"

    def test_unlocked_layout_keeps_the_old_behaviour(self):
        """不传 `subtitle_font` 的老调用点（老片子重合成）行为不变。"""
        from app.services.video_render import _band_layout

        size, _, _, _, _ = _band_layout(SHORT, PORTRAIT)
        assert size > 0


def _first_line_height(path: Path) -> int:
    """一张字幕带里**第一行字**的像素高度（用来比较两张带子的字高是否一致）。"""
    pymupdf = pytest.importorskip("pymupdf")
    pix = pymupdf.Pixmap(str(path))
    w, h, n, d = pix.width, pix.height, pix.n, pix.samples
    has_alpha = n == 4

    def ink(x: int, y: int) -> bool:
        index = (y * w + x) * n
        if has_alpha and d[index + 3] < 128:
            return False
        return min(d[index], d[index + 1], d[index + 2]) < 200

    rows = [y for y in range(h) if any(ink(x, y) for x in range(0, w, 4))]
    if not rows:
        return 0
    top = rows[0]
    height = 0
    for row in rows:
        if row - top > height + 2:  # 断开 = 第二行字了
            break
        height = row - top
    return height + 1
