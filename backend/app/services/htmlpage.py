"""把一页画面写成 HTML/CSS，交给 Chrome 光栅化（`htmlframe`）。

## 这一层替代了什么

老路径是「Python 手算坐标 → 拼 SVG 字符串 → resvg」：文字换行靠 `_wrap`（一张
近似宽度表）、放不下就缩字号靠 `_fit_subtitle`（试六档）、数字高亮的底色靠
**手工累加每个字的宽度**（`_caption_line_parts` 里的 `cursor`）……
这些浏览器本来就会做，而且做得比我们准 —— 它量的是**真实字体度量**。

HTML 版本里对应的事是这样完成的：

| 原来（Python/SVG） | 现在（HTML/CSS） |
|---|---|
| `_wrap()` 按近似字宽折行 | 浏览器折行（`max-width`） |
| `_fit_subtitle()` 试六档字号 | `fitText()`：浏览器实测 `scrollHeight` 逐级缩到放得下 |
| 数字高亮手工累加 x 坐标 | `<span class="num">` 一个内联背景 |
| 图片等比缩放 + 居中要自己算 | `object-fit: contain` + flex 居中 |
| 封面标题块高、面板高度全要算 | 面板高度由内容撑开（CSS 自动） |
| 毛玻璃要拼 clipPath + filter + image 变换 | `filter: blur()` + `overflow: hidden` |

## 版式数值仍然来自 `design.Layout`

区域**位置和大小**（图片区、图注、强调行、字幕带）照旧由 `Layout` 决定 ——
那是**产品决策**（每块留多少地方），不是排版计算。被拿掉的只是「块内部怎么排」。

## 页面与渲染器的约定

- `window.prepare()`：可选的**异步**函数，在「加载完 + 字体就绪 + 图片解码完」之后、
  第一次截图之前被 `await`。字号自适应走这里。
- `window.seek(seconds)`：可选的动画 seek 钩子（CSS/WAAPI 动画不用它，渲染器会自己 seek）。
"""

from __future__ import annotations


import html as html_lib
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .design import (
    BG_COLOR,
    BRAND_DARK,
    BRAND_DARK_PANEL,
    BRAND_GOLD,
    BRAND_GREEN,
    BRAND_TEXT,
    CAPTION_COLOR,
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
    COVER_HEADLINE_MAX_FONT,
    COVER_HEADLINE_MAX_LINES,
    COVER_HEADLINE_MIN_FONT,
    COVER_PAD,
    COVER_PANEL_FILL,
    COVER_PANEL_MAX_W,
    COVER_PANEL_PAD,
    COVER_PANEL_RADIUS,
    COVER_SUBTITLE_COLOR,
    COVER_SUBTITLE_FONT,
    COVER_TITLE_COLOR,
    FOCUS_BORDER,
    FOCUS_BORDER_W,
    FOCUS_DIM_ALPHA,
    FONT_STACK,
    FRAME_STROKE,
    POINT_BAR,
    POINT_BAR_W,
    POINT_BG,
    POINT_TEXT,
    SUBTITLE_BG,
    SUBTITLE_RULE,
    SUBTITLE_TEXT,
    WATERMARK_PAD,
    WATERMARK_W,
    Layout,
)
from .htmlframe import Page

if TYPE_CHECKING:  # 只为类型注解；运行时不导入，避免与 video.py 成环
    from .video import Scene


# 字幕带里「数字高亮」的观感（和强调行同一套浅蓝底）。
# 老路径是「先算这一段文字的像素宽、再在它下面垫一个圆角矩形」，
# 这里一个 `<span>` 的内联背景就够了。
# 压暗的「洞」比框线本身大一圈：框线是压着边界画的，不大一圈的话框线自己会被压暗
FOCUS_PAD = 5

NUMBER_BG = POINT_BG
NUMBER_TEXT = POINT_TEXT

# 整页字幕的字号区间。上限沿用老路径 `_fit_subtitle` 的 max_size=30，
# 下限也是它的兜底值 20 —— 再小观众在手机上看不清。
SUBTITLE_MAX_FONT = 30.0
SUBTITLE_MIN_FONT = 20.0
# 字幕带（单独一层，逐句出现用）字号可以更大：观众真正在读的就是这一两行。
SUBTITLE_BAND_MAX_FONT = 38.0


def _figure_text(text: str) -> str:
    """数字（带单位）包一层 `.num`，其余原样转义。

    `_NUMBER_TOKEN` 借用 video.py 里那一份 —— 两边的高亮范围必须**完全一致**，
    否则「同一句话在整页字幕里有底色、在字幕带里没底色」这种不一致会莫名其妙地出现。
    """
    from .video import _NUMBER_TOKEN

    out: list[str] = []
    cursor = 0
    for match in _NUMBER_TOKEN.finditer(text):
        if match.start() > cursor:
            out.append(html_lib.escape(text[cursor : match.start()]))
        out.append(f'<span class="num">{html_lib.escape(match.group(0))}</span>')
        cursor = match.end()
    if cursor < len(text):
        out.append(html_lib.escape(text[cursor:]))
    return "".join(out)


