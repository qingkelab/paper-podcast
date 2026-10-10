"""HTML 画面层（`services/htmlpage.py`）的测试。

分两类：
- **页面结构**（不启 Chrome）：画布尺寸、转义、该有/不该有的元素 —— 快，任何时候都跑；
- **渲染出来的像素事实**（需要 Chrome）：光有 HTML 字符串说明不了画面对不对，
  尤其是「图层是不是真的透明」「两条渲染路径的强调行是不是像素级对齐」。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.htmlframe import ChromeSession, chrome_available

requires_chrome = pytest.mark.skipif(
    not chrome_available(), reason="这台机器上没有 Chrome，跳过需要真渲染的用例"
)


@pytest.fixture(scope="module")
def session():
    """整个模块共用一个 Chrome：启动约 2 秒，每个用例起一个太浪费。"""
    if not chrome_available():
        pytest.skip("这台机器上没有 Chrome")
    with ChromeSession.start() as active:
        yield active


def make_png(path: Path, w: int = 400, h: int = 300, color=(30, 60, 100)) -> Path:
    import pymupdf

    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, w, h), False)
    pix.set_rect(pix.irect, color)
    pix.save(str(path))
    return path


def _pixels(path):
    import pymupdf

    pix = pymupdf.Pixmap(str(path))
    n, W = pix.n, pix.width
    samples = pix.samples

    def at(x: int, y: int):
        i = (y * W + x) * n
        return (samples[i], samples[i + 1], samples[i + 2])

    return at, pix.width, pix.height


def _hex(rgb: str) -> tuple[int, int, int]:
    return tuple(int(rgb[i : i + 2], 16) for i in (1, 3, 5))  # type: ignore[return-value]




# ---------------------------------------------------------------------------
# 页面结构：HTML 版画面「有没有画出该有的东西」
# ---------------------------------------------------------------------------


def _scene(tmp_path, **overrides):
    from app.services.video import Scene

    image = make_png(tmp_path / "img.png")
    kwargs = dict(
        start=0.0,
        end=5.0,
        image=image,
        kind="figure",
        caption="Figure 1: Overview of the method",
        text="这套方法在 16 个任务上把精度保住了 67%，显存却只要三分之一。",
        point="6.93 倍压缩，精度不掉",
    )
    kwargs.update(overrides)
    return Scene(**kwargs)


class TestScenePageMarkup:
    """不启 Chrome 也能查的部分：画布尺寸、转义、该有/不该有的元素。"""

    def test_canvas_is_the_layout_size(self, tmp_path):
        from app.services.design import LANDSCAPE, PORTRAIT
        from app.services.htmlpage import scene_page

        assert (scene_page(_scene(tmp_path), PORTRAIT).width, scene_page(_scene(tmp_path), PORTRAIT).height) == (936, 1210)
        page = scene_page(_scene(tmp_path), LANDSCAPE)
        assert (page.width, page.height) == (1920, 1080)

    def test_layer_pages_are_transparent_and_region_sized(self, tmp_path):
        """图层页必须：透明底 + 画幅就是那一块区域（ffmpeg 按固定坐标叠它）。"""
        from app.services.design import PORTRAIT
        from app.services.htmlpage import image_card_page, point_row_page

        card = image_card_page(_scene(tmp_path), PORTRAIT)
        assert card.transparent is True
        assert (card.width, card.height) == (PORTRAIT.image_box_w, PORTRAIT.image_box_h)
        assert ".image-box { top: 0; left: 0; }" in card.html, "图层页的区域要落在原点"

        row = point_row_page("6.93 倍压缩", PORTRAIT)
        assert row.transparent is True
        assert (row.width, row.height) == (PORTRAIT.width, PORTRAIT.point_height), "强调行是整页宽的一层"

    def test_cover_page_does_not_repeat_the_paper_title(self, tmp_path):
        """封面的大字标题下面**已经**有一行论文原题（玻璃面板里），再画一遍就是重复。

        老路径有这个重复（面板里一行、图片下面的图注行又一行），
        HTML 版去掉了 —— 这是刻意的设计修正，不是漏画。
        """
        from app.services.design import PORTRAIT
        from app.services.htmlpage import scene_page

        scene = _scene(tmp_path, headline="4 比特反超 INT8", caption="STEPQuant: When and Where")
        html = scene_page(scene, PORTRAIT).html
        assert html.count("STEPQuant") == 1, "论文原题在画面上出现了不止一次"

    def test_headline_is_escaped(self, tmp_path):
        """模型/用户给的字直接进 HTML，必须转义 —— 否则一个 `<` 就能把整页结构打乱。"""
        from app.services.design import PORTRAIT
        from app.services.htmlpage import scene_page

        scene = _scene(tmp_path, headline="<script>alert(1)</script> 标题")
        html = scene_page(scene, PORTRAIT).html
        assert "<script>alert(1)</script>" not in html.replace('<script>\n', "").replace("</script>", "")
        assert "&lt;script&gt;" in html

    def test_numbers_are_wrapped_for_highlight(self, tmp_path):
        """数字高亮是「论文解读里最有信息量的是一个百分比」的落点，必须留着。"""
        from app.services.design import PORTRAIT
        from app.services.htmlpage import scene_page

        html = scene_page(_scene(tmp_path), PORTRAIT).html
        assert '<span class="num">67%</span>' in html

    def test_caption_band_is_full_width_and_opaque(self, tmp_path):
        """字幕带换成 HTML 之后仍然是「整条不透明」—— 底图那条空白不能透出来。"""
        from app.services.design import PORTRAIT
        from app.services.htmlpage import caption_band_page

        page = caption_band_page("这是一句测试字幕。", PORTRAIT)
        assert (page.width, page.height) == (PORTRAIT.width, PORTRAIT.height - PORTRAIT.subtitle_top)
        assert page.transparent is False

    def test_subtitle_can_be_left_out_for_the_base_layer(self, tmp_path):
        """字幕分句时底图不能画字幕，否则第一句出现之前整段字就已经在画面上了。"""
        from app.services.design import PORTRAIT
        from app.services.htmlpage import scene_page

        html = scene_page(_scene(tmp_path), PORTRAIT, include_subtitle=False).html
        assert "保住" not in html

    def test_fit_attributes_are_emitted(self, tmp_path):
        """字号自适应交给浏览器量真实字形，页面里必须留下这个钩子。"""
        from app.services.design import PORTRAIT
        from app.services.htmlpage import scene_page

        html = scene_page(_scene(tmp_path), PORTRAIT).html
        assert "data-fit-max" in html and "window.prepare" in html


@requires_chrome
class TestRenderedPageFacts:
    """真渲染出来的像素事实（光有 HTML 字符串说明不了画面对不对）。"""

    def test_cover_page_has_the_brand_accent_bar(self, session, tmp_path):
        from app.services.design import BRAND_GREEN, PORTRAIT
        from app.services.htmlpage import scene_page

        scene = _scene(tmp_path, kind="cover", headline="4 比特反超 INT8")
        out = session.render(scene_page(scene, PORTRAIT), tmp_path / "cover.png", work_dir=tmp_path)
        at, width, height = _pixels(out)

        want = _hex(BRAND_GREEN)
        hits = sum(
            1
            for y in range(0, height, 3)
            for x in range(0, width, 3)
            if all(abs(at(x, y)[i] - want[i]) < 12 for i in range(3))
        )
        # 短杠是 76×7，抽样后理论上有 ~59 个采样点命中，取 30 当阈值（留足抗锯齿余量）
        assert hits > 30, f"封面上的品牌绿短杠没画出来（命中 {hits} 个像素）"

    def test_number_highlight_has_a_background(self, session, tmp_path):
        """数字要带浅蓝底 —— 这是「突出解读」的一部分，不是装饰。"""
        from app.services.design import PORTRAIT, POINT_BG
        from app.services.htmlpage import caption_band_page

        out = session.render(
            caption_band_page("精度保住 67% 不掉", PORTRAIT),
            tmp_path / "band.png",
            work_dir=tmp_path,
        )
        at, width, height = _pixels(out)
        want = _hex(POINT_BG)
        hits = sum(
            1
            for y in range(0, height, 2)
            for x in range(0, width, 2)
            if at(x, y) == want
        )
        assert hits > 100, f"数字底色没画出来（{want} 命中 {hits} 个像素）"

    def test_point_row_layer_matches_the_svg_layer(self, session, tmp_path):
        """强调行这一层两条路径**必须像素级对齐**：ffmpeg 叠它用的是固定坐标。

        画幅、区域位置、左侧蓝条的位置只要差一点，整条片子上的强调行就整体错位。
        """
        from app.services.design import PORTRAIT, POINT_BAR
        from app.services.video import render_point_row
        from app.services.htmlpage import point_row_page

        text = "6.93 倍压缩，精度不掉"
        svg_path = render_point_row(text, tmp_path / "point-svg.png", layout=PORTRAIT)
        html_path = session.render(
            point_row_page(text, PORTRAIT), tmp_path / "point-html.png", work_dir=tmp_path
        )

        def bar_x(path):
            at, width, height = _pixels(path)
            want = _hex(POINT_BAR)
            xs = [x for x in range(width) if at(x, height // 2) == want]
            return xs[0], xs[-1]

        assert bar_x(svg_path) == bar_x(html_path), "强调行的蓝条位置两条路径不一致"


# ---------------------------------------------------------------------------
# 端到端：真出一条片，而且走的是 HTML 那条路
# ---------------------------------------------------------------------------


@requires_chrome
class TestHtmlPipeline:
    """`compose_video` 在 HTML 路径下端到端跑通。

    别的测试默认走 SVG（见 `conftest.py`），所以这条是唯一覆盖「整条流水线 + Chrome」
    的用例。它盯的是**不变量**：视频长度仍然等于音频长度、图层仍然齐、
    而且这次真的是 HTML 画的（`result.renderer`）。
    """

    def _compose(self, tmp_path, monkeypatch, backend: str):
        import wave

        from app.services.video import compose_video

        monkeypatch.setenv("RENDER_BACKEND", backend)
        assets = tmp_path / "assets"
        assets.mkdir(parents=True, exist_ok=True)
        cover = make_png(assets / "cover.png", 600, 800)
        figure = make_png(assets / "f1.png", 600, 400, (240, 240, 240))
        audio = tmp_path / "a.wav"
        with wave.open(str(audio), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(8000)
            handle.writeframes(b"\x00\x00" * 8000 * 3)

        class Timing:
            def __init__(self, index, speaker, start, end):
                self.index, self.speaker, self.start, self.end = index, speaker, start, end

        return compose_video(
            segments=[
                {"speaker": "A", "text": "开场先交代这篇论文要解决的问题，66% 的准确率。"},
                {"speaker": "B", "text": "然后看这张图，纵轴是压缩率，中间那一段掉得最快。"},
            ],
            timings=[Timing(0, "A", 0.0, 1.5), Timing(1, "B", 1.5, 3.0)],
            audio_path=audio,
            audio_duration=3.0,
            cover_path=str(cover),
            # figures 是**字典**（pipeline 传进来的形状），不是 ImageAsset
            figures=[{"id": "f1", "path": str(figure), "kind": "figure",
                      "caption": "Figure 1: overview"}],
            illustration_png=None,
            work_dir=tmp_path / "work",
            output_path=tmp_path / "out.mp4",
            title="Test Paper",
            llm=None,
            # 强调行跟着 preset_scenes 一起复用（和线上「重新合成不问模型」同一条规矩）
            preset_scenes=[
                {"index": 0, "image": "cover", "point": "66% 的准确率"},
                {"index": 1, "image": "f1", "point": "中间那一段掉得最快"},
            ],
            preset_assets={"cover": str(cover), "f1": str(figure)},
            orientation="portrait",
        )

    def test_html_path_produces_a_video_of_the_right_length(self, tmp_path, monkeypatch):
        from app.services.video import probe_media_duration

        result = self._compose(tmp_path, monkeypatch, "html")
        assert result.renderer == "html", "这次出片应当走 HTML 渲染"
        assert (tmp_path / "out.mp4").exists()
        duration = probe_media_duration(tmp_path / "out.mp4")
        assert duration is not None
        # **长度对齐是硬约束**：画面比声音长/短都是错的
        assert abs(duration - 3.0) <= 0.3, f"视频长度 {duration} 和音频 3.0 秒对不上"

    def test_layer_geometry_matches_between_html_and_svg(self, session, tmp_path):
        """两条路径画出来的图层**画幅必须一致** —— 不一致 ffmpeg 的叠加坐标就错位了。

        注意这里直接调路由器，而不是看 `compose_video` 的输出目录：出片结束时那些
        中间图层会被删掉（它们是临时文件），所以「事后去 work 目录里比」是查不到的。
        """
        import pymupdf

        from app.services.design import PORTRAIT, SUBTITLE_BG
        from app.services.video import (
            _render_band_layer,
            _render_card_layer,
            _render_chrome_layer,
            _render_point_layer,
        )

        scene = _scene(tmp_path, image=make_png(tmp_path / "g.png", 600, 400))
        cases = [
            ("chrome", lambda p, s: _render_chrome_layer(
                scene, p, include_subtitle=True, include_point=False,
                layout=PORTRAIT, session=s)),
            ("card", lambda p, s: _render_card_layer(scene, p, layout=PORTRAIT, session=s)),
            ("point", lambda p, s: _render_point_layer(
                scene.point, p, layout=PORTRAIT, session=s)),
            # 字幕带也要比：它是**最窄的一层**（936×242），差一行就错位
            ("band", lambda p, s: _render_band_layer(
                "这一句字幕要放得下，还要把 67% 标出来。", p, layout=PORTRAIT, session=s)),
        ]
        for name, render in cases:
            svg_path = render(tmp_path / f"{name}-svg.png", None)
            html_path = render(tmp_path / f"{name}-html.png", session)
            sizes = []
            for path in (svg_path, html_path):
                with pymupdf.open(str(path)) as doc:
                    page = doc.load_page(0)
                    sizes.append((int(page.rect.width), int(page.rect.height)))
            assert sizes[0] == sizes[1], f"{name} 层两条路径尺寸不一致：{sizes}"

        # 骨架层还顺手验一个像素事实：字幕带底色在两条路径上都要有
        want = _hex(SUBTITLE_BG)
        for name in ("chrome-svg.png", "chrome-html.png"):
            at, width, height = _pixels(tmp_path / name)
            assert at(4, height - 4) == want, f"{name} 的字幕带底色不对"

    def test_falls_back_to_svg_when_chrome_is_unusable(self, tmp_path, monkeypatch):
        """Chrome 起不来必须**降级**，不能把出片整个搞失败。

        这是这次迁移里最重要的一条保底：新渲染器是「更好」，不是「必须」。
        """
        monkeypatch.setenv("RENDER_BACKEND", "html")
        monkeypatch.setenv("CHROME_PATH", "/definitely/not/here/chrome")
        result = self._compose(tmp_path, monkeypatch, "html")
        assert result.renderer == "svg"
        assert (tmp_path / "out.mp4").exists()


# ---------------------------------------------------------------------------
# 片尾品牌卡 与 图内聚光灯
# ---------------------------------------------------------------------------


class TestEndcardMarkup:
    """片尾卡是**品牌卡**：logo 是浅色字标，底必须深；引导语画面和语音都有。"""

    def test_logo_src_is_a_real_data_uri(self, tmp_path):
        """logo 的 src 必须是**一个** data URI 字符串。

        踩过：`_logo_uri()` 返回的是 `(uri, 宽, 高)` 三元组，整个塞进 f-string 会生成
        `src="('data:image/png;base64,...', 460, 123)"` —— 浏览器当成坏图，
        `naturalWidth=0`、高度 0，画面上就是「片尾卡上少了个 logo」，
        而且**不报任何错**（`alt=""` 连占位都没有）。
        """
        from app.services.design import PORTRAIT
        from app.services.htmlpage import endcard_page

        html = endcard_page(_scene(tmp_path), PORTRAIT).html
        start = html.index('class="endcard-logo" src="') + len('class="endcard-logo" src="')
        src = html[start : html.index('"', start)]
        assert src.startswith("data:image/png;base64,"), f"logo 的 src 不是 data URI：{src[:60]}"
        assert "(" not in src and " " not in src and "," in src

    def test_background_is_brand_dark_not_white(self, tmp_path):
        """正文白底、片尾深底 —— 反过来的话浅色 logo 直接消失。"""
        from app.services.design import BRAND_DARK, PORTRAIT
        from app.services.htmlpage import endcard_page

        html = endcard_page(_scene(tmp_path), PORTRAIT).html
        assert f"background: {BRAND_DARK}" in html


@requires_chrome
class TestEndcardAndFocusRendering:
    def test_endcard_logo_is_actually_drawn(self, session, tmp_path):
        """端到端确认 logo **真的解码出来了**：naturalWidth 必须是 460。"""
        from app.services.design import PORTRAIT
        from app.services.htmlpage import endcard_page

        session.open_page(endcard_page(_scene(tmp_path), PORTRAIT), work_dir=tmp_path)
        raw = session._evaluate(
            "(() => { const i = document.querySelector('.endcard-logo');"
            " return i ? i.naturalWidth : -1; })()"
        )
        assert raw == 460, f"logo 没解码出来（naturalWidth={raw}）"

    def test_endcard_and_focus_layers_match_the_svg_sizes(self, session, tmp_path):
        """两条路径的画幅必须一致（ffmpeg 叠加用的是固定坐标）。"""
        import pymupdf

        from app.services.design import PORTRAIT
        from app.services.video import _render_endcard_layer, _render_focus_layer

        scene = _scene(tmp_path, image=make_png(tmp_path / "f.png", 700, 500))
        focus = {"x": 0.05, "y": 0.15, "w": 0.3, "h": 0.4, "label": "Recurrent state"}
        cases = [
            ("endcard", lambda p, s: _render_endcard_layer(scene, p, layout=PORTRAIT, session=s)),
            ("focus", lambda p, s: _render_focus_layer(
                scene, p, focus=focus, layout=PORTRAIT, session=s)),
        ]
        for name, render in cases:
            svg_path = render(tmp_path / f"{name}-svg.png", None)
            html_path = render(tmp_path / f"{name}-html.png", session)
            sizes = []
            for path in (svg_path, html_path):
                with pymupdf.open(str(path)) as doc:
                    page = doc.load_page(0)
                    sizes.append((int(page.rect.width), int(page.rect.height)))
            assert sizes[0] == sizes[1], f"{name} 两条路径尺寸不一致：{sizes}"

    def test_focus_dims_everything_but_the_highlighted_box(self, session, tmp_path):
        """聚光灯的判据：框内**没有**被压暗、框外**被**压暗。

        只看「有没有白像素」是查不出来的（白底本来就白）—— 得比 alpha：
        压暗层是半透明白（alpha≈0.72*255），框内那块是透明。
        """
        from app.services.design import PORTRAIT
        from app.services.htmlpage import focus_overlay_page

        import pymupdf

        scene = _scene(tmp_path, image=make_png(tmp_path / "f2.png", 700, 500))
        page = focus_overlay_page(scene, PORTRAIT, {"x": 0.3, "y": 0.3, "w": 0.3, "h": 0.3})
        out = session.render(page, tmp_path / "focus.png", work_dir=tmp_path)

        pix = pymupdf.Pixmap(str(out))
        n, width, height = pix.n, pix.width, pix.height
        data = pix.samples
        assert n == 4, "聚光灯层必须有 alpha 通道"

        def alpha(x, y):
            return data[(y * width + x) * n + 3]

        # 框中心（0.3+0.15 → 卡中间偏左）应当透明，四角应当被压暗
        inside = alpha(int(width * 0.45), int(height * 0.45))
        corner = alpha(6, 6)
        assert inside < 60, f"框内不该被压暗（alpha={inside}）"
        assert corner > 120, f"框外应当被压暗（alpha={corner}）"


# ---------------------------------------------------------------------------
# Tier B：内容驱动动画页（只对动画窗口逐帧抓取）
# ---------------------------------------------------------------------------


class TestFocusMorphPage:
    """聚光灯「从上一处移过来」的页面：结构层面的检查（不启 Chrome）。"""

    def test_static_page_has_no_animation(self, tmp_path):
        from app.services.design import PORTRAIT
        from app.services.htmlpage import focus_overlay_page

        page = focus_overlay_page(
            _scene(tmp_path), PORTRAIT, {"x": 0.1, "y": 0.1, "w": 0.3, "h": 0.3, "label": "A"}
        )
        assert 'class="focus-frame focus-morph"' not in page.html

    def test_morph_page_carries_both_labels(self, tmp_path):
        """「移过去」时旧标签要**留在原地**淡出，新标签跟着框走。

        所以旧标签必须挂在另一个容器上 —— 挂在正在移动的那个框上会跟着滑走，
        看起来像标签自己飞过去了。
        """
        from app.services.design import PORTRAIT
        from app.services.htmlpage import focus_overlay_page

        page = focus_overlay_page(
            _scene(tmp_path),
            PORTRAIT,
            {"x": 0.6, "y": 0.3, "w": 0.3, "h": 0.4, "label": "右侧"},
            origin={"x": 0.05, "y": 0.1, "w": 0.3, "h": 0.4, "label": "左侧"},
        )
        assert 'class="focus-frame focus-morph"' in page.html, "移动动画的类没加上"
        assert "chip-out" in page.html and "chip-in" in page.html
        assert "左侧" in page.html and "右侧" in page.html
        assert "chip-host" in page.html

    def test_identical_rects_do_not_animate(self, tmp_path):
        """位置没动就别演动画（同位置的两个标签不同也不该动）。"""
        from app.services.design import PORTRAIT
        from app.services.htmlpage import focus_overlay_page

        rect = {"x": 0.2, "y": 0.2, "w": 0.3, "h": 0.3}
        page = focus_overlay_page(
            _scene(tmp_path),
            PORTRAIT,
            {**rect, "label": "B"},
            origin={**rect, "label": "A"},
        )
        assert 'class="focus-frame focus-morph"' not in page.html, "位置没动却加了移动动画"


class TestFocusMorphTrigger:
    """什么时候该「移过来」：条件缺一不可（这条决定要不要花十几次渲染）。"""

    def _scenes(self, tmp_path, *, second_focus, second_image=None, second_brand=""):
        from app.services.video import Scene

        image = make_png(tmp_path / "fig.png")
        other = make_png(tmp_path / "other.png", 300, 300)
        first = Scene(
            start=0, end=5, image=image, kind="figure", text="第一段",
            focus={"x": 0.05, "y": 0.1, "w": 0.3, "h": 0.4, "label": "左侧"},
        )
        second = Scene(
            start=5, end=10, image=second_image or image, kind="figure", text="第二段",
            focus=second_focus, brand=second_brand,
        )
        return [first, second]

    def test_moves_when_the_same_figure_is_framed_elsewhere(self, tmp_path):
        from app.services.video import _focus_morph_origin

        scenes = self._scenes(
            tmp_path, second_focus={"x": 0.6, "y": 0.3, "w": 0.3, "h": 0.4, "label": "右侧"}
        )
        origin = _focus_morph_origin(scenes, 1)
        assert origin is not None and origin["label"] == "左侧"

    def test_no_move_without_a_previous_spotlight(self, tmp_path):
        from app.services.video import _focus_morph_origin

        scenes = self._scenes(tmp_path, second_focus={"x": 0.6, "y": 0.3, "w": 0.3, "h": 0.4})
        scenes[0].focus = None
        assert _focus_morph_origin(scenes, 1) is None

    def test_no_move_across_different_figures(self, tmp_path):
        """换了一张图就没有「移动」可言 —— 框在另一张图上的位置毫无关系。"""
        from app.services.video import _focus_morph_origin

        scenes = self._scenes(
            tmp_path,
            second_focus={"x": 0.6, "y": 0.3, "w": 0.3, "h": 0.4},
            second_image=make_png(tmp_path / "another.png", 320, 320),
        )
        assert _focus_morph_origin(scenes, 1) is None

    def test_no_move_when_the_box_barely_changed(self, tmp_path):
        from app.services.video import _focus_morph_origin

        scenes = self._scenes(
            tmp_path, second_focus={"x": 0.051, "y": 0.101, "w": 0.3, "h": 0.4}
        )
        assert _focus_morph_origin(scenes, 1) is None

    def test_no_move_from_or_into_the_endcard(self, tmp_path):
        from app.services.video import _focus_morph_origin

        scenes = self._scenes(
            tmp_path,
            second_focus={"x": 0.6, "y": 0.3, "w": 0.3, "h": 0.4},
            second_brand="outro",
        )
        assert _focus_morph_origin(scenes, 1) is None

    def test_first_scene_has_nothing_to_move_from(self, tmp_path):
        from app.services.video import _focus_morph_origin

        scenes = self._scenes(tmp_path, second_focus={"x": 0.6, "y": 0.3, "w": 0.3, "h": 0.4})
        assert _focus_morph_origin(scenes, 0) is None


@requires_chrome
class TestFocusMorphRendering:
    def test_the_frame_actually_travels_and_ends_on_target(self, session, tmp_path):
        """逐帧抓下来之后：框**真的在移动**，而且末帧**就是**静态版那一张。

        后半句是关键：序列播完要接上「定住」的那段画面，末帧和静态版差一点，
        观众就会看到画面跳一下。
        """
        import pymupdf

        from app.services.design import FOCUS_BORDER, PORTRAIT
        from app.services.htmlpage import FOCUS_MORPH_SEC, focus_overlay_page

        scene = _scene(tmp_path, image=make_png(tmp_path / "fig2.png", 640, 480))
        target = {"x": 0.62, "y": 0.30, "w": 0.30, "h": 0.45, "label": "右侧的结果"}
        origin = {"x": 0.02, "y": 0.10, "w": 0.26, "h": 0.35, "label": "左侧"}
        want = _hex(FOCUS_BORDER)

        def border_left(path):
            pix = pymupdf.Pixmap(str(path))
            n, width, height = pix.n, pix.width, pix.height
            data = pix.samples
            xs = [
                x
                for y in range(0, height, 2)
                for x in range(0, width, 2)
                if data[(y * width + x) * n + 3] > 200
                and all(abs(data[(y * width + x) * n + k] - want[k]) < 20 for k in range(3))
            ]
            return min(xs) if xs else None

        page = focus_overlay_page(scene, PORTRAIT, target, origin=origin)
        count = int(round(FOCUS_MORPH_SEC * 30))
        frames = session.render_frames(
            page, [i / 30 for i in range(count)], tmp_path / "morph", work_dir=tmp_path
        )
        positions = [border_left(path) for path in frames]
        assert all(pos is not None for pos in positions), positions
        assert positions[0] < positions[-1], f"框没有移动：{positions}"
        assert all(
            positions[i] <= positions[i + 1] + 2 for i in range(len(positions) - 1)
        ), f"移动不单调：{positions}"

        static_path = session.render(
            focus_overlay_page(scene, PORTRAIT, target),
            tmp_path / "static.png",
            work_dir=tmp_path,
        )
        assert abs(border_left(frames[-1]) - border_left(static_path)) <= 1, (
            "动画末帧和静态版对不上，接上去会跳一下"
        )


@requires_chrome
class TestFocusMorphReachesTheVideo:
    """端到端：帧序列真的进了 MP4，而且**长度对齐没破**。

    这是 Tier B 唯一不能靠单测糊过去的地方 —— ffmpeg 那边多挂了一路图片序列输入，
    一旦帧数或时间基没对上，整条片子就会比声音长/短，或者画面卡在错误的位置。
    """

    def _compose(self, tmp_path, monkeypatch):
        import wave

        from app.services.video import compose_video

        monkeypatch.setenv("RENDER_BACKEND", "html")
        assets = tmp_path / "assets"
        assets.mkdir(parents=True, exist_ok=True)
        figure = make_png(assets / "fig.png", 640, 480, (245, 245, 245))
        audio = tmp_path / "a.wav"
        with wave.open(str(audio), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(8000)
            handle.writeframes(b"\x00\x00" * 8000 * 4)

        class Timing:
            def __init__(self, index, start, end):
                self.index, self.speaker, self.start, self.end = index, "A", start, end

        return compose_video(
            segments=[
                {"speaker": "A", "text": "先看左边这一块，它讲的是误差从哪里来。"},
                {"speaker": "A", "text": "再看右边这一块，这里是它最终的结果。"},
            ],
            timings=[Timing(0, 0.0, 2.0), Timing(1, 2.0, 4.0)],
            audio_path=audio,
            audio_duration=4.0,
            cover_path=None,
            figures=[{"id": "f1", "path": str(figure), "kind": "figure", "caption": "Figure 1"}],
            illustration_png=None,
            work_dir=tmp_path / "work",
            output_path=tmp_path / "out.mp4",
            title="Morph",
            llm=None,
            preset_scenes=[
                {"index": 0, "image": "f1", "point": "误差从哪里来",
                 "focus": {"x": 0.03, "y": 0.10, "w": 0.26, "h": 0.35, "label": "左侧"}},
                {"index": 1, "image": "f1", "point": "最终的结果",
                 "focus": {"x": 0.62, "y": 0.30, "w": 0.30, "h": 0.45, "label": "右侧"}},
            ],
            preset_assets={"f1": str(figure)},
            orientation="portrait",
        )

    def test_spotlight_moves_inside_the_encoded_video(self, tmp_path, monkeypatch):
        import subprocess

        import pymupdf

        from app.services.design import FOCUS_BORDER
        from app.services.video import probe_media_duration

        result = self._compose(tmp_path, monkeypatch)
        assert result.renderer == "html"
        duration = probe_media_duration(tmp_path / "out.mp4")
        assert duration is not None and abs(duration - 4.0) <= 0.3, (
            f"多挂了一路序列输入之后长度对不上了：{duration}"
        )

        want = _hex(FOCUS_BORDER)

        def border_left(at: float):
            frame = tmp_path / f"f{at}.png"
            subprocess.run(
                ["ffmpeg", "-v", "error", "-ss", str(at), "-i", str(tmp_path / "out.mp4"),
                 "-frames:v", "1", "-y", str(frame)],
                check=True,
            )
            pix = pymupdf.Pixmap(str(frame))
            n, width, height = pix.n, pix.width, pix.height
            data = pix.samples
            xs = [
                x
                for y in range(int(height * 0.1), int(height * 0.7), 2)
                for x in range(0, width, 2)
                if all(abs(data[(y * width + x) * n + k] - want[k]) < 30 for k in range(3))
            ]
            return min(xs) if xs else None

        # 第二段从 2.0s 开始，动画窗口是它开头的 0.45 秒
        early, late = border_left(2.05), border_left(2.45)
        assert early is not None and late is not None, (early, late)
        assert early < late - 40, f"成片里聚光灯没移动：{early} → {late}"


# 量「浏览器实际怎么断行」的小工具：逐字取 Range 的矩形，按 top 分行。
# 这是唯一能问出「这一行到底是哪几个字」的办法（innerText 给的是整段，
# 看不出断行，也看不出标点是不是被甩到了下一行）。
_BAND_LINES_JS = """(() => {
  const el = document.querySelector('.band-text');
  if (!el) return null;
  // ⚠️ 不能只取 `el.firstChild`：数字会被包成 `<span class="num">`
  //（「6比特方案…」这种句子第一个子节点就是元素，不是文本节点），
  // 用 TreeWalker 走**所有**文本节点才稳。
  const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
  const out = [];
  let node;
  while ((node = walker.nextNode())) {
    for (let i = 0; i < node.length; i++) {
      const r = document.createRange(); r.setStart(node, i); r.setEnd(node, i + 1);
      const rects = r.getClientRects();
      if (!rects.length) continue;
      const top = Math.round(rects[0].top);
      let line = out.find(l => Math.abs(l.top - top) <= 2);
      if (!line) { line = { top: top, text: '' }; out.push(line); }
      line.text += node.data[i];
    }
  }
  out.sort((a, b) => a.top - b.top);
  return JSON.stringify(out.map(l => l.text));
})()"""


@requires_chrome
class TestLineBreaking:
    """断行归浏览器之后，**标点不会被甩到下一行独占一行**。

    老路径（`_fit_subtitle` 按近似字宽表折行）在库里的 404 条真实字幕里有 **14 条**
    （3.5%）把 `。` / `，` 单独排到了下一行 —— 我在成片抽帧里亲眼见过一次：
    字幕末行只有一个「。」。中文排印规则不允许行首出现收尾标点，浏览器直接照做。
    """

    ORPHANS = [
        "是看错了，还是推理歪了，还是压根没理你的要求。",
        "全部由Gemini-3-Pro在同一套提示和协议下标的，",
        "也就是说，它在用一个模型的判断去教另一个模型。",
        "6比特方案已经在SGLang上跑通了，但真要上生产，",
    ]
    CLOSERS = "。，、；：！？）」』…"

    def test_no_line_starts_with_a_closing_punctuation(self, session, tmp_path):
        from app.services.design import PORTRAIT
        from app.services.htmlpage import caption_band_page

        for index, text in enumerate(self.ORPHANS):
            session.open_page(caption_band_page(text, PORTRAIT), work_dir=tmp_path)
            raw = session._evaluate(_BAND_LINES_JS)
            assert raw, f"量不到断行：{text}"
            lines = json.loads(raw)
            bad = [
                line
                for line in lines[1:]
                if line.strip() and len(line.strip()) <= 2 and line.strip()[0] in self.CLOSERS
            ]
            assert not bad, f"标点被甩到单独一行：{lines}"
            assert len(lines) <= 2, f"这句话应当两行放下：{lines}"

    def test_the_old_python_wrapping_had_this_defect(self):
        """把「这条测试为什么存在」钉住：老路径对同一句话**确实**会断出孤行标点。

        如果哪天有人改了 `_fit_subtitle` 让这段断言失败，说明老路径的折行行为变了 ——
        那时候该重新评估的是这条测试的前提，而不是把它删掉。
        """
        from app.services.video import _fit_subtitle

        lines = _fit_subtitle(self.ORPHANS[0], max_lines=3, max_size=38.0)[1]
        assert lines[-1].strip() == "。", f"老路径的孤行标点行为变了：{lines}"


@requires_chrome
class TestTextNeverOverflowsItsBox:
    """字号自适应真的跑了（`window.prepare` 接上之后）—— 量的是「没跑出来」的后果。"""

    def test_caption_is_clamped_not_overflowing(self, session, tmp_path):
        """超长图注只能被行的上限截断（带省略号），**不能溢出到字幕带上**。"""
        from app.services.design import PORTRAIT
        from app.services.htmlpage import scene_page

        scene = _scene(tmp_path, caption="Figure 1: " + "这段图注特别长，" * 40)
        session.open_page(scene_page(scene, PORTRAIT), work_dir=tmp_path)
        raw = session._evaluate(
            "(() => { const c = document.querySelector('.caption');"
            " return JSON.stringify({h: c.scrollHeight, box: c.clientHeight,"
            " lines: getComputedStyle(c).webkitLineClamp,"
            " overflow: getComputedStyle(c).overflow}); })()"
        )
        info = json.loads(raw)
        assert info["overflow"] == "hidden", "图注必须裁掉多余的行"
        assert info["lines"] == "2", "图注最多两行"
        assert info["box"] <= 46 + 1, f"图注的框高固定两行，实际 {info['box']}"

    def test_long_caption_shrinks_instead_of_being_cut_early(self, session, tmp_path):
        """放不下要**先缩字号**再截 —— 同一片地方能多看到四成字。

        实测库里的真实图注：老路径（18px 硬切两行）中位数能看到 160 字，
        缩字号之后中位数 227 字（+42%），而且末尾带省略号而不是半句断掉。
        """
        from app.services.design import PORTRAIT
        from app.services.htmlpage import scene_page

        scene = _scene(tmp_path, caption="Figure 2: " + "子图说明文字。" * 18)
        session.open_page(scene_page(scene, PORTRAIT), work_dir=tmp_path)
        size = session._evaluate(
            "parseFloat(getComputedStyle(document.querySelector('.caption')).fontSize)"
        )
        assert 14 <= size < 18, f"长图注应当把字号压到 18 以下（实际 {size}）"
