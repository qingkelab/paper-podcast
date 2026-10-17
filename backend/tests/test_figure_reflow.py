"""「文字层跟着图走」：图注与强调行贴着**图的下沿**，而不是钉在卡片的底边上。

为什么值得单独一组测试：图片区是固定尺寸，而论文插图大多是**宽**的 ——
一张 962×440 的图装进 912×742 的卡只占中间 400px 高，图的下沿在 y≈645，
而图注钉在 y=822：**图注离它描述的那张图 348px**（实测）。观众看到的不是
「一张带说明的图」，而是「图在上面、说明在下面很远的地方」。

这条改动同时是**两条不变量**的边界，所以两边都要钉住：

1. 图铺满卡片时（高瘦的图）返回值必须**逐字段相同** —— 老片子重合成要一模一样；
2. 图注/强调行只许**往上挪**，不许压到图上、也不许跑到字幕带下面。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services import design, video_core, video_render
from app.services.design import LANDSCAPE, PORTRAIT, content_layout

LAY = PORTRAIT


def _wide_figure(tmp_path: Path, *, w: int = 1200, h: int = 400) -> Path:
    """宽图：内容铺满整幅（所以「内容包围盒」≈整张图），装进卡片后只占中间一条。"""
    import pymupdf

    path = tmp_path / f"wide-{w}x{h}.png"
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, w, h), False)
    pix.set_rect(pix.irect, (255, 255, 255))
    pix.set_rect(pymupdf.IRect(20, 20, w - 20, h - 20), (70, 70, 70))
    pix.save(str(path))
    return path


def _scene(path: Path, **kwargs) -> video_core.Scene:
    defaults = dict(
        start=0.0,
        end=6.0,
        image=path,
        kind="figure",
        caption="Figure 1: Overview of the pipeline.",
        text="这一段的字幕在这里。",
    )
    defaults.update(kwargs)
    return video_core.Scene(**defaults)  # type: ignore[arg-type]


class TestFigureHeightIsMeasured:
    def test_wide_figure_does_not_fill_the_card(self, tmp_path):
        scene = _scene(_wide_figure(tmp_path))
        height = video_render.fitted_figure_height(scene, LAY)
        assert height is not None
        assert height < LAY.image_box_h * 0.7, (
            f"1200×400 的图不该占满 {LAY.image_box_h}px 高的卡（量到 {height}）"
        )

    def test_tall_figure_fills_the_card(self, tmp_path):
        scene = _scene(_tall_figure(tmp_path))
        height = video_render.fitted_figure_height(scene, LAY)
        assert height == pytest.approx(LAY.image_box_h, abs=2)

    def test_cover_and_diagram_scenes_are_not_measured(self, tmp_path):
        """封面（有 headline）与图解镜头都不走「跟着图走」这条路。"""
        scene = _scene(_wide_figure(tmp_path), headline="爆款标题")
        assert video_render.fitted_figure_height(scene, LAY) is None


def _tall_figure(tmp_path: Path, *, w: int = 500, h: int = 900) -> Path:
    import pymupdf

    path = tmp_path / f"tall-{w}x{h}.png"
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, w, h), False)
    pix.set_rect(pix.irect, (255, 255, 255))
    pix.set_rect(pymupdf.IRect(20, 20, w - 20, h - 20), (70, 70, 70))
    pix.save(str(path))
    return path


class TestCaptionFollowsTheFigure:
    def test_caption_moves_up_under_the_figure(self):
        """宽图：图注从「卡片底边下 6px」挪到「图下沿下 `FIGURE_CAPTION_GAP`」。"""
        figure_h = 400
        new = content_layout(LAY, figure_h, has_caption=True, has_point=False)
        ink_bottom = LAY.image_top + (LAY.image_box_h + figure_h) // 2
        assert new.caption_top == ink_bottom + design.FIGURE_CAPTION_GAP
        assert new.caption_top < LAY.caption_top - 100, "宽图的图注必须明显上移"

    def test_the_highlight_row_stays_at_the_bottom(self):
        """**强调行不跟着图走**（用户要求：「高亮的文字位置不要靠上，放在底部，字幕上边就行」）。

        原来它跟着图上移，于是同一个元素在不同段里上下跳（实测宽图时 y≈740、窄图时 878）。
        现在它固定在 `point_top`，只要求它**永远贴在字幕带上面**。
        """
        for figure_h in (300, 400, 599, 742):
            new = content_layout(LAY, figure_h, has_caption=True, has_point=True)
            assert new.point_top == LAY.point_top, f"图高 {figure_h} 时强调行动了：{new.point_top}"
        assert LAY.point_top + LAY.point_height <= LAY.subtitle_top, "强调行本来就在字幕带上面"
        assert LAY.subtitle_top - (LAY.point_top + LAY.point_height) <= 24, "而且紧贴字幕带"

    def test_caption_font_grows_one_step_when_there_is_room(self):
        new = content_layout(LAY, 400, has_caption=True, has_point=True)
        assert new.caption_max_font == LAY.caption_max_font + design.CAPTION_FONT_STEP

    def test_full_card_returns_the_layout_untouched(self):
        """图铺满卡片 → 逐字段相同（老片子重合成必须一模一样）。"""
        same = content_layout(LAY, LAY.image_box_h, has_caption=True, has_point=True)
        assert same == LAY
        assert content_layout(LAY, None, has_caption=True, has_point=True) is LAY

    def test_caption_never_moves_down(self):
        """只许往上挪：图很高时保持原位，绝不把说明往下推（那会顶到字幕带）。"""
        for figure_h in range(100, LAY.image_box_h + 1, 50):
            new = content_layout(LAY, figure_h, has_caption=True, has_point=True)
            assert new.caption_top <= LAY.caption_top
            assert new.point_top == LAY.point_top, "强调行的位置一个字都不该动"
            assert new.subtitle_top == LAY.subtitle_top
            assert new.image_top == LAY.image_top
            assert new.waveform_top == LAY.waveform_top

    def test_caption_never_overlaps_the_figure(self):
        """图注必须落在**图的下沿之下**。第一版按 `image_top + figure_h` 算，
        忘了图在卡里是居中的 —— 图注被摆到了图上（实测渲染出来直接压住图）。"""
        for figure_h in (200, 400, 600, 700):
            new = content_layout(LAY, figure_h, has_caption=True, has_point=True)
            ink_bottom = LAY.image_top + (LAY.image_box_h + figure_h) // 2
            assert new.caption_top >= ink_bottom + design.FIGURE_CAPTION_GAP

    def test_landscape_also_reflows(self):
        new = content_layout(LANDSCAPE, 300, has_caption=True, has_point=True)
        assert new.caption_top < LANDSCAPE.caption_top
        assert new.point_top + LANDSCAPE.point_height <= LANDSCAPE.subtitle_top - 8


class TestRenderedCaptionIsWhereTheLayoutSays:
    def test_caption_ink_lands_under_the_figure(self, tmp_path):
        """**像素级**：真的把骨架渲染出来，量图注那行字的墨迹落在哪一行。

        只看 `content_layout` 的返回值是不够的 —— 页面是拼出来的，
        「版式改了但渲染没接上」在画面上就是「什么都没发生」。
        """
        import pymupdf

        scene = _scene(_wide_figure(tmp_path))
        figure_h = video_render.fitted_figure_height(scene, LAY)
        assert figure_h is not None
        new = content_layout(LAY, figure_h, has_caption=True, has_point=False)
        out = tmp_path / "chrome.png"
        video_render.render_chrome(scene, out, layout=new, include_subtitle=False)

        pix = pymupdf.Pixmap(str(out))
        n, width, height, data = pix.n, pix.width, pix.height, pix.samples
        rows = []
        for y in range(LAY.image_top, LAY.subtitle_top):
            for x in range(new.subtitle_left, new.subtitle_left + new.subtitle_width, 2):
                i = (y * width + x) * n
                if data[i + 3] > 40 and min(data[i], data[i + 1], data[i + 2]) < 200:
                    rows.append(y)
                    break
        assert rows, "图注一个字都没渲染出来"
        ink_bottom = LAY.image_top + (LAY.image_box_h + figure_h) // 2
        assert min(rows) >= ink_bottom, (
            f"图注压到图上了：图注墨迹从 {min(rows)} 开始，图的下沿在 {ink_bottom}"
        )
        assert min(rows) < LAY.caption_top - 100, "图注没有跟着图上移"
