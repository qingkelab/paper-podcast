"""画面渲染：把 `Scene` 画成 SVG（resvg）再光栅化成 PNG —— 就是「画面长什么样」。

这一层管整页骨架、图片卡（含封面毛玻璃与大字标题）、图注折行与字号、强调行、
字幕带（数字高亮）、图内聚光灯、片尾品牌卡、进度条素材，以及**字幕逐句出现的时间窗**
（`beat_windows` / `beat_windows_aligned` —— 谁先出现、出现到什么时候，和字幕带怎么画
是同一件事）。只依赖 `design` + `video_core` + 叶子模块（`panels` / `illustration` / `branding`），
**不 import `video.py`**。

分层：`design → video_core → video_render → video_encode → video`（本层是第 3 层）。
想改滤镜链 / 帧数 / ffmpeg → `video_encode.py`；想改场景编排 / 转场 / 素材池 → `video.py`。
"""

from __future__ import annotations

import base64
import html
import logging
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from ..branding import BRAND_CTA_SUBTITLE, BRAND_CTA_TITLE, logo_path
from .audio_track import Pause, align_boundaries
from .design import (
    BEAT_IMAGE_MIN_SEC,
    BG_COLOR,
    BRAND_DARK,
    BRAND_DARK_PANEL,
    BRAND_GOLD,
    BRAND_GREEN,
    BRAND_TEXT,
    CAPTION_BAND_MAX_FONT,
    CAPTION_COLOR,
    CAPTION_FADE_SEC,
    CHIP_POP_BUMP_DELAY,
    CHIP_POP_FPS,
    CHIP_SPRING_SEC,
    COVER_ACCENT_GAP,
    COVER_ACCENT_H,
    COVER_ACCENT_W,
    COVER_GLASS_ANCHOR,
    COVER_GLASS_BLUR,
    COVER_GLASS_BLUR_REF_SCALE,
    COVER_GLASS_TINT,
    COVER_GLASS_VEIL,
    COVER_GLASS_ZOOM,
    COVER_GLASS_ZOOM_CY,
    COVER_HEADLINE_BASELINE,
    COVER_HEADLINE_LEADING,
    COVER_PAD,
    COVER_PANEL_FILL,
    COVER_PANEL_PAD,
    COVER_PANEL_RADIUS,
    COVER_SUBTITLE_COLOR,
    COVER_SUBTITLE_FONT,
    COVER_TITLE_COLOR,
    FOCUS_BORDER,
    FOCUS_BORDER_W,
    FOCUS_DIM,
    FOCUS_DIM_ALPHA,
    FOCUS_LABEL_CHARS,
    FONT_STACK,
    FRAME_STROKE,
    POINT_BAR,
    POINT_BAR_W,
    POINT_BG,
    POINT_TEXT,
    PORTRAIT,
    PROGRESS_BAR_COLOR,
    PROGRESS_BAR_H,
    SUBTITLE_BG,
    SUBTITLE_RULE,
    SUBTITLE_TEXT,
    VIDEO_H,
    VIDEO_W,
    WATERMARK_PAD,
    WATERMARK_W,
    Layout,
    cover_panel_width,
    cover_title_layout,
    hook_lines,
    wrap_units,
)
from .illustration import _char_width, _tokenize
from .panels import Panel, detect_panels, panel_count_from_caption, panel_labels
from .prompts.points import _NUMBER_TOKEN, normalize_focus
from .video_core import ImageAsset, Scene, VideoError

# 沿用原来的 logger 名：main.py 的日志 format 里带 %(name)s，搬家不该换掉日志通道。
logger = logging.getLogger("app.services.video")

__all__ = [
    "BeatLayers",
    "FigurePanels",
    "_caption_line_parts",
    "_fit_subtitle",
    "_image_data_uri",
    "_logo_data_uri",
    "_number_runs",
    "_panel_label",
    "_prepare_image",
    "_rasterize_custom",
    "_render_slide_layers",
    "_wrap",
    "beat_windows",
    "beat_windows_aligned",
    "build_panel_catalog",
    "image_card_markup",
    "progress_bar_svg",
    "render_beat_layers",
    "render_caption_band",
    "render_caption_chip",
    "render_chrome",
    "render_endcard",
    "render_focus_overlay",
    "render_image_card",
    "render_point_row",
    "render_slide",
]


# --------------------------------------------------------------------------
# 图片 / logo → data URI，以及折行与字号
# --------------------------------------------------------------------------


_LOGO_CACHE: dict[int, tuple[str, int, int]] = {}


def _logo_data_uri(width: int) -> tuple[str, int, int] | None:
    """读取内置的社区 logo，缩放到指定宽度后转 data URI。

    每帧都要内嵌一次，所以按宽度缓存 —— 否则每帧都要重新解码缩放同一张 PNG。
    logo 是**浅色**字标（为深色底设计），放在白底正文上必须垫一块深色底，
    否则白字会消失。片尾卡整张就是深色，可以直接用。
    """
    if width in _LOGO_CACHE:
        return _LOGO_CACHE[width]

    path = logo_path()
    if not path.exists():
        logger.warning("未找到社区 logo（%s），跳过水印", path)
        return None

    try:
        import pymupdf

        pix = pymupdf.Pixmap(str(path))
        if width != pix.width:
            target_h = max(int(pix.height * width / pix.width), 1)
            pix = pymupdf.Pixmap(pix, width, target_h)
        uri = "data:image/png;base64," + base64.b64encode(pix.tobytes("png")).decode("ascii")
    except Exception as exc:  # noqa: BLE001
        logger.warning("社区 logo 处理失败，跳过水印：%s", exc)
        return None

    _LOGO_CACHE[width] = (uri, pix.width, pix.height)
    return _LOGO_CACHE[width]


def _prepare_image(path: Path, max_w: int, max_h: int) -> tuple[str, int, int]:
    """把图缩放到展示尺寸并转成 data URI。

    先缩放再内嵌是有必要的：原始配图可能接近 1000x1200，直接 base64 内嵌会让
    每张幻灯片的 SVG 膨胀到几百 KB，resvg 每次都要重新解码整张大图。
    缩放后单帧渲染从 ~200ms 降到 ~40ms，30 帧就是好几秒的差别。
    """
    try:
        import pymupdf
    except ImportError as exc:  # pragma: no cover
        raise VideoError("缺少 pymupdf，无法处理配图") from exc

    try:
        pix = pymupdf.Pixmap(str(path))
    except Exception as exc:  # noqa: BLE001
        raise VideoError(f"配图无法读取：{path.name}（{exc}）") from exc

    scale = min(max_w / pix.width, max_h / pix.height, 1.0)
    if scale < 1.0:
        try:
            target_w = max(int(pix.width * scale), 1)
            target_h = max(int(pix.height * scale), 1)
            pix = pymupdf.Pixmap(pix, target_w, target_h)
        except Exception as exc:  # noqa: BLE001
            logger.warning("配图缩放失败，使用原尺寸（会慢一些）：%s", exc)

    data = pix.tobytes("png")
    return (
        "data:image/png;base64," + base64.b64encode(data).decode("ascii"),
        pix.width,
        pix.height,
    )


