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
    wrap_units,
    DIAGRAM_ACCENT,
    DIAGRAM_CARD_FILL,
    DIAGRAM_CARD_STROKE,
    DIAGRAM_GRID,
    DIAGRAM_MUTED,
    DIAGRAM_TEXT,
    FONT_STACK,
    Layout,
)
from .video_core import (
    DiagramItem,
    DiagramMetric,
    DiagramNode,
    DiagramShot,
    diagram_shot_is_drawable,
)

__all__ = [
    "DiagramError",
    "shot_state_count",
    "state_moments",
    "diagram_svg",
    "render_diagram_state",
    "render_diagram_state_html",
    "verify_shot",
    "shot_is_drawable",
    "VerificationResult",
]


class DiagramError(Exception):
    """图解镜头渲染失败（调用方据此退回原来的画面流程）。"""


# 节点卡片的高度规则：卡长到内容需要的高度，夹在最小/最大之间。
# 最小 132 是「两行字 + 上下留白」看上去还像一张卡的下限；最大 360 是因为再高
# 就成一块大白板了（早先固定摊满 + 封顶 240，两头都不对）。
NODE_MIN_H = 132.0
NODE_MAX_H = 360.0
NODE_PAD_Y = 18.0

# 大字卡（hook / summary）那块至少占卡片高度的这个比例：一行标题 + 一行要点本身
# 撑不满一张卡，靠把富余空间摊进间距来做到「占住中间一块」。
HERO_MIN_SPAN = 0.5


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

    # 「要有节点」这条只对流程/对比镜头成立：hook 与 summary 只保证「讲得清」，
    # 节点数交给模型自己定（实测第一版把它们也要求 2 个节点，结果开场钩子整条被丢掉）。
    if shot.kind in ("flow", "compare") and len(nodes) < min_nodes and not metrics:
        dropped.append(f"有效节点不足 {min_nodes} 个且没有可用指标 → 整条镜头放弃")
        return VerificationResult(None, dropped)
    # 指标镜头：数字被核验掉之后就什么也没剩了（它本来就是为了那个数字），
    # 不能靠标题凑数 —— 那等于把编造的数字换成一句空话，还占掉一个画面。
    if shot.kind == "metric" and not metrics:
        dropped.append("指标全部没通过核验 → 整条镜头放弃")
        return VerificationResult(None, dropped)
    # hook / summary 允许只有一句话 —— 它们走 `_hero_title` 的大字版式（整幅居中），
    # 不是「只有标题的空画面」。（这条曾经是「必须有点节点或指标」，因为那时它们
    # 确实只画顶部一行标题、八成是白的；改了版式之后就没有这个坑了。）

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


# 判据只有一份（数据层 `video_core.diagram_shot_is_drawable`），这里再导出一次方便同层调用。
shot_is_drawable = diagram_shot_is_drawable


def shot_state_count(shot: DiagramShot) -> int:
    """这个镜头一共几个状态（= 需要渲染几张卡）。

    状态之间**只多一件事**：多一个节点、多画一段连线、或数字滚到下一个值。
    这就是「节点依次出现、连线按顺序绘制、数字递增」的落法 —— 一次只前进一步，
    观众的眼睛才跟得上。
    """
    if shot.kind == "metric":
        # **数字递增必须给满三个状态**（0 → 一半 → 目标值）。原来算的是「指标数 + 1」，
        # 只有一个指标时只有 2 个状态 —— 末状态停在「一半」：66% 的指标画面上永远是 33%，
        # 目标值一次都没出现过（实测 `最高5.3倍，吞吐高66%` 那个镜头就是这样）。
        return 3 if shot.metrics else 1
    if shot.kind == "flow":
        return max(1, min(len(shot.nodes), 4))
    if shot.kind == "compare":
        return max(1, min(len(shot.nodes), 3))
    return 1


