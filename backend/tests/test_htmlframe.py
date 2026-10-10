"""HTML → PNG 渲染（`services/htmlframe.py`）的测试。

这一层的价值全在**确定性**和**能退回**这两件事上，所以测试也盯这两条：
- 逐帧 seek 出来的画面必须每次一样（否则同一篇论文出两次片画面不同）；
- 没有 Chrome / 环境变量要求走老路时，`backend_name()` 必须让调用方走回 SVG，
  而不是抛异常把整条流水线打断。

真正要起 Chrome 的用例在没有 Chrome 的机器上跳过（`requires_chrome`），
纯逻辑的那些永远跑。
"""

from __future__ import annotations

import pytest

from app.services.htmlframe import (
    CHROME_PATH_ENV,
    RENDER_BACKEND_ENV,
    ChromeSession,
    HtmlRenderError,
    Page,
    _png_size,
    backend_name,
    chrome_available,
    chrome_path,
    html_rendering_enabled,
)

requires_chrome = pytest.mark.skipif(
    not chrome_available(), reason="这台机器上没有 Chrome，跳过需要真渲染的用例"
)


# ---------------------------------------------------------------------------
# 选路：有 Chrome 走 HTML，没 Chrome 必须能退回 SVG
# ---------------------------------------------------------------------------


class TestBackendSelection:
    def test_forced_svg_wins(self, monkeypatch):
        """显式要求老路时必须听话，哪怕机器上有 Chrome。"""
        monkeypatch.setenv(RENDER_BACKEND_ENV, "svg")
        assert backend_name() == "svg"
        assert not html_rendering_enabled()

    @pytest.mark.parametrize("value", ["resvg", "legacy", "SVG", " svg "])
    def test_svg_aliases(self, monkeypatch, value):
        monkeypatch.setenv(RENDER_BACKEND_ENV, value)
        assert backend_name() == "svg"

    def test_missing_chrome_falls_back_to_svg(self, monkeypatch):
        """**最重要的一条**：要求 HTML 但机器上没有 Chrome，不能失败，要退回 SVG。

        理由是「部署环境没装 Chrome」不该让用户拿不到视频 —— 画面差一点也比没有强。
        """
        monkeypatch.setenv(RENDER_BACKEND_ENV, "html")
        monkeypatch.setenv(CHROME_PATH_ENV, "/definitely/not/here/chrome")
        assert chrome_path() is None
        assert backend_name() == "svg"

    def test_unknown_value_is_treated_as_auto(self, monkeypatch):
        """环境变量拼错一个字不该让出片失败，按自动判定处理。"""
        monkeypatch.setenv(RENDER_BACKEND_ENV, "htlm")
        monkeypatch.setenv(CHROME_PATH_ENV, "/definitely/not/here/chrome")
        assert backend_name() == "svg"

    def test_env_override_path_must_exist(self, monkeypatch):
        monkeypatch.setenv(CHROME_PATH_ENV, "/definitely/not/here/chrome")
        assert chrome_path() is None


class TestPngSize:
    def test_rejects_garbage(self):
        with pytest.raises(HtmlRenderError):
            _png_size(b"not a png at all")
        with pytest.raises(HtmlRenderError):
            _png_size(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)


# ---------------------------------------------------------------------------
# 真渲染
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def session():
    """整个模块共用**一个** Chrome：启动约 2 秒，每个用例起一个太浪费。"""
    if not chrome_available():
        pytest.skip("这台机器上没有 Chrome")
    with ChromeSession.start() as active:
        yield active


def _pixels(path):
    """读 PNG 像素（和其他视频测试同一套办法，不引新依赖）。"""
    import pymupdf

    pix = pymupdf.Pixmap(str(path))
    n, W = pix.n, pix.width
    samples = pix.samples

    def at(x: int, y: int):
        i = (y * W + x) * n
        return (samples[i], samples[i + 1], samples[i + 2])

    return at, pix.width, pix.height


SQUARE_PAGE = """<!doctype html><meta charset="utf-8">
<style>
  html,body{{margin:0;width:100%;height:100%;background:#fff;
    font-family:"PingFang SC",system-ui,sans-serif}}
  #box{{position:absolute;top:40px;left:0;width:60px;height:60px;background:#2f6fb5;
    animation:slide 2s linear forwards}}
  @keyframes slide{{from{{transform:translateX(0)}}to{{transform:translateX(800px)}}}}
  h1{{position:absolute;top:160px;left:20px;font-size:40px;margin:0;color:#111}}
</style>
<div id="box"></div><h1>{title}</h1>
"""


def _square_centroid(path) -> int | None:
    """顺着小方块那一行的中间带扫，返回蓝色像素的 x 中心。"""
    at, width, _ = _pixels(path)
    xs = []
    for y in range(80, 100):
        for x in range(width):
            r, g, b = at(x, y)
            if b > 150 and r < 150:
                xs.append(x)
    return int(sum(xs) / len(xs)) if xs else None