def _image_data_uri(path: Path, *, max_side: int = 1400) -> tuple[str, int, int]:
    """把图缩到 `max_side` 以内再 base64 内嵌，返回 data URI。

    封面要用 `preserveAspectRatio="slice"` 裁满整张卡（比例不受控），
    所以不能走 `_prepare_image`（那个按 contain 算尺寸）。模糊之后细节本来就没了，
    缩到 1400 以内既够用又不让 SVG 膨胀。

    返回 `(data URI, 原始宽, 原始高)` —— 宽高要给调用方算**它在卡片里被放大/缩小了多少**，
    模糊半径得按那个倍率走（见 `image_card_markup`）。
    """
    try:
        import pymupdf
    except ImportError as exc:  # pragma: no cover
        raise VideoError("缺少 pymupdf，无法处理配图") from exc
    try:
        pix = pymupdf.Pixmap(str(path))
    except Exception as exc:  # noqa: BLE001
        raise VideoError(f"配图无法读取：{path.name}（{exc}）") from exc
    scale = min(max_side / max(pix.width, pix.height), 1.0)
    if scale < 1.0:
        try:
            pix = pymupdf.Pixmap(pix, max(int(pix.width * scale), 1), max(int(pix.height * scale), 1))
        except Exception as exc:  # noqa: BLE001
            logger.warning("封面底图缩放失败，使用原尺寸：%s", exc)
    return (
        "data:image/png;base64," + base64.b64encode(pix.tobytes("png")).decode("ascii"),
        pix.width,
        pix.height,
    )


def _wrap(text: str, max_units: float) -> list[str]:
    """按全角单位宽度折行（西文按 0.55 计）。

    实现在 `design.wrap_units`：折行规则 HTML 与 SVG 两条渲染路径必须共用，
    所以它跟封面标题的断行规则（`design.hook_lines`）放在一起。这里只留旧名字。
    """
    return wrap_units(text, max_units)


def _fit_subtitle(
    text: str,
    *,
    max_lines: int = 6,
    max_size: float = 30.0,
    layout: Layout = PORTRAIT,
) -> tuple[float, list[str]]:
    """选一个既能放下、又不至于太小的字号。

    竖版画幅只有 896px 宽，比横版窄很多，所以必须**同时**检查行数和总高度：
    光看行数会在字号偏大时让文字溢出到画面外。
    """
    text = " ".join((text or "").split())
    if not text:
        return 24.0, []

    def wrap_at(size: float) -> list[str]:
        units = layout.subtitle_width / size
        lines: list[str] = []
        current = ""
        width = 0.0
        for token in _tokenize(text):
            token_width = sum(_char_width(c) for c in token)
            if current and width + token_width > units:
                lines.append(current.strip())
                current = ""
                width = 0.0
            if not current and token == " ":
                continue
            current += token
            width += token_width
        if current.strip():
            lines.append(current.strip())
        return lines

    for size in (max_size - 2 * step for step in range(6)):
        lines = wrap_at(size)
        if len(lines) <= max_lines and len(lines) * size * 1.36 <= layout.subtitle_max_height:
            return size, lines

    # 还是放不下：用最小字号，超出部分截断加省略号
    size = min(20.0, max_size)
    lines = wrap_at(size)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1][:-1] + "…"
    return size, lines


def render_slide(
    scene: Scene,
    output_path: Path,
    *,
    include_subtitle: bool = True,
    include_point: bool = True,
    layout: Layout = PORTRAIT,
) -> Path:
    """渲染一帧画面：配图 + 图注 + 字幕（白底竖版，与论文首页同尺寸）。

    `include_subtitle=False` 时只画字幕区的底色和分隔线，不画文字 ——
    这一段的字幕会拆成几句、由 `render_caption_band` 单独渲染、再按时间叠上去
    （见 `CaptionBand`）。底图里若还留着一整段文字，第一句出现之前就会露馅。

    这个函数现在等于「骨架 + 图片卡」两层叠在一起（见下面的
    `render_chrome` / `render_image_card`）。保留它是为了两件事：
    现有的测试与「一页到底」的简单用法，以及需要单张整页图时不必自己拼。
    """
    return _render_slide_layers(
        scene,
        output_path,
        include_subtitle=include_subtitle,
        include_point=include_point,
        include_image=True,
        layout=layout,
    )


def render_chrome(
    scene: Scene,
    output_path: Path,
    *,
    include_subtitle: bool = True,
    include_point: bool = True,
    layout: Layout = PORTRAIT,
) -> Path:
    """只渲染**骨架**：白底 + logo + 图注 + 强调行 + 字幕带（图片区留白）。

    为什么要拆出骨架：转场要的是「图片在变、版面不动」。实测那条 40 秒的论文宣传片
    就是这种观感 —— 白底、每 9~10 秒换一次画面、换的时候整页快速淡过去。
    只有把图片单独成层，才能做到「图片溶解、标题/图注/字幕/进度条一动不动」。
    """
    return _render_slide_layers(
        scene,
        output_path,
        include_subtitle=include_subtitle,
        include_point=include_point,
        include_image=False,
        layout=layout,
    )