def _data_uri(path: Any, *, max_side: int) -> str:
    """图片 → data URI（借用 video.py 的加载/缩放实现，别在这里再写一套）。

    为什么内联而不是引用文件：整页 HTML 因此是**自包含**的 —— 一份 HTML 丢到哪都能
    渲染出同样的画面，截图失败的现场也好复现。
    """
    from .video import _image_data_uri

    uri, _, _ = _image_data_uri(Path(path), max_side=max_side)
    return uri


def _logo_uri(width: int = WATERMARK_W) -> tuple[str, int, int] | None:
    from .video import _logo_data_uri

    return _logo_data_uri(width)


def _layout_vars(layout: Layout) -> str:
    """把 `Layout` 的像素值注入成 CSS 变量。

    这样静态 CSS 可以写成一段普通字符串（不用为了几个数字去 f-string 里
    转义满屏的 `{}`），版式数值仍然只有一个来源。
    """
    pairs = {
        "page-w": layout.width,
        "page-h": layout.height,
        "image-top": layout.image_top,
        "image-left": layout.image_box_left,
        "image-w": layout.image_box_w,
        "image-h": layout.image_box_h,
        "caption-top": layout.caption_top,
        "caption-left": layout.subtitle_left,
        "caption-w": layout.subtitle_width,
        "subtitle-top": layout.subtitle_top,
        "subtitle-left": layout.subtitle_left,
        "subtitle-w": layout.subtitle_width,
        "subtitle-text-top": layout.subtitle_text_top,
        "point-top": layout.point_top,
        "point-left": layout.point_left,
        "point-w": layout.point_width,
        "point-h": layout.point_height,
        "point-font": layout.point_max_font,
    }
    return "\n".join(f"  --{name}: {value}px;" for name, value in pairs.items())


