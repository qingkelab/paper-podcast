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


def _panel_by_expected(
    grid: list[list[bool]], cols: int, rows: int, expected: int
) -> list[tuple[int, int, int, int]]:
    """已知子图个数时，按「最深的沟」把它切开。

    先试竖切（一排子图），再试横切（一列子图），最后试因数分解的网格（2×2、2×3…）。
    每种都要求切出来的每块**都有墨**，否则换下一种 —— 切错了会框到空白。
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

    whole = (0, 0, cols, rows)
    for candidate in (columns(whole, expected), rows_of(whole, expected)):
        if candidate and all(_ink_ratio(grid, box) >= MIN_PANEL_INK for box in candidate):
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
        if grid_boxes and all(_ink_ratio(grid, box) >= MIN_PANEL_INK for box in grid_boxes):
            return grid_boxes
    return []


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


def detect_panels(path: Path, *, expected: int | None = None) -> list[Panel]:
    """切出这张图里的子图。

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
        # 图注告诉我们有几个子图 → 按「最深的沟」切，比死抠「完全空白」稳得多
        boxes = _panel_by_expected(grid, cols, rows, min(expected, MAX_PANELS))
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
        box = (x0 + inset_x, y0 + inset_y, x1 - inset_x, y1 - inset_y)
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
    # 从上到下、从左到右排序：这样「第 3 个子图」和读者数图注的顺序一致
    panels.sort(key=lambda panel: (round(panel.y, 2), round(panel.x, 2)))
    return panels if len(panels) > 1 else []