def image_card_markup(scene: Scene, layout: Layout = PORTRAIT) -> str:
    """图片卡那一层的 SVG 内容，**坐标以图片区左上角为原点**。

    为什么要单独抽出来：卡片这一层现在有两个消费者 ——
    分层的编码路径（`render_image_card`，图片单独一层、参与转场）和
    「一页到底」的合成路径（`render_slide` 把它整段贴进骨架里）。
    两处如果各画一份，封面的大字标题就只会在其中一条路径上出现，
    而这种不一致平时看不出来（谁也不会同时跑两条路径对比）。

    ## 两种卡
    - **普通页**：图片 + 贴着它的那圈浅灰细边框（白底上用来界定图片边界）；
    - **封面页**（`scene.headline` 非空）：论文首页整幅铺满 → 高斯模糊 → 白纱 →
      一块玻璃面板，面板里是爆款标题（大字）+ 品牌绿短杠 + 论文原题（小字）。
      模糊的半径按卡片尺寸缩放（横版卡片更大，同一个 stdDeviation 看起来会更清楚）。
    """
    card_w, card_h = layout.image_box_w, layout.image_box_h
    clip_id = "coverclip"

    size, lines, sub_lines, block_h, panel_h = cover_title_layout(
        scene.headline, scene.caption, layout
    )
    if not lines:
        data_uri, img_w, img_h = _prepare_image(scene.image, card_w, card_h)
        img_x = (card_w - img_w) / 2
        img_y = (card_h - img_h) / 2
        return "\n".join(
            [
                f'<rect x="{img_x - 2:.1f}" y="{img_y - 2:.1f}" width="{img_w + 4}" '
                f'height="{img_h + 4}" rx="8" fill="none" stroke="{FRAME_STROKE}" stroke-width="1.5"/>',
                f'<image x="{img_x:.1f}" y="{img_y:.1f}" width="{img_w}" height="{img_h}" href="{data_uri}"/>',
            ]
        )

    # 底图用**裁满**（slice）而不是等比放下：模糊层要铺满整张卡，留白边就露馅了
    data_uri, page_w, page_h = _image_data_uri(scene.image)
    # 模糊半径要跟着**首页在卡片里被放大了多少**走：竖版是把整页缩到 ~0.96 倍，
    # 横版卡片又宽又矮，裁满时首页被放大到近 2 倍 —— 同一个半径在横版上会明显更「清楚」。
    # 实测（同一篇论文）：竖版半径 6 的纹理跨度和横版半径 4.5 对不上，按倍率走才一致。
    page_scale = max(card_w / page_w, card_h / page_h)
    blur = COVER_GLASS_BLUR * (page_scale / COVER_GLASS_BLUR_REF_SCALE)
    panel_w = cover_panel_width(layout)
    panel_x = (card_w - panel_w) / 2
    panel_y = max((card_h - panel_h) / 2, COVER_PAD)

    parts: list[str] = [
        "<defs>",
        f'<clipPath id="{clip_id}"><rect width="{card_w}" height="{card_h}" rx="8"/></clipPath>',
        f'<filter id="coverblur" x="-5%" y="-5%" width="110%" height="110%">'
        f'<feGaussianBlur stdDeviation="{blur:.1f}"/></filter>',
        "</defs>",
        f'<g clip-path="url(#{clip_id})">',
        f'<image x="0" y="0" width="{card_w}" height="{card_h}" preserveAspectRatio="xMidY{COVER_GLASS_ANCHOR} slice" '
        f'transform="translate({card_w / 2:.1f},{card_h * COVER_GLASS_ZOOM_CY:.1f}) '
        f'scale({COVER_GLASS_ZOOM}) translate({-card_w / 2:.1f},{-card_h * COVER_GLASS_ZOOM_CY:.1f})" '
        f'filter="url(#coverblur)" href="{data_uri}"/>',
        # 白纱：把「纸上的字」压成纹理（读不出字，但看得出是论文）
        f'<rect width="{card_w}" height="{card_h}" fill="{COVER_GLASS_TINT}" opacity="{COVER_GLASS_VEIL}"/>',
        # 玻璃面板：比纱再实一点的一层 + 一道亮边，这就是「毛玻璃」的观感
        f'<rect x="{panel_x:.1f}" y="{panel_y:.1f}" width="{panel_w:.1f}" height="{panel_h:.1f}" '
        f'rx="{COVER_PANEL_RADIUS}" fill="#ffffff" opacity="{COVER_PANEL_FILL}" '
        f'stroke="#ffffff" stroke-width="2"/>',
        "</g>",
    ]

    center = card_w / 2
    y = panel_y + COVER_PANEL_PAD + size * COVER_HEADLINE_BASELINE
    for line in lines:
        parts.append(
            f'<text x="{center:.1f}" y="{y:.1f}" text-anchor="middle" font-family="{FONT_STACK}" '
            f'font-size="{size:.0f}" font-weight="700" fill="{COVER_TITLE_COLOR}">{html.escape(line)}</text>'
        )
        y += size * COVER_HEADLINE_LEADING
    bar_y = panel_y + COVER_PANEL_PAD + block_h + COVER_ACCENT_GAP
    parts.append(
        f'<rect x="{center - COVER_ACCENT_W / 2:.1f}" y="{bar_y:.1f}" width="{COVER_ACCENT_W}" '
        f'height="{COVER_ACCENT_H}" rx="{COVER_ACCENT_H / 2:.1f}" fill="{BRAND_GREEN}"/>'
    )
    if sub_lines:
        y = bar_y + COVER_ACCENT_H + COVER_ACCENT_GAP + COVER_SUBTITLE_FONT
        for line in sub_lines:
            parts.append(
                f'<text x="{center:.1f}" y="{y:.1f}" text-anchor="middle" font-family="{FONT_STACK}" '
                f'font-size="{COVER_SUBTITLE_FONT}" fill="{COVER_SUBTITLE_COLOR}">{html.escape(line)}</text>'
            )
            y += COVER_SUBTITLE_FONT * 1.4
    return "\n".join(parts)


def render_image_card(
    scene: Scene, output_path: Path, *, layout: Layout = PORTRAIT
) -> Path:
    """只渲染**图片卡**：图片 + 贴着图片的那圈浅灰细边框，其余透明。

    画幅是图片区（`layout.image_box_w × layout.image_box_h`），叠在骨架的 `(20, layout.image_top)`。
    边框跟着图片走（它是贴着图片量的 2px 内缩），所以它属于这一层 ——
    转场时「带框的图」整体淡入淡出，而不是框留在原地、图在里面换。

    **封面帧（`scene.headline` 非空）走另一套排版**：上面是爆款标题（大字 + 品牌绿短杠），
    下面才是论文首页的图。放在这一层而不是骨架层，是因为这一层已经被转场当成
    「会变的那一块」在用了 —— 标题跟着封面一起淡入、一起溶解掉，不需要动滤镜图里
    任何一处坐标（骨架与图片窗口全程不变）。
    """
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{layout.image_box_w}" height="{layout.image_box_h}" '
        f'viewBox="0 0 {layout.image_box_w} {layout.image_box_h}">',
        image_card_markup(scene, layout),
        "</svg>",
    ]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _rasterize_custom(
        "\n".join(parts), output_path, width=layout.image_box_w, height=layout.image_box_h
    )
    return output_path