# 整页的静态 CSS。区域位置全部走 CSS 变量（见 `_layout_vars`），
# 块内部一律交给浏览器排版。
BASE_CSS = """
* { box-sizing: border-box; }
html, body {
  margin: 0; padding: 0;
  width: var(--page-w); height: var(--page-h);
  background: %(bg)s;
  font-family: %(font)s;
  -webkit-font-smoothing: antialiased;
}
.page { position: relative; width: var(--page-w); height: var(--page-h); overflow: hidden; }

/* 右上角社区 logo：浅色字标必须垫深色圆角底，否则白底上看不见 */
.logo-chip {
  position: absolute; top: 18px; right: 40px;
  background: %(brand_dark)s; opacity: 0.9;
  border-radius: 8px; display: flex; align-items: center;
  padding: %(logo_pad)spx;
}
.logo-chip img { display: block; width: %(logo_w)spx; height: auto; }

/* 图片区：位置和大小来自 Layout，块内怎么摆交给 flex */
.image-box {
  position: absolute; top: var(--image-top); left: var(--image-left);
  width: var(--image-w); height: var(--image-h);
  display: flex; align-items: center; justify-content: center;
  overflow: hidden; border-radius: 8px;
}
.image-frame {
  display: flex; padding: 2px; border: 1.5px solid %(frame)s;
  border-radius: 8px; background: %(bg)s;
  max-width: 100%%; max-height: 100%%;
}
.image-frame img { display: block; max-width: 100%%; max-height: 100%%; border-radius: 6px; }

/* 封面：论文首页铺满 + 模糊 + 冷调薄纱，标题压在玻璃面板上。
   模糊层做成「比容器大一圈」的内层（inset 由 Python 按倍率给），
   让羽化边缘落在裁剪区之外 —— 直接给容器加 blur 会把四边啃掉一圈发白。 */
.cover-fill { position: absolute; }
.cover-fill img {
  width: 100%%; height: 100%%; object-fit: cover;
  object-position: center %(cover_anchor)s;
  transform: scale(%(cover_zoom)s);
  transform-origin: center %(cover_zoom_cy)s;
  filter: blur(%(cover_blur).1fpx);
}
.cover-veil { position: absolute; inset: 0; background: %(cover_tint)s; opacity: %(cover_veil)s; }
.cover-panel {
  position: relative; z-index: 1;
  width: min(calc(var(--image-w) - %(cover_pad_px)dpx), %(cover_panel_max)dpx);
  background: %(cover_panel_fill)s; border: 2px solid %(cover_panel_fill)s;
  border-radius: %(cover_panel_radius)dpx;
  padding: %(cover_panel_pad)dpx;
  display: flex; flex-direction: column; align-items: center; text-align: center;
}
.cover-headline {
  font-size: %(cover_headline)spx; font-weight: 700; line-height: 1.22;
  color: %(cover_title_color)s; text-wrap: balance;
  max-width: 100%%;
}
.cover-accent {
  width: %(cover_accent_w)dpx; height: %(cover_accent_h)dpx;
  border-radius: %(cover_accent_h_half).1fpx; background: %(brand_green)s;
  margin: %(cover_accent_gap)dpx 0;
}
.cover-paper {
  font-size: %(cover_subtitle)dpx; line-height: 1.4; color: %(cover_subtitle_color)s;
  max-width: 100%%;
}

/* 图注（论文原题 / 图片说明）：一行灰字 */
.caption {
  position: absolute; top: var(--caption-top); left: var(--caption-left);
  width: var(--caption-w);
  font-size: 18px; line-height: 23px; color: %(caption_color)s;
  max-height: 46px; overflow: hidden;
  display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2;
}

/* 字幕区：极浅底 + 一条分隔线。这里**不显示「主播A/主播B」标签** ——
   谁在说话听声音就知道，多一行标签只会分散注意力、还占掉字幕空间。 */
.subtitle-band {
  position: absolute; left: 0; top: var(--subtitle-top);
  width: var(--page-w); height: calc(var(--page-h) - var(--subtitle-top));
  background: %(subtitle_bg)s; border-top: 2px solid %(subtitle_rule)s;
}
.subtitle-text {
  position: absolute; left: var(--subtitle-left); top: var(--subtitle-text-top);
  width: var(--subtitle-w);
  /* ⚠️ 高度必须**显式给**：字号自适应靠 `scrollHeight <= clientHeight` 判断放不放得下，
     而 height:auto 的元素这两者恒等 —— 约束会静默失效（检查永远为真）。
     给死高度之后，放不下才会真的缩字号。 */
  height: calc(var(--page-h) - var(--subtitle-text-top));
  font-size: %(subtitle_font).0fpx; line-height: 1.36; color: %(subtitle_text)s;
}

/* 「本段要点」强调行：浅蓝底 + 左侧色条 + 大字。画面上最抢眼的一行，
   也是「突出解读内容」的落点（不是装饰，是这一段的核心结论）。 */
.point-row {
  position: absolute; top: var(--point-top); left: var(--point-left);
  width: var(--point-w); height: var(--point-h);
  background: %(point_bg)s; border-radius: 10px;
  display: flex; align-items: center;
}
.point-bar {
  width: %(point_bar_w)dpx; height: var(--point-h);
  border-radius: 3px; background: %(point_bar)s; flex: none;
}
.point-text {
  font-size: var(--point-font); font-weight: 600; color: %(point_text)s;
  margin: 0 18px; white-space: nowrap;
  /* ⚠️ 必须给一个**有限宽度**：`flex: 1 1 auto` + `min-width: 0` 之后
     这个元素的 clientWidth 才是「可用宽度」，而不是「内容宽度」。
     否则 `scrollWidth <= clientWidth` 恒为真（两者一起长），
     字号自适应永远不会触发，超长文案会直接溢出浅蓝底、甚至溢出画面。 */
  flex: 1 1 auto; min-width: 0; overflow: hidden;
}

/* 数字高亮：论文解读里真正有信息量的往往就是那个「67%%」 */
.num { background: %(number_bg)s; color: %(number_text)s; border-radius: 4px; padding: 0 2px; }
""" % {
    "bg": BG_COLOR,
    "font": FONT_STACK,
    "brand_dark": BRAND_DARK,
    "brand_green": BRAND_GREEN,
    "logo_pad": WATERMARK_PAD,
    "logo_w": WATERMARK_W,
    "frame": FRAME_STROKE,
    "cover_pad_px": COVER_PAD,
    "cover_panel_max": COVER_PANEL_MAX_W,
    "cover_panel_fill": "rgba(255,255,255,%.2f)" % COVER_PANEL_FILL,
    "cover_panel_radius": COVER_PANEL_RADIUS,
    "cover_panel_pad": COVER_PANEL_PAD,
    "cover_headline": COVER_HEADLINE_MAX_FONT,
    "cover_title_color": COVER_TITLE_COLOR,
    "cover_accent_w": COVER_ACCENT_W,
    "cover_accent_h": COVER_ACCENT_H,
    "cover_accent_h_half": COVER_ACCENT_H / 2,
    "cover_accent_gap": COVER_ACCENT_GAP,
    "cover_subtitle": COVER_SUBTITLE_FONT,
    "cover_subtitle_color": COVER_SUBTITLE_COLOR,
    "cover_tint": COVER_GLASS_TINT,
    "cover_veil": COVER_GLASS_VEIL,
    "cover_zoom": COVER_GLASS_ZOOM,
    "cover_zoom_cy": "%d%%" % int(COVER_GLASS_ZOOM_CY * 100),
    "cover_anchor": "top" if COVER_GLASS_ANCHOR == "Min" else "center",
    "cover_blur": COVER_GLASS_BLUR,
    "caption_color": CAPTION_COLOR,
    "subtitle_bg": SUBTITLE_BG,
    "subtitle_rule": SUBTITLE_RULE,
    "subtitle_text": SUBTITLE_TEXT,
    "subtitle_font": SUBTITLE_MAX_FONT,
    "point_bg": POINT_BG,
    "point_bar": POINT_BAR,
    "point_bar_w": POINT_BAR_W,
    "point_text": POINT_TEXT,
    "number_bg": NUMBER_BG,
    "number_text": NUMBER_TEXT,
}


