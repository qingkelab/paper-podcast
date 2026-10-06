"""配图提取与生成信息图的测试。

配图这块的失败模式都很隐蔽（提取到空白区域、模型 SVG 里塞了脚本、
清洗后 XML 反而非法），所以这里用**合成 PDF / 合成 SVG** 做确定性验证，
不依赖任何外部网络。
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from app.services.figures import (
    MAX_FIGURES,
    extract_figures,
    render_first_page,
)
from app.services.illustration import (
    CANVAS_H,
    CANVAS_W,
    IllustrationError,
    fallback_svg,
    rasterize_svg,
    sanitize_svg,
)

SVG_NS = "http://www.w3.org/2000/svg"


# --------------------------------------------------------------------------
# 合成测试用 PDF
# --------------------------------------------------------------------------


def make_pdf(*, drawings: bool = True, caption: str | None = "Figure 1: A test diagram."):
    """造一页含矢量图形和图注的 PDF。

    drawings=False 时只放文字——用来模拟「纯文字排版的表格」，
    这种没有图形可截，提取器应当跳过而不是截一段文字。
    """
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)  # A4

    if drawings:
        # 画一个明显的「图」：外框 + 几个填充块 + 连线
        page.draw_rect(pymupdf.Rect(80, 150, 515, 430), color=(0, 0, 0), width=1.5)
        page.draw_rect(pymupdf.Rect(110, 190, 230, 270), color=(0.2, 0.4, 0.8), fill=(0.8, 0.87, 0.95))
        page.draw_rect(pymupdf.Rect(330, 190, 450, 270), color=(0.2, 0.4, 0.8), fill=(0.8, 0.87, 0.95))
        page.draw_line(pymupdf.Point(230, 230), pymupdf.Point(330, 230), color=(0.2, 0.4, 0.8), width=2)
        page.draw_rect(pymupdf.Rect(200, 330, 380, 400), color=(0.4, 0.3, 0.6), fill=(0.92, 0.9, 0.96))

    if caption:
        page.insert_text((80, 460), caption, fontsize=10)

    # 图注下方再放点正文，确保「图注上方」这个方向是真的按位置区分的
    page.insert_text((80, 500), "Body text below the caption. " * 4, fontsize=10)

    data = doc.tobytes()
    doc.close()
    return data


# --------------------------------------------------------------------------
# 封面
# --------------------------------------------------------------------------


class TestCover:
    def test_renders_first_page(self, tmp_path):
        path, width, height = render_first_page(make_pdf(), tmp_path / "cover.png")
        assert path.exists()
        assert width > 300 and height > 400
        assert path.stat().st_size > 2000

    def test_returns_none_for_garbage(self, tmp_path):
        """封面渲染失败必须返回 None 而不是抛异常——配图不该拖垮整期播客。"""
        assert render_first_page(b"not a pdf at all", tmp_path / "x.png") is None

    def test_returns_none_for_empty_bytes(self, tmp_path):
        assert render_first_page(b"", tmp_path / "x.png") is None


# --------------------------------------------------------------------------
# 正文配图提取
# --------------------------------------------------------------------------


class TestExtractFigures:
    def test_extracts_figure_above_caption(self, tmp_path):
        """图注在图下方，所以要取图注**上方**的图形区域。

        方向搞反是这块最容易犯的错：会截到图注下方的正文，得到一片空白。
        """
        figures = extract_figures(make_pdf(), tmp_path, "ep1")
        assert len(figures) == 1
        figure = figures[0]
        assert figure.kind == "figure"
        assert figure.label == "Figure 1"
        assert figure.page == 1
        assert "A test diagram" in figure.caption
        assert figure.width > 200 and figure.height > 100
        assert (tmp_path / f"ep1-{figure.id}.png").exists()

    def test_skips_text_only_pages(self, tmp_path):
        """没有矢量图形的区域截出来就是一段文字截图，没有配图价值，应跳过。"""
        figures = extract_figures(make_pdf(drawings=False), tmp_path, "ep2")
        assert figures == []

    def test_handles_pdf_without_caption(self, tmp_path):
        assert extract_figures(make_pdf(caption=None), tmp_path, "ep3") == []

    def test_invalid_pdf_returns_empty(self, tmp_path):
        """损坏的 PDF 只能降级为「没有配图」，不能抛异常。"""
        assert extract_figures(b"definitely not a pdf", tmp_path, "ep4") == []

    def test_respects_max_figures(self, tmp_path):
        """多页多图时要被上限截断，避免页面被淹没。"""
        import pymupdf

        doc = pymupdf.open()
        for index in range(MAX_FIGURES + 3):
            page = doc.new_page(width=595, height=842)
            page.draw_rect(pymupdf.Rect(80, 120, 515, 380), width=1.5)
            page.draw_rect(pymupdf.Rect(120, 160, 300, 300), fill=(0.8, 0.85, 0.95))
            page.insert_text((80, 410), f"Figure {index + 1}: diagram {index + 1}.", fontsize=10)
        data = doc.tobytes()
        doc.close()

        figures = extract_figures(data, tmp_path, "ep5")
        assert 0 < len(figures) <= MAX_FIGURES

    def test_figure_ids_are_unique(self, tmp_path):
        import pymupdf

        doc = pymupdf.open()
        for index in range(3):
            page = doc.new_page(width=595, height=842)
            page.draw_rect(pymupdf.Rect(80, 120, 515, 380), width=1.5)
            page.draw_rect(pymupdf.Rect(120, 160, 300, 300), fill=(0.7, 0.8, 0.9))
            page.insert_text((80, 410), f"Figure {index + 1}: diagram.", fontsize=10)
        data = doc.tobytes()
        doc.close()

        figures = extract_figures(data, tmp_path, "ep6")
        ids = [f.id for f in figures]
        assert len(ids) == len(set(ids))


# --------------------------------------------------------------------------
# SVG 清洗（安全相关，最重要）
# --------------------------------------------------------------------------


class TestSanitizeSvg:
    def _wrap(self, inner: str, extra_attrs: str = "") -> str:
        return (
            f'<svg xmlns="{SVG_NS}" viewBox="0 0 {CANVAS_W} {CANVAS_H}"{extra_attrs}>'
            f"{inner}</svg>"
        )

    def test_strips_script_tags(self):
        svg = self._wrap('<script>alert(1)</script><rect width="10" height="10"/>')
        cleaned = sanitize_svg(svg)
        assert "script" not in cleaned.lower()
        assert "alert" not in cleaned

    def test_strips_event_handlers(self):
        svg = self._wrap('<rect width="10" height="10" onclick="alert(1)" onload="x()"/>')
        cleaned = sanitize_svg(svg)
        assert "onclick" not in cleaned.lower()
        assert "onload" not in cleaned.lower()

    def test_strips_foreign_object(self):
        svg = self._wrap('<foreignObject><div>html</div></foreignObject><rect width="5" height="5"/>')
        cleaned = sanitize_svg(svg)
        assert "foreignobject" not in cleaned.lower()

    def test_strips_external_href(self):
        svg = self._wrap('<image href="https://evil.example/x.png"/>')
        cleaned = sanitize_svg(svg)
        assert "evil.example" not in cleaned

    def test_strips_javascript_url(self):
        svg = self._wrap('<a href="javascript:alert(1)"><rect width="5" height="5"/></a>')
        cleaned = sanitize_svg(svg)
        assert "javascript" not in cleaned.lower()

    def test_keeps_legitimate_content(self):
        svg = self._wrap(
            '<rect x="10" y="10" width="100" height="50" fill="#123456" rx="8"/>'
            '<text x="20" y="40" font-size="18" fill="#fff">注意力机制</text>'
        )
        cleaned = sanitize_svg(svg)
        assert "注意力机制" in cleaned
        assert "#123456" in cleaned

    def test_keeps_smil_animation(self):
        """动画是配图的卖点，不能被清洗掉。"""
        svg = self._wrap(
            '<circle r="5"><animateMotion dur="3s" repeatCount="indefinite" path="M0,0 L50,0"/></circle>'
        )
        cleaned = sanitize_svg(svg)
        assert "animateMotion" in cleaned
        assert "indefinite" in cleaned

    def test_output_is_reparsable(self):
        """清洗结果必须能重新解析。

        曾经这里有个真实 bug：手动 set("xmlns") 而 ElementTree 因为注册了默认
        命名空间会自动再输出一次，产生重复属性 → XML 非法。resvg 宽容能渲染，
        但浏览器会直接报错。
        """
        svg = self._wrap('<rect width="10" height="10"/>')
        cleaned = sanitize_svg(svg)
        ET.fromstring(cleaned)  # 不抛异常即通过
        assert cleaned.count("xmlns") == 1

    def test_adds_namespace_when_model_omits_it(self):
        """模型漏写 xmlns 时要补回，否则浏览器不会当 SVG 渲染。"""
        cleaned = sanitize_svg('<svg viewBox="0 0 1280 720"><rect width="10" height="10"/></svg>')
        assert SVG_NS in cleaned
        ET.fromstring(cleaned)

    def test_extracts_svg_from_markdown_fence(self):
        raw = f'```svg\n{self._wrap("<rect/>")}\n```'
        assert sanitize_svg(raw).startswith("<svg")

    def test_extracts_svg_from_surrounding_prose(self):
        raw = f'好的，这是配图：\n{self._wrap("<rect/>")}\n希望有帮助。'
        assert "希望有帮助" not in sanitize_svg(raw)

    def test_rejects_output_without_svg(self):
        with pytest.raises(IllustrationError):
            sanitize_svg("这里完全没有 SVG")

    def test_rejects_malformed_svg(self):
        with pytest.raises(IllustrationError):
            sanitize_svg("<svg><rect</svg>")

    def test_forces_canvas_size(self):
        cleaned = sanitize_svg(self._wrap('<rect width="1" height="1"/>', extra_attrs=' width="99"'))
        root = ET.fromstring(cleaned)
        assert root.get("width") == str(CANVAS_W)
        assert root.get("height") == str(CANVAS_H)


# --------------------------------------------------------------------------
# 栅格化与兜底图
# --------------------------------------------------------------------------


class TestRasterize:
    def test_rasterizes_to_png(self, tmp_path):
        svg = sanitize_svg(
            f'<svg xmlns="{SVG_NS}" viewBox="0 0 {CANVAS_W} {CANVAS_H}">'
            f'<rect width="{CANVAS_W}" height="{CANVAS_H}" fill="#101a2b"/>'
            f'<text x="60" y="120" font-size="48" fill="#ffffff">测试配图</text></svg>'
        )
        path, width, height = rasterize_svg(svg, tmp_path / "a.png")
        assert path.exists()
        assert (width, height) == (CANVAS_W, CANVAS_H)
        assert path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"

    def test_cjk_glyphs_actually_render(self, tmp_path):
        """不同汉字必须渲染出不同图像。

        如果字体解析失败，汉字会变成豆腐块（□），不同文字会渲染成
        完全一样的图——这个差分测试能抓住那种情况。
        """
        import resvg_py

        def render(text: str) -> bytes:
            svg = sanitize_svg(
                f'<svg xmlns="{SVG_NS}" viewBox="0 0 {CANVAS_W} {CANVAS_H}">'
                f'<rect width="{CANVAS_W}" height="{CANVAS_H}" fill="#fff"/>'
                f'<text x="40" y="120" font-size="64" fill="#000">{text}</text></svg>'
            )
            return bytes(resvg_py.svg_to_bytes(svg_string=svg, width=600, height=200))

        assert render("注意力机制") != render("残差学习网")

    def test_raises_on_unrenderable_svg(self, tmp_path):
        with pytest.raises(IllustrationError):
            rasterize_svg("不是 svg", tmp_path / "b.png")


class TestFallbackSvg:
    ANALYSIS = {
        "background": "背景",
        "innovations": ["创新点一", "创新点二", "创新点三"],
        "method": "方法",
        "experiments": "实验",
        "conclusion": "结论",
        "limitations": ["不足"],
        "value": "价值",
        "future": ["方向"],
    }
    META = {
        "title": "Attention Is All You Need",
        "year": 2017,
        "venue": "NeurIPS",
        "keywords": ["Transformer", "自注意力"],
    }

    def test_is_valid_svg(self):
        cleaned = sanitize_svg(fallback_svg(self.ANALYSIS, self.META))
        ET.fromstring(cleaned)

    def test_renders_without_model(self, tmp_path):
        cleaned = sanitize_svg(fallback_svg(self.ANALYSIS, self.META))
        path, width, height = rasterize_svg(cleaned, tmp_path / "fb.png")
        assert path.stat().st_size > 3000
        assert (width, height) == (CANVAS_W, CANVAS_H)

    def test_handles_empty_input(self):
        """任何字段都缺失时也必须产出合法 SVG，绝不能崩。"""
        cleaned = sanitize_svg(fallback_svg({}, None))
        ET.fromstring(cleaned)

    def test_includes_title_and_innovations(self):
        cleaned = sanitize_svg(fallback_svg(self.ANALYSIS, self.META))
        assert "Attention Is All You Need" in cleaned
        assert "创新点一" in cleaned

    def test_escapes_xml_special_chars(self):
        """标题里带 & < > 时不能破坏 XML。"""
        meta = {"title": "A & B <script>alert(1)</script>", "keywords": ["a<b"]}
        cleaned = sanitize_svg(fallback_svg(self.ANALYSIS, meta))
        ET.fromstring(cleaned)
        assert "<script>" not in cleaned

    def test_wraps_long_title(self):
        meta = {"title": "超长标题" * 30}
        cleaned = sanitize_svg(fallback_svg(self.ANALYSIS, meta))
        root = ET.fromstring(cleaned)
        texts = [t for t in root.iter(f"{{{SVG_NS}}}text")]
        assert len(texts) >= 3  # 标题被折成多行


class TestFigureDeduplication:
    """同一张图会被多种写法命中，必须去重。

    实测 ResNet 那篇同时命中了 `Figure 4`（真图注）和正文里的
    `Fig. 4 shows the training procedures`（交叉引用），产出两张重复的图。
    正文引用落在行首时，正则无法与真图注区分，只能靠「后处理择优」。
    """

    def _pdf_with_reference(self):
        """一页里既有真图注，又有正文里的同号交叉引用。"""
        import pymupdf

        doc = pymupdf.open()
        page = doc.new_page(width=595, height=842)
        page.draw_rect(pymupdf.Rect(80, 120, 515, 380), width=1.5)
        page.draw_rect(pymupdf.Rect(120, 160, 300, 300), fill=(0.75, 0.82, 0.93))
        page.insert_text((80, 410), "Figure 4. Training curves on ImageNet.", fontsize=10)
        # 正文里的交叉引用，故意放在行首
        page.insert_text((80, 440), "Fig. 4 shows the training procedures.", fontsize=10)
        data = doc.tobytes()
        doc.close()
        return data

    def test_same_number_not_extracted_twice(self, tmp_path):
        figures = extract_figures(self._pdf_with_reference(), tmp_path, "dedup")
        labels = [f.label for f in figures]
        assert len(labels) == len(set(labels)), f"出现重复图注：{labels}"

    def test_prefers_full_word_caption(self, tmp_path):
        """`Figure 4` 是正式图注写法，`Fig. 4` 更可能是正文引用，优先保留前者。"""
        figures = extract_figures(self._pdf_with_reference(), tmp_path, "dedup2")
        for figure in figures:
            assert not figure.label.lower().startswith("fig."), (
                f"保留了缩写写法 {figure.label!r}，应当优先 Figure 全称"
            )

    def test_caption_excludes_surrounding_text(self, tmp_path):
        """图注文字不能带上同一行左侧的其他内容。

        PyMuPDF 的 get_text(clip=...) 返回的是相交文本块而非严格裁剪，
        会把图里的坐标轴标签一起带进来。
        """
        import pymupdf

        doc = pymupdf.open()
        page = doc.new_page(width=595, height=842)
        page.draw_rect(pymupdf.Rect(80, 120, 515, 380), width=1.5)
        page.draw_rect(pymupdf.Rect(120, 160, 300, 300), fill=(0.7, 0.8, 0.9))
        # 同一行左侧放干扰文字，右侧才是图注
        page.insert_text((80, 410), "iter. (1e4) iter. (1e4)", fontsize=8)
        page.insert_text((300, 410), "Figure 7. Training on CIFAR-10.", fontsize=10)
        data = doc.tobytes()
        doc.close()

        figures = extract_figures(data, tmp_path, "cap")
        assert len(figures) == 1
        assert "Figure 7" in figures[0].caption
        assert "iter." not in figures[0].caption


# --------------------------------------------------------------------------
# 图内文字方向：把整体转了的图摆正
# --------------------------------------------------------------------------


class TestTextDirection:
    """有些论文的图把轴标签整个转了 90°，图里所有文字方向都是 (0,-1)。

    实测 Attention 那篇的 Figure 3/4/5 就是 108 行文字全部竖直、没有一行横排，
    按原样截出来在一堆正常图里看着就是歪的。
    """

    def test_rotation_mapping(self):
        from app.services.figures import _rotation_for

        assert _rotation_for((1.0, 0.0)) == 0      # 正常横排
        assert _rotation_for((0.0, -1.0)) == 90    # 从下往上 → 顺时针 90°
        assert _rotation_for((0.0, 1.0)) == 270    # 从上往下
        assert _rotation_for((-1.0, 0.0)) == 180   # 从右往左 = 真正倒置
        assert _rotation_for(None) == 0

    def test_rotate_pixmap_swaps_dimensions(self):
        """顺时针 90° 必须交换宽高（用 set_rotation 绕行，因为 get_pixmap 没有 rotate 参数）。"""
        import pymupdf

        from app.services.figures import _rotate_pixmap

        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 200, 100), False)
        assert (_rotate_pixmap(pix, 90).width, _rotate_pixmap(pix, 90).height) == (100, 200)
        assert (_rotate_pixmap(pix, 180).width, _rotate_pixmap(pix, 180).height) == (200, 100)
        assert (_rotate_pixmap(pix, 0).width, _rotate_pixmap(pix, 0).height) == (200, 100)

    def test_rotate_pixmap_is_clockwise(self):
        """方向必须验证过，否则会把图转反。

        做法：左上角放红块、右下角放蓝块，顺时针 90° 后红块应到右上、蓝块到左下。
        """
        import pymupdf

        from app.services.figures import _rotate_pixmap

        doc = pymupdf.open()
        page = doc.new_page(width=200, height=200)
        page.draw_rect(pymupdf.Rect(10, 10, 50, 50), fill=(1, 0, 0))
        page.draw_rect(pymupdf.Rect(150, 150, 190, 190), fill=(0, 0, 1))
        base = page.get_pixmap(clip=page.rect, dpi=72)
        doc.close()

        rotated = _rotate_pixmap(base, 90)
        n, W, H = rotated.n, rotated.width, rotated.height
        s = rotated.samples

        def pixel(x: int, y: int) -> tuple[int, int, int]:
            i = (y * W + x) * n
            return (s[i], s[i + 1], s[i + 2])

        top_right = pixel(int(W * 0.85), int(H * 0.15))
        bottom_left = pixel(int(W * 0.15), int(H * 0.85))
        assert top_right[0] > 140 and top_right[2] < 90, f"红块没到右上角：{top_right}"
        assert bottom_left[2] > 140 and bottom_left[0] < 90, f"蓝块没到左下角：{bottom_left}"

    @staticmethod
    def _rotated_text_pdf():
        """造一页：图形区域内的文字全部竖排（模拟注意力可视化那种图）。"""
        import pymupdf

        doc = pymupdf.open()
        page = doc.new_page(width=595, height=842)
        # 一个明显的「图」边框
        page.draw_rect(pymupdf.Rect(80, 120, 515, 380), width=1.5)
        # 竖排文字：rotate=90 让文字方向变成非横排
        for index in range(8):
            page.insert_text((110 + index * 22, 360), f"WORD{index}", fontsize=11, rotate=90)
        page.insert_text((80, 410), "Figure 1: A figure with rotated labels.", fontsize=10)
        data = doc.tobytes()
        doc.close()
        return data

    @staticmethod
    def _horizontal_text_pdf():
        """造一页：图内文字正常横排。"""
        import pymupdf

        doc = pymupdf.open()
        page = doc.new_page(width=595, height=842)
        page.draw_rect(pymupdf.Rect(80, 120, 515, 380), width=1.5)
        for index in range(8):
            page.insert_text((110, 160 + index * 20), f"LINE {index} normal text", fontsize=11)
        page.insert_text((80, 410), "Figure 1: A normal figure.", fontsize=10)
        data = doc.tobytes()
        doc.close()
        return data

    def test_rotates_figure_with_vertical_text(self, tmp_path):
        """文字竖排的图，宽高应当相对原样输出发生交换。

        注意不能断言「转完是横向」—— 区域本身可能是横的也可能是竖的，
        转正只是把宽高对调，方向取决于原区域的形状。
        """
        import pymupdf

        raw = pymupdf.Pixmap(
            extract_figures(
                self._rotated_text_pdf(), tmp_path, "raw", auto_upright=False
            )[0].path
        )
        fixed = pymupdf.Pixmap(
            extract_figures(self._rotated_text_pdf(), tmp_path, "fixed")[0].path
        )
        assert (fixed.width, fixed.height) == (raw.height, raw.width), (
            f"竖排文字的图应当被旋转（原样 {raw.width}x{raw.height}，"
            f"实际 {fixed.width}x{fixed.height}）"
        )

    def test_leaves_horizontal_text_figure_alone(self, tmp_path):
        import pymupdf

        raw = pymupdf.Pixmap(
            extract_figures(
                self._horizontal_text_pdf(), tmp_path, "hraw", auto_upright=False
            )[0].path
        )
        fixed = pymupdf.Pixmap(
            extract_figures(self._horizontal_text_pdf(), tmp_path, "hfix")[0].path
        )
        assert (fixed.width, fixed.height) == (raw.width, raw.height), (
            "横排文字的图不该被旋转"
        )

    def test_auto_upright_can_be_disabled(self, tmp_path):
        """关掉开关就完全按 PDF 原样输出，不做任何旋转。"""
        import pymupdf

        figures = extract_figures(
            self._rotated_text_pdf(), tmp_path, "off", auto_upright=False
        )
        assert len(figures) == 1
        off = pymupdf.Pixmap(figures[0].path)
        on = pymupdf.Pixmap(
            extract_figures(self._rotated_text_pdf(), tmp_path, "on")[0].path
        )
        assert (off.width, off.height) == (on.height, on.width)

    def test_region_without_text_is_left_alone(self, tmp_path):
        """图里没有文字就无从判断方向，保持原样，不做猜测。"""
        import pymupdf

        doc = pymupdf.open()
        page = doc.new_page(width=595, height=842)
        page.draw_rect(pymupdf.Rect(80, 120, 515, 380), width=1.5)
        page.draw_rect(pymupdf.Rect(120, 160, 400, 340), fill=(0.8, 0.85, 0.9))
        page.insert_text((80, 410), "Figure 1: Pure graphics, no labels.", fontsize=10)
        data = doc.tobytes()
        doc.close()

        figures = extract_figures(data, tmp_path, "notext")
        assert len(figures) == 1
        pix = pymupdf.Pixmap(figures[0].path)
        assert pix.width > pix.height, "无文字的图不该被旋转"