def _render_slide_layers(
    scene: Scene,
    output_path: Path,
    *,
    include_subtitle: bool,
    include_point: bool,
    include_image: bool,
    layout: Layout = PORTRAIT,
) -> Path:
    """骨架与整页共用的渲染实现（`include_image` 决定图片层要不要画进来）。"""
    font_size, lines = _fit_subtitle(scene.text)

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{layout.width}" height="{layout.height}" '
        f'viewBox="0 0 {layout.width} {layout.height}">',
        f'<rect width="{layout.width}" height="{layout.height}" fill="{BG_COLOR}"/>',
    ]

    # ⚠️ 这里**没有**顶部标题条：论文标题不再挂在每一帧顶上。
    # 挂在顶上的那行小字有两个问题：一是 22px 的论文全名在一屏里根本读不完，
    # 二是「每帧都有的东西」等于没有信息量。标题现在只出现在封面（见 render_image_card），
    # 而且换成了抓人的爆款标题 + 下面一行的论文原题。

    # 右上角社区 logo 水印。
    # logo 是浅色字标，白底上直接放会看不见，所以垫一块深色圆角底。
    logo = _logo_data_uri(WATERMARK_W)
    if logo:
        uri, lw, lh = logo
        chip_w, chip_h = lw + WATERMARK_PAD * 2, lh + WATERMARK_PAD * 2
        chip_x = layout.width - 40 - chip_w
        chip_y = 18
        parts.append(
            f'<rect x="{chip_x}" y="{chip_y}" width="{chip_w}" height="{chip_h}" '
            f'rx="8" fill="{BRAND_DARK}" opacity="0.9"/>'
        )
        parts.append(
            f'<image x="{chip_x + WATERMARK_PAD}" y="{chip_y + WATERMARK_PAD}" '
            f'width="{lw}" height="{lh}" href="{uri}"/>'
        )

    # 配图（浅灰细边框，白底上用来界定图片边界）；骨架模式下这块留白，由图片卡叠上来。
    # 用的是和分层路径**同一段** markup（`image_card_markup`），所以封面的大字标题
    # 在一页到底的用法里也在 —— 两条路径不会画成两个样子。
    if include_image:
        parts.append(
            f'<g transform="translate({layout.image_box_left}, {layout.image_top})">'
            f"{image_card_markup(scene, layout)}</g>"
        )

    # 图注
    if scene.caption:
        caption_lines = _wrap(scene.caption, layout.subtitle_width / 18)[:2]
        for offset, line in enumerate(caption_lines):
            parts.append(
                f'<text x="{layout.subtitle_left}" y="{layout.caption_top + 22 + offset * 23}" '
                f'font-family="{FONT_STACK}" font-size="18" fill="{CAPTION_COLOR}">'
                f"{html.escape(line)}</text>"
            )

    # 字幕区：极浅底 + 一条分隔线，正文用深色。
    # 这里**不显示「主播A / 主播B」标签** —— 谁在说话听声音就知道，
    # 画面上多一行标签只会分散注意力、也占掉字幕的空间。
    parts.append(
        f'<rect x="0" y="{layout.subtitle_top}" width="{layout.width}" '
        f'height="{layout.height - layout.subtitle_top}" fill="{SUBTITLE_BG}"/>'
    )
    parts.append(
        f'<rect x="0" y="{layout.subtitle_top}" width="{layout.width}" height="2" fill="{SUBTITLE_RULE}"/>'
    )

    if include_point and scene.point:
        # 强调行：浅蓝底 + 左侧色条 + 大字。它是画面上最抢眼的一行，
        # 也是「突出解读内容」的落点（不是装饰，是每一段的核心结论）。
        parts.append(
            f'<rect x="{layout.point_left}" y="{layout.point_top}" width="{layout.point_width}" '
            f'height="{layout.point_height}" rx="10" fill="{POINT_BG}"/>'
        )
        parts.append(
            f'<rect x="{layout.point_left}" y="{layout.point_top}" width="{POINT_BAR_W}" '
            f'height="{layout.point_height}" rx="3" fill="{POINT_BAR}"/>'
        )
        point_size, point_lines = _fit_subtitle(
            scene.point, max_lines=1, max_size=layout.point_max_font
        )
        if point_lines:
            baseline = layout.point_top + layout.point_height / 2 + point_size * 0.36
            parts.append(
                f'<text x="{layout.point_left + POINT_BAR_W + 18}" y="{baseline:.0f}" '
                f'font-family="{FONT_STACK}" font-size="{point_size:.0f}" '
                f'font-weight="600" fill="{POINT_TEXT}">{html.escape(point_lines[0])}</text>'
            )

    if include_subtitle:
        line_height = font_size * 1.36
        for offset, line in enumerate(lines):
            parts.append(
                f'<text x="{layout.subtitle_left}" y="{layout.subtitle_text_top + offset * line_height:.0f}" '
                f'font-family="{FONT_STACK}" font-size="{font_size:.0f}" '
                f'fill="{SUBTITLE_TEXT}">{html.escape(line)}</text>'
            )

    parts.append("</svg>")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    _rasterize_custom(
        "\n".join(parts),
        output_path,
        width=layout.width,
        height=layout.height,
    )
    return output_path


def _number_runs(line: str) -> list[tuple[str, bool]]:
    """把一行字幕切成 [普通文字, 数字, 普通文字, ...]，数字段要单独上色。

    只认阿拉伯数字：「第一」「两倍」这种中文数字太高频，标出来满屏都是重点。
    """
    runs: list[tuple[str, bool]] = []
    cursor = 0
    for match in _NUMBER_TOKEN.finditer(line):
        if match.start() > cursor:
            runs.append((line[cursor : match.start()], False))
        runs.append((match.group(0), True))
        cursor = match.end()
    if cursor < len(line):
        runs.append((line[cursor:], False))
    return runs


def _caption_line_parts(
    line: str,
    *,
    baseline: float,
    font_size: float,
    layout: Layout = PORTRAIT,
    chip: bool = True,
) -> list[str]:
    """渲染字幕的一行：数字用强调色 + 浅色底标出来，其余照常。

    为什么值得单独做：一期论文解读里真正有信息量的往往就是那个「67%」，
    而它夹在一整行灰白文字里最容易被读漏。强调它不需要任何模型输出，也不会看错。

    `chip=False` 时**不画胶囊**、数字也用普通文字颜色（整行就是一段普通文字）。
    逐句强调层（`render_caption_chip`）晚 0.12 秒在同一处把它点亮 ——
    带子里本来就画了胶囊的话，那一层叠上去等于没叠（见 docs/BEAT_MOTION.md）。
    默认 `True`：`render_slide` 那条「一页到底」的路一点不变。
    """
    runs = _number_runs(line)
    if not chip or not any(is_number for _, is_number in runs):
        return [
            f'<text x="{layout.subtitle_left}" y="{baseline:.0f}" '
            f'font-family="{FONT_STACK}" font-size="{font_size:.0f}" '
            f'fill="{SUBTITLE_TEXT}">{html.escape(line)}</text>'
        ]

    parts: list[str] = []
    cursor = layout.subtitle_left
    for text_run, is_number in runs:
        width = sum(_char_width(char) for char in text_run) * font_size
        if is_number:
            parts.append(
                f'<rect x="{cursor - 3:.1f}" y="{baseline - font_size * 0.92:.1f}" '
                f'width="{width + 6:.1f}" height="{font_size * 1.22:.1f}" rx="4" '
                f'fill="{POINT_BG}"/>'
            )
        parts.append(
            f'<text x="{cursor:.1f}" y="{baseline:.0f}" '
            f'font-family="{FONT_STACK}" font-size="{font_size:.0f}" '
            f'fill="{POINT_TEXT if is_number else SUBTITLE_TEXT}">{html.escape(text_run)}</text>'
        )
        cursor += width
    return parts


def render_point_row(
    point: str, output_path: Path, *, layout: Layout = PORTRAIT
) -> Path:
    """只渲染「本段要点」那一行（画幅 936×layout.point_height，叠在 y=layout.point_top）。

    单独成层是为了让它**滑入**：一段新的内容开始时，这行从左边滑进来 0.4 秒。
    这是有意保留的动效 —— 它标记「内容换了一段」，跟画面在讲什么直接相关，
    与那种「整张图慢慢放大」的无意义运动不是一回事。
    """
    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{layout.width}" height="{layout.point_height}" '
        f'viewBox="0 0 {layout.width} {layout.point_height}">',
        f'<rect x="{layout.point_left}" y="0" width="{layout.point_width}" height="{layout.point_height}" '
        f'rx="10" fill="{POINT_BG}"/>',
        f'<rect x="{layout.point_left}" y="0" width="{POINT_BAR_W}" height="{layout.point_height}" '
        f'rx="3" fill="{POINT_BAR}"/>',
    ]
    size, lines = _fit_subtitle(point, max_lines=1, max_size=layout.point_max_font)
    if lines:
        baseline = layout.point_height / 2 + size * 0.36
        parts.append(
            f'<text x="{layout.point_left + POINT_BAR_W + 18}" y="{baseline:.0f}" '
            f'font-family="{FONT_STACK}" font-size="{size:.0f}" '
            f'font-weight="600" fill="{POINT_TEXT}">{html.escape(lines[0])}</text>'
        )
    parts.append("</svg>")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _rasterize_custom(
        "\n".join(parts),
        output_path,
        width=layout.width,
        height=layout.point_height,
    )
    return output_path