# 字号自适应：**浏览器量的是真实字体度量**，比 `_char_width` 那张近似表准，
# 尤其是中英数字混排的时候。
#
# 两条模式：
# - `wrap`（整页字幕、封面标题、图注）：一行放不下会自动折行，所以判据是**行数**；
# - `nowrap`（强调行）：必须一行放下，判据是 `scrollWidth`。
#
# 缩到一个下限还放不下就停在那儿（宁可溢一点，也不要缩成小字报）。
FIT_JS = """
function fitText(el) {
  const max = parseFloat(el.dataset.fitMax || '30');
  const min = parseFloat(el.dataset.fitMin || '16');
  const mode = el.dataset.fitMode || 'wrap';
  const maxLines = parseInt(el.dataset.fitLines || '1', 10);
  const cs = getComputedStyle(el);
  const ratio = (parseFloat(cs.lineHeight) || parseFloat(cs.fontSize) * 1.36)
                / parseFloat(cs.fontSize);
  for (let size = max; size >= min; size -= 1) {
    el.style.fontSize = size + 'px';
    if (mode === 'nowrap') {
      if (el.scrollWidth <= el.clientWidth + 1) { return; }
    } else {
      const lineHeight = size * ratio;
      const lines = Math.max(1, Math.round(el.scrollHeight / lineHeight));
      const roomOk = !el.dataset.fitHeight || el.scrollHeight <= el.clientHeight + 1;
      if (lines <= maxLines && roomOk) { return; }
    }
  }
}
// 把标了 `data-clamp` 的东西拉回容器范围内。
// 浏览器量的是真实尺寸 —— 老路径那套「(字数+2)*字号*0.62 估宽度」估错就会出画。
function clampBoxes() {
  document.querySelectorAll('[data-clamp]').forEach(el => {
    const host = el.offsetParent || el.parentElement;
    const hr = host.getBoundingClientRect();
    const r = el.getBoundingClientRect();
    let dx = 0, dy = 0;
    if (r.left < hr.left + 8) { dx = hr.left + 8 - r.left; }
    if (r.right + dx > hr.right - 8) { dx = hr.right - 8 - r.right; }
    if (r.top < hr.top + 6) { dy = hr.top + 6 - r.top; }
    if (r.bottom + dy > hr.bottom - 6) { dy = hr.bottom - 6 - r.bottom; }
    if (dx || dy) { el.style.transform = `translate(${dx}px, ${dy}px)`; }
  });
}
window.prepare = async function prepare() {
  document.querySelectorAll('[data-fit-max]').forEach(fitText);
  clampBoxes();
};
"""


def _fit_attrs(*, max_font: float, min_font: float, lines: int, mode: str = "wrap", height: bool = False) -> str:
    attrs = (
        f'data-fit-max="{max_font:.0f}" data-fit-min="{min_font:.0f}" '
        f'data-fit-lines="{lines}" data-fit-mode="{mode}"'
    )
    if height:
        attrs += ' data-fit-height="1"'
    return attrs


def _logo_chip() -> str:
    logo = _logo_uri()
    if not logo:
        return ""
    uri, _, _ = logo
    return f'<div class="logo-chip"><img src="{uri}" alt=""></div>'


def _image_block(scene: "Scene", layout: Layout) -> str:
    """图片区：普通页是「图 + 贴着它的细边框」，封面页是「毛玻璃 + 玻璃面板」。

    封面是**一整块**（底图 + 纱 + 面板都在这个容器里），所以转场时它们一起淡入淡出 ——
    和老路径把封面排版画在图片卡那一层是同一个考虑。
    """
    headline = " ".join((scene.headline or "").split())
    caption = " ".join((scene.caption or "").split())

    if not headline:
        if not scene.image:
            return '<div class="image-box"></div>'
        uri = _data_uri(scene.image, max_side=int(max(layout.image_box_w, layout.image_box_h) * 1.5))
        return (
            '<div class="image-box">'
            f'<div class="image-frame"><img src="{uri}" alt=""></div>'
            "</div>"
        )

    # 模糊半径跟着「首页在卡片里被放大了多少」走：竖版把整页缩到约 0.96 倍，
    # 横版卡片又宽又矮、裁满时首页被放大到近 2 倍 —— 同一个半径在横版上明显更清楚。
    uri = _data_uri(scene.image, max_side=1800)
    scale = _cover_page_scale(scene.image, layout)
    blur = COVER_GLASS_BLUR * (scale / COVER_GLASS_BLUR_REF_SCALE)
    pad = COVER_PAD + 24  # 内层比容器大出去这一圈，让羽化边缘被裁掉

    paper = (
        f'<div class="cover-paper" {_fit_attrs(max_font=COVER_SUBTITLE_FONT, min_font=14, lines=2)}>'
        f"{html_lib.escape(caption)}</div>"
        if caption
        else ""
    )
    return (
        '<div class="image-box">'
        f'<div class="cover-fill" style="inset: -{pad}px"><img src="{uri}" alt="" '
        f'style="filter: blur({blur:.1f}px)"></div>'
        '<div class="cover-veil"></div>'
        '<div class="cover-panel">'
        f'<div class="cover-headline" '
        f'{_fit_attrs(max_font=COVER_HEADLINE_MAX_FONT, min_font=COVER_HEADLINE_MIN_FONT, lines=COVER_HEADLINE_MAX_LINES)}>'
        f"{html_lib.escape(headline)}</div>"
        '<div class="cover-accent"></div>'
        f"{paper}"
        "</div>"
        "</div>"
    )


