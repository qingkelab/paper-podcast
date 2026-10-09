"""多子图切分：把一张论文原图按「空白沟」切成若干个子图（panel）。

## 为什么需要它

「讲到哪一块就把那一块框出来」这件事的难点从来不是画框，而是**知道那块在哪**。
让模型直接给坐标试过了，不行 —— 模型看不到图，只能猜（实测 16 段里 4 个框
全是同一块「左半张」，只是标签不同）。

但有一件事模型是**能做对**的：图上写着 `(a) (b) (c)` 的时候，
图注里也写着 `(a) ... (b) ...`，让它在几步里挑「第几个子图」是可靠的文字活。
于是分工变成：
- **几何由像素决定**（这个文件负责）：沟在哪、每块多大，从图本身量出来，不猜；
- **语义由模型决定**：这一段在讲第几个子图，它从图注能读出来。

## 怎么切

把图缩到「格子」（每格标记有没有墨），再做**行/列投影**，找连续的空沟当分隔。
递归地在最靠中间的那条够宽的沟处切一刀，直到没有可切的沟或切到上限。
不用 numpy（项目里没这个依赖），格子化之后是纯 Python 的整数运算，一张
1280×720 的图毫秒级。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# 格子边长（像素）：先降采样再看投影，既快又能滤掉单像素噪声
BLOCK = 4
# 判定「有墨」的灰度阈值（白底图上低于这个就算内容）
INK_LEVEL = 236
# 沟的最小宽度（占该方向的比例）与最小绝对宽度（像素）
GUTTER_RATIO = 0.035
GUTTER_MIN_BLOCKS = 6
# 一块子图至少要占的比例、至少要有的墨比例（挡掉把噪点当子图）
MIN_PANEL_SIDE = 0.08
MIN_PANEL_INK = 0.01
MAX_PANELS = 6
# 只在中间这段区间里找沟：贴着边缘的「沟」多半是页边距，不是子图分隔
SPLIT_ZONE = (0.25, 0.75)
# 子图之间留一点余量，别把相邻子图的内容裁掉
PANEL_PAD = 0.004
# 做投影时上下（或左右）各忽略多少：那是坐标轴刻度/标题/标注所在的边带
BAND_INSET = 0.15
# 子图标注算「同一横排」的纵向容差（比例，约等于一行文字）
MARK_ROW_TOL = 0.12
# 排「阅读顺序」时，纵向差多少以内算同一排
ROW_TOL = 0.12
# 收到内容边界后再外扩一点，免得把坐标轴框线压掉
TRIM_PAD = 0.01
# 收到只剩这么小一块就退回收之前的框（收过头比不收更糟）
TRIM_MIN_KEEP = 0.3


@dataclass(frozen=True)
class Panel:
    """一块子图，坐标是**相对整张图的 0~1 比例**（和 focus 用同一套坐标）。"""

    x: float
    y: float
    w: float
    h: float

    @property
    def area(self) -> float:
        return self.w * self.h

    def to_focus(self, label: str = "") -> dict[str, object]:
        return {"x": self.x, "y": self.y, "w": self.w, "h": self.h, "label": label}


_PANEL_MARK = re.compile(r"\(([a-hj-z]|[1-9])\)")


def panel_count_from_caption(caption: str, *, cap: int = MAX_PANELS) -> int | None:
    """图注里 `(a) (b) (c)` 这种枚举就说明这张图有几个子图。

    这是**文字活**，模型和我们都能读对；几何仍然由像素决定。
    少于 2 个的一律当「没有子图」。
    """
    marks = {match.group(1).lower() for match in _PANEL_MARK.finditer(caption or "")}
    if len(marks) < 2:
        return None
    return min(len(marks), cap)


def panel_labels(caption: str, *, cap: int = MAX_PANELS) -> list[tuple[str, str]]:
    """把图注拆成「子图标记 → 这一块讲什么」。

    例：`Figure 1: ... (a) Recurrent state memory grows with concurrent requests
    and can exceed model weight memory. (b) Heads with longer gate half-lives ...`
    → `[("a", "Recurrent state memory grows ... memory."), ("b", "Heads with ...")]`

    **为什么要拆**：让模型「挑第几个子图」需要它能分辨这几块是什么。只给它一个
    数字（「这张图有 3 块」）它是挑不对的；给它每块的说明文字，这就是纯文本匹配，
    它能挑对。顺序就是图注里的出现顺序，也是 `detect_panels` 的排序口径
    （从上到下、从左到右），两者要能对上。
    """
    text = re.sub(r"\s+", " ", caption or "").strip()
    hits = list(_PANEL_MARK.finditer(text))
    if len(hits) < 2:
        return []

    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for index, hit in enumerate(hits):
        mark = hit.group(1).lower()
        if mark in seen:
            continue
        end = hits[index + 1].start() if index + 1 < len(hits) else len(text)
        body = text[hit.end() : end].strip(" .;，。；")
        seen.add(mark)
        out.append((mark, body))
        if len(out) >= cap:
            break
    return out


def _valleys(profile: list[int], *, span: int, count: int) -> list[int]:
    """投影里的「沟」（相对低点）。

    真实论文图很少有「完全空白」的分隔带：曲线会渗出一点、表格有横线，
    严格找 0 永远找不到。所以改找**相对低点** —— 比周围明显矮的地方就是分隔处。
    """
    if count <= 0:
        return []
    limit = max(1, int(span * 0.10))
    runs: list[tuple[int, int]] = []
    start: int | None = None
    for index, value in enumerate(profile):
        if value <= limit:
            start = index if start is None else start
        else:
            if start is not None:
                runs.append((start, index))
            start = None
    if start is not None:
        runs.append((start, len(profile)))

    scored = []
    for start, end in runs:
        if start <= 1 or end >= len(profile) - 1:
            continue  # 贴边的「沟」是页边距，不是分隔
        depth = limit - min(profile[start:end])
        width = end - start
        scored.append((depth + width, (start + end) // 2))
    scored.sort(reverse=True)
    picked = sorted(centre for _, centre in scored[:count])
    return picked


def _gutter_candidates(grid: list[list[bool]], box: tuple[int, int, int, int]) -> list[tuple[float, str, int, int]]:
    """这个框里所有「可以切一刀」的沟，带分数（越大越像真分隔）。

    分数 = 沟的宽度 − 0.5 × 偏离中心的距离（都换算成格子数）。
    两项都算上是有原因的：真实论文图的排版里**内嵌的图例/网格空白**也是空白的，
    只看「空」会把一张折线图切成两半；而只挑最靠中间的，又会漏掉像
    「左边一大块 + 右边上下两块」这种真分隔不在正中间的排版。

    投影只在**中间那条带**上算（见 `_band_profile`）：图里的坐标轴刻度、坐标轴标题、
    子图标注都排在上下边缘，它们会让每一列都「有墨」，于是真正的空白分隔线
    在整幅投影里根本看不出来 —— 实测 Figure 2 整幅投影 0 条沟、只看中间带 2 条。
    """
    x0, y0, x1, y1 = box
    span_x, span_y = x1 - x0, y1 - y0

    def scored(profile: list[int], *, axis: str, span: int, other: int) -> list[tuple[float, str, int, int]]:
        out: list[tuple[float, str, int, int]] = []
        for start, end in _gutters(profile, total_other=other, long_side=span):
            ratio = ((start + end) / 2) / max(span, 1)
            if not (SPLIT_ZONE[0] <= ratio <= SPLIT_ZONE[1]):
                continue
            off = abs(ratio - 0.5) * span
            out.append(((end - start) - 0.5 * off, axis, start, end))
        return out

    prof_x, used_y = _band_profile(grid, box, axis="x")
    prof_y, used_x = _band_profile(grid, box, axis="y")
    found = scored(prof_x, axis="x", span=span_x, other=used_y)
    found += scored(prof_y, axis="y", span=span_y, other=used_x)
    return [
        (score, axis, x0 + start, x0 + end) if axis == "x" else (score, axis, y0 + start, y0 + end)
        for score, axis, start, end in found
    ]


def _band_profile(
    grid: list[list[bool]], box: tuple[int, int, int, int], *, axis: str
) -> tuple[list[int], int]:
    """只在框的中间带做投影，返回 `(投影, 参与统计的另一方向长度)`。

    竖切（找左右分隔）时忽略上下各 15%：那里是坐标轴刻度和子图标注，它们横跨整幅宽度。
    横切（找上下分隔）时忽略左右各 15%：那里是纵轴刻度和图例。
    这一条是实测出来的：Figure 2 用整幅投影找不到任何一条沟，只看中间带就出现
    两条干净的分隔（0.32~0.36、0.62~0.66），正好是三块子图。
    """
    x0, y0, x1, y1 = box
    if axis == "x":
        lo = y0 + int((y1 - y0) * BAND_INSET)
        hi = y1 - int((y1 - y0) * BAND_INSET)
        lo, hi = max(lo, y0), max(hi, lo + 1)
        profile = [sum(1 for row in range(lo, hi) if grid[row][col]) for col in range(x0, x1)]
        return profile, hi - lo
    lo = x0 + int((x1 - x0) * BAND_INSET)
    hi = x1 - int((x1 - x0) * BAND_INSET)
    lo, hi = max(lo, x0), max(hi, lo + 1)
    profile = [sum(1 for col in range(lo, hi) if grid[row][col]) for row in range(y0, y1)]
    return profile, hi - lo


def _widest_gap(profile: list[int], lo: int, hi: int) -> int | None:
    """在 `[lo, hi)` 里找最宽的一条「几乎空白」带，返回它的中心；一条都没有则返回 None。

    用「最宽的空白带」而不是「最空的那一列」：空白带才是排版上的分隔缝，
    而单看最小值会被抗锯齿噪点骗到 —— 实测同一张图里 0.601 和 0.645 两列都是 0 墨，
    但只有前者是一条 4 格宽的带。

    **找不到空白带时返回 None 而不是「最空的列」**：真粘在一起的两块（整片都是墨）
    本来就不该切，硬挑一列切出来的框会横跨两个子图 —— 那种框比不框更糟。
    """
    if hi <= lo:
        return None
    limit = 1
    best: tuple[int, int] | None = None
    start: int | None = None
    for index in range(lo, hi):
        if profile[index] <= limit:
            start = index if start is None else start
        else:
            if start is not None:
                if best is None or (index - start) > (best[1] - best[0]):
                    best = (start, index)
                start = None
    if start is not None and (best is None or (hi - start) > (best[1] - best[0])):
        best = (start, hi)
    if best is None:
        return None
    return (best[0] + best[1]) // 2


def _split_by_marks(
    grid: list[list[bool]],
    cols: int,
    rows: int,
    marks: list[tuple[float, float]],
    expected: int,
) -> list[tuple[int, int, int, int]]:
    """图上的子图标注 `(a) (b) (c)` 在哪，边界就切在它们中间那条空白缝上。

    这是把**文字位置**和**像素几何**合起来用：标注是 PDF 里真实存在的文字
    （不是猜的），而边界仍然由像素里的空白缝决定。为什么需要它：
    Figure 1 是「三个子图横向排开」，但每两个子图之间夹着下一个子图的纵轴刻度，
    中间带里**没有一列是空的**，纯投影怎么切都切不出 3 块；
    而 `(a)/(b)/(c)` 三个标注的位置把搜索范围收窄到两条缝上，一找一个准。

    只会处理「一横排」或「一竖列」的排布：2×2 这种网格里 `(c)` 会回到第一列，
    从左到右的顺序和标注顺序对不上，硬按顺序映射会指错块 —— 那种情况返回空，
    交给纯几何那几条路。
    """
    if len(marks) != expected or expected < 2:
        return []
    xs = [point[0] for point in marks]
    ys = [point[1] for point in marks]
    same_row = (max(ys) - min(ys)) < MARK_ROW_TOL
    same_col = (max(xs) - min(xs)) < MARK_ROW_TOL
    if same_row == same_col:
        return []  # 既不是一横排也不是一竖列（网格）→ 不用这条路

    axis = "x" if same_row else "y"
    order = sorted(range(len(marks)), key=lambda i: xs[i] if axis == "x" else ys[i])
    length = cols if axis == "x" else rows
    lo_inset = int(length * BAND_INSET)
    hi_inset = length - int(length * BAND_INSET)

    cuts: list[int] = []
    for left, right in zip(order, order[1:]):
        a = int(marks[left][0] * cols) if axis == "x" else int(marks[left][1] * rows)
        b = int(marks[right][0] * cols) if axis == "x" else int(marks[right][1] * rows)
        a, b = min(a, b), max(a, b)
        # 只在这两个标注之间、且不在边缘带上找缝
        a = max(a, lo_inset)
        b = min(b, hi_inset)
        if b - a < 2:
            return []
        gap = _widest_gap(_band_profile(grid, (0, 0, cols, rows), axis=axis)[0], a, b)
        if gap is None:
            return []  # 两个标注之间没有空白缝 → 这两块粘在一起，别硬切
        cuts.append(gap)
    if len(cuts) != expected - 1:
        return []
    edges = [0, *sorted(cuts), length]
    boxes: list[tuple[int, int, int, int]] = []
    for index in range(len(edges) - 1):
        start, end = edges[index], edges[index + 1]
        if end - start < 2:
            return []
        boxes.append((start, 0, end, rows) if axis == "x" else (0, start, cols, end))
    return boxes


def _trim_to_ink(
    grid: list[list[bool]], box: tuple[int, int, int, int], cols: int, rows: int
) -> tuple[int, int, int, int]:
    """把框收到「真的有内容」的范围上，再留一点余量。

    切出来的框常常带一大片白边（Figure 1 左边有 18% 是页边距）：
    聚光灯是「压暗框外、框出框内」，框得越松，被压暗的区域就越小、突出效果越弱。
    收得太狠（收到只剩中间一小块）就退回原框 —— 那样比不收更糟。
    """
    x0, y0, x1, y1 = box
    left, right, top, bottom = x1, x0, y1, y0
    for row in range(y0, y1):
        line = grid[row]
        for col in range(x0, x1):
            if line[col]:
                left = min(left, col)
                right = max(right, col)
                top = min(top, row)
                bottom = max(bottom, row)
    if right < left or bottom < top:
        return box
    pad_x = max(1, int((x1 - x0) * TRIM_PAD))
    pad_y = max(1, int((y1 - y0) * TRIM_PAD))
    trimmed = (
        max(x0, left - pad_x),
        max(y0, top - pad_y),
        min(x1, right + 1 + pad_x),
        min(y1, bottom + 1 + pad_y),
    )
    original = (x1 - x0) * (y1 - y0)
    kept = (trimmed[2] - trimmed[0]) * (trimmed[3] - trimmed[1])
    if original <= 0 or kept < original * TRIM_MIN_KEEP:
        return box
    return trimmed


def _split_to_count(
    grid: list[list[bool]], cols: int, rows: int, expected: int
) -> list[tuple[int, int, int, int]]:
    """贪心地切到「正好 expected 块」：每次在**最好的一刀**上切，而不是一次算完。

    `_panel_by_expected` 里那几种策略都要求「一次性算出 expected−1 条分隔线」，
    所以它只处理得了等分式排版（一排 3 块、2×2）。真实论文里常见的是
    **嵌套**的：Figure 1 实测是「左边一块占 60% + 右边上下两块」——
    第一刀竖着切在 0.63 处，第二刀得**只在右半张**里横着切。
    这种结构只能一块一块地递归切出来。

    `_split` 也是递归切，但它切到切不动为止、不管块数；这里以**块数**为目标，
    所以每次挑当前所有框里最好的那一刀。
    """
    boxes: list[tuple[int, int, int, int]] = [(0, 0, cols, rows)]
    while len(boxes) < expected:
        best: tuple[float, int, str, int, int] | None = None
        for index, box in enumerate(boxes):
            for score, axis, start, end in _gutter_candidates(grid, box):
                if best is None or score > best[0]:
                    best = (score, index, axis, start, end)
        if best is None:
            break
        _, index, axis, start, end = best
        box = boxes[index]
        if axis == "x":
            boxes[index : index + 1] = [
                (box[0], box[1], start, box[3]),
                (end, box[1], box[2], box[3]),
            ]
        else:
            boxes[index : index + 1] = [
                (box[0], box[1], box[2], start),
                (box[0], end, box[2], box[3]),
            ]
    if len(boxes) != expected:
        return []
    if any(_ink_ratio(grid, box) < MIN_PANEL_INK for box in boxes):
        return []
    return boxes


def _panel_by_expected(
    grid: list[list[bool]], cols: int, rows: int, expected: int
) -> list[tuple[int, int, int, int]]:
    """已知子图个数时，按「最深的沟」把它切开。

    先试竖切（一排子图），再试横切（一列子图），最后试因数分解的网格（2×2、2×3…），
    再不行就贪心递归切（见 `_split_to_count`）。
    每种都要求**切出来的块数正好等于 expected、且每块都有墨**，否则换下一种 ——
    块数对不上时「第 3 个子图」这种定位就是错的，宁可当没有子图。
    """
    def columns(box: tuple[int, int, int, int], count: int) -> list[tuple[int, int, int, int]]:
        x0, y0, x1, y1 = box
        profile = [sum(1 for row in range(y0, y1) if grid[row][col]) for col in range(x0, x1)]
        cuts = _valleys(profile, span=y1 - y0, count=count - 1)
        if len(cuts) != count - 1:
            return []
        edges = [0, *cuts, x1 - x0]
        return [(x0 + edges[i], y0, x0 + edges[i + 1], y1) for i in range(count)]

    def rows_of(box: tuple[int, int, int, int], count: int) -> list[tuple[int, int, int, int]]:
        x0, y0, x1, y1 = box
        profile = [sum(1 for col in range(x0, x1) if grid[row][col]) for row in range(y0, y1)]
        cuts = _valleys(profile, span=x1 - x0, count=count - 1)
        if len(cuts) != count - 1:
            return []
        edges = [0, *cuts, y1 - y0]
        return [(x0, y0 + edges[i], x1, y0 + edges[i + 1]) for i in range(count)]

    def usable(boxes: list[tuple[int, int, int, int]]) -> bool:
        return len(boxes) == expected and all(
            _ink_ratio(grid, box) >= MIN_PANEL_INK for box in boxes
        )

    whole = (0, 0, cols, rows)
    for candidate in (columns(whole, expected), rows_of(whole, expected)):
        if candidate and usable(candidate):
            return candidate

    for across in range(2, expected):
        if expected % across:
            continue
        down = expected // across
        if down < 1:
            continue
        bands = rows_of(whole, down) or columns(whole, down)
        if not bands:
            continue
        if down == expected:
            grid_boxes = bands
        else:
            grid_boxes = []
            for band in bands:
                cells = columns(band, across)
                if not cells or any(_ink_ratio(grid, cell) < MIN_PANEL_INK for cell in cells):
                    grid_boxes = []
                    break
                grid_boxes.extend(cells)
        if grid_boxes and usable(grid_boxes):
            return grid_boxes
    return _split_to_count(grid, cols, rows, expected)


def _ink_grid(path: Path, *, block: int = BLOCK) -> tuple[list[list[bool]], int, int, int, int] | None:
    """把图压成「有墨/没墨」的格子矩阵。

    返回 `(grid, cols, rows, width, height)`；打不开图返回 None。
    """
    try:
        import pymupdf
    except ImportError:  # pragma: no cover - 运行环境一定有
        return None
    try:
        pix = pymupdf.Pixmap(str(path))
    except Exception:  # noqa: BLE001 - 图坏了就当没有子图，不影响出片
        return None

    width, height = pix.width, pix.height
    if width < block * 4 or height < block * 4:
        return None
    cols, rows = width // block, height // block
    samples = pix.samples
    channels = pix.n
    grid: list[list[bool]] = []
    for row in range(rows):
        line: list[bool] = []
        y0 = row * block
        for col in range(cols):
            x0 = col * block
            has_ink = False
            for y in range(y0, y0 + block):
                base = y * width
                for x in range(x0, x0 + block):
                    index = (base + x) * channels
                    level = (samples[index] + samples[index + 1] + samples[index + 2]) // 3
                    if level < INK_LEVEL:
                        has_ink = True
                        break
                if has_ink:
                    break
            line.append(has_ink)
        grid.append(line)
    return grid, cols, rows, width, height


def _profile(grid: list[list[bool]], *, axis: str) -> list[int]:
    """投影：按行或按列数「有墨的格子数」。"""
    if axis == "x":
        cols = len(grid[0]) if grid else 0
        return [sum(1 for row in grid if row[col]) for col in range(cols)]
    return [sum(1 for cell in row if cell) for row in grid]


def _gutters(profile: list[int], *, total_other: int, long_side: int) -> list[tuple[int, int]]:
    """找出连续的「空沟」区间（左闭右开）。

    判据是「几乎全是空的」而不是「严格为 0」：抗锯齿和细网格线会让真正的分隔处
    残留一两个格子，用严格 0 会切不开。
    """
    limit = max(1, int(total_other * 0.02))
    min_len = max(GUTTER_MIN_BLOCKS, int(long_side * GUTTER_RATIO))
    runs: list[tuple[int, int]] = []
    start: int | None = None
    for index, value in enumerate(profile):
        if value <= limit:
            start = index if start is None else start
        else:
            if start is not None and index - start >= min_len:
                runs.append((start, index))
            start = None
    if start is not None and len(profile) - start >= min_len:
        runs.append((start, len(profile)))
    return runs


def _ink_ratio(grid: list[list[bool]], box: tuple[int, int, int, int]) -> float:
    x0, y0, x1, y1 = box
    total = 0
    inked = 0
    for row in range(y0, y1):
        line = grid[row]
        for col in range(x0, x1):
            total += 1
            if line[col]:
                inked += 1
    return inked / total if total else 0.0


def _split(
    grid: list[list[bool]],
    box: tuple[int, int, int, int],
    *,
    out: list[tuple[int, int, int, int]],
    cols: int,
    rows: int,
) -> None:
    """递归切分：优先在最靠中间、够宽的沟处切一刀。"""
    if len(out) >= MAX_PANELS:
        return
    x0, y0, x1, y1 = box

    candidates: list[tuple[float, str, int, int]] = []
    # 竖直沟（把左右分开）
    profile = [sum(1 for row in range(y0, y1) if grid[row][col]) for col in range(x0, x1)]
    for start, end in _gutters(profile, total_other=y1 - y0, long_side=x1 - x0):
        centre = (x0 + start + x0 + end) / 2
        ratio = (centre - x0) / max(x1 - x0, 1)
        if SPLIT_ZONE[0] <= ratio <= SPLIT_ZONE[1]:
            candidates.append((abs(ratio - 0.5), "x", x0 + start, x0 + end))
    # 水平沟（把上下分开）
    profile_y = [sum(1 for col in range(x0, x1) if grid[row][col]) for row in range(y0, y1)]
    for start, end in _gutters(profile_y, total_other=x1 - x0, long_side=y1 - y0):
        centre = (y0 + start + y0 + end) / 2
        ratio = (centre - y0) / max(y1 - y0, 1)
        if SPLIT_ZONE[0] <= ratio <= SPLIT_ZONE[1]:
            candidates.append((abs(ratio - 0.5), "y", y0 + start, y0 + end))

    if not candidates:
        out.append(box)
        return
    candidates.sort(key=lambda item: item[0])
    _, axis, start, end = candidates[0]
    if axis == "x":
        _split(grid, (x0, y0, start, y1), out=out, cols=cols, rows=rows)
        _split(grid, (end, y0, x1, y1), out=out, cols=cols, rows=rows)
    else:
        _split(grid, (x0, y0, x1, start), out=out, cols=cols, rows=rows)
        _split(grid, (x0, end, x1, y1), out=out, cols=cols, rows=rows)


def _score_valleys(profile: list[int], *, span: int) -> list[tuple[float, int]]:
    """把投影里的沟打分：越深、越宽 = 越像真正的分隔。返回 [(分数, 位置)]，按分数降序。"""
    limit = max(1, int(span * 0.10))
    scored: list[tuple[float, int]] = []
    start: int | None = None
    for index, value in enumerate(profile):
        if value <= limit:
            start = index if start is None else start
        else:
            if start is not None and start > 1 and index < len(profile) - 1:
                depth = limit - min(profile[start:index])
                scored.append((depth + (index - start), (start + index) // 2))
            start = None
    if start is not None and start > 1:
        depth = limit - min(profile[start:])
        scored.append((depth + (len(profile) - start), (start + len(profile)) // 2))
    scored.sort(reverse=True)
    return scored


def _split_automatically(
    grid: list[list[bool]], cols: int, rows: int
) -> list[tuple[int, int, int, int]]:
    """没有图注可依时，自己判断该怎么切。

    做法：两个方向各算一遍沟的分数，谁的最深沟更明显就往谁那边切；同一方向里
    分数达到最好那条 60% 以上的沟都算分隔，于是子图个数由**图的形状**决定，
    不需要外部信息。切完要求每块都有墨 —— 切错了会框到空白，那就宁可返回空。
    """
    def axis_boxes(axis: str) -> list[tuple[int, int, int, int]]:
        if axis == "x":
            profile = [sum(1 for row in range(rows) if grid[row][col]) for col in range(cols)]
            span = rows
        else:
            profile = [sum(1 for col in range(cols) if grid[row][col]) for row in range(rows)]
            span = cols
        scored = _score_valleys(profile, span=span)
        if not scored:
            return []
        best = scored[0][0]
        picked = sorted(centre for score, centre in scored if score >= best * 0.6)[: MAX_PANELS - 1]
        if not picked:
            return []
        edges = [0, *picked, (cols if axis == "x" else rows)]
        boxes: list[tuple[int, int, int, int]] = []
        for index in range(len(edges) - 1):
            if axis == "x":
                boxes.append((edges[index], 0, edges[index + 1], rows))
            else:
                boxes.append((0, edges[index], cols, edges[index + 1]))
        return boxes

    scored_x = _score_valleys([sum(1 for row in range(rows) if grid[row][col]) for col in range(cols)], span=rows)
    scored_y = _score_valleys([sum(1 for col in range(cols) if grid[row][col]) for row in range(rows)], span=cols)
    best_x = scored_x[0][0] if scored_x else 0.0
    best_y = scored_y[0][0] if scored_y else 0.0
    order = ["x", "y"] if best_x >= best_y else ["y", "x"]
    for axis in order:
        boxes = axis_boxes(axis)
        if len(boxes) < 2:
            continue
        if any(_ink_ratio(grid, box) < MIN_PANEL_INK for box in boxes):
            continue
        # 别把「一大块 + 一小条」当子图：每块都得占得住地方
        sizes = [((box[2] - box[0]) * (box[3] - box[1])) / (cols * rows) for box in boxes]
        if min(sizes) < MIN_PANEL_SIDE:
            continue
        return boxes
    return []


def detect_panels(
    path: Path,
    *,
    expected: int | None = None,
    marks: list[tuple[float, float]] | None = None,
) -> list[Panel]:
    """切出这张图里的子图。

    `marks` 是图上 `(a) (b) (c)` 这些**子图标注**相对整图的 0~1 位置（按标注顺序）。
    给出来时优先按它切（见 `_split_by_marks`）—— 那是 PDF 里真实存在的文字位置，
    比纯投影可靠得多（实测纯投影在 Figure 1 上切不出 3 块，靠标注一找一个准）。

    只有**真的能切**的时候才返回多块：
    - 切不出沟 → 返回 1 块（等于「整张图就是一块」），调用方据此不做聚光灯；
    - 切出来的块太小、太空（噪点/边框）→ 丢掉；
    - 丢到只剩 1 块 → 同样当「没有子图」。
    """
    made = _ink_grid(Path(path))
    if made is None:
        return []
    grid, cols, rows, width, height = made

    boxes: list[tuple[int, int, int, int]] = []
    if expected and expected > 1:
        expected = min(expected, MAX_PANELS)
        if marks:
            boxes = _split_by_marks(grid, cols, rows, list(marks), expected)
        if not boxes:
            # 图注告诉我们有几个子图 → 按「最深的沟」切，比死抠「完全空白」稳得多
            boxes = _panel_by_expected(grid, cols, rows, expected)
    if not boxes:
        boxes = _split_automatically(grid, cols, rows)
    if not boxes:
        _split(grid, (0, 0, cols, rows), out=boxes, cols=cols, rows=rows)

    panels: list[Panel] = []
    for x0, y0, x1, y1 in boxes:
        if x1 - x0 < cols * MIN_PANEL_SIDE or y1 - y0 < rows * MIN_PANEL_SIDE:
            continue
        inset_x = max(1, int((x1 - x0) * PANEL_PAD))
        inset_y = max(1, int((y1 - y0) * PANEL_PAD))
        box = _trim_to_ink(grid, (x0 + inset_x, y0 + inset_y, x1 - inset_x, y1 - inset_y), cols, rows)
        if _ink_ratio(grid, box) < MIN_PANEL_INK:
            continue
        panels.append(
            Panel(
                x=box[0] / cols,
                y=box[1] / rows,
                w=(box[2] - box[0]) / cols,
                h=(box[3] - box[1]) / rows,
            )
        )
    return _reading_order(panels) if len(panels) > 1 else []


def _reading_order(panels: list[Panel], *, tolerance: float = ROW_TOL) -> list[Panel]:
    """按「从上到下、从左到右」排序，让第 i 块对上图注里的第 i 个字母。

    不能直接 `sort(key=(y, x))`：对齐到内容边界之后，同一排的几块 y 会差几个千分点
    （实测 0.027 / 0.041 / 0.082），按 y 排会变成「右边那块排第一」——
    而聚光灯是按序号取块的，顺序错了就会框错子图。
    所以先按 y 分行（容差内算同一排），行内再按 x 排。
    """
    remaining = sorted(panels, key=lambda panel: (panel.y, panel.x))
    ordered: list[Panel] = []
    while remaining:
        head = remaining[0]
        row = [panel for panel in remaining if panel.y - head.y <= tolerance]
        rest = [panel for panel in remaining if panel.y - head.y > tolerance]
        row.sort(key=lambda panel: panel.x)
        ordered.extend(row)
        remaining = rest
    return ordered

