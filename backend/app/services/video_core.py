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

    @property
    def duration(self) -> float:
        return max(self.end - self.start, 0.05)


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