def _cover_page_scale(image_path: Any, layout: Layout) -> float:
    """论文首页在卡片里被放大了多少（裁满时的倍率），用来换算模糊半径。"""
    try:
        from .video import _image_data_uri  # noqa: F401  仅确保依赖可用
        import pymupdf

        with pymupdf.open(str(image_path)) as doc:  # type: ignore[arg-type]
            page = doc.load_page(0)
            width, height = page.rect.width, page.rect.height
        if width <= 0 or height <= 0:
            return COVER_GLASS_BLUR_REF_SCALE
        return max(layout.image_box_w / width, layout.image_box_h / height)
    except Exception:  # noqa: BLE001 - 量不出来就用竖版的基准倍率，不该因此渲染失败
        return COVER_GLASS_BLUR_REF_SCALE


def scene_page(
    scene: "Scene",
    layout: Layout,
    *,
    include_image: bool = True,
    include_subtitle: bool = True,
    include_point: bool = True,
) -> Page:
    """把一段画面写成一整页 HTML。

    三个 `include_*` 对应老路径的 `render_slide(...)` / `render_chrome(...)`：
    - `include_image=False` = 骨架页（只有白底 + logo + 图注 + 字幕带），
      图片卡由 `image_card_page` 单独出一层叠上来 —— 分层的编码路径要这一种；
    - `include_subtitle=False`：字幕分句时底图**不能**画字幕，否则第一句出现之前
      整段文字就已经在画面上了；
    - `include_point=False`：强调行是滑入的，底图留着那行字就露馅了。
    """
    blocks: list[str] = [_logo_chip()]
    if include_image:
        blocks.append(_image_block(scene, layout))

    caption = " ".join((scene.caption or "").split())
    if caption and not (scene.headline or "").strip():
        # 封面页的原题已经压在玻璃面板里了，再在下面重复一遍就是两行一样的字
        # （老路径有这个重复，见 AGENTS.md 的「标题与封面」一节）
        blocks.append(
            f'<div class="caption" {_fit_attrs(max_font=18, min_font=14, lines=2)}>'
            f"{html_lib.escape(caption)}</div>"
        )

    blocks.append('<div class="subtitle-band"></div>')

    if include_point and (scene.point or "").strip():
        blocks.append(_point_row_markup(scene.point, layout))

    if include_subtitle and (scene.text or "").strip():
        text = " ".join(scene.text.split())
        blocks.append(
            f'<div class="subtitle-text" '
            f'{_fit_attrs(max_font=SUBTITLE_MAX_FONT, min_font=SUBTITLE_MIN_FONT, lines=6, height=True)}>'
            f"{_figure_text(text)}</div>"
        )

    return Page(
        html=_document(BASE_CSS, _layout_vars(layout), "\n".join(blocks)),
        width=layout.width,
        height=layout.height,
    )


def _point_row_markup(point: str, layout: Layout) -> str:
    """强调行：浅蓝底 + 左侧色条 + 大字，一行放下（放不下就缩字号）。"""
    return (
        '<div class="point-row"><div class="point-bar"></div>'
        f'<div class="point-text" '
        f'{_fit_attrs(max_font=layout.point_max_font, min_font=22, lines=1, mode="nowrap")}>'
        f"{html_lib.escape(' '.join(point.split()))}</div></div>"
    )


# 图层页（图片卡 / 强调行 / 聚光灯）的公共 CSS：**透明底** + **区域落在原点**。
#
# 两条都是「图层」这个身份决定的：
# - 不透明的话每个图层都带一整块白，叠到骨架页上就是把底座整块盖掉，
#   而画面看起来还是白的 —— 这种错不报错，只是别的东西全不见了；
# - 图层页的画布**就是那一块区域**（图片卡 = 图片区、强调行 = 一行高），
#   而老路径也是以 `0,0` 为原点画的、由 ffmpeg 叠到固定坐标上。
#   不把区域重置到原点的话，`top: 878px` 这种值会让内容整个落到画布外面，
#   截出来就是一张空图。
LAYER_CSS = """
html, body { background: transparent; }
.page { background: transparent; }
.image-box { top: 0; left: 0; }
.point-row { top: 0; }
"""


