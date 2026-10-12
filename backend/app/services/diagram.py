"""动态图解镜头：**用论文里已有的信息画一张会长的图**，不是让模型自由编造图表。

为什么要有它：现在的画面只有三种 —— 论文原图、我们生成的信息图/段落示意图、
纯文字页。它们能说明「这一句在讲什么」，但讲不了**结构**：一个流程有几个节点、
谁处理什么、指标之间差多少。参考片里最能打的就是这种「节点依次出现、连线按顺序画出来、
数字滚到目标值」的图解镜头。

## 三条硬规矩（用户明确要求的）

1. **画面里的每一个数字和术语都要有出处**。`DiagramItem.source` 记着它是从哪来的
   （论文解读的哪个字段、或图注/脚本原句），`verify_shot()` 把查不到依据的条目**整条丢掉**
   —— 丢掉的镜头会退回原来的画面流程，而不是画一张看起来很像的图。
2. **不让模型自由画图**。模型只输出**结构化草案**（标题/节点/连线/指标 + 出处），
   几何、配色、字号、动效全部由这里决定 —— 这是「不许编造技术图表」的落地方式。
3. **动效只有四类**：淡入、位移、连线绘制、数字递增（外加一次轻微缩放）。
   每个镜头都复用同一套，不每个镜头各做一套。

## 怎么动起来的（复用现有管线，不引新框架）

一个镜头 = 若干**状态**：状态 0 是「标题 + 第一个节点」，之后每个状态多显出一个节点、
把连线再画长一段、或者把数字滚到下一个值。每个状态都是一张**整幅画面卡**，
正好复用现有的「逐句图片卡」机制（`Scene.beats` 的 card 层）：
- 单状态镜头 = 一张卡，整段不动；
- 多状态镜头 = 状态 i 在第 i 句的时间窗出现（`beat_windows_aligned` 算的窗口，与字幕同一组），
  编码侧用 `fade` + `enable` 叠上去 —— 和「字幕按小句依次出现」是同一套机制。

所以 `video_encode.py` **一行都不用改**：新镜头就是「会换图的卡」。

## 竖版 / 横版

所有几何都从 `Layout` 推（`diagram_box()`），竖版 896×742、横版按比例放大；
字号用「按内容长度自适应 + 下限」两段式，横竖两版都不会溢出（有测试真的量包围盒）。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable, Sequence

from .design import (
    DIAGRAM_ACCENT,
    DIAGRAM_CARD_FILL,
    DIAGRAM_CARD_STROKE,
    DIAGRAM_GRID,
    DIAGRAM_MUTED,
    DIAGRAM_TEXT,
    FONT_STACK,
    Layout,
)
from .video_core import DiagramItem, DiagramMetric, DiagramNode, DiagramShot

__all__ = [
    "DiagramError",
    "shot_state_count",
    "diagram_svg",
    "render_diagram_state",
    "render_diagram_state_html",
    "verify_shot",
    "VerificationResult",
]


class DiagramError(Exception):
    """图解镜头渲染失败（调用方据此退回原来的画面流程）。"""


# --------------------------------------------------------------------------
# 证据核验：查不到依据的条目一律丢掉
# --------------------------------------------------------------------------


class VerificationResult:
    """核验结果：留下来的镜头 + 被丢掉的东西（带原因，写进日志便于复查）。"""

    def __init__(self, shot: DiagramShot | None, dropped: list[str]) -> None:
        self.shot = shot
        self.dropped = dropped

    @property
    def ok(self) -> bool:
        return self.shot is not None

    def __bool__(self) -> bool:  # pragma: no cover - 方便调用点写 if result
        return self.ok

    def summary(self) -> str:
        if self.shot is None:
            return "图解镜头没有通过核验（整条退回原画面）：" + "；".join(self.dropped)
        if not self.dropped:
            return "图解镜头核验通过，没有条目被丢"
        return "图解镜头核验：丢掉 " + str(len(self.dropped)) + " 项（" + "；".join(self.dropped) + "）"


def _normalise(text: str) -> str:
    """证据比对用的归一化：去掉空白与常见标点，只留字与数字。"""
    keep = []
    for char in text or "":
        if char.isalnum() or char.isdigit():
            keep.append(char.lower())
        elif char in "%." and keep and keep[-1].isdigit():
            keep.append(char)
    return "".join(keep)


# 单位词：出现在条目里时**只查数字、不查这个词本身**（否则「28 MB」会因为
# 解说里写的是「28 兆」而被丢掉）
_UNIT_WORDS = frozenset(
    {
        "mb", "gb", "kb", "tb", "ms", "s", "sec", "secs", "bit", "bits", "byte",
        "bytes", "fps", "hz", "khz", "mhz", "ghz", "token", "tokens", "k", "m", "b", "x",
    }
)


def _number_forms(raw: str) -> set[str]:
    """一个数字的几种等价写法（`28` ↔ `28.0`）。"""
    forms = {raw}
    if raw.endswith(".0"):
        forms.add(raw[:-2])
    elif "." not in raw:
        forms.add(f"{raw}.0")
    return forms


def _evidence_blob(evidence: Iterable[str]) -> str:
    return _normalise(" ".join(evidence))


def _supported(item_text: str, blob: str) -> bool:
    """这一条的内容在证据里查得到吗？

    规矩（宁可丢掉，也不要画一张编出来的图）：
    - 条目里的**每个数字**都必须能在证据里找到（数字是最容易被编的东西）；
    - 条目里的**每个拉丁词 / 缩写**（如 INT6、Qwen、KDA、FP32）也必须在证据里出现；
    - 中文只要求「有实义词命中」——逐字比对会把同义改写全判死，那不是我们要拦的东西。
    """
    import re

    numbers = re.findall(r"\d+(?:\.\d+)?", item_text or "")
    for number in numbers:
        # 数字要**按数值比**，不按写法：解说里写「28 兆」、指标里写「28.0 MB」，
        # 它们是同一个数（实测就是这么被误判掉的）。
        if not any(form in blob for form in _number_forms(number)):
            return False
    latin = re.findall(r"[A-Za-z][A-Za-z0-9]{1,}", item_text or "")
    for word in latin:
        # 单位不算「术语」：「MB / 兆」「bit / 比特」是同一个东西的两种写法，
        # 要求它们也在证据里逐字出现，只会把正确的条目判死。
        if word.lower() in _UNIT_WORDS:
            continue
        if word.lower() not in blob:
            return False
    cjk = re.findall(r"[\u4e00-\u9fff]{2,}", item_text or "")
    if not numbers and not latin and cjk:
        # 纯中文条目（标题、结论这类**改写过的句子**）：
        # 要求至少 2 个不同的二字片段能在证据里找到 —— 一个字面都不重合的句子
        # 基本就是编的；而「那这近似会不会让结果不可信？」→「这个近似，会让结果不可信吗？」
        # 这种同义改写仍然能过（近似 / 可信 / 结果 都命中）。
        # 不要求逐字相同：那会把所有正常改写全判死，等于这条核验没人能用。
        hits = set()
        for word in cjk:
            for index in range(len(word) - 1):
                gram = word[index : index + 2]
                if gram in blob:
                    hits.add(gram)
        return len(hits) >= 2
    return True


def verify_shot(
    shot: DiagramShot,
    *,
    evidence: Sequence[str],
    min_nodes: int = 2,
) -> VerificationResult:
    """按证据核验一个图解镜头：不通过的条目丢掉，节点少于 `min_nodes` 就整条放弃。

    `evidence` 是「允许作为依据的文本」的集合 —— 调用方把论文解读的各个字段、
    脚本原句、图注都塞进来（见 `video.py::diagram_evidence`）。
    **短视频里屏幕上多一个查不到来源的数字，比少一个图严重得多**：前者是编，
    后者只是没画。所以这里的默认动作是丢，而不是「保留但标个问号」。
    """
    blob = _evidence_blob(evidence)
    dropped: list[str] = []

    def keep(text: str, label: str) -> bool:
        if not (text or "").strip():
            return False
        if _supported(text, blob):
            return True
        dropped.append(f"{label}「{text[:24]}」查不到依据")
        return False

    nodes: list[DiagramNode] = []
    for node in shot.nodes:
        if keep(node.label, "节点") and (not node.detail or keep(node.detail, "节点说明")):
            nodes.append(node)
        elif not node.detail:
            pass

    kept_ids = {node.id for node in nodes}
    links = [link for link in shot.links if link.src in kept_ids and link.dst in kept_ids]
    if len(links) != len(shot.links):
        dropped.append("有连线指向被丢掉的节点，一并去掉")

    metrics: list[DiagramMetric] = []
    for metric in shot.metrics:
        if keep(f"{metric.value}{metric.unit}", "指标") and keep(metric.label, "指标名"):
            metrics.append(metric)

    bullets = [text for text in shot.bullets if keep(text, "要点")]
    if keep(shot.title, "标题") is False:
        dropped.append("标题无依据 → 整条镜头放弃")
        return VerificationResult(None, dropped)

    # 「要有节点」这条只对流程/对比镜头成立：hook 与 summary 本来就是纯标题镜头
    # （实测第一版把它们也要求 2 个节点，结果开场钩子整条被丢掉）。
    if shot.kind in ("flow", "compare") and len(nodes) < min_nodes and not metrics:
        dropped.append(f"有效节点不足 {min_nodes} 个且没有可用指标 → 整条镜头放弃")
        return VerificationResult(None, dropped)
    if shot.kind not in ("flow", "compare") and not (nodes or metrics or bullets or shot.subtitle):
        dropped.append("标题之外的每一部分都没通过核验 → 整条镜头放弃")
        return VerificationResult(None, dropped)

    # 统一成元组：`DiagramShot` 的字段是 frozen tuple，返回 list 会让
    # 「同一个镜头渲染两次」的比较结果不一致（测试里逮到的）
    verified = replace(
        shot,
        nodes=tuple(nodes),
        links=tuple(links),
        metrics=tuple(metrics),
        bullets=tuple(bullets),
    )
    return VerificationResult(verified, dropped)


# --------------------------------------------------------------------------
# 状态：镜头怎么「长出来」
# --------------------------------------------------------------------------


def shot_state_count(shot: DiagramShot) -> int:
    """这个镜头一共几个状态（= 需要渲染几张卡）。

    状态之间**只多一件事**：多一个节点、多画一段连线、或数字滚到下一个值。
    这就是「节点依次出现、连线按顺序绘制、数字递增」的落法 —— 一次只前进一步，
    观众的眼睛才跟得上。
    """
    if shot.kind == "metric":
        return max(1, min(len(shot.metrics) + 1, 3))
    if shot.kind == "flow":
        return max(1, min(len(shot.nodes), 4))
    if shot.kind == "compare":
        return max(1, min(len(shot.nodes), 3))
    return 1


def _visible(shot: DiagramShot, state: int) -> tuple[int, int, int]:
    """状态 `state` 下：显几个节点 / 画几条连线 / 指标滚到第几个值。"""
    count = shot_state_count(shot)
    state = max(0, min(state, count - 1))
    if shot.kind == "metric":
        # 状态 0 = 条形空、数字为 0；之后每步把一组指标滚到目标值
        return 0, 0, state
    step = state + 1
    nodes = min(len(shot.nodes), step if shot.kind != "compare" else step + 1)
    links = min(len(shot.links), max(0, state))
    return nodes, links, 0


def _metric_shown(metric: DiagramMetric, state: int) -> float:
    """数字递增：状态 0 显示 0，最后一个状态显示目标值（中间状态按比例）。"""
    steps = 2  # 0 → 一半 → 目标值
    if state <= 0:
        return 0.0
    if state >= steps:
        return metric.value
    return round(metric.value / 2, 2)


# --------------------------------------------------------------------------
# 画：一张状态图
# --------------------------------------------------------------------------


def diagram_svg(shot: DiagramShot, layout: Layout, *, state: int = 0) -> str:
    """把镜头的一个状态画成 SVG（**画幅就是图片卡的大小**，坐标原点在卡片左上角）。

    HTML 路径与 SVG 路径共用它：HTML 页面把它整段内联（同一份几何，两条路不会画得不一样）。
    """
    box = diagram_box(layout)
    width, height = int(box["w"]), int(box["h"])
    nodes, links, metric_state = _visible(shot, state)
    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">',
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#ffffff"/>',
        _header(shot, box),
    ]
    if shot.kind == "metric" or shot.metrics:
        parts.append(_metrics(shot, box, metric_state))
    if shot.kind in ("flow", "compare") and shot.nodes:
        parts.append(_nodes(shot, box, nodes, links))
    if shot.bullets:
        parts.append(_bullets(shot, box, nodes))
    parts.append("</svg>")
    return "\n".join(part for part in parts if part)


def diagram_box(layout: Layout) -> dict[str, float]:
    """图解内容区：就用图片卡那块地方（与现有图片卡同一坐标系、同一边距）。

    这样图解镜头和普通图片卡在版式上**是同一块地方**，不需要为它再调一次版式，
    转场/边框/位置全部复用。
    """
    w, h = layout.image_box_w, layout.image_box_h
    pad = 28.0
    return {
        "w": w,
        "h": h,
        "pad": pad,
        "left": pad,
        "right": w - pad,
        "top": 34.0,
        "bottom": h - pad,
    }


def _fit_font(text: str, *, want: float, minimum: float, max_chars: float) -> float:
    """按长度自适应字号（有下限）——**不许溢出**是硬要求（竖版横版都要）。

    估算用「中文一个字约一个字号宽」，与项目里别处同一套近似（`design.wrap_units`）。
    """
    length = max(1, len(text or ""))
    size = min(want, max(minimum, want * max_chars / length))
    return round(size, 1)


def _header(shot: DiagramShot, box: dict[str, float]) -> str:
    title = (shot.title or "").strip()
    if not title:
        return ""
    size = _fit_font(title, want=52, minimum=28, max_chars=16)
    y = box["top"] + size
    accent_w = 8.0
    out = [
        f'<rect x="{box["left"]:.1f}" y="{box["top"] + size * 0.08:.1f}" width="{accent_w:.1f}" '
        f'height="{size * 0.82:.1f}" rx="4" fill="{DIAGRAM_ACCENT}"/>',
        f'<text x="{box["left"] + accent_w + 14:.1f}" y="{y:.1f}" font-family="{FONT_STACK}" '
        f'font-size="{size:.0f}" font-weight="600" fill="{DIAGRAM_TEXT}">{_escape(title)}</text>',
    ]
    subtitle = (shot.subtitle or "").strip()
    if subtitle:
        sub_size = _fit_font(subtitle, want=24, minimum=17, max_chars=30)
        out.append(
            f'<text x="{box["left"] + accent_w + 14:.1f}" y="{y + sub_size * 1.5:.1f}" '
            f'font-family="{FONT_STACK}" font-size="{sub_size:.0f}" '
            f'fill="{DIAGRAM_MUTED}">{_escape(subtitle)}</text>'
        )
    return "\n".join(out)


def _content_top(shot: DiagramShot, box: dict[str, float]) -> float:
    top = box["top"] + 60
    if shot.subtitle:
        top += 30
    return top


def _bullets(shot: DiagramShot, box: dict[str, float], nodes: int) -> str:
    """底部一行要点（也是**有依据才画**，`verify_shot` 已经筛过）。"""
    if not shot.bullets:
        return ""
    size = 24.0
    y = box["bottom"] - size * 0.4
    return "\n".join(
        f'<text x="{box["left"]:.1f}" y="{y - index * (size * 1.6):.1f}" font-family="{FONT_STACK}" '
        f'font-size="{size:.0f}" fill="{DIAGRAM_MUTED}">· {_escape(text)}</text>'
        for index, text in enumerate(shot.bullets[:2])
    )


def _nodes(shot: DiagramShot, box: dict[str, float], visible: int, links: int) -> str:
    """节点卡片 + 连线。竖版横排两列（宽卡），横版一行摆开。

    连线**按顺序画出来**：状态 k 只画前 k 条 —— 与节点的出现顺序一一对应，
    观众看到的是一条一条接上去，而不是一开始就是一张成图。
    """
    nodes = list(shot.nodes)
    if not nodes:
        return ""
    top = _content_top(shot, box)
    area_h = box["bottom"] - top - (60 if shot.bullets else 10)
    columns = 1 if box["w"] < 560 else 2
    rows = max(1, (len(nodes) + columns - 1) // columns)
    gap = 18.0
    cell_w = (box["right"] - box["left"] - gap * (columns - 1)) / columns
    # 卡片高度按行数摊满可用区域（封顶 240：再高就成一块大白板了）
    cell_h = min(240.0, (area_h - gap * (rows - 1)) / rows)

    positions: dict[str, tuple[float, float, float, float]] = {}
    out: list[str] = []
    for index, node in enumerate(nodes):
        row, column = divmod(index, columns)
        x = box["left"] + column * (cell_w + gap)
        y = top + row * (cell_h + gap)
        positions[node.id] = (x, y, cell_w, cell_h)
        if index >= visible:
            # 还没轮到的节点：**保留占位**（不画）—— 位置固定，后面的节点一动就到位
            continue
        is_current = index == visible - 1
        label_size = _fit_font(node.label, want=30, minimum=20, max_chars=12)
        out.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{cell_w:.1f}" height="{cell_h:.1f}" rx="14" '
            f'fill="{DIAGRAM_CARD_FILL}" stroke="{DIAGRAM_ACCENT if is_current else DIAGRAM_CARD_STROKE}" '
            f'stroke-width="{3 if is_current else 1.5}"/>'
        )
        out.append(
            f'<text x="{x + 20:.1f}" y="{y + label_size + 18:.1f}" font-family="{FONT_STACK}" '
            f'font-size="{label_size:.0f}" font-weight="600" fill="{DIAGRAM_TEXT}">{_escape(node.label)}</text>'
        )
        if node.detail:
            detail_size = _fit_font(node.detail, want=20, minimum=15, max_chars=30)
            out.append(
                f'<text x="{x + 20:.1f}" y="{y + label_size + 18 + detail_size * 1.6:.1f}" '
                f'font-family="{FONT_STACK}" font-size="{detail_size:.0f}" '
                f'fill="{DIAGRAM_MUTED}">{_escape(node.detail)}</text>'
            )
    # 连线：只画前 `links` 条，且两端节点都已出现。
    # ⚠️ 端点要落在**卡片边缘之间**：连中心点的话整条线都躲在卡片底下 ——
    # 实测第一版就是这样，画面上「有连线」但一根也看不见。
    for index, link in enumerate(shot.links[:links]):
        src, dst = positions.get(link.src), positions.get(link.dst)
        if not src or not dst:
            continue
        x1, y1 = _edge_point(src, dst)
        x2, y2 = _edge_point(dst, src)
        out.append(
            f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
            f'stroke="{DIAGRAM_ACCENT}" stroke-width="3" stroke-linecap="round" '
            f'marker-end="url(#diagram-arrow)"/>'
        )
    # 箭头标记：定义一次，全局复用（蓝图式的统一视觉）
    return (
        '<defs><marker id="diagram-arrow" viewBox="0 0 10 10" refX="8" refY="5" '
        f'markerWidth="6" markerHeight="6" orient="auto-start-reverse">'
        f'<path d="M 0 0 L 10 5 L 0 10 z" fill="{DIAGRAM_ACCENT}"/></marker></defs>\n'
        + "\n".join(out)
    )


def _edge_point(
    rect: tuple[float, float, float, float], other: tuple[float, float, float, float]
) -> tuple[float, float]:
    """从 `rect` 的中心朝 `other` 的中心看，落在 `rect` 边上的那个点。"""
    cx, cy = rect[0] + rect[2] / 2, rect[1] + rect[3] / 2
    ox, oy = other[0] + other[2] / 2, other[1] + other[3] / 2
    dx, dy = ox - cx, oy - cy
    half_w, half_h = rect[2] / 2 + 6, rect[3] / 2 + 6
    if abs(dx) < 1e-6 and abs(dy) < 1e-6:
        return cx, cy
    scale = min(
        half_w / abs(dx) if abs(dx) > 1e-6 else float("inf"),
        half_h / abs(dy) if abs(dy) > 1e-6 else float("inf"),
    )
    return cx + dx * scale, cy + dy * scale


def _metrics(shot: DiagramShot, box: dict[str, float], state: int) -> str:
    """指标：数字递增 + 横向条形。**只画核验过的值**（调用方已经筛过）。"""
    metrics = list(shot.metrics[:3])
    if not metrics:
        return ""
    top = _content_top(shot, box)
    row_h = 120.0
    bar_max = box["right"] - box["left"] - 20
    out: list[str] = []
    for index, metric in enumerate(metrics):
        y = top + index * row_h
        shown = _metric_shown(metric, state)
        # 条形按**这个指标自己的目标值**算比例：跨指标比长度没有意义
        # （「5 倍」和「28 MB」谁长谁短说明不了任何事），观众读到的是「填满」和那个数字
        ratio = max(0.0, min(1.0, shown / (metric.value or 1.0)))
        label_size = _fit_font(metric.label, want=26, minimum=19, max_chars=18)
        value_size = 56.0
        text = _format_value(shown, metric)
        out.append(
            f'<text x="{box["left"]:.1f}" y="{y + label_size:.1f}" font-family="{FONT_STACK}" '
            f'font-size="{label_size:.0f}" fill="{DIAGRAM_MUTED}">{_escape(metric.label)}</text>'
        )
        out.append(
            f'<text x="{box["left"]:.1f}" y="{y + label_size + value_size:.1f}" '
            f'font-family="{FONT_STACK}" font-size="{value_size:.0f}" font-weight="700" '
            f'fill="{DIAGRAM_ACCENT}">{_escape(text)}</text>'
        )
        bar_y = y + label_size + value_size + 16
        out.append(
            f'<rect x="{box["left"]:.1f}" y="{bar_y:.1f}" width="{bar_max:.1f}" height="16" rx="8" '
            f'fill="{DIAGRAM_GRID}"/>'
        )
        if ratio > 0:
            out.append(
                f'<rect x="{box["left"]:.1f}" y="{bar_y:.1f}" width="{bar_max * ratio:.1f}" '
                f'height="16" rx="8" fill="{DIAGRAM_ACCENT}"/>'
            )
    return "\n".join(out)


def _format_value(value: float, metric: DiagramMetric) -> str:
    if metric.unit == "%":
        return f"{value:.1f}%"
    if abs(value - round(value)) < 0.05:
        return f"{round(value)}{metric.unit}"
    return f"{value:.1f}{metric.unit}"


def _escape(text: str) -> str:
    import html as html_lib

    return html_lib.escape(text or "")


# --------------------------------------------------------------------------
# 落地：两张渲染路径
# --------------------------------------------------------------------------


def render_diagram_state(
    shot: DiagramShot,
    output_path: Path,
    *,
    layout: Layout,
    state: int = 0,
) -> Path:
    """SVG 降级路径：直接把状态图光栅化成 PNG（在图片卡那块地方）。

    **Chrome 不可用时走这条** —— 与其它图层一样复用 `_rasterize_custom`，
    尺寸显式传图片卡的宽高（resvg 是按宽度缩放的，不传高会按比例推错）。
    """
    from .video_render import _rasterize_custom  # 同层内复用光栅化实现

    output_path.parent.mkdir(parents=True, exist_ok=True)
    _rasterize_custom(
        diagram_svg(shot, layout, state=state),
        output_path,
        width=layout.image_box_w,
        height=layout.image_box_h,
    )
    return output_path


def render_diagram_state_html(
    shot: DiagramShot,
    output_path: Path,
    *,
    layout: Layout,
    state: int = 0,
    session: Any | None = None,
) -> Path:
    """HTML 路径：把同一份 SVG 内联进一个自包含页面，交给 Chrome 截一张。

    两条路径**画的是同一份 SVG**（`diagram_svg`），所以不会出现
    「有 Chrome 和没 Chrome 出的图不一样」这种只有换机器才看得出来的问题。
    没有会话时退回 SVG 路径（调用方不用自己判断）。
    """
    if session is None:
        return render_diagram_state(shot, output_path, layout=layout, state=state)
    from .htmlframe import Page

    width, height = int(layout.image_box_w), int(layout.image_box_h)
    page = Page(
        html=(
            '<!doctype html><html lang="zh-Hans"><head><meta charset="utf-8">'
            f'<style>html,body{{margin:0;padding:0;background:#fff;width:{width}px;height:{height}px;overflow:hidden}}</style>'
            f"</head><body>{diagram_svg(shot, layout, state=state)}</body></html>"
        ),
        width=width,
        height=height,
    )
    session.render(page, output_path, work_dir=output_path.parent)
    return output_path