def state_moments(
    text: str,
    duration: float,
    state_count: int,
    *,
    pauses: Sequence[Any] | None = None,
    start: float = 0.0,
    tail_hold: float = 1.2,
) -> list[float]:
    """状态出现的时刻（相对本段起点，秒）：**跟着讲稿的句子走**，不是均匀铺。

    为什么不用均匀铺：状态是「这句话讲到了这里」。均匀铺会出现「人还在讲第一个节点，
    图上已经长到第三个」—— 参考片里最要紧的节奏感恰恰是这句话与画面的对应。
    句子边界用 `beat_windows_aligned`（与字幕带、逐句卡同一组窗口，含真实停顿），
    所以状态和字幕天然同步。

    两条兜底：
    - 句子数不够分（比如一句话要长出 3 个节点）→ 退回均匀铺（总比全挤在开头好）；
    - 最后一个状态至少留 `tail_hold` 秒再结束（刚长出来镜头就切走等于白画）。
    """
    count = max(1, int(state_count))
    moments: list[float] = []
    if count > 1:
        from .video_render import beat_windows_aligned  # 同层循环：函数内导入

        sentences = _sentences(text) or [text or ""]
        windows = beat_windows_aligned(sentences, duration, start=start, pauses=pauses)
        starts = [max(0.0, win[0] - start) for win in windows]
        if len(starts) >= count:
            moments = starts[:count]
            # 末状态留在最后一句的起点，但不能贴到片尾：至少留 `tail_hold`
            moments[-1] = min(moments[-1], max(0.0, duration - tail_hold))
            # 时间必须递增（末状态被 clamp 到前面时，就退回均匀铺）
            if any(b <= a for a, b in zip(moments, moments[1:])):
                moments = []
        if not moments:
            moments = [duration * index / count for index in range(count)]
    if not moments:
        moments = [0.0]
    return [round(moment, 2) for moment in moments]


def _sentences(text: str) -> list[str]:
    """按句号切（与字幕分句同一套规则：`split_caption_beats` 也只看句子与逗号）。"""
    parts: list[str] = []
    current = ""
    for char in text or "":
        current += char
        if char in "。！？!?":
            if current.strip():
                parts.append(current.strip())
            current = ""
    if current.strip():
        parts.append(current.strip())
    return parts


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
        # 没有节点也没有指标（hook / summary 常见）→ 整幅当大字卡排，
        # 否则照旧在顶部放小标题（节点/指标自己有版式，不需要它抢地方）
        _hero_block(shot, box) if _is_hero(shot) else _header(shot, box),
    ]
    if shot.kind == "metric" or shot.metrics:
        parts.append(_metrics(shot, box, metric_state))
    if shot.nodes:
        # **别按 kind 挑着画**：模型给的 kind 是 `flow/compare/metric/hook/summary` 里挑一个，
        # 但 `hook`/`summary` 也常带节点 —— 早先这里只画 `flow`/`compare` 的节点，
        # 于是那两个镜头**只剩一个标题**，画面九成是白的（实测 93% 空白行）。
        # 有节点就画，kind 只决定它想强调什么。
        parts.append(_nodes(shot, box, nodes, links))
    if shot.bullets and not _is_hero(shot):
        # hero 版式把要点并进居中那块了，这里不再钉到画面底部
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


def _is_hero(shot: DiagramShot) -> bool:
    """这个镜头走「大字卡」版式吗：既没有节点也没有指标（hook / summary 常见）。"""
    return not (shot.nodes or shot.metrics)