def image_card_page(scene: "Scene", layout: Layout) -> Page:
    """图片卡那一层（透明底），画幅就是图片区。

    叠在骨架页的 `(layout.image_box_left, layout.image_top)`，所以画布只有图片区那么大 ——
    ffmpeg 的 overlay 坐标一个数都不用改。
    """
    body = _image_block(scene, layout)
    return Page(
        html=_document(BASE_CSS + LAYER_CSS, _layout_vars(layout), body),
        width=layout.image_box_w,
        height=layout.image_box_h,
        transparent=True,
    )


def point_row_page(point: str, layout: Layout) -> Page:
    """强调行那一层（透明底）：画幅是**整页宽** × `point_height`。

    画幅必须和老路径的 `render_point_row` 一致 —— 它也是整页宽、内容画在
    `point_left` 处。ffmpeg 叠这一层用的是固定坐标，宽度不一致就直接错位。

    它是**滑入**的（ffmpeg 的逐帧 `x` 表达式），所以必须是独立一层 ——
    画进骨架页就没法只让它动。
    """
    return Page(
        html=_document(
            BASE_CSS + LAYER_CSS, _layout_vars(layout), _point_row_markup(point, layout)
        ),
        width=layout.width,
        height=layout.point_height,
        transparent=True,
    )


def caption_band_page(text: str, layout: Layout) -> Page:
    """底部那条字幕带单独一层（「字幕逐句出现」用）。

    画幅是 `layout.width × (band 高)`，位置固定叠在 `y=layout.subtitle_top`，
    所以它盖住的就是底图那一条空字幕区。字号比整页字幕大（观众真正在读的就是这两行），
    竖直居中 —— 照搬整页那条基线会让一两行字顶在上沿、下面空一大片。
    """
    band_h = layout.height - layout.subtitle_top
    css = BASE_CSS + f"""
    html, body {{ height: {band_h}px; }}
    .page {{ height: {band_h}px; }}
    .band-text {{
      position: absolute; left: var(--subtitle-left); top: 0;
      width: var(--subtitle-w); height: {band_h}px;
      display: flex; align-items: center;
      font-size: {SUBTITLE_BAND_MAX_FONT:.0f}px; line-height: 1.36; color: {SUBTITLE_TEXT};
    }}
    """
    attrs = _fit_attrs(
        max_font=SUBTITLE_BAND_MAX_FONT, min_font=24, lines=3, height=True
    )
    body = (
        f'<div class="page"><div class="band-text" {attrs}>'
        f"{_figure_text(' '.join((text or '').split()))}</div></div>"
    )
    return Page(html=_document(css, _layout_vars(layout), body), width=layout.width, height=band_h)


# 聚光灯从上一处**移到**这一处要用多久。这个数字不大是有原因的：
# 它是「讲解移动到了图里的另一块」，属于标记内容推进的那一类动效，
# 只需要让眼睛跟得上；再慢就变成观众在等动画。
FOCUS_MORPH_SEC = 0.45


def _focus_rect(scene: "Scene", layout: Layout, focus: dict[str, Any]) -> tuple[float, float, float, float]:
    """聚光灯框在图片卡里的像素位置（图片是等比居中的，所以要先算出图片的落点）。"""
    from .video import _prepare_image

    _, img_w, img_h = _prepare_image(scene.image, layout.image_box_w, layout.image_box_h)
    img_x = (layout.image_box_w - img_w) / 2
    img_y = (layout.image_box_h - img_h) / 2
    return (
        img_x + float(focus["x"]) * img_w,
        img_y + float(focus["y"]) * img_h,
        float(focus["w"]) * img_w,
        float(focus["h"]) * img_h,
    )


