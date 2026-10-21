"""配图在卡片里怎么装填：**只算几何**，不碰渲染。

为什么要单独一层：这件事现在有**两条渲染路径**（HTML 由 Chrome 截、SVG 由 resvg
光栅化，见 docs/RENDERING.md），而「图装多大、摆在哪」必须两边一致。原来几何只写在
SVG 那条路里（`video_render._fit_figure`），HTML 那条路交给浏览器的
`max-width/max-height` 自动缩 —— 于是同一张图在默认路径（HTML）上**没有按内容居中**：

    实测 a1ed76d0 的 Fig.2：源图 1136×562，右边烘进去 154px 白边（左边只有 20px）。
    浏览器缩的是**整张图**，白边也算宽度 → 画面上的图整体偏左 67px。
    用户的反馈就是「视频中的图片要左右居中」。

所以几何搬到这里，`htmlpage` 与 `video_render` 都调它 —— 两个数出自同一段代码。

## 为什么要先量「墨迹包围盒」

论文原图大多四周带一圈白（页边距、图表自身留白）。按整张图等比缩放，内容只占卡片
中间一条（实测整帧 52% 的行是空白，看着像「这一段没有配图」）。所以先量出**有墨**的
矩形，按它算缩放和落点：内容是满的，白边溢出到卡片之外 —— 页面本来就是白底，
那点白边看不见。**只量、不裁**：裁像素会丢内容，而溢出最多是不好看。

溢出上限 `CARD_OVERHANG_RATIO`：判白阈值 242 与 255 之间只有 13 级差，肉眼看不出来；
给到卡片尺寸的 1/4 就够，再大就怕某张图底色不是纯白（245 的浅灰页底）时露出方块。
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

# 图外白边可以溢出卡片多少（相对卡片尺寸）
CARD_OVERHANG_RATIO = 0.25
# 放大倍数上限：小的示意图放大过头会糊（而且是「按内容装填」带来的副作用）
MAX_FIT_SCALE = 3.0
# 判白阈值：>= 这个灰度就当白
INK_THRESHOLD = 242


@dataclass(frozen=True)
class FigureFit:
    """一张图的装填结果（坐标都在**卡片**坐标系里，原点在卡片左上角）。

    `img_*` 是缩放后的整张图（含白边，会溢出卡片）；
    `ink_*` 是**内容**那一块 —— 图注要贴它的下沿，边框要包它，居中也按它算。
    """

    scale: float
    img_w: int
    img_h: int
    img_x: float
    img_y: float
    ink_w: float
    ink_h: float
    ink_x: float
    ink_y: float

    @property
    def frame(self) -> tuple[float, float, float, float]:
        """内容包围盒（x, y, w, h）—— 边框与「贴图下沿」都用它。"""
        return (self.ink_x, self.ink_y, self.ink_w, self.ink_h)


def ink_bbox(pix: Any, *, threshold: int = INK_THRESHOLD) -> tuple[int, int, int, int] | None:
    """一张图里**真正有内容**的矩形（x0, y0, x1, y1）；整张全白时返回 None。"""
    import pymupdf

    try:
        gray = pymupdf.Pixmap(pymupdf.csGRAY, pix)
    except Exception:  # noqa: BLE001 - 转灰度失败就按 RGB 自己算
        gray = None
    width, height = pix.width, pix.height
    if gray is not None:
        width, height, data, step = gray.width, gray.height, gray.samples, 1
    else:  # pragma: no cover - 极少数颜色空间才会走到
        data, step = pix.samples, pix.n
    left, right, top, bottom = width, -1, height, -1
    for y in range(height):
        row = y * width * step
        for x in range(width):
            value = data[row + x * step]
            if step > 1:  # 非灰度：取三个通道最小
                value = min(value, data[row + x * step + 1], data[row + x * step + 2])
            if value < threshold:
                if x < left:
                    left = x
                if x > right:
                    right = x
                if y < top:
                    top = y
                if y > bottom:
                    bottom = y
    if right < 0 or bottom < 0:
        return None
    return left, top, right + 1, bottom + 1


@lru_cache(maxsize=64)
def ink_bbox_of_file(path: str, threshold: int = INK_THRESHOLD) -> tuple[int, int, int, int] | None:
    """按**文件**缓存的墨迹包围盒：同一张图一次出片要量好几次（渲染 + 排文字 + 编码），
    而逐像素扫一遍 962×440 要 0.4 秒、1600×1200 要 1.5 秒（纯 Python 循环）。"""
    import pymupdf

    try:
        return ink_bbox(pymupdf.Pixmap(path), threshold=threshold)
    except Exception:  # noqa: BLE001 - 读不出来就当整张图都是内容
        return None


def fit_figure(
    card_w: float,
    card_h: float,
    source_w: int,
    source_h: int,
    ink: tuple[int, int, int, int] | None,
) -> FigureFit:
    """按内容包围盒把图放进卡片：**内容居中**，白边溢出（有上限）。

    `ink=None`（量不出来 / 整张全白）时退化成「整张图等比装填并居中」——
    老行为，不会更糟。
    """
    if not ink:
        scale = min(card_w / source_w, card_h / source_h, MAX_FIT_SCALE)
        img_w = max(1, int(round(source_w * scale)))
        img_h = max(1, int(round(source_h * scale)))
        img_x = (card_w - img_w) / 2
        img_y = (card_h - img_h) / 2
        return FigureFit(
            scale=scale, img_w=img_w, img_h=img_h, img_x=img_x, img_y=img_y,
            ink_w=float(img_w), ink_h=float(img_h), ink_x=img_x, ink_y=img_y,
        )

    x0, y0, x1, y1 = ink
    bw, bh = max(1, x1 - x0), max(1, y1 - y0)
    over_w = card_w * CARD_OVERHANG_RATIO
    over_h = card_h * CARD_OVERHANG_RATIO
    scale = min(
        card_w / bw,
        card_h / bh,
        (card_w + over_w * 2) / source_w,
        (card_h + over_h * 2) / source_h,
        MAX_FIT_SCALE,
    )
    img_w = max(1, int(round(source_w * scale)))
    img_h = max(1, int(round(source_h * scale)))
    # 内容包围盒的**中心**落在卡片中心 —— 白边因此左右（上下）对称地溢出
    img_x = card_w / 2 - (x0 + x1) / 2 * scale
    img_y = card_h / 2 - (y0 + y1) / 2 * scale
    return FigureFit(
        scale=scale,
        img_w=img_w,
        img_h=img_h,
        img_x=img_x,
        img_y=img_y,
        ink_w=bw * scale,
        ink_h=bh * scale,
        ink_x=img_x + x0 * scale,
        ink_y=img_y + y0 * scale,
    )


def fit_for_file(
    path: str | Path, card_w: float, card_h: float
) -> tuple[FigureFit, tuple[int, int]]:
    """一步到位：量源图尺寸 + 墨迹，返回（装填结果, 源图尺寸）。读不出尺寸时抛异常。"""
    import pymupdf

    pix = pymupdf.Pixmap(str(path))
    return fit_figure(card_w, card_h, pix.width, pix.height, ink_bbox_of_file(str(path))), (
        pix.width,
        pix.height,
    )