def _hero_block(shot: DiagramShot, box: dict[str, float]) -> str:
    """没有节点也没有指标时的版式：**整块居中的大字卡**（标题 + 副标题 + 要点）。

    hook / summary 这两种镜头本来就只是一句钩子或一句结论。早先它们照 `_header` 只画
    顶部一行 52px 标题，要点再钉在画面最底部 —— 中间一大片白的（实测空白行 87%），
    用户截图里那张「只有一个标题 + 一整个白画面」就是这个。现在这三样合成**一块**
    居中排：标题按可用宽度折行、字号按字数定档，副标题与要点跟着走，不再各贴一头。
    """
    title = (shot.title or "").strip()
    if not title:
        return ""
    accent_w = 8.0
    text_left = box["left"] + accent_w + 16
    max_w = box["right"] - text_left
    # 字号按字数定档，再看折出来的行数够不够「一块」；行数超了就降一档重排
    size = 92.0 if len(title) <= 10 else 76.0 if len(title) <= 14 else 62.0 if len(title) <= 22 else 50.0
    lines = wrap_units(title, max_w / size)
    while len(lines) > 3 and size > 34:
        size -= 6.0
        lines = wrap_units(title, max_w / size)
    line_h = size * 1.34
    subtitle = (shot.subtitle or "").strip()
    sub_size = 30.0 if subtitle else 0.0

    bullet_size = 26.0
    bullet_lines: list[str] = []
    for text in shot.bullets:
        bullet_lines.extend(wrap_units(text, (max_w - 24) / bullet_size) or [text])

    # 一行标题 + 一行要点，撑起来的其实只有卡片的一小条 —— 字号再大也一样。
    # 所以把富余的纵向空间**摊进这一块的间距**里（标题与要点之间多留一些、
    # 要点之间也多留一些），让它占住卡片中间一半：既是一整块，又不贴边。
    #
    # ⚠️ `span_h` 必须和下面真画出来的位置**同一套算法**：第一版用「行数 × 行距」
    # 算跨度（多算了一道行距），结果整块偏上、下面多出一截（实测上 22% / 下 38%）。
    area_h = box["bottom"] - box["top"]
    base_gap = 30.0
    base_lead = bullet_size * 1.6

    def span(gap: float, lead: float) -> float:
        height = len(lines) * line_h
        if subtitle:
            height += 20 + sub_size * 1.3
        if bullet_lines:
            height += gap + bullet_size + max(0, len(bullet_lines) - 1) * lead
        return height

    slack = max(0.0, HERO_MIN_SPAN * area_h - span(base_gap, base_lead))
    # slack 摊在「标题与要点之间」和「要点之间」这几道缝上（只有一行要点时全给第一道）
    gaps = (1 + max(0, len(bullet_lines) - 1)) if bullet_lines else 0
    per_gap = (slack / gaps) if gaps else 0.0
    gap_to_bullets = base_gap + per_gap
    bullet_lead = base_lead + per_gap
    span_h = span(gap_to_bullets, bullet_lead)

    top = box["top"] + max(0.0, (area_h - span_h) / 2)
    out = [
        f'<rect x="{box["left"]:.1f}" y="{top + size * 0.08:.1f}" width="{accent_w:.1f}" '
        f'height="{len(lines) * line_h:.1f}" rx="4" fill="{DIAGRAM_ACCENT}"/>'
    ]
    for index, line in enumerate(lines):
        out.append(
            f'<text x="{text_left:.1f}" y="{top + size * 0.94 + index * line_h:.1f}" '
            f'font-family="{FONT_STACK}" font-size="{size:.0f}" font-weight="600" '
            f'fill="{DIAGRAM_TEXT}">{_escape(line)}</text>'
        )
    cursor = top + len(lines) * line_h
    if subtitle:
        cursor += 20
        out.append(
            f'<text x="{text_left:.1f}" y="{cursor + sub_size:.1f}" '
            f'font-family="{FONT_STACK}" font-size="{sub_size:.0f}" '
            f'fill="{DIAGRAM_MUTED}">{_escape(subtitle)}</text>'
        )
        cursor += sub_size * 1.3
    if bullet_lines:
        cursor += gap_to_bullets
        for index, line in enumerate(bullet_lines):
            baseline = cursor + bullet_size + index * bullet_lead
            out.append(
                f'<text x="{text_left:.1f}" y="{baseline:.1f}" font-family="{FONT_STACK}" '
                f'font-size="{bullet_size:.0f}" fill="{DIAGRAM_MUTED}">· {_escape(line)}</text>'
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
    # 卡高先按**内容量**算，再夹在 [NODE_MIN_H, NODE_MAX_H] 之间，且不超过摊满所需。
    #
    # 原来是「按行数摊满可用区域、封顶 240」，两行长字的卡片被拉到顶格高，
    # 字全贴着卡上沿、下半截是空的 —— 用户截图里那张「一个框 + 一整片白」就是这么来的。
    # 现在卡只长到内容需要的高度（同网格取最大值，保证各卡一致），
    # 剩下空间交给整块竖向居中。
    label_sizes: list[float] = []
    detail_sizes: list[float] = []
    needed: list[float] = []
    for node in nodes:
        label_size = _fit_font(node.label, want=30, minimum=20, max_chars=12)
        detail_size = _fit_font(node.detail, want=20, minimum=15, max_chars=30) if node.detail else 0.0
        label_sizes.append(label_size)
        detail_sizes.append(detail_size)
        needed.append(2 * NODE_PAD_Y + label_size + (detail_size * 1.6 + 8 if detail_size else 0.0))
    # 卡高 = max(内容需要的高度, 摊满可用区域的高度)，封顶 NODE_MAX_H。
    #
    # 两头都试过了：**固定摊满 + 封顶 240** 会把两行长字的卡拉成一块高白板（用户截图里那张
    # 「一个框 + 一整片白」）；**只按内容长** 又变成画面中间一条窄带（空白行 74%）。
    # 所以：内容多就按内容长，内容少就长到摊满为止 —— 但字在卡内**竖向居中**，
    # 卡片高一点也不会显得空。
    cell_h = min(
        max(NODE_MAX_H, max(needed)),
        max(NODE_MIN_H, max(needed), (area_h - gap * (rows - 1)) / rows),
    )
    # **竖向居中**：整块节点区落在可用区域中间，上下留白对称。
    block_h = rows * cell_h + gap * (rows - 1)
    top += max(0.0, (area_h - block_h) / 2)

    positions: dict[str, tuple[float, float, float, float]] = {}
    out: list[str] = []
    for index, node in enumerate(nodes):
        row, column = divmod(index, columns)
        x = box["left"] + column * (cell_w + gap)
        y = top + row * (cell_h + gap)
        positions[node.id] = (x, y, cell_w, cell_h)
        if index >= visible:
            # 还没轮到的节点：画一个**占位框**（浅底 + 虚线，不带字）。
            #
            # 原来是「保留位置但不画」—— 于是第一个状态只有一个框，其余三分之二全是白，
            # 用户的反馈是「避免出现这样的画面」。占位框让构图从第 0 帧就是完整的：
            # 观众看到的是「一张正在长出来的图」，而不是「一个孤零零的方框」。
            out.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{cell_w:.1f}" height="{cell_h:.1f}" rx="14" '
                f'fill="{DIAGRAM_CARD_FILL}" fill-opacity="0.45" stroke="{DIAGRAM_CARD_STROKE}" '
                f'stroke-width="1.5" stroke-dasharray="7 7"/>'
            )
            continue
        is_current = index == visible - 1
        label_size = label_sizes[index]
        detail_size = detail_sizes[index]
        # 文字在卡内**竖向居中**（卡比内容高时上下留白对称，不再顶着卡上沿）
        text_h = label_size + (detail_size * 1.6 + 8 if detail_size else 0.0)
        text_top = y + max(NODE_PAD_Y, (cell_h - text_h) / 2)
        out.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{cell_w:.1f}" height="{cell_h:.1f}" rx="14" '
            f'fill="{DIAGRAM_CARD_FILL}" stroke="{DIAGRAM_ACCENT if is_current else DIAGRAM_CARD_STROKE}" '
            f'stroke-width="{3 if is_current else 1.5}"/>'
        )
        out.append(
            f'<text x="{x + 20:.1f}" y="{text_top + label_size:.1f}" font-family="{FONT_STACK}" '
            f'font-size="{label_size:.0f}" font-weight="600" fill="{DIAGRAM_TEXT}">{_escape(node.label)}</text>'
        )
        if node.detail:
            out.append(
                f'<text x="{x + 20:.1f}" y="{text_top + label_size + detail_size * 1.6 + 8:.1f}" '
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
    area_h = box["bottom"] - top - (60 if shot.bullets else 10)
    bar_max = box["right"] - box["left"] - 20
    # 只有一个指标时走「主角数字」版式：标签在上、数字占中部、条形在下，整块竖向居中。
    # 原来一律每行 120 高从顶部往下排 —— 一个数字写完，画面剩下八成全白（实测 81%）。
    if len(metrics) == 1:
        metric = metrics[0]
        shown = _metric_shown(metric, state)
        text = _format_value(shown, metric)
        label_size = 30.0
        value_size = min(132.0, 860.0 / max(3.0, len(text) * 0.62))
        bar_h = 24.0
        block_h = label_size + 12 + value_size + 22 + bar_h
        block_top = top + max(0.0, (area_h - block_h) / 2)
        ratio = max(0.0, min(1.0, shown / (metric.value or 1.0)))
        out = [
            f'<text x="{box["left"]:.1f}" y="{block_top + label_size:.1f}" '
            f'font-family="{FONT_STACK}" font-size="{label_size:.0f}" '
            f'fill="{DIAGRAM_MUTED}">{_escape(metric.label)}</text>',
            f'<text x="{box["left"]:.1f}" y="{block_top + label_size + 12 + value_size:.1f}" '
            f'font-family="{FONT_STACK}" font-size="{value_size:.0f}" font-weight="700" '
            f'fill="{DIAGRAM_ACCENT}">{_escape(text)}</text>',
        ]
        bar_y = block_top + label_size + 12 + value_size + 22
        out.append(
            f'<rect x="{box["left"]:.1f}" y="{bar_y:.1f}" width="{bar_max:.1f}" '
            f'height="{bar_h:.0f}" rx="{bar_h / 2:.0f}" fill="{DIAGRAM_GRID}"/>'
        )
        if ratio > 0:
            out.append(
                f'<rect x="{box["left"]:.1f}" y="{bar_y:.1f}" width="{bar_max * ratio:.1f}" '
                f'height="{bar_h:.0f}" rx="{bar_h / 2:.0f}" fill="{DIAGRAM_ACCENT}"/>'
            )
        return "\n".join(out)

    # 多指标：行高按个数摊开（封顶 220），整块竖向居中 —— 数字跟着行长。
    row_h = min(220.0, max(120.0, area_h / len(metrics)))
    value_size = min(96.0, max(56.0, row_h * 0.42))
    top += max(0.0, (area_h - len(metrics) * row_h) / 2)
    out = []
    for index, metric in enumerate(metrics):
        y = top + index * row_h
        shown = _metric_shown(metric, state)
        # 条形按**这个指标自己的目标值**算比例：跨指标比长度没有意义
        # （「5 倍」和「28 MB」谁长谁短说明不了任何事），观众读到的是「填满」和那个数字
        ratio = max(0.0, min(1.0, shown / (metric.value or 1.0)))
        label_size = _fit_font(metric.label, want=26, minimum=19, max_chars=18)
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