def focus_overlay_page(
    scene: "Scene",
    layout: Layout,
    focus: dict[str, Any],
    *,
    origin: dict[str, Any] | None = None,
) -> Page:
    """聚光灯图层；给了 `origin` 就做成「从上一处**移过来**」的动画页（Tier B）。

    为什么这是一个值得逐帧抓的动效（而不是直接切过去）：
    连着两段都在讲同一张图的**不同子图**时，聚光灯从 a 滑到 b 讲的是
    「讲解走到这里了」—— 和强调行的滑入是同一类信号，只是对象换成了图里的位置。
    而它**做不成 ffmpeg 表达式**：这一层是「压暗蒙版 + 框线 + 药丸标签」三件事贴在一起，
    整层平移会让蒙版跟着走（洞跑到画外），只有逐帧重画才对。

    没有 `origin` 时就是**一张静态图**（Tier A）：这一段的聚光灯是淡入进来的，
    没有「从哪儿来」可言。
    """
    fx, fy, fw, fh = _focus_rect(scene, layout, focus)
    label = str(focus.get("label") or "").strip()

    if origin is None:
        chip = f'<div class="focus-chip" data-clamp>{html_lib.escape(label)}</div>' if label else ""
        body = (
            f'<div class="focus-frame" style="left:{fx:.1f}px; top:{fy:.1f}px; '
            f'width:{fw:.1f}px; height:{fh:.1f}px; --pad:{FOCUS_PAD}px">'
            '<div class="focus-border"></div>'
            f"{chip}"
            "</div>"
        )
        return Page(
            html=_document(BASE_CSS + LAYER_CSS + FOCUS_CSS, _layout_vars(layout), body),
            width=layout.image_box_w,
            height=layout.image_box_h,
            transparent=True,
        )

    ox, oy, ow, oh = _focus_rect(scene, layout, origin)
    old_label = str(origin.get("label") or "").strip()
    # 旧框要是和新的完全重合，就没什么可移的（标签不同也一样：位置没动就别演动画）
    moving = abs(ox - fx) > 0.5 or abs(oy - fy) > 0.5 or abs(ow - fw) > 0.5 or abs(oh - fh) > 0.5

    new_chip = f'<div class="focus-chip chip-in" data-clamp>{html_lib.escape(label)}</div>' if label else ""
    # 旧标签**留在原地**淡出：它是 `.focus-frame` 的子元素，所以不能挂在正在移动的那个框上
    # （挂上去会跟着滑走，看起来像标签自己飞过去了）。这里单独用一个不带边框/蒙版的
    # 同尺寸容器把它钉在旧位置。
    old_chip = (
        f'<div class="focus-frame chip-host" style="left:{ox:.1f}px; top:{oy:.1f}px; '
        f'width:{ow:.1f}px; height:{oh:.1f}px; --pad:0px">'
        f'<div class="focus-chip chip-out" data-clamp>{html_lib.escape(old_label)}</div>'
        "</div>"
        if old_label and old_label != label
        else ""
    )
    frame_style = (
        f"left:{ox:.1f}px; top:{oy:.1f}px; width:{ow:.1f}px; height:{oh:.1f}px; "
        f"--pad:{FOCUS_PAD}px; --to-x:{fx:.1f}px; --to-y:{fy:.1f}px; "
        f"--to-w:{fw:.1f}px; --to-h:{fh:.1f}px"
    )
    body = (
        f'<div class="focus-frame {"focus-morph" if moving else ""}" '
        f'style="{frame_style}; animation-duration:{FOCUS_MORPH_SEC}s">'
        '<div class="focus-border"></div>'
        f"{new_chip}"
        "</div>"
        f"{old_chip}"
    )
    return Page(
        html=_document(
            # ⚠️ 不要 `.format()`：这段 CSS 里全是 `{}`，会被当成占位符
            BASE_CSS + LAYER_CSS + FOCUS_CSS + FOCUS_MORPH_CSS,
            _layout_vars(layout),
            body,
        ),
        width=layout.image_box_w,
        height=layout.image_box_h,
        transparent=True,
    )


# 「移过去」只用一份样式：关键帧里**只写目标值**，起点自动取元素当下的
# `left/top/width/height`（它们在行内样式里）——所以不必在 Python 里逐帧插值。
FOCUS_MORPH_CSS = """
.focus-morph {
  animation-name: focus-morph;
  animation-timing-function: cubic-bezier(.3,.7,.3,1);
  animation-fill-mode: both;
}
@keyframes focus-morph {
  to { left: var(--to-x); top: var(--to-y); width: var(--to-w); height: var(--to-h); }
}
/* 钉在旧位置的那个容器：只用来装「旧标签」，不画边框也不压暗 */
.chip-host::before { display: none; }
.chip-in { animation: chip-in .3s ease-out both; }
.chip-out { animation: chip-out .3s ease-in both; }
@keyframes chip-in { from { opacity: 0; } to { opacity: 1; } }
@keyframes chip-out { from { opacity: 1; } to { opacity: 0; } }
"""


FOCUS_CSS = """
.focus-frame { position: absolute; }
/* 压暗：一次 box-shadow 的扩散就够（比四条边少一半坐标计算），
   扩散到画布外由容器的 overflow:hidden 收掉。 */
.focus-frame::before {
  content: ''; position: absolute; inset: calc(var(--pad) * -1);
  border-radius: 8px; box-shadow: 0 0 0 9999px rgba(255,255,255,%.2f);
}
.focus-border {
  position: absolute; inset: 0; border: %dpx solid %s; border-radius: 6px;
}
.focus-chip {
  position: absolute; left: 0; bottom: calc(100%% + 6px);
  background: %s; color: #ffffff; border-radius: 8px;
  font-size: 26px; font-weight: 500; line-height: 1.9; padding: 0 13px;
  white-space: nowrap;
}
""" % (FOCUS_DIM_ALPHA, FOCUS_BORDER_W, FOCUS_BORDER, FOCUS_BORDER)


