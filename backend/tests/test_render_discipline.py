"""渲染纪律：**每一页都必须只由时间决定**。

参考做法里那条规矩写得最直白：「每一条视频都只由时间决定：`window.render(t)` 画出第 t 秒。
不用 CSS 动画、不用计时器，帧和帧之间不保存状态，随机数带固定种子。」
我们比它宽松一点（CSS/WAAPI 动画是**可 seek** 的，渲染器会把它们暂停再设 `currentTime`），
但下面三件事会**静默**破坏逐帧可复现，而且症状很难查：

| 禁用 | 为什么 |
|---|---|
| CSS `transition` | **不可 seek**：`document.getAnimations()` 拿不到它，抓到的那一帧取决于抓帧时机器跑多快 —— 同一份代码两次出的片子画面不一样 |
| `setTimeout` / `setInterval` / `requestAnimationFrame` 驱动的状态 | 同上，而且**帧间保存状态**：抓帧顺序一变结果就变（Tier B 是「先 open 再逐帧 seek」） |
| `Math.random()` | 每一帧的粒子/抖动都不同，无法复现，也无法验收 |

CSS `animation` + `@keyframes` 是允许的（渲染器 seek 的就是它），
`window.seek(t)` 是允许的（那是我们自己的约定），`document.fonts.ready` 之类的等待也是。

这个测试扫的是**真的渲染出来的 HTML**，不是源码里的字符串 —— 页面是拼出来的，
「CSS 在别处引入」这种情况只有看最终产物才拦得住。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.services.design import LANDSCAPE, PORTRAIT
from app.services.htmlpage import (
    caption_band_page,
    caption_chip_page,
    endcard_page,
    focus_overlay_page,
    image_card_page,
    point_row_page,
    scene_page,
)
from app.services.video_core import Scene

# 允许出现的东西（先剥掉，再找剩下的）
_TIMER = re.compile(r"\bset(?:Timeout|Interval)\s*\(")
_RANDOM = re.compile(r"\bMath\.random\s*\(")
_TRANSITION = re.compile(r"(?<!animation-)\btransition\s*:", re.I)


def _scene(tmp_path: Path, **kwargs) -> Scene:
    image = tmp_path / "cover.png"
    if not image.exists():
        # 要一张**真 PNG**：图片卡那一页会读图算等比落点（假文件直接抛 VideoError）
        import pymupdf

        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 400, 300), False)
        pix.set_rect(pix.irect, (250, 250, 250))
        pix.save(str(image))
    defaults = dict(
        start=0.0, end=8.0, image=image, kind="figure",
        text="这套方法在 16 个任务上把精度保住了 67%。", caption="Figure 1: Overview.",
    )
    defaults.update(kwargs)
    return Scene(**defaults)  # type: ignore[arg-type]


def _pages(tmp_path: Path) -> dict[str, str]:
    scene = _scene(tmp_path)
    focus = {"x": 0.1, "y": 0.1, "w": 0.4, "h": 0.4, "label": "a"}
    return {
        "整页（scene_page）": scene_page(scene, PORTRAIT).html,
        "整页（横版）": scene_page(scene, LANDSCAPE).html,
        "字幕带": caption_band_page(scene.text, PORTRAIT).html,
        "数字胶囊（静态）": caption_chip_page(scene.text, PORTRAIT).html,
        "数字胶囊（弹簧）": caption_chip_page("显存直降 68.7%", PORTRAIT, spring=True).html,
        "强调行": point_row_page("6.93 倍压缩，精度不掉", PORTRAIT).html,
        "图片卡": image_card_page(scene, PORTRAIT).html,
        "片尾品牌卡": endcard_page(scene, PORTRAIT).html,
        "聚光灯": focus_overlay_page(scene, PORTRAIT, focus).html,
    }


class TestPagesAreTimeDetermined:
    def test_no_css_transitions(self, tmp_path):
        """CSS transition 不可 seek —— 抓到的那一帧取决于机器快慢。"""
        offenders = {
            name: html for name, html in _pages(tmp_path).items() if _TRANSITION.search(html)
        }
        assert not offenders, f"这些页面用了 CSS transition（不可 seek）：{list(offenders)}"

    def test_no_timers(self, tmp_path):
        """计时器驱动的动效在逐帧抓图里必然错乱（而且帧间保存状态）。"""
        offenders = {
            name: html for name, html in _pages(tmp_path).items() if _TIMER.search(html)
        }
        assert not offenders, f"这些页面用了计时器：{list(offenders)}"

    def test_no_unseeded_randomness(self, tmp_path):
        """`Math.random()` 让同一份代码两次出的片子不一样，也没法验收。"""
        offenders = {
            name: html for name, html in _pages(tmp_path).items() if _RANDOM.search(html)
        }
        assert not offenders, f"这些页面用了 Math.random()：{list(offenders)}"

    def test_the_spring_chip_page_is_still_seekable(self, tmp_path):
        """弹簧那页动的是 `window.seek(t)`（我们的约定），**不能**改成计时器。"""
        html = _pages(tmp_path)["数字胶囊（弹簧）"]
        assert "window.seek" in html
        assert "requestAnimationFrame" not in html, (
            "页面里不该自己跑 rAF 循环：抓帧是「open 一次 + 逐帧 seek」，"
            "自己跑循环的动画和 seek 会互相打架"
        )

    def test_css_animations_are_still_allowed_and_seekable(self, tmp_path):
        """反过来也要钉住：**允许** CSS animation（渲染器 seek 的就是它）。

        这条是防止矫枉过正 —— 有人看到「不许 transition」就把聚光灯的
        `@keyframes` 也删了，那等于把逐帧的「移过去」也一起删了。
        """
        scene = _scene(tmp_path)
        focus = {"x": 0.1, "y": 0.1, "w": 0.4, "h": 0.4, "label": "a"}
        origin = {"x": 0.6, "y": 0.1, "w": 0.3, "h": 0.3, "label": "b"}
        html = focus_overlay_page(scene, PORTRAIT, focus, origin=origin).html
        assert "@keyframes focus-morph" in html


# --------------------------------------------------------------------------
# 缓动与弹簧：**渲染模块里不许出现字面量**，只能用具名令牌/预设
# --------------------------------------------------------------------------
#
# 规矩来自参考做法（claude-remotion-skill 的 `theme.ts` 第一行注释：
# 「NEVER inline colors/easings in components」）。理由不是洁癖：
# 同一个缓动值散落在五个文件里，改一处等于没改，而「这条动画怎么突然不顺了」
# 是最难查的一类问题 —— 数值本身是量出来的，必须只有一份。

#: 允许出现缓动字面量的地方：定义处（design 的令牌、motion 的公式）与测试
_EASING_HOME = {"design.py", "motion.py"}

_RENDER_MODULES = (
    "video_render.py",
    "video_encode.py",
    "htmlpage.py",
    "diagram.py",
    "video.py",
)


def _render_sources() -> list[tuple[str, str]]:
    root = Path(__file__).resolve().parents[1] / "app" / "services"
    return [(name, (root / name).read_text(encoding="utf-8")) for name in _RENDER_MODULES]


class TestNoInlineEasing:
    def test_no_bezier_literals_outside_the_token_module(self):
        import re

        offenders: list[str] = []
        for name, source in _render_sources():
            if name in _EASING_HOME:
                continue
            for line_number, line in enumerate(source.splitlines(), start=1):
                if re.search(r"cubic-bezier\s*\(", line):
                    offenders.append(f"{name}:{line_number} {line.strip()[:60]}")
        assert not offenders, (
            "缓动字面量必须来自 `design.EASE_*`：\n" + "\n".join(offenders)
        )

    def test_no_hand_written_spring_expressions(self):
        """弹簧表达式只能从 `motion.ffmpeg_ease()` 来（那里是唯一的公式）。

        手写 `exp(-k*t)` 的危险在于：它**看起来**像弹簧，但过冲量、峰值时刻
        和另外几处对不上 —— 参考做法把这类东西收进 `theme.ts` 就是为了这个。
        """
        import re

        offenders: list[str] = []
        for name, source in _render_sources():
            if name in _EASING_HOME:
                continue
            for line_number, line in enumerate(source.splitlines(), start=1):
                code = line.split("#", 1)[0]
                if re.search(r"\bexp\(|(?<![\w.])sin\(|(?<![\w.])cos\(", code):
                    offenders.append(f"{name}:{line_number} {line.strip()[:60]}")
        assert not offenders, (
            "弹簧表达式必须用 `motion.ffmpeg_ease()`：\n" + "\n".join(offenders)
        )

    def test_presets_are_named_and_used_by_name(self):
        """具名预设是「调用点不写数值」的前提：没有名字就只能到处抄数字。"""
        from app.services.motion import SPRING_PRESETS, spring_kwargs

        assert set(SPRING_PRESETS) >= {"slide", "pop", "entrance"}
        for name in SPRING_PRESETS:
            assert spring_kwargs(name)["overshoot"] > 0
        with pytest.raises(KeyError):  # 拼错名字要当场炸，不要静默用默认值
            spring_kwargs("slidee")


# ---------------------------------------------------------------------------
# 配图装填：**按内容包围盒**放图，而不是按「整张图（含它自己的白边）」
# ---------------------------------------------------------------------------
def _padded_figure(tmp_path: Path, box=(450, 470, 1150, 730)) -> Path:
    """造一张「图小、白边大」的插图：1600×1200 画布，内容只占中间一小块。

    论文插图大量是这种：导出时带一圈页边距/画布留白。按整张图等比缩放，
    内容就只占卡片中间一条，整帧一半的行是空白 —— 用户报的「画面合成」问题。
    """
    import pymupdf

    path = tmp_path / "padded.png"
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 1600, 1200), False)
    pix.set_rect(pix.irect, (255, 255, 255))
    pix.set_rect(pymupdf.IRect(*box), (60, 60, 60))
    pix.save(str(path))
    return path


def _card_image_rect(html: str) -> tuple[float, float, float, float]:
    match = re.search(
        r'<image x="([-\d.]+)" y="([-\d.]+)" width="(\d+)" height="(\d+)"', html
    )
    assert match, "图片卡里没有 <image>"
    x, y, w, h = match.groups()
    return float(x), float(y), float(w), float(h)


class TestFigureIsFittedByItsContent:
    """「裁掉图片自身白边再装填」的验收：它是**量**出来的，不是感觉出来的。

    实测（896×742 的卡）：论文里的高瘦插图 10.1% 的空白行 → 0.0%；
    白边极大的插图 73.5% → 63.9%。宽图基本不变（它受**宽度**限制，
    卡片再高也放不大）——那是另一件事（卡片高度跟着图的比例走），别混。
    """

    def test_white_margins_do_not_shrink_the_figure(self, tmp_path):
        from app.services import video_render
        from app.services.design import layout_for
        from app.services.video_render import image_card_markup

        layout = layout_for("portrait")
        scene = _scene(tmp_path, image=_padded_figure(tmp_path), kind="figure")
        layout_w = layout.image_box_w

        x, _y, w, _h = _card_image_rect(image_card_markup(scene, layout))
        filled = w / layout_w  # 整张图（含白边）相对卡宽的占比

        # 老行为 = 按整张图装填。**要打在缓存那一层**：`_ink_bbox_cached` 缓存过之后，
        # 改 `_ink_bbox` 是不会生效的（第一版就是这么写出「改前=改后」的假结论的）。
        real = video_render._ink_bbox_cached
        video_render._ink_bbox_cached = lambda *a, **k: None
        try:
            _ox, _oy, old_w, _oh = _card_image_rect(image_card_markup(scene, layout))
        finally:
            video_render._ink_bbox_cached = real

        assert filled > old_w / layout_w * 1.2, (
            f"按内容装填应该明显放大（{old_w:.0f} → {w:.0f} px）；"
            f"如果这条挂了，多半是 `_ink_bbox` 静默失败退回了 `_prepare_image`"
        )

    def test_content_never_leaves_the_card(self, tmp_path):
        """溢出卡片的是**图外的白边**，内容一个像素都不能出去。"""
        from app.services.design import layout_for
        from app.services.video_render import image_card_markup

        layout = layout_for("portrait")
        box = (450, 470, 1150, 730)
        scene = _scene(tmp_path, image=_padded_figure(tmp_path, box), kind="figure")
        x, y, w, _h = _card_image_rect(image_card_markup(scene, layout))
        scale = w / 1600
        left, right = x + box[0] * scale, x + box[2] * scale
        top, bottom = y + box[1] * scale, y + box[3] * scale
        assert left >= -1 and top >= -1, f"内容左上角出界：({left:.1f}, {top:.1f})"
        assert right <= layout.image_box_w + 1, f"内容右边出界：{right:.1f}"
        assert bottom <= layout.image_box_h + 1, f"内容下边出界：{bottom:.1f}"

    def test_frame_hugs_the_content_not_the_canvas(self, tmp_path):
        """细边框贴着**内容**走：不然框里还是一大圈白，等于没改。"""
        from app.services.design import layout_for
        from app.services.video_render import image_card_markup

        layout = layout_for("portrait")
        box = (450, 470, 1150, 730)
        scene = _scene(tmp_path, image=_padded_figure(tmp_path, box), kind="figure")
        html = image_card_markup(scene, layout)
        x, y, w, _h = _card_image_rect(html)
        rects = [tuple(float(v) for v in m) for m in re.findall(
            r'<rect x="([-\d.]+)" y="([-\d.]+)" width="([\d.]+)" height="([\d.]+)" rx="8" '
            r'fill="none"',
            html,
        )]
        assert rects, "图片卡里没有细边框"
        frame_x, frame_y, frame_w, frame_h = rects[0]
        scale = w / 1600
        assert abs((frame_x + 2) - (x + box[0] * scale)) < 2, "框没有贴住内容左边"
        assert abs((frame_y + 2) - (y + box[1] * scale)) < 2, "框没有贴住内容上边"
        assert abs(frame_w - (box[2] - box[0]) * scale) < 6, "框宽不等于内容宽"
        assert abs(frame_h - (box[3] - box[1]) * scale) < 6, "框高不等于内容高"

    def test_all_white_figure_does_not_crash(self, tmp_path):
        """整张全白的图（量不出包围盒）必须退回老路径，而不是抛异常。"""
        import pymupdf

        from app.services.design import layout_for
        from app.services.video_render import image_card_markup

        path = tmp_path / "white.png"
        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 300, 300), False)
        pix.set_rect(pix.irect, (255, 255, 255))
        pix.save(str(path))
        scene = _scene(tmp_path, image=path, kind="figure")
        x, y, w, h = _card_image_rect(image_card_markup(scene, layout_for("portrait")))
        assert w > 0 and h > 0
        assert x >= 0 and y >= 0