@dataclass(frozen=True)
class FigurePanels:
    """一张论文原图切出来的子图，以及每个子图「讲什么」。

    字母来自**图注**（`(a) … (b) …`），框来自**像素**（见 `panels.detect_panels`）。
    两者必须对得上号（个数相同）才会被用：对不上时字母和块序就可能错位，
    那还不如不框 —— 框错子图比不框更糟。
    """

    letters: list[str]
    bodies: list[str]
    panels: list[Panel]

    def options(self) -> list[tuple[str, str]]:
        return list(zip(self.letters, self.bodies))

    def focus_for(self, letter: str) -> dict[str, Any] | None:
        """字母 → 聚光灯用的框。认不出的字母返回 None（不框）。

        过一遍 `normalize_focus`：聚光灯那几个下限（框太小、覆盖整张图）在这里也要适用，
        否则会出现「存了一个下游必然丢掉的框」，看起来像是聚光灯莫名其妙没生效。
        """
        key = (letter or "").strip().lower()
        if key not in self.letters:
            return None
        index = self.letters.index(key)
        body = self.bodies[index] if index < len(self.bodies) else ""
        return normalize_focus(self.panels[index].to_focus(_panel_label(body)))


def _panel_label(body: str, *, limit: int = FOCUS_LABEL_CHARS) -> str:
    """子图说明里取一小段当框上的标签（框上的小药丸只有十来个字的位置）。

    英文按**词**截：`normalize_focus` 只按字符数砍，砍在词中间会得到
    「Recurrent state 」这种带半截词的标签 —— 实测第一次就出现了。
    判据是「**有没有中日韩字**」而不是 `isascii()`：英文图注里常有
    `6.93×` / `–` 这类非 ASCII 符号，用 isascii 判会把它们当成中文按字符砍，
    实测得到「Mean accuracy ac」。
    """
    clean = " ".join((body or "").split())
    if not clean:
        return ""
    if not any("\u3400" <= char <= "\u9fff" for char in clean):
        picked: list[str] = []
        for word in clean.split():
            if picked and len(" ".join([*picked, word])) > limit:
                break
            picked.append(word)
            if len(picked) >= 4:
                break
        return " ".join(picked)[:limit]
    return clean[:limit]


def build_panel_catalog(
    figures: list[dict[str, Any]] | None, pool: dict[str, "ImageAsset"]
) -> dict[str, FigurePanels]:
    """给每张**有多子图**的论文原图切出子图，返回 `图片 id → FigurePanels`。

    只在图注里写着 `(a) (b) (c)`（`panel_count_from_caption`）时才做这件事 ——
    那是「这张图有几个子图」的唯一可靠来源，而子图个数又决定几何怎么切。
    **切出来的块数必须和图注说的完全一致**，否则整张图放弃（宁可没有聚光灯）。
    """
    catalog: dict[str, FigurePanels] = {}
    for figure in figures or []:
        if not isinstance(figure, dict):
            continue
        asset_id = str(figure.get("id") or "")
        asset = pool.get(asset_id)
        if asset is None or not asset.exists:
            continue
        caption = str(figure.get("caption") or "")
        count = panel_count_from_caption(caption)
        if not count:
            continue
        labels = panel_labels(caption, cap=count)
        if len(labels) != count:
            continue
        marks = [
            (float(mark["x"]), float(mark["y"]))
            for mark in (figure.get("panel_marks") or [])
            if isinstance(mark, dict) and "x" in mark and "y" in mark
        ]
        panels = detect_panels(asset.path, expected=count, marks=marks or None)
        if len(panels) != count:
            logger.info(
                "配图 %s 的图注说有 %d 个子图，但只切出 %d 块 —— 不做聚光灯",
                figure.get("label") or asset_id,
                count,
                len(panels),
            )
            continue
        catalog[asset_id] = FigurePanels(
            letters=[letter for letter, _ in labels],
            bodies=[body for _, body in labels],
            panels=panels,
        )
        logger.info(
            "配图 %s 切成 %d 个子图，可用于聚光灯", figure.get("label") or asset_id, count
        )
    return catalog


def render_focus_overlay(
    scene: Scene,
    output_path: Path,
    *,
    focus: dict[str, Any],
    layout: Layout = PORTRAIT,
) -> Path:
    """渲染「聚光灯」图层：图卡大小，除了要讲的那一块之外全部压上半透明白。

    图层与图片卡**同一个画幅、同一个坐标系**（图片在卡里是等比居中放好的），
    所以这里只要按比例算出那块区域的像素位置即可 —— 不需要回头去动 PDF。
    """
    data_uri, img_w, img_h = _prepare_image(scene.image, layout.image_box_w, layout.image_box_h)
    img_x = (layout.width - img_w) / 2 - layout.image_box_left
    img_y = (layout.image_box_h - img_h) / 2

    fx = img_x + float(focus["x"]) * img_w
    fy = img_y + float(focus["y"]) * img_h
    fw = float(focus["w"]) * img_w
    fh = float(focus["h"]) * img_h
    pad = FOCUS_BORDER_W + 2

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{layout.image_box_w}" '
        f'height="{layout.image_box_h}" viewBox="0 0 {layout.image_box_w} {layout.image_box_h}">',
        # 四条压暗边（围绕那块「洞」），比用 mask 简单，也不依赖渲染器对 mask 的支持
        f'<g fill="{FOCUS_DIM}" fill-opacity="{FOCUS_DIM_ALPHA}">',
        f'<rect x="0" y="0" width="{layout.image_box_w}" height="{max(fy - pad, 0):.1f}"/>',
        f'<rect x="0" y="{fy + fh + pad:.1f}" width="{layout.image_box_w}" '
        f'height="{max(layout.image_box_h - fy - fh - pad, 0):.1f}"/>',
        f'<rect x="0" y="{max(fy - pad, 0):.1f}" width="{max(fx - pad, 0):.1f}" '
        f'height="{fh + pad * 2:.1f}"/>',
        f'<rect x="{fx + fw + pad:.1f}" y="{max(fy - pad, 0):.1f}" '
        f'width="{max(layout.image_box_w - fx - fw - pad, 0):.1f}" height="{fh + pad * 2:.1f}"/>',
        "</g>",
        # 框线：把「正在讲的是这里」说清楚
        f'<rect x="{fx:.1f}" y="{fy:.1f}" width="{fw:.1f}" height="{fh:.1f}" rx="6" '
        f'fill="none" stroke="{FOCUS_BORDER}" stroke-width="{FOCUS_BORDER_W}"/>',
    ]
    label = str(focus.get("label") or "").strip()
    if label:
        size = 26.0 if not layout.landscape else 30.0
        chip_h = size * 1.9
        chip_w = (len(label) + 2) * size * 0.62
        chip_x = min(max(fx, 8), layout.image_box_w - chip_w - 8)
        chip_y = max(fy - chip_h - 6, 6)
        chip_y = chip_y if chip_y + chip_h < layout.image_box_h else min(fy + fh + 6, layout.image_box_h - chip_h - 6)
        parts.append(
            f'<rect x="{chip_x:.1f}" y="{chip_y:.1f}" width="{chip_w:.1f}" height="{chip_h:.1f}" '
            f'rx="8" fill="{FOCUS_BORDER}"/>'
        )
        parts.append(
            f'<text x="{chip_x + size * 0.5:.1f}" y="{chip_y + chip_h * 0.68:.1f}" '
            f'font-family="{FONT_STACK}" font-size="{size:.0f}" fill="#ffffff">{html.escape(label)}</text>'
        )
    parts.append("</svg>")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _rasterize_custom(
        "\n".join(parts), output_path, width=layout.image_box_w, height=layout.image_box_h
    )
    return output_path