def endcard_page(scene: "Scene", layout: Layout) -> Page:
    """片尾品牌卡：深色底 + 社区 logo + 关注引导。

    **为什么片尾是深色**：logo 是浅色字标，白底上会直接消失；而正文必须是白底
    （论文配图本身就是白底图表）。所以两种底各归其位 —— 正文白底保证可读，
    片尾深色保证品牌正确，顺带让「节目结束」有个明确的视觉信号。

    引导语画面和语音都要有（听众往往是听到结尾才决定要不要关注），
    所以这里的大字和 `branding.BRAND_OUTRO` 说的是同一件事。
    """
    from ..branding import BRAND_CTA_SUBTITLE, BRAND_CTA_TITLE

    logo = _logo_uri(460)
    # ⚠️ 记得取 [0]：`_logo_uri` 返回的是 (uri, 宽, 高)，整个塞进 src 会渲染成
    # `src="('data:image/png;base64,...', 460, 123)"` —— 浏览器当作坏图，
    # naturalWidth=0、高度 0，画面上就是「片尾卡上少了个 logo」而**不报任何错**。
    logo_markup = (
        f'<img class="endcard-logo" src="{logo[0]}" alt="">'
        if logo
        else '<div class="endcard-wordmark">青稞社区</div>'
    )
    text = " ".join((scene.text or "").split())
    subtitle = (
        f'<div class="subtitle-text endcard-text" '
        f'{_fit_attrs(max_font=SUBTITLE_MAX_FONT, min_font=SUBTITLE_MIN_FONT, lines=4, height=True)}>'
        f"{html_lib.escape(text)}</div>"
        if text
        else ""
    )
    body = (
        '<div class="endcard-stack">'
        f"{logo_markup}"
        '<div class="endcard-divider"></div>'
        f'<div class="endcard-cta">{html_lib.escape(BRAND_CTA_TITLE)}</div>'
        f'<div class="endcard-sub">{html_lib.escape(BRAND_CTA_SUBTITLE)}</div>'
        "</div>"
        '<div class="subtitle-band endcard-band"></div>'
        f"{subtitle}"
    )
    return Page(
        html=_document(BASE_CSS + ENDCARD_CSS, _layout_vars(layout), body),
        width=layout.width,
        height=layout.height,
    )


ENDCARD_CSS = """
html, body { background: %(dark)s; }
/* 品牌区（字幕带以上那一整块）里竖直居中：老路径是写死的 y=392 往下排，
   居中之后长一点的屏幕比例也不会把内容顶到边上。 */
.endcard-stack {
  position: absolute; left: 0; top: 0; width: var(--page-w);
  height: var(--subtitle-top);
  display: flex; flex-direction: column; align-items: center; justify-content: center;
}
.endcard-logo { width: 460px; height: auto; display: block; }
.endcard-wordmark { font-size: 56px; font-weight: 600; color: %(text)s; }
.endcard-divider {
  width: 220px; height: 2px; background: %(green)s; opacity: 0.85;
  margin: 60px 0 0;
}
.endcard-cta { font-size: 44px; font-weight: 600; color: %(gold)s; margin-top: 56px; }
.endcard-sub { font-size: 24px; color: %(green)s; margin-top: 22px; }
/* 字幕区：深色面板 + 浅色字（和其他页的浅底深字相反） */
.endcard-band { background: %(panel)s; border-top: 2px solid %(green)s; }
.endcard-text { color: %(text)s; }
""" % {
    "dark": BRAND_DARK,
    "panel": BRAND_DARK_PANEL,
    "green": BRAND_GREEN,
    "gold": BRAND_GOLD,
    "text": BRAND_TEXT,
    # 深色卡上的字幕带分隔线用品牌绿，透明度 0.5（和上面那条分隔线一样）
}


def _document(css: str, variables: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="zh-Hans"><head><meta charset="utf-8">
<style>
:root {{
{variables}
}}
{css}
</style>
<script>
{FIT_JS}
</script>
</head>
<body>
<div class="page">
{body}
</div>
</body></html>
"""


def write_page_debug(page: Page, path: Path) -> Path:
    """把生成的 HTML 存下来（出问题时能直接丢进浏览器看，比看 PNG 有用得多）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(page.html, encoding="utf-8")
    return path


__all__ = [
    "BASE_CSS",
    "FIT_JS",
    "LAYER_CSS",
    "NUMBER_BG",
    "NUMBER_TEXT",
    "SUBTITLE_BAND_MAX_FONT",
    "SUBTITLE_MAX_FONT",
    "SUBTITLE_MIN_FONT",
    "ENDCARD_CSS",
    "FOCUS_CSS",
    "FOCUS_MORPH_SEC",
    "caption_band_page",
    "endcard_page",
    "focus_overlay_page",
    "image_card_page",
    "point_row_page",
    "scene_page",
    "write_page_debug",
]