@requires_chrome
class TestRendering:
    def test_renders_one_frame_at_the_requested_size(self, session, tmp_path):
        page = Page(html=SQUARE_PAGE.format(title="中文排版"), width=936, height=300)
        out = session.render(page, tmp_path / "one.png", work_dir=tmp_path / "work")

        assert out.exists() and out.stat().st_size > 0
        assert _png_size(out.read_bytes()) == (936, 300), "画布尺寸必须就是要求的尺寸"

    def test_text_actually_renders(self, session, tmp_path):
        """文字要真的画上去 —— 空页面也能生成合法的 PNG，光看文件在不在没用。"""
        page = Page(html=SQUARE_PAGE.format(title="量化误差"), width=936, height=300)
        out = session.render(page, tmp_path / "text.png", work_dir=tmp_path / "work")
        at, width, height = _pixels(out)

        dark = sum(
            1
            for y in range(150, 240)
            for x in range(0, width)
            if sum(at(x, y)) < 300
        )
        assert dark > 200, f"文字那一行几乎没有墨迹（{dark} 个暗像素），多半没渲染出来"

    def test_animation_seek_is_deterministic(self, session, tmp_path):
        """逐帧 seek 必须是**可复现**的：同一时刻截出来的画面每次一样。

        这条不成立的话，「同一篇论文出两次片画面不同」就成了常态。
        理论位置：2 秒走 800px、方块宽 60，所以 t=0/0.5/1.0/1.5/2.0 时
        x 中心分别是 30/230/430/630/830。连**数值**一起断言 ——
        只断言「在变大」的话，seek 到错误时刻（比如永远停在末帧）也能通过。

        绝对位置允许 ±2 像素：量的是「偏蓝像素的重心」，边缘抗锯齿会少吃一两个像素
        （实测稳定在 29/229/429/629/829）。**步长必须是精确的 200** ——
        那才是「seek 到的时间对不对」的判据，抗锯齿影响不到它。
        """
        page = Page(html=SQUARE_PAGE.format(title="seek"), width=936, height=300)
        frames = session.render_frames(
            page, [0.0, 0.5, 1.0, 1.5, 2.0], tmp_path / "frames", work_dir=tmp_path / "work"
        )
        assert len(frames) == 5
        positions = [_square_centroid(path) for path in frames]
        assert all(pos is not None for pos in positions), f"有帧里找不到方块：{positions}"
        assert abs(positions[0] - 30) <= 2, f"起步位置不对：{positions}"
        assert [pos - positions[0] for pos in positions] == [0, 200, 400, 600, 800]

    def test_seek_reports_animation_count(self, session, tmp_path):
        """`seek` 返回动画条数，调用方靠它判断「这一页到底有没有动画」。"""
        page = Page(html=SQUARE_PAGE.format(title="count"), width=936, height=300)
        session.open_page(page, work_dir=tmp_path / "work")
        assert session.seek(0.5) == 1

    def test_static_page_has_no_animations(self, session, tmp_path):
        page = Page(
            html="<!doctype html><meta charset='utf-8'><style>html,body{margin:0;background:#fff}"
            "h1{font-size:30px}</style><h1>静态页</h1>",
            width=400,
            height=200,
        )
        session.open_page(page, work_dir=tmp_path / "work")
        assert session.seek(0.0) == 0

    def test_file_url_image_is_loaded(self, session, tmp_path):
        """页面引用本地图片必须真的解码出来。

        这条钉的是「为什么把 HTML 写成临时文件再 `file://` 导航」——
        `Page.setDocumentContent` 的文档源是 `about:blank`，从那里加载 `file://`
        图片会被拦掉，页面上就是一块空白，而且**不报错**。
        """
        import pymupdf

        image_path = tmp_path / "patch.png"
        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 40), False)
        pix.set_rect(pix.irect, (220, 40, 40))  # 纯红
        pix.save(str(image_path))

        page = Page(
            html=f"<!doctype html><meta charset='utf-8'>"
            f"<style>html,body{{margin:0;background:#fff}}"
            f"img{{position:absolute;top:0;left:0;width:40px;height:40px}}</style>"
            f"<img src='{image_path.as_uri()}'>",
            width=200,
            height=100,
        )
        out = session.render(page, tmp_path / "img.png", work_dir=tmp_path / "work")
        at, _, _ = _pixels(out)
        r, g, b = at(20, 20)
        assert r > 180 and g < 100 and b < 100, f"图片没画上去（中心点像素是 {(r, g, b)}）"


@requires_chrome
class TestSessionLifecycle:
    def test_close_kills_process_and_removes_profile(self):
        """进程和临时 profile 都要清掉 —— 否则跑几次就攒一堆僵尸 Chrome。"""
        session = ChromeSession.start()
        process = session.process
        profile = session.profile_dir
        try:
            assert process.poll() is None
            assert profile.exists()
        finally:
            session.close()

        assert process.poll() is not None, "close 之后 Chrome 进程还在"
        assert not profile.exists(), "close 之后临时 profile 目录还在"

    def test_render_after_close_raises(self):
        session = ChromeSession.start()
        session.close()
        with pytest.raises(HtmlRenderError):
            session._evaluate("1")