def _band_layout(
    text: str, layout: Layout
) -> tuple[float, list[str], float, float, int]:
    """字幕带（以及只画胶囊那一层）共用的排版：字号、折行、首行基线、行高、带高。

    **两层必须共用这一份**：胶囊的位置是从基线推出来的，各算一遍就会歪。
    """
    band_h = layout.height - layout.subtitle_top
    font_size, lines = _fit_subtitle(text, max_lines=3, max_size=CAPTION_BAND_MAX_FONT)
    line_height = font_size * 1.36
    block_h = line_height * len(lines)
    # 竖直居中：整页字幕是从固定基线往下排的，这里只有一两行，
    # 照搬那个基线会让文字顶在上沿、下面空一大片
    first_baseline = (band_h - block_h) / 2 + font_size * 0.95
    return font_size, lines, first_baseline, line_height, band_h


def _chip_rects(
    line: str, *, baseline: float, font_size: float, layout: Layout = PORTRAIT
) -> list[str]:
    """一行里每个数字（带单位）的**胶囊矩形**。

    几何跟 `_caption_line_parts` 里那一段逐字一致 —— 那条带子里的胶囊是同一套数
    算出来的，所以两层叠上去是重合的，不是「差不多」。
    """
    parts: list[str] = []
    cursor = layout.subtitle_left
    for text_run, is_number in _number_runs(line):
        width = sum(_char_width(char) for char in text_run) * font_size
        if is_number:
            parts.append(
                f'<rect x="{cursor - 3:.1f}" y="{baseline - font_size * 0.92:.1f}" '
                f'width="{width + 6:.1f}" height="{font_size * 1.22:.1f}" rx="4" '
                f'fill="{POINT_BG}"/>'
            )
        cursor += width
    return parts


def render_caption_chip(
    text: str,
    output_path: Path,
    *,
    speaker: str = "",
    layout: Layout = PORTRAIT,
    session: Any | None = None,
) -> Path | None:
    """**只画这一句里的数字强调胶囊**、其余全透明的一层；句子里没有数字就返回 None。

    为什么要单独一层：这一层要「晚于自己那句字幕 0.12 秒再弹出来」，而字幕带是静止的
    一整层 —— 只有把要动的那一小块抠出来，才能让它自己长出来。

    位置与整条字幕带里那颗胶囊**对齐**：HTML 路径复用 `htmlpage.caption_chip_page`
    （同一份 DOM/CSS，只把字变透明，所以是同一个排版引擎算出来的同一处）；
    SVG 路径复用 `_chip_rects`（与 `_caption_line_parts` 同一组数）。
    `speaker` 现在还不影响配色（按主播上色的胶囊还没有产品需求），留着是为了不改签名。
    """
    clean = " ".join((text or "").split())
    if not clean or not _NUMBER_TOKEN.search(clean):
        return None

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if session is not None:
        from .htmlpage import caption_chip_page

        session.render(caption_chip_page(clean, layout), output_path, work_dir=output_path.parent)
        return output_path

    font_size, lines, first_baseline, line_height, band_h = _band_layout(clean, layout)
    rects: list[str] = []
    for offset, line in enumerate(lines):
        rects.extend(
            _chip_rects(
                line,
                baseline=first_baseline + offset * line_height,
                font_size=font_size,
                layout=layout,
            )
        )
    if not rects:
        return None

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{layout.width}" height="{band_h}" '
        f'viewBox="0 0 {layout.width} {band_h}">',
        *rects,
        "</svg>",
    ]
    # 不画底：这一层只有胶囊，其余必须透明（不透明的话整条字幕带会被它盖白）
    _rasterize_custom("\n".join(parts), output_path, width=layout.width, height=band_h)
    return output_path


def render_caption_chip_frames(
    text: str,
    work_dir: Path,
    *,
    index: int = 0,
    beat_index: int = 0,
    layout: Layout = PORTRAIT,
    session: Any | None = None,
) -> list[Path]:
    """数字胶囊「弹一下」的帧序列（Tier B）；不做或没有 Chrome 时返回空列表。

    **只在这一句恰好有一个数字时做**：这一层是靠整层缩放动的，轴心只有一个点 ——
    一句话里两个数字隔了半个屏，峰值 1.09 会把远的那颗推偏 45px（对不上带子里的数字）。
    一个数字时轴心就是它自己，对齐误差恒为 0。`caption_chip_page` 里也有同一道守卫
    （两道都留着：那道管「页面自己不会乱动」，这道管「不白抓 10 帧」）。
    """
    if session is None:
        return []
    clean = " ".join((text or "").split())
    if len(_NUMBER_TOKEN.findall(clean)) != 1:
        return []
    from .htmlpage import caption_chip_page

    page = caption_chip_page(clean, layout, spring=True)
    # 帧要覆盖「等待鼓包」那一段（`CHIP_POP_BUMP_DELAY`）+ 弹簧本身
    span = CHIP_POP_BUMP_DELAY + CHIP_SPRING_SEC
    count = max(2, int(round(span * CHIP_POP_FPS)))
    times = [step / CHIP_POP_FPS for step in range(count + 1)]
    prefix = f"chip-{index:04d}-{beat_index}"
    return session.render_frames(page, times, work_dir, prefix=prefix, work_dir=work_dir)


