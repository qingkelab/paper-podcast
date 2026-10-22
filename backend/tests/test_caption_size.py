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
from app.services.htmlframe import ChromeSession, chrome_available

requires_chrome = pytest.mark.skipif(
    not chrome_available(), reason="这台机器上没有 Chrome，跳过需要真渲染的用例"
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


class TestTheSentenceReadsInOrder:
    """用户反馈「字幕不用这样分段」的**真正原因**：一句话被排成了并排的几列。

    小句是 `<span class="clause">`。字幕带是 `display:flex`（为了竖向居中），
    如果这些 span **直接**是 flex 容器的子元素，每个 span 都会变成一个 flex item ——
    也就是一句话被摆成几列、每列各自折行，读起来字序全乱：

        这篇论文想解决 | 能不能不重新训练 | 只靠少量物理试验就让机器
        的就是：       | 大模型，           | 人恢复状态?

    这不是动效，是排版坏了。修法是中间垫一层 `.band-inner`（flex 只负责居中，
    里面照旧是行内文字）。
    """

    SENTENCE = "这篇论文想解决的就是：能不能不重新训练大模型，只靠少量物理试验就让机器人恢复状态?"

    def test_the_clauses_are_not_flex_items(self):
        """结构层就能拦住：小句不能是 flex 容器的直接子元素。"""
        from dataclasses import replace

        from app.services.htmlpage import caption_band_page

        page = caption_band_page(self.SENTENCE, replace(PORTRAIT, subtitle_font=31.0))
        assert '<div class="band-inner">' in page.html, "字幕正文要垫一层块，否则小句会各占一列"
        assert 'class="band-text" data-fit-max' in page.html

    @requires_chrome
    def test_the_rendered_lines_read_in_order(self, tmp_path):
        """真渲染之后按行读回来：拼起来必须还是原句（列排版会打乱顺序）。"""
        import json
        from dataclasses import replace

        from app.services.htmlpage import caption_band_page

        js = """(() => {
          const el = document.querySelector('.band-text');
          const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
          const lines = [];
          let node;
          while ((node = walker.nextNode())) {
            const range = document.createRange();
            for (let i = 0; i < node.data.length; i++) {
              range.setStart(node, i); range.setEnd(node, i + 1);
              const r = range.getBoundingClientRect();
              const top = Math.round(r.top + window.scrollY);
              let line = lines.find(l => Math.abs(l.top - top) <= 2);
              if (!line) { line = {top, text: ''}; lines.push(line); }
              line.text += node.data[i];
            }
          }
          lines.sort((a, b) => a.top - b.top);
          return JSON.stringify(lines.map(l => l.text));
        })()"""
        with ChromeSession.start() as session:
            session.open_page(
                caption_band_page(self.SENTENCE, replace(PORTRAIT, subtitle_font=31.0)),
                work_dir=tmp_path,
            )
            lines = json.loads(session._evaluate(js))
        assert len(lines) == 2, f"这句应当两行：{lines}"
        assert "".join(lines) == self.SENTENCE, f"按行读回来字序乱了：{lines}"
