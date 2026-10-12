"""视频层的**类型与错误类型** —— 分层里的最底一层（叶子）。

`Scene` / `CaptionBand` / `Transition` / `ImageAsset` / `VideoResult` 是「数据长什么样」，
`VideoError` 是本层对外唯一约定好的失败类型，`_SENTENCE_END` 是分句用的解析规则。
这一层只依赖 `design`（版式令牌）与标准库，**不 import 本包里的任何其他视频模块** ——
所以 `video_render` / `video_encode` / `video` 都能放心从这一层取类型，方向永远是单向的。

分层：`design → video_core → video_render → video_encode → video`（本层是第 2 层）。
想改画面怎么画（坐标 / SVG / 字幕排版）→ `video_render.py`；想改滤镜链 / 帧数 / ffmpeg
→ `video_encode.py`；想改场景编排 / 转场 / 素材池 → `video.py`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 版式与配色令牌的老家在 `design.py`（`Layout` / `PORTRAIT` 这些）。
# 这里照着 `video.py` 原来的写法 import *：本层的 dataclass 与它们的默认值共用同一套令牌，
# 两边各自开一份就是两个口径。
from .design import *  # noqa: F401,F403

__all__ = [
    "CaptionBand",
    "ImageAsset",
    "Scene",
    "Transition",
    "VideoError",
    "VideoResult",
    "_SENTENCE_END",
]


# 句子结束符（分句用）。留在这一层而不是 design.py：它是**解析规则**，不是版式令牌。
_SENTENCE_END = "。！？!?；;…"


class VideoError(Exception):
    """视频合成失败。属于可降级错误——没有视频也要能听播客。"""


@dataclass
class Scene:
    """一个画面片段：在 [start, end) 期间显示 image，并叠加字幕。"""

    start: float
    end: float
    image: Path
    kind: str          # "cover" | "figure" | "illustration"
    speaker: str = ""
    text: str = ""
    caption: str = ""  # 图片说明（图注），显示在图片下方
    # 这一段的**逐句**画面安排（每句一张图 / 一个聚焦块）。空列表 = 整段一张图（老行为）。
    # 字幕的分句本身是编码时按真实停顿算的，这里只负责「哪一句配什么画面」——
    # 两边都用 `split_caption_beats` 的同一套切句规则，所以**句数与顺序天然对齐**。
    beats: list[Beat] = field(default_factory=list)
    # 「本段要点」：一句不超过 14 个字的大字强调（模型写，兜底从原文抠数字）
    point: str = ""
    # 「这一段为什么配这张图」（模型在逐段选图的**同一次调用**里写的一句话）。
    # 同一张图被反复用于多段时，「是不是真的在讲同一件事」以前只能靠人重读全稿复查；
    # 存下理由之后，只看这一列就能判断图文相符 —— 它不参与渲染，是**可复查性**的数据。
    why: str = ""
    # 图内聚光灯：这一段在讲图里的哪一块（相对 0~1 比例 + 短标签）。None = 不框
    focus: dict[str, Any] | None = None
    # 封面上的大字标题（**只有片头那一帧用**）。正文页顶部不再放论文标题 ——
    # 一行小字挂在每帧顶上既不抓人、又占掉画面，标题的活儿交给封面。
    headline: str = ""
    # "outro" 表示这是片尾品牌段 —— 视频层会把它渲染成品牌卡片而不是普通配图页
    brand: str = ""
    # **动态图解镜头**（见 `services/diagram.py`）：非空时这一段的「图片卡」
    # 画的是论文信息组成的图解，而不是论文原图/示意图。空 = 老行为。
    # 每个状态各渲染一张卡，按 `beats` 的时间窗依次叠上去 —— 编码侧一行都不用改。
    diagram: "DiagramShot | None" = None
    language: str = "zh"

    @property
    def duration(self) -> float:
        return max(self.end - self.start, 0.05)


# 小句的分界符。**只在这几个停顿处切**：逗号/顿号/分号/冒号 ——
# 句号不切（那是另一句字幕的事了）。
#
# 为什么放在这一层：`htmlpage`（页面）和 `video_render`（SVG 降级路径）都要用它，
# 而这两边谁都不能 import 对方（`htmlpage` 惰性依赖 `video`，反向就成环了）。
CLAUSE_BREAKS = "，,、；;：:"


def clause_spans(text: str) -> list[tuple[int, int]]:
    """把一句话切成**小句**（含标点），返回每个小句在原文里的区间。

    用途是「字幕按小句依次出现」：一句 30 字的话念 10 秒，整句在第 0.35 秒就全出现了，
    中间 10 秒画面上什么都不变 —— 实测全片有 52.5%~67.7% 的时长落在这种
    「≥4 秒没有任何可见变化」里（见 docs/VIDEO_QA.md）。按小句依次出现，
    观众读到哪里、画面长到哪里，也顺带把那些空档填上。

    **判据必须确定**：同一句话在任何机器上切出同样的区间（不看字体、不看宽度），
    否则渲染侧和编码侧算出来的时间点会对不上。
    只有一个小句（整句没有标点）时返回空列表 —— 那说明没得「依次出现」。
    """
    spans: list[tuple[int, int]] = []
    start = 0
    for index, char in enumerate(text):
        if char in CLAUSE_BREAKS:
            spans.append((start, index + 1))
            start = index + 1
    if start < len(text):
        spans.append((start, len(text)))
    return spans if len(spans) > 1 else []


def plan_reveals(
    text: str,
    duration: float,
    *,
    min_seconds: float | None = None,
    min_gap: float | None = None,
    tail_hold: float | None = None,
    max_reveals: int | None = None,
) -> list[tuple[int, float]]:
    """把「这一句的小句依次出现」排成时间表：`[(藏起从第几个小句起, 相对本句起点的秒数)]`。

    每个状态都是一张整条带子（同一套排版，只把还没轮到的小句变成透明），
    所以「显露」这件事在编码侧就是按时序叠一张同样的带子。

    规则（全是时间，与字体、宽度无关 —— 渲染侧和编码侧不会各算一个样）：

    - 只有一个小句、或这一句**太短**（`min_seconds`）→ 不做（一闪而过比不动更难看）；
    - 第 k 个小句出现的时刻 = 按**小句字数**占整句的比例 × 时长。我们没有逐字时间戳，
      同一段里语速基本恒定，按字数分是能达到的最好近似（和 `beat_windows` 一个道理）；
    - 两次显露至少隔 `min_gap`，挨太近的直接跳过（并把那部分小句并进下一次）；
    - **最后一个状态一定是「全都显示」**，而且至少留 `tail_hold` 才结束 ——
      否则最后那个小句永远不会出现（这是最容易写错的地方：只按「边界时刻」排，
      靠近句尾的那一刀会被 `tail_hold` 挡掉，那一小句就永远藏着）；
    - 一次最多分 `max_reveals` 次（每多一次就多渲染一张整条带子）。
    """
    from .design import (
        CLAUSE_REVEAL_MAX,
        CLAUSE_REVEAL_MIN_GAP,
        CLAUSE_REVEAL_MIN_SEC,
        CLAUSE_REVEAL_TAIL_HOLD,
    )

    min_seconds = CLAUSE_REVEAL_MIN_SEC if min_seconds is None else min_seconds
    min_gap = CLAUSE_REVEAL_MIN_GAP if min_gap is None else min_gap
    tail_hold = CLAUSE_REVEAL_TAIL_HOLD if tail_hold is None else tail_hold
    max_reveals = CLAUSE_REVEAL_MAX if max_reveals is None else max_reveals

    spans = clause_spans(text)
    if not spans or duration < min_seconds:
        return []
    lengths = [max(1, end - begin) for begin, end in spans]
    total = sum(lengths)
    latest = duration - tail_hold
    if latest <= 0.4:
        return []

    # ① 候选：每个小句边界的时刻（按字数占比）。最后一个状态必须是「全都显示」，
    #    所以边界只能落在 `latest` 之前，否则那条边界的意义就没了。
    candidates: list[tuple[int, float]] = []
    for order in range(1, len(spans)):
        moment = sum(lengths[:order]) / total * duration
        if moment > latest:
            break
        candidates.append((order + 1, moment))
    if not candidates:
        return []

    # ② 间隔：挨得太近的边界并进下一次（一次显露要看得见，连着闪不如不闪）
    kept: list[tuple[int, float]] = []
    for entry in candidates:
        if kept and entry[1] - kept[-1][1] < min_gap:
            continue
        kept.append(entry)

    # ③ 数量：中间状态最多 `max_reveals - 1` 个（剩一个位置留给「全都显示」）。
    #    **均匀取，不是取前几个**：贪心取前面几个会让「全都显示」落在句子一半处，
    #    后半句又变回一动不动（实测 4 小句 12 秒的句子：1.8 / 4.7 / 5.8 之后全静止）。
    if len(kept) > max_reveals - 1:
        step = (len(kept) - 1) / (max_reveals - 1)
        picked: list[tuple[int, float]] = []
        for index in range(max_reveals - 1):
            entry = kept[min(len(kept) - 1, int(round(index * step)))]
            if not picked or entry[0] != picked[-1][0]:
                picked.append(entry)
        kept = picked

    # ④ 收尾：一个「全都显示」的状态，紧跟在最后一个中间状态之后（能早就早，
    #    但不能早过 `min_gap`，也不能晚过 `latest`）。
    if kept and kept[-1][0] >= len(spans):
        return kept
    # 收尾时刻**尽量落在最后一个小句边界**上（那是「全都显示」最自然的时刻）：
    # 上面第 ③ 步可能把最后那条边界砍掉（它排在中间状态的位置上），
    # 但「全都显示」这一张正好可以顶上那个时刻 —— 不然最后一个小句会提前两三秒出现，
    # 后半句又变回一动不动。至少要晚过上一个状态 `min_gap`，也不能晚过 `latest`。
    previous = kept[-1][1] if kept else 0.0
    final_moment = min(latest, max(candidates[-1][1], previous + min_gap))
    kept.append((len(spans), final_moment))
    # 两个状态挨太近就并掉前一个（前一个本来只多显示一个小句）
    while len(kept) > 1 and kept[-1][1] - kept[-2][1] < min_gap * 0.5:
        kept.pop(-2)
    return kept




# --------------------------------------------------------------------------
# 动态图解镜头：**用论文里已有的信息画一张会长的图**
# --------------------------------------------------------------------------
#
# 为什么数据结构在「最底层」这一层：它是「画面里有什么」的描述，
# 与渲染方式无关（SVG 光栅化、Chrome 截图两条路都用同一份数据）。
# 每一条内容都带 `source`：**查不到依据的条目会被 `diagram.verify_shot` 丢掉** ——
# 这是「不许编造技术图表」的落点，屏幕上一个查不到来源的数字比少一个图严重得多。


@dataclass(frozen=True)
class DiagramItem:
    """图解里的一条内容 + 它的**出处**（论文解读的字段名、或脚本原句、或图注）。"""

    text: str
    source: str = ""


@dataclass(frozen=True)
class DiagramNode:
    """流程/对比里的一个节点：一个名字 + 一句说明（都带出处）。"""

    id: str
    label: str
    detail: str = ""
    source: str = ""


@dataclass(frozen=True)
class DiagramLink:
    """节点之间的一条连线：状态 k 只画前 k 条 —— 观众看到的是「一条一条接上去」。"""

    src: str
    dst: str
    label: str = ""


@dataclass(frozen=True)
class DiagramMetric:
    """一个指标：论文里明确给出的数字 + 单位 + 出处。**没有出处的数字不许进这里**。"""

    label: str
    value: float
    unit: str = ""
    source: str = ""


@dataclass(frozen=True)
class DiagramShot:
    """一个图解镜头。`kind` 决定它长什么样（同一套视觉语言，只换排布）：

    - `hook`：开场钩子，大字 + 关键词强调；
    - `flow`：方法流程，节点依次出现、连线按顺序画；
    - `compare`：卡片 + 箭头，讲「谁处理什么」；
    - `metric`：实验数字，条形 + 数字递增；
    - `summary`：结论收束（通常再切回论文原图，见 `fallback_image`）。
    """

    kind: str
    title: str
    title_source: str = ""
    subtitle: str = ""
    subtitle_source: str = ""
    nodes: tuple[DiagramNode, ...] = ()
    links: tuple[DiagramLink, ...] = ()
    metrics: tuple[DiagramMetric, ...] = ()
    bullets: tuple[str, ...] = ()
    bullet_sources: tuple[str, ...] = ()
    # 这一条镜头讲不完整时**退回哪张图**（论文原图 id / 封面 / 生成图）——
    # 「找不到依据的内容不渲染，退回现有画面流程」就是靠它。
    fallback_image: str = ""

    def items(self) -> list[DiagramItem]:
        """这个镜头里所有「需要在证据里查得到」的内容（供核验遍历）。"""
        out = [DiagramItem(self.title, self.title_source)]
        if self.subtitle:
            out.append(DiagramItem(self.subtitle, self.subtitle_source))
        out.extend(DiagramItem(node.label, node.source) for node in self.nodes)
        out.extend(
            DiagramItem(node.detail, node.source) for node in self.nodes if node.detail
        )
        out.extend(
            DiagramItem(f"{metric.value}{metric.unit}", metric.source)
            for metric in self.metrics
        )
        out.extend(
            DiagramItem(text, source)
            for text, source in zip(self.bullets, self.bullet_sources or ("",) * len(self.bullets))
        )
        return out


@dataclass
class CaptionBand:
    """一句字幕：文字（用来按字数分配时长）+ 渲染好的底部字幕带图片。

    字幕带是**独立图层**：底图不画字幕，字幕按句做成带子，用 overlay 按时序叠上去。
    这样同一张静止画面在段中间有一次内容变化 —— 这是「字幕逐句出现」的全部目的。
    """

    text: str
    image: Path
    # **这一句里被强调的那个数字/关键词单独一层**（透明底，只有那个胶囊）。
    # 为什么单独分一层：字幕带本身是静止的一层，想让它「跟着句子长出来」就只能
    # 把要动的那一小块抠出来、在编码时按这句的起点淡入 —— 整条带子重新淡入是另一回事。
    # 句子里没有数字/关键词时为 None（那就不动）。
    chip: Path | None = None
    # **这一句按小句依次出现的后续状态**：`(这一状态的整条带子, 相对本句起点的秒数)`。
    # 每个状态都是「前 N 个小句可见、其余透明」的同一张带子（排版完全一致，只改颜色），
    # 编码时按秒数依次叠上去 —— 观众看到的是「读到哪里，字长到哪里」。
    # 时长由 `plan_reveals` 按小句字数分配，渲染侧算完存进来，编码侧直接用（不各算一遍）。
    reveals: tuple[tuple[Path, float], ...] = ()
    # 强调层相对本句起点**什么时候点亮**。`None` = 用默认的 `CHIP_POP_DELAY`（0.12 秒）。
    # 为什么不能固定：整句按小句出现时，被强调的数字可能在第 3 个小句里 ——
    # 它还没出现的时候弹一颗空胶囊，观众看到的是「一个框里没有字」。
    # 所以渲染侧按小句时间表把它推到「那个小句出现之后」。
    chip_delay: float | None = None


@dataclass
class Beat:
    """视频里的**一句**：字幕按句拆，每句可以有自己的一张图和一个聚焦块。

    为什么要这一层：画面原来是按「段」换的（一段 2~4 句、10~20 秒），
    于是一句话讲完了画面还停在那儿 —— 用户的要求是「为每句话都生成相应的视频动画」。
    现在每一句都能带自己的图（句子换了图就换，0.25 秒溶解）和自己的聚光灯框。

    **空值一律表示「沿用本段」**，所以老数据（没有 beats）天然等于「一句话都不切画面」，
    不需要任何迁移。
    """

    text: str
    # 这一句用哪张图（asset id）。空 = 沿用本段/上一句的图。
    image: str = ""
    # 这一句讲图里的哪一块（0~1 比例 + 标签）。None = 沿用本段的 focus。
    focus: dict[str, Any] | None = None


@dataclass
class Transition:
    """一段画面开头的转场。

    `kind`：
    - `none`：直接切（图片没变、或片段太短）
    - `fade_in`：整页从白底淡入（只在第一段）
    - `dissolve`：上一张图淡出、这张淡入（默认）
    - `push`：上一张往左推出去、这张从右推入
    - `to_card`：上一整页淡出、露出底下的片尾品牌卡
    """

    kind: str = "none"
    seconds: float = 0.0
    # dissolve/push 需要上一段的**图片卡**；to_card 需要上一段的**整页**
    previous: Path | None = None


@dataclass
class ImageAsset:
    """一张可用作画面的图。"""

    id: str
    path: Path
    kind: str      # "cover" | "figure" | "table" | "illustration"
    caption: str = ""

    @property
    def exists(self) -> bool:
        return self.path.exists()


@dataclass
class VideoResult:
    video_path: Path
    duration_sec: float
    scene_count: int
    bytes_written: int
    assignment: str  # "model" | "heuristic"
    # 每一段用的是哪张图（图片 id，如 f1 / cover / topic2）。
    # 存下来是为了「重新合成视频」时**复用**，而不是再问一次模型 ——
    # 再问一次结果会变、还会把主题图重新生成一遍（4 次调用 + 几十秒）。
    scenes: list[dict[str, Any]] = field(default_factory=list)
    # 用到的素材 id -> 文件路径，重建时按它找图
    assets: dict[str, str] = field(default_factory=dict)
    # 合成时各素材的版本号（mtime-size），用来判断视频是否已经过时：
    # 用户手动旋转了配图、或重新提取过，视频里还是旧画面
    asset_versions: dict[str, str] = field(default_factory=dict)
    # 封面上的大字标题（爆款标题）。和 scenes 里的 point 一样属于
    # 「模型写一次、以后复用」的数据 —— 重新合成时不该换一句。
    hook: str = ""
    # **视频第一帧的静帧**（PNG）。给前端当 `<video poster>`：不填的话，
    # 页面上的视频位只能拿论文首页当封面 —— 那是**没烘进标题的原始 PDF 页**，
    # 观众（和用户）就会以为「封面上没有标题」。（实测踩到：用户打开首页说「没有看到」。）
    poster_path: str = ""
    # 封面标题改过、但还没重新合成时为 true（由 PATCH /cover 打上）。
    # 这里恒为 false：**刚合成出来的视频，画面和标题必然是一致的** ——
    # `to_dict()` 整份覆盖库里的 video，所以重新合成本身就是把标记清掉。
    title_pending: bool = False
    # 这一份画面是**哪条渲染路径**画出来的：`html`（Chrome 无头）或 `svg`（resvg）。
    # 存下来是为了「同一篇论文，两台机器出的画面不一样」这类问题有一句话可查 ——
    # 两条路径都在，出片时用了哪条光看 MP4 是看不出来的。
    renderer: str = "svg"
    # **成片自查**的结果（`video_qa.qa_video`）：最长静止多久、响度多少、音画差多少。
    # 这三件事只有成片里才看得出来（分层测试能证明「会动」，证明不了「有 10 秒没动」），
    # 所以合成完顺手量一次存下来 —— 光打日志的话，回头看某集「当时是什么水平」就查不到了。
    qa: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "duration_sec": round(self.duration_sec, 2),
            "scene_count": self.scene_count,
            "bytes": self.bytes_written,
            "assignment": self.assignment,
            "scenes": self.scenes,
            "assets": self.assets,
            "asset_versions": self.asset_versions,
            "hook": self.hook,
            "poster": self.poster_path,
            "renderer": self.renderer,
            "title_pending": self.title_pending,
            "qa": self.qa,
        }