def render_caption_band(
    text: str, output_path: Path, *, layout: Layout = PORTRAIT, chip: bool = True
) -> Path:
    """只渲染底部那条字幕带，用于「字幕逐句出现」。

    画幅是 `layout.width × (layout.height - layout.subtitle_top)`，位置固定叠在 `y=layout.subtitle_top`，
    所以它盖住的就是底图那一条空字幕区。

    字号比整页字幕放大到 38：观众真正在读的是这两行，而一句比一整段短得多，
    放得下更大的字（放不下会自动往小退，见 `_fit_subtitle`）。

    `chip=False`：这一句的**数字不画胶囊**（普通文字颜色、位置不变）。
    只有「这一句有逐句强调层」时才会这么渲染 —— 否则强调层叠上去看不出来
    （见 docs/BEAT_MOTION.md「让数字强调真的看得见」）。默认 `True`，老路径不受影响。
    """
    font_size, lines, first_baseline, line_height, band_h = _band_layout(text, layout)

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{layout.width}" height="{band_h}" '
        f'viewBox="0 0 {layout.width} {band_h}">',
        f'<rect width="{layout.width}" height="{band_h}" fill="{SUBTITLE_BG}"/>',
        f'<rect width="{layout.width}" height="2" fill="{SUBTITLE_RULE}"/>',
    ]

    for offset, line in enumerate(lines):
        baseline = first_baseline + offset * line_height
        parts.extend(
            _caption_line_parts(line, baseline=baseline, font_size=font_size, chip=chip)
        )

    parts.append("</svg>")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _rasterize_custom(
        "\n".join(parts),
        output_path,
        width=layout.width,
        height=band_h,
    )
    return output_path


# --------------------------------------------------------------------------
# 逐句画面：每一句自己的一张图片卡（见 docs/BEAT_MOTION.md）
# --------------------------------------------------------------------------


@dataclass
class BeatLayers:
    """一段画面里**逐句**的两层：每句一张图片卡、每句一个数字强调层（没有就是 None）。

    字段名与 `video.RenderedLayers` 里要加的那两个一致，所以那边接过去几乎不用改：
    `cards` = 这一句要不要换画面（空 = 不换），`chips` = 这一句的数字胶囊层。
    """

    cards: list[Path | None] = field(default_factory=list)
    chips: list[Path | None] = field(default_factory=list)
    # 「弹一下」那种逐帧抓的胶囊层（Tier B）：有帧序列时 `chips` 那一格是 None
    # （静态图和帧序列不能同时叠，否则序列播完会被静态图顶掉）。
    chip_frames: list[list[Path]] = field(default_factory=list)


def _beat_image_path(value: Any, assets: dict[str, Any] | None) -> Path | None:
    """`Beat.image`（素材 id）→ 真实文件路径。认不出 / 文件不在就 None（= 这一句不换）。"""
    if not value:
        return None
    if assets:
        asset = assets.get(str(value))
        if asset is not None:
            candidate = Path(getattr(asset, "path", asset))
            if candidate.exists():
                return candidate
            return None
    candidate = Path(str(value))
    return candidate if candidate.exists() else None


def render_beat_layers(
    scene: Scene,
    *,
    index: int = 0,
    work_dir: Path,
    layout: Layout = PORTRAIT,
    assets: dict[str, Any] | None = None,
    pauses: list[Pause] | None = None,
    session: Any | None = None,
) -> BeatLayers:
    """把一段画面里的**逐句**图层渲染出来（逐句图片卡 + 逐句数字强调层）。

    三条守卫都在这里（注释里各写了为什么）：
    **封面段与片尾段永远不切画面**、**和上一句同图不切**、**这一句太短不切**
    （用 `beat_windows_aligned` 算出来的**实际窗口时长**判，不是按字数估的）。

    句子的时间窗和解码侧用的是同一个函数、同一组参数 —— 两边各算一遍就会错位。
    """
    beats = list(getattr(scene, "beats", None) or [])
    layers = BeatLayers(
        cards=[None] * len(beats),
        chips=[None] * len(beats),
        chip_frames=[[] for _ in beats],
    )
    # 只有一句的段落没有「逐句」可言：字幕带那条路也是 `len(beats) > 1` 才拆
    if len(beats) <= 1:
        return layers

    texts = [beat.text or "" for beat in beats]
    windows = beat_windows_aligned(
        texts, scene.duration, start=scene.start, pauses=pauses
    )
    # 守卫①：封面段（headline 非空）要始终是「论文首页 + 标题」，片尾是品牌卡。
    # 「第一帧必须是论文首页」是产品要求，所以这里是硬守卫。
    allow_cards = not (scene.headline or "").strip() and scene.brand != "outro"
    current = Path(str(scene.image))

    for beat_index, beat in enumerate(beats):
        # 数字强调层：和「换不换画面」无关，每一句都可以有。
        # HTML 路径下这一步是**逐帧**的（弹簧「弹一下」，见 caption_chip_page 的注释）；
        # 退回 SVG 时还是那张静态图 + 淡入。
        frames = render_caption_chip_frames(
            beat.text or "",
            work_dir,
            index=index,
            beat_index=beat_index,
            layout=layout,
            session=session,
        )
        if frames:
            layers.chip_frames[beat_index] = frames
        else:
            chip_path = work_dir / f"chip-{index:04d}-{beat_index}.png"
            layers.chips[beat_index] = render_caption_chip(
                beat.text or "", chip_path, layout=layout, session=session
            )

        path = _beat_image_path(beat.image, assets)
        if path is None or not allow_cards:
            continue
        if beat_index == 0:
            # 第 0 句不需要单独一层：它就是这一段原本的那张卡
            current = path
            continue
        # 守卫②：跟上一句是同一张图 —— 图没变就别动，无意义的溶解只会让人以为卡了一下
        if path == current:
            continue
        beat_start, beat_end = windows[beat_index]
        # 守卫③：这一句太短（按**实际窗口**判）：几个字一闪而过比不切更难看
        if (beat_end - beat_start) < BEAT_IMAGE_MIN_SEC:
            continue

        card_path = work_dir / f"beat-{index:04d}-{beat_index}.png"
        # 逐句卡用和本段同一套排版（`render_image_card`），只有图换了 ——
        # 边框、裁切、叠放坐标因此一个字都不用改
        render_image_card(
            replace(scene, image=path, headline=""), card_path, layout=layout
        )
        layers.cards[beat_index] = card_path
        current = path

    return layers


