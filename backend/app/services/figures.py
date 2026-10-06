"""从论文 PDF 里取配图。

两件事：
1. **封面**：把 PDF 第一页整页渲染成图片。论文首页有标题、作者、摘要和
   teaser 图，是辨识度最高的封面。
2. **正文插图**：按 `Figure N:` 图注定位，再渲染图注上方的图形区域。

为什么不能只抽「内嵌图片」：论文里的图大部分是**矢量图形**，不是位图。
实测 Attention 那篇只有 3 张内嵌位图，但第 13~15 页各有 600~1000 个矢量
绘图操作（注意力可视化）。只调 get_images() 会漏掉几乎所有真正的图。

所以这里的做法是「图注锚点 + 图形包围盒」：
- 图注位置用文字搜索拿到（`Figure 3:` 这种前缀非常规整，可靠）
- 图形范围用页面上所有矢量绘图 + 位图放置矩形的联合包围盒
- 两者的交集就是图区，避开正文文字

一个关键约定：**图注在图下方，表注在表上方**。所以 Figure 取图注上方，
Table 取表注下方。这个方向搞反会得到一片空白。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, asdict
from pathlib import Path

logger = logging.getLogger(__name__)

# `Figure 1:` / `Fig. 1.` / `Table 2：`  —— 冒号可能是半角或全角
_CAPTION = re.compile(r"^\s*(Figure|Fig\.?|Table)\s+(\d+)\s*[:.．：]", re.IGNORECASE | re.MULTILINE)

# 渲染精度。150 DPI 对屏幕上展示足够清晰，单张通常 300KB 以内。
FIGURE_DPI = 150
COVER_DPI = 110

# 单集最多保留多少张正文插图——太多会淹没页面，也没有收听价值
MAX_FIGURES = 6

# 过滤阈值
MIN_FIGURE_WIDTH_PT = 90
MIN_FIGURE_HEIGHT_PT = 40
MIN_INK_RATIO = 0.008  # 低于这个比例基本是空白区域
MIN_AREA_PX = 120 * 60

# 判定「这张图的文字是歪的」
#
# 背景：有些论文的图（典型是注意力可视化那种词对齐网格）把轴标签整个转了 90°，
# 于是图里所有文字的方向都是 (0,-1)（从下往上排）。实测 Attention 那篇的
# Figure 3/4/5 就是 108 行文字**全部**竖直，没有一行横排。
# 这种图按原样截出来，在一堆正常图里看就很别扭。
#
# 判定条件刻意保守：文字行数够多、且竖直占比极高，才认为「整张图该转」。
# 只有少数几行标签是竖的图（大量正常图都有一两个竖排轴标签）不会被误转。
VERTICAL_TEXT_RATIO = 0.9
MIN_TEXT_LINES_FOR_ROTATION = 5


@dataclass
class ExtractedFigure:
    id: str
    kind: str  # "figure" | "table"
    label: str  # "Figure 1"
    caption: str
    page: int  # 1-based
    path: str
    width: int
    height: int

    def to_dict(self) -> dict:
        return asdict(self)


class PdfAssetsError(Exception):
    """PDF 配图提取失败。属于可降级错误——没有配图也要能出播客。"""


def _open_pdf(pdf_bytes: bytes):
    try:
        import pymupdf
    except ImportError as exc:  # pragma: no cover
        raise PdfAssetsError("服务端缺少 pymupdf 依赖，无法处理 PDF 配图") from exc

    try:
        return pymupdf.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:
        raise PdfAssetsError(f"PDF 无法打开：{exc}") from exc


# --------------------------------------------------------------------------
# 封面：第一页整页渲染
# --------------------------------------------------------------------------


def render_first_page(pdf_bytes: bytes, output_path: Path) -> tuple[Path, int, int] | None:
    """把 PDF 第一页渲染成 PNG，作为这一集的封面。

    返回 (路径, 宽, 高)，失败返回 None。
    """
    try:
        doc = _open_pdf(pdf_bytes)
    except PdfAssetsError as exc:
        logger.warning("封面渲染失败：%s", exc)
        return None

    try:
        if doc.page_count < 1:
            return None
        page = doc[0]
        pix = page.get_pixmap(dpi=COVER_DPI)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        pix.save(str(output_path))
        logger.info("封面已生成：%s (%dx%d)", output_path.name, pix.width, pix.height)
        return output_path, pix.width, pix.height
    except Exception as exc:  # noqa: BLE001
        logger.warning("封面渲染异常：%s", exc)
        return None
    finally:
        doc.close()


# --------------------------------------------------------------------------
# 正文插图
# --------------------------------------------------------------------------


def _graphics_bbox(page):
    """页面上所有图形（矢量绘图 + 位图）的联合包围盒。取不到则返回 None。"""
    import pymupdf

    boxes: list = []
    try:
        for drawing in page.get_drawings():
            rect = drawing.get("rect")
            if rect and rect.width > 1 and rect.height > 1:
                boxes.append(rect)
    except Exception:  # noqa: BLE001 - 个别损坏页不影响整体
        pass

    try:
        for info in page.get_images(full=True):
            for rect in page.get_image_rects(info[0]):
                if rect.width > 1 and rect.height > 1:
                    boxes.append(rect)
    except Exception:  # noqa: BLE001
        pass

    if not boxes:
        return None

    union = boxes[0]
    for box in boxes[1:]:
        union |= box
    return union


def _text_direction(page, region) -> tuple[float, float] | None:
    """统计区域内的文字方向，返回占绝对多数的那个方向。

    返回值是 PDF 的文字推进方向向量：
    - (1, 0)  正常横排
    - (0, -1) 从下往上排（逆时针转了 90°）
    - (0, 1)  从上往下排（顺时针转了 90°）
    - (-1, 0) 从右往左排（转了 180°，真正的倒置）

    没有文字、或方向不集中时返回 None —— 那种情况保持原样，不做猜测。
    """
    try:
        data = page.get_text("dict", clip=region)
    except Exception:  # noqa: BLE001
        return None

    counts: dict[tuple[float, float], int] = {}
    for block in data.get("blocks", []):
        for line in block.get("lines", []):
            text = "".join(span.get("text", "") for span in line.get("spans", [])).strip()
            if not text:
                continue
            direction = tuple(round(float(v), 1) for v in line.get("dir", (1.0, 0.0)))
            counts[direction] = counts.get(direction, 0) + 1

    total = sum(counts.values())
    if total < MIN_TEXT_LINES_FOR_ROTATION:
        return None

    dominant, hits = max(counts.items(), key=lambda item: item[1])
    if hits / total < VERTICAL_TEXT_RATIO:
        return None
    return dominant  # 占绝对多数


def _rotation_for(direction: tuple[float, float] | None) -> int:
    """文字方向 → 让文字横过来所需的顺时针旋转角度。"""
    if direction is None:
        return 0
    dx, dy = direction
    if abs(dx) >= abs(dy):
        # 横向：(-1,0) 是从右往左，属于真正倒置，转 180°
        return 180 if dx < 0 else 0
    # 纵向：(0,-1) 从下往上 → 顺时针转 90；(0,1) 从上往下 → 逆时针转 90（即 270）
    return 90 if dy < 0 else 270


def _rotate_pixmap(pix, angle: int):
    """把 Pixmap 顺时针旋转指定角度。

    ⚠️ PyMuPDF 的 `Page.get_pixmap()` **没有 rotate 参数**（试过，直接 TypeError）。
    这里绕一下：把 pixmap 放回一个临时 PDF 页，设置页面的 /Rotate，再渲染出来。
    实测 set_rotation(90) 就是顺时针 90°（用不对称色块验证过方向）。
    """
    if angle % 360 == 0:
        return pix
    import pymupdf

    tmp = pymupdf.open()
    try:
        page = tmp.new_page(width=pix.width, height=pix.height)
        page.insert_image(page.rect, pixmap=pix)
        page.set_rotation(angle % 360)
        return page.get_pixmap(dpi=72)
    finally:
        tmp.close()


def _ink_ratio(pix) -> float:
    """采样估算非白像素占比，用来判断区域是不是空白。"""
    samples = pix.samples
    if not samples:
        return 0.0
    stride = max(len(samples) // 4000, 1)
    step = stride - (stride % pix.n or pix.n)
    sampled = samples[::step]
    if not sampled:
        return 0.0
    # 只看每个像素的第一个通道即可判断深浅
    dark = sum(1 for value in sampled if value < 240)
    return dark / len(sampled)


def _caption_text(page, rect) -> str:
    """取图注那一行的完整文字，作为图片说明。

    ⚠️ 不能用 `page.get_text(clip=...)`：它返回的是**与裁剪框相交的整个文本块**，
    不是严格裁剪后的文字。实测会把图里的坐标轴标签一起带进来
    （"iter. (1e4) iter. (1e4) Figure 6. ..."）。

    改用逐词过滤：只保留起点在图注标签右侧、且纵向落在同一行的词。
    """
    try:
        words = page.get_text("words")
    except Exception:  # noqa: BLE001
        return ""

    line_words = [
        word
        for word in words
        if word[1] >= rect.y0 - 4      # 与标签同一行（纵向接近）
        and word[3] <= rect.y1 + 6
        and word[0] >= rect.x0 - 3     # 不取标签左侧的内容
    ]
    if not line_words:
        return ""

    line_words.sort(key=lambda w: w[0])
    # words 的排序是按阅读顺序给出的，这里再按 x 排一遍保证连贯
    text = " ".join(word[4] for word in line_words)
    return re.sub(r"\s+", " ", text).strip()[:220]


def _canonical_key(kind: str, number: str) -> tuple[str, str]:
    """图注的规范键，用来去重。

    `Figure 4` 和 `Fig. 4` 指的是同一张图，必须归一到同一个键，
    否则会同时提取出两遍（实测 ResNet 那篇就重复了一对）。
    """
    return ("table" if kind.lower().startswith("tab") else "figure", number)


def _is_strong_label(raw_label: str) -> bool:
    """判断是否是「正式图注」的写法。

    正文里的交叉引用通常写成 `Fig. 4 shows the training procedures`，
    而真正的图注是 `Figure 4. ...`。全文拼写是更可靠的信号，
    所以同号冲突时优先保留全文写法。
    """
    head = raw_label.split()[0].lower().rstrip(".")
    return head == "figure" or head == "table"


def extract_figures(
    pdf_bytes: bytes,
    output_dir: Path,
    stem: str,
    *,
    max_figures: int = MAX_FIGURES,
    auto_upright: bool = True,
) -> list[ExtractedFigure]:
    """按图注定位并渲染论文里的图和表。

    auto_upright=True 时，会检测图内文字方向，把整体转了 90°/180° 的图摆正
    （关闭就完全按 PDF 原样输出）。

    失败一律降级为「没有配图」，不抛异常——配图是锦上添花，
    不该因为它没提取到就让整期播客生成失败。
    """
    import pymupdf

    try:
        doc = _open_pdf(pdf_bytes)
    except PdfAssetsError as exc:
        logger.warning("配图提取跳过：%s", exc)
        return []

    output_dir.mkdir(parents=True, exist_ok=True)

    # 先收集候选，最后统一去重排序。
    # 不能边扫边收：同一张图可能被正文引用误命中，需要和真图注比较后择优。
    candidates: list[tuple[dict, "pymupdf.Rect"]] = []
    seen_keys: dict[tuple[str, str], int] = {}

    try:
        for page_index in range(doc.page_count):
            page = doc[page_index]
            try:
                page_text = page.get_text()
            except Exception:  # noqa: BLE001
                continue

            graphics = _graphics_bbox(page)
            if graphics is None:
                # 没有矢量图形/位图 —— 多数是纯文字排版的表格，截出来就是
                # 一段文字截图，没有配图价值
                continue

            for match in _CAPTION.finditer(page_text):
                raw_label = match.group(0).strip().rstrip(":.．：").strip()
                kind_raw = match.group(1)
                number = match.group(2)
                is_table = kind_raw.lower().startswith("tab")

                try:
                    hits = page.search_for(raw_label)
                except Exception:  # noqa: BLE001
                    continue
                if not hits:
                    continue
                caption_rect = hits[0]

                if is_table:
                    # 表注在表上方：往下取
                    search_area = pymupdf.Rect(
                        page.rect.x0, caption_rect.y1, page.rect.x1, page.rect.y1
                    )
                else:
                    # 图注在图下方：往上取
                    search_area = pymupdf.Rect(
                        page.rect.x0, page.rect.y0, page.rect.x1, caption_rect.y0
                    )

                region = search_area & graphics
                if region.is_empty:
                    continue
                if (
                    region.width < MIN_FIGURE_WIDTH_PT
                    or region.height < MIN_FIGURE_HEIGHT_PT
                ):
                    continue

                key = _canonical_key(kind_raw, number)
                strong = _is_strong_label(raw_label)

                # 同号的图只保留一张，优先「Figure N」全文写法
                if key in seen_keys:
                    existing_index = seen_keys[key]
                    if not strong or candidates[existing_index][0]["strong"]:
                        continue
                    candidates.pop(existing_index)
                    seen_keys = {
                        k: (v - 1 if v > existing_index else v)
                        for k, v in seen_keys.items()
                        if v != existing_index
                    }

                seen_keys[key] = len(candidates)
                candidates.append(
                    (
                        {
                            "kind": "table" if is_table else "figure",
                            "label": raw_label,
                            "strong": strong,
                            "page": page_index + 1,
                            "caption": _caption_text(page, caption_rect),
                            "region": region,
                            "page_obj": page,
                        },
                        region,
                    )
                )

        # 渲染候选，跳过空白区域
        figures: list[ExtractedFigure] = []
        for entry, region in candidates:
            if len(figures) >= max_figures:
                break
            page = entry["page_obj"]
            padded = pymupdf.Rect(region.x0 - 4, region.y0 - 4, region.x1 + 4, region.y1 + 4)
            padded &= page.rect

            # 图里文字全是竖排时，把图转正再输出（见 _text_direction 的说明）
            rotation = 0
            if auto_upright:
                direction = _text_direction(page, region)
                rotation = _rotation_for(direction)
                if rotation:
                    entry["rotation"] = rotation
                    logger.info(
                        "配图 %s 的文字方向为 %s，已顺时针旋转 %d° 摆正",
                        entry["label"],
                        direction,
                        rotation,
                    )

            try:
                pix = page.get_pixmap(clip=padded, dpi=FIGURE_DPI)
                if rotation:
                    pix = _rotate_pixmap(pix, rotation)
            except Exception as exc:  # noqa: BLE001
                # 不要把异常静默吞掉：之前正是因为 except 后直接 continue，
                # 把「get_pixmap 不认识 rotate 参数」这个真实错误藏了整整一轮。
                logger.warning("配图 %s 渲染失败：%s", entry.get("label"), exc)
                continue

            if pix.width * pix.height < MIN_AREA_PX:
                continue
            if _ink_ratio(pix) < MIN_INK_RATIO:
                logger.debug(
                    "第 %d 页 %s 区域接近空白，跳过", entry["page"], entry["label"]
                )
                continue

            figure_id = f"{'t' if entry['kind'] == 'table' else 'f'}{len(figures) + 1}"
            path = output_dir / f"{stem}-{figure_id}.png"
            try:
                pix.save(str(path))
            except Exception as exc:  # noqa: BLE001
                logger.warning("配图保存失败：%s", exc)
                continue

            figures.append(
                ExtractedFigure(
                    id=figure_id,
                    kind=entry["kind"],
                    label=entry["label"],
                    caption=entry["caption"],
                    page=entry["page"],
                    path=str(path),
                    width=pix.width,
                    height=pix.height,
                )
            )
            logger.info(
                "提取到配图 %s（第 %d 页，%dx%d）",
                entry["label"],
                entry["page"],
                pix.width,
                pix.height,
            )

    except Exception as exc:  # noqa: BLE001
        logger.warning("配图提取中途出错，返回已提取的部分：%s", exc)
    finally:
        doc.close()

    return figures
