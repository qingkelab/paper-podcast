"""HTML 画面层（`services/htmlpage.py`）的测试。

分两类：
- **页面结构**（不启 Chrome）：画布尺寸、转义、该有/不该有的元素 —— 快，任何时候都跑；
- **渲染出来的像素事实**（需要 Chrome）：光有 HTML 字符串说明不了画面对不对，
  尤其是「图层是不是真的透明」「两条渲染路径的强调行是不是像素级对齐」。
"""

from __future__ import annotations

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