def render_endcard(
    scene: Scene, output_path: Path, *, layout: Layout = PORTRAIT
) -> Path:
    """片尾品牌卡：深色底 + 社区 logo + 关注引导。

    ## 为什么片尾要单独做成深色

    logo 是**浅色**字标（社区自己的视频也用深色星空底），白底上会直接消失。
    而正文又必须是白底 —— 论文配图本身就是白底图表，深色画布会把它们衬得像贴图。

    所以两种底各归其位：**正文白底保证可读，片尾深色保证品牌正确**。
    顺带这也让「节目结束」有一个明确的视觉信号，而不是在正文风格里悄悄停掉。

    引导语除了画面上出大字，语音里也会说一遍（见 branding.BRAND_OUTRO）——
    听众往往是听到结尾才决定要不要关注，这时画面和声音必须同时给到指引。
    """
    font_size, lines = _fit_subtitle(scene.text, max_lines=4)
    logo = _logo_data_uri(460)

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{layout.width}" height="{layout.height}" '
        f'viewBox="0 0 {layout.width} {layout.height}">',
        f'<rect width="{layout.width}" height="{layout.height}" fill="{BRAND_DARK}"/>',
    ]

    # 片尾也不放论文标题：这张卡是品牌卡，画面上的主角是关注引导，
    # 左上角再挂一行论文全名只会跟引导语抢注意力（正文页的标题条也一并去掉了）。

    if logo:
        uri, lw, lh = logo
        lx = (layout.width - lw) / 2
        ly = 392
        parts.append(f'<image x="{lx:.1f}" y="{ly}" width="{lw}" height="{lh}" href="{uri}"/>')
        divider_y = ly + lh + 60
    else:
        # 没有 logo 也不能空着，直接用社区名顶上
        parts.append(
            f'<text x="{layout.width / 2:.0f}" y="470" text-anchor="middle" '
            f'font-family="{FONT_STACK}" font-size="56" fill="{BRAND_TEXT}" '
            f'font-weight="600">青稞社区</text>'
        )
        divider_y = 530

    # 青稞绿的细分隔线
    parts.append(
        f'<rect x="{layout.width / 2 - 110:.0f}" y="{divider_y}" width="220" height="2" '
        f'fill="{BRAND_GREEN}" opacity="0.85"/>'
    )

    # 关注引导：主句用麦金强调，副句用青稞绿
    parts.append(
        f'<text x="{layout.width / 2:.0f}" y="{divider_y + 88}" text-anchor="middle" '
        f'font-family="{FONT_STACK}" font-size="44" font-weight="600" '
        f'fill="{BRAND_GOLD}">{html.escape(BRAND_CTA_TITLE)}</text>'
    )
    parts.append(
        f'<text x="{layout.width / 2:.0f}" y="{divider_y + 140}" text-anchor="middle" '
        f'font-family="{FONT_STACK}" font-size="24" '
        f'fill="{BRAND_GREEN}">{html.escape(BRAND_CTA_SUBTITLE)}</text>'
    )

    # 字幕区：深色面板 + 浅色字（与其他页的浅底深字相反）
    parts.append(
        f'<rect x="0" y="{layout.subtitle_top}" width="{layout.width}" '
        f'height="{layout.height - layout.subtitle_top}" fill="{BRAND_DARK_PANEL}"/>'
    )
    parts.append(
        f'<rect x="0" y="{layout.subtitle_top}" width="{layout.width}" height="2" '
        f'fill="{BRAND_GREEN}" opacity="0.5"/>'
    )
    line_height = font_size * 1.36
    for offset, line in enumerate(lines):
        parts.append(
            f'<text x="{layout.subtitle_left}" y="{layout.subtitle_text_top + offset * line_height:.0f}" '
            f'font-family="{FONT_STACK}" font-size="{font_size:.0f}" '
            f'fill="{BRAND_TEXT}">{html.escape(line)}</text>'
        )

    parts.append("</svg>")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    _rasterize_custom(
        "\n".join(parts),
        output_path,
        width=layout.width,
        height=layout.height,
    )
    return output_path


def _rasterize_custom(
    svg_text: str,
    output_path: Path,
    *,
    width: int = VIDEO_W,
    height: int = VIDEO_H,
) -> None:
    """按给定尺寸渲染（illustration.rasterize_svg 会强制套用信息图的画布尺寸）。

    ⚠️ resvg 在这里的行为是**按宽度缩放、高度按比例推**，不是拉伸到给定尺寸：
    传 `width=936` 渲染一个 896×742 的 SVG，会得到 936×776（比例一样、但尺寸不对）。
    「整页/字幕带/强调行/进度条」都是 936 宽，正好蒙对；**图片卡是 896 宽，必须显式传尺寸**。
    """
    try:
        import resvg_py
    except ImportError as exc:  # pragma: no cover
        raise VideoError("缺少 resvg-py，无法渲染画面") from exc

    try:
        png = bytes(
            resvg_py.svg_to_bytes(
                svg_string=svg_text,
                width=width,
                height=height,
                languages=["zh-Hans", "en"],
            )
        )
    except Exception as exc:  # noqa: BLE001
        raise VideoError(f"画面渲染失败：{exc}") from exc

    if not png:
        raise VideoError("画面渲染返回空内容")
    output_path.write_bytes(png)

# --------------------------------------------------------------------------
# 进度条素材（叠在画面顶上的那条全宽色带）
# --------------------------------------------------------------------------


def progress_bar_svg(*, layout: Layout = PORTRAIT) -> str:
    """进度条素材：一条**全宽**的色带。

    用法不是「把它截短」，而是整条叠上去、用 overlay 的逐帧 `x` 把它从左往右推进画面。

    为什么不用 `drawbox` 直接画：`drawbox` 的 `w` 只在初始化时求值一次，
    写 `w='iw*t/duration'` 实测每一帧都是满宽（`t` 在那个上下文里取不到值，被钳到了 iw）。
    overlay 的 `x`/`y` 是**逐帧**求值的，所以能拿到平滑增长的进度。
    这不是猜的：实测 2 秒进度条在 0.2s / 1.0s / 1.8s 处量到 96 / 468 / 842 像素
    （理论值 93 / 468 / 842）。
    """
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{layout.width}" height="{PROGRESS_BAR_H}">'
        f'<rect width="{layout.width}" height="{PROGRESS_BAR_H}" fill="{PROGRESS_BAR_COLOR}"/>'
        f"</svg>"
    )


# --------------------------------------------------------------------------
# 字幕逐句出现的时间窗
# --------------------------------------------------------------------------


def beat_windows(
    beats: list[str], duration: float, *, fade: float = CAPTION_FADE_SEC
) -> list[tuple[float, float]]:
    """按**字数占比**把这一段时长分给每句字幕，返回每句的 [起, 止)。

    为什么按字数：我们只有整段的音频时间轴（TTS 是按段合成的），没有逐句时间戳。
    按字数分是这里能做到的最好近似 —— 同一段里语速基本恒定。
    """
    if not beats:
        return []
    total = sum(len(beat) for beat in beats) or 1
    windows: list[tuple[float, float]] = []
    cursor = 0.0
    for beat in beats:
        span = duration * len(beat) / total
        windows.append((cursor, cursor + span))
        cursor += span
    return windows


def beat_windows_aligned(
    beats: list[str],
    duration: float,
    *,
    start: float = 0.0,
    pauses: list[Pause] | None = None,
    fade: float = CAPTION_FADE_SEC,
) -> list[tuple[float, float]]:
    """字幕分句的时间窗：先按字数比例算，再**吸附到真实的说话停顿**。

    为什么要吸附：语速不均匀，按字数算出来的换句点经常落在句子中间，
    看起来就是「字幕换得莫名其妙」。音频里本来就有停顿，`silencedetect` 直接给出来
    （实测 250 秒的音频 0.14 秒跑完，82 个停顿）。

    `pauses` 为空（检测失败 / 没有停顿）时就是原来的比例切分 —— 这条路径必须永远可用。
    """
    windows = beat_windows(beats, duration, fade=fade)
    if not pauses or len(windows) < 2:
        return windows

    # 内部边界（不含首尾）的绝对时间，吸附后再还原成窗口
    boundaries = [start + end for _, end in windows[:-1]]
    aligned = align_boundaries(
        boundaries, pauses, start=start, end=start + duration
    )
    edges = [0.0, *[item - start for item in aligned], duration]
    return [(edges[i], edges[i + 1]) for i in range(len(edges) - 1)]
