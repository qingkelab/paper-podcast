"""把配图和播客音频合成「视频解读播客」。

## 同步是怎么做到的

不去猜语速、也不做静音检测。豆包在合成时每个轮次都会返回
`start_time` / `end_time`（见 podcast_tts 的 RoundTiming），
这就是每段脚本在最终音频里的**确切时间区间**。视频的时间轴直接由它驱动，
所以画面切换和字幕出现天然和声音对齐。

## 画面结构

```
[0, 第一段开始)      片头音乐   → 封面（PDF 第一页）
[第 i 段时间区间]     第 i 段脚本 → 该段配图 + 该段字幕
[最后一段结束, 结尾]  片尾音乐   → 信息图 + 结束卡
```

配图分配优先让大模型判断「哪张图适合出现在哪一段」（图的图注 + 脚本内容
都在手里，一次调用就够），失败则退回均匀分布——保证任何情况下都有画面。

## 为什么用 resvg 渲染每一帧而不是 ffmpeg drawtext

字幕是中文，ffmpeg 的 drawtext 需要指定字体文件且转义规则很坑。
用 SVG 渲染（resvg 已验证能正确解析中文字形）排版完全可控，
也复用了信息图那套渲染链路。
"""

from __future__ import annotations

import base64
import html
import re
import logging
import math
import shutil
import os
import subprocess
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .audio_track import Pause, align_boundaries, detect_pauses
from .panels import Panel, detect_panels, panel_count_from_caption, panel_labels

# 版式与配色令牌搬去了 `design.py`（HTML 渲染路径也要用同一套数值）。
# 这里 `import *` 是为了**一个字都不改地**再导出给现有调用方 ——
# tests 和别的模块都写着 `from app.services.video import PORTRAIT` 之类。
from .design import *  # noqa: F401,F403

# --------------------------------------------------------------------------
# 文案（prompt + 模型输出的归一化）搬去 `prompts/` 包了
# --------------------------------------------------------------------------
#
# 为什么搬：这个文件是**渲染**（坐标、滤镜、ffmpeg），文案是另一件事 ——
# 文案天天改（措辞、口吻、字段），渲染一个月动一次。混在一个 3600 行的文件里，
# 每次改一句 prompt 都得先读一遍渲染代码（用户的原话：「不要每次改都要读全部代码」）。
#
# 下面**原样再导出一次**：老调用点（测试里有几十处 `from .video import build_points_messages`）
# 一行都不用改；新代码请直接 `from .prompts.points import ...`。
from .prompts.assign import (  # noqa: F401
    ASSIGN_SYSTEM,
    ASSIGN_WHY_MAX_CHARS,
    _assignment_notes,
    _normalize_per_segment,
    build_assign_messages,
    heuristic_assignment,
    heuristic_per_segment,
)
from .prompts.cover import (  # noqa: F401
    HOOK_BREAK_KEEP,
    HOOK_MAX_CHARS,
    HOOK_MAX_WORDS_EN,
    HOOK_SYSTEM,
    HOOK_SYSTEM_EN,
    HOOK_TAIL_GRACE,
    _HOOK_BREAKS,
    _cover_fallback_headline,
    _trim_hook,
    build_hook_messages,
    normalize_hook,
)
from .prompts.points import (  # noqa: F401
    POINTS_SYSTEM,
    POINTS_SYSTEM_EN,
    _NUMBER_TOKEN,
    _normalize_points,
    build_points_messages,
    local_point,
    normalize_focus,
    normalize_point_items,
    shorten_point,
)


logger = logging.getLogger(__name__)

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
        }


# --------------------------------------------------------------------------
# 配图分配
# --------------------------------------------------------------------------

# 「这一段为什么配这张图」的长度上限。prompt 里写的是 20 个字，这里留一倍余量：
# 它是**给人看的复查依据**，模型偶尔写长一点不该被拦；但也不能放任它塞一整段进来
# （那会把 `scenes` 撑大，而 `video.scenes` 是随单集走数据库的）。
# --------------------------------------------------------------------------


def build_scenes(
    *,
    segments: list[dict[str, Any]],
    timings: list[Any],
    audio_duration: float,
    assets: dict[str, ImageAsset],
    image_for_segment: list[str],
    fallback_id: str,
    points: list[str] | None = None,
    focuses: list[dict[str, Any] | None] | None = None,
    whys: list[str] | None = None,
    headline: str = "",
    cover_caption: str = "",
) -> list[Scene]:
    """把脚本、时间戳和逐段配图拼成画面时间轴。

    `timings` 是 RoundTiming 列表（顺序与脚本段一一对应），
    里面已经包含了片头音乐的偏移，可以直接当绝对时间用。

    `image_for_segment[i]` 是第 i 段脚本要显示的图片 id —— 逐段指定而不是
    「从某段开始一直用到下一张图」，这样才能保证画面跟着话题走。

    `points[i]` 是第 i 段的「要点强调行」文案（可为空串 → 这一段不显示强调行）。

    `whys[i]` 是第 i 段「为什么配这张图」的一句话（只存不渲染，给复查用）。

    `headline` 是封面上的大字标题（爆款标题），`cover_caption` 是它下面那行小字
    （论文原题）—— 两者都只作用在片头那一帧。
    """
    if not segments or not timings:
        raise VideoError("缺少脚本或时间戳，无法建立视频时间轴")
    if not assets:
        raise VideoError("没有任何可用配图，无法生成视频")

    def asset_for(index: int) -> ImageAsset:
        if index < len(image_for_segment):
            asset = assets.get(image_for_segment[index])
            if asset and asset.exists:
                return asset
        asset = assets.get(fallback_id)
        if asset and asset.exists:
            return asset
        return next(a for a in assets.values() if a.exists)

    ordered = sorted(timings, key=lambda t: t.start)
    scenes: list[Scene] = []

    # ---- 片头：封面 ----
    #
    # 只有「有片头音乐」时才会有这一段独立的封面帧（`head_end > 0.3`）。
    # 社区话术把片头音乐换掉之后，第一句人声就从 0 秒开始了 —— 于是这一段不存在，
    # 视频的**第一帧**变成了第一段脚本的画面。所以封面另有两条保底规则（见函数末尾）：
    # 「第一帧必须是论文首页」和「首页那几帧放大字标题」。
    head_end = ordered[0].start
    cover = assets.get("cover")
    cover_asset = cover if (cover and cover.exists) else None
    head_asset = cover_asset or asset_for(0)
    if head_end > 0.3:
        scenes.append(
            Scene(
                start=0.0,
                end=head_end,
                image=head_asset.path,
                kind=head_asset.kind,
                caption=head_asset.caption,
            )
        )

    # ---- 正文：逐段按语义配图 ----
    for index, timing in enumerate(ordered):
        segment = segments[index] if index < len(segments) else {}
        asset = asset_for(index)
        scenes.append(
            Scene(
                start=float(timing.start),
                end=float(timing.end),
                image=asset.path,
                kind=asset.kind,
                speaker=str(segment.get("speaker") or getattr(timing, "speaker", "") or "A"),
                text=str(segment.get("text") or ""),
                caption=asset.caption,
                point=(points[index] if points and index < len(points) else ""),
                focus=(focuses[index] if focuses and index < len(focuses) else None),
                why=(whys[index] if whys and index < len(whys) else ""),
                brand=str(segment.get("brand") or ""),
            )
        )

    # ---- 封面帧：第一帧必须是论文首页，而且要有大字标题 ----
    #
    # **第一帧必须是论文首页**（PDF 第一页）是产品要求：观众第一眼要看到这是哪篇论文，
    # 而不是某张配图。模型给第一段配了别的图时，这里也要把它换回封面。
    if cover_asset and scenes and scenes[0].kind != "cover":
        scenes[0].image = cover_asset.path
        scenes[0].kind = "cover"
        scenes[0].caption = cover_asset.caption
        # 模型原本配的是别的图，它写的 `why` 说的是那张图 —— 图被我们换掉了，
        # 理由就成了假信息，清掉（宁可空着，也不要让人以为模型解释过封面）。
        scenes[0].why = ""
    # 首页那**连续几帧**（片头话术通常讲两三段，都还停在封面上）一起放大字标题：
    # 只放第一帧的话，标题会在一秒后消失，观众来不及读。
    leading = 0
    for scene in scenes:
        if scene.kind != "cover" or scene.brand == "outro":
            break
        leading += 1
    for scene in scenes[:leading]:
        if headline:
            scene.headline = headline
            # 有大字标题时，下面那行小字换成**论文原题**（观众得知道这是哪篇）
            scene.caption = cover_caption or scene.caption

    # ---- 片尾：信息图 + 结束卡 ----
    tail_start = ordered[-1].end
    if audio_duration > tail_start + 0.3:
        illustration = assets.get("illustration")
        if illustration and illustration.exists:
            tail_image, tail_kind, tail_caption = (
                illustration.path,
                illustration.kind,
                illustration.caption,
            )
        else:
            # 没有信息图就沿用最后一段的画面，别让片尾变成黑屏
            tail_image = scenes[-1].image
            tail_kind = scenes[-1].kind
            tail_caption = scenes[-1].caption

        scenes.append(
            Scene(
                start=tail_start,
                end=audio_duration,
                image=tail_image,
                kind=tail_kind,
                caption=tail_caption,
                text="以上就是这篇论文的解读，感谢收听。",
                brand="outro",  # 收尾一律用品牌卡，不要用普通配图页结尾
            )
        )

    # 补齐空隙：相邻场景之间若有缝，前一个延长到下一个开始
    for index in range(len(scenes) - 1):
        if scenes[index].end < scenes[index + 1].start:
            scenes[index].end = scenes[index + 1].start

    return scenes


# --------------------------------------------------------------------------
# 幻灯片渲染
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

    from ..branding import logo_path

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
    from .illustration import _char_width, _tokenize

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
    line: str, *, baseline: float, font_size: float, layout: Layout = PORTRAIT
) -> list[str]:
    """渲染字幕的一行：数字用强调色 + 浅色底标出来，其余照常。

    为什么值得单独做：一期论文解读里真正有信息量的往往就是那个「67%」，
    而它夹在一整行灰白文字里最容易被读漏。强调它不需要任何模型输出，也不会看错。
    """
    from .illustration import _char_width

    runs = _number_runs(line)
    if not any(is_number for _, is_number in runs):
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


def render_caption_band(
    text: str, output_path: Path, *, layout: Layout = PORTRAIT
) -> Path:
    """只渲染底部那条字幕带，用于「字幕逐句出现」。

    画幅是 `layout.width × (layout.height - layout.subtitle_top)`，位置固定叠在 `y=layout.subtitle_top`，
    所以它盖住的就是底图那一条空字幕区。

    字号比整页字幕放大到 38：观众真正在读的是这两行，而一句比一整段短得多，
    放得下更大的字（放不下会自动往小退，见 `_fit_subtitle`）。
    """
    band_h = layout.height - layout.subtitle_top
    font_size, lines = _fit_subtitle(text, max_lines=3, max_size=CAPTION_BAND_MAX_FONT)

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{layout.width}" height="{band_h}" '
        f'viewBox="0 0 {layout.width} {band_h}">',
        f'<rect width="{layout.width}" height="{band_h}" fill="{SUBTITLE_BG}"/>',
        f'<rect width="{layout.width}" height="2" fill="{SUBTITLE_RULE}"/>',
    ]

    if lines:
        line_height = font_size * 1.36
        block_h = line_height * len(lines)
        # 竖直居中：整页字幕是从固定基线往下排的，这里只有一两行，
        # 照搬那个基线会让文字顶在上沿、下面空一大片
        first_baseline = (band_h - block_h) / 2 + font_size * 0.95
        for offset, line in enumerate(lines):
            baseline = first_baseline + offset * line_height
            parts.extend(
                _caption_line_parts(line, baseline=baseline, font_size=font_size)
            )

    parts.append("</svg>")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _rasterize_custom(
        "\n".join(parts),
        output_path,
        width=layout.width,
        height=layout.height - layout.subtitle_top,
    )
    return output_path


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
    from ..branding import BRAND_CTA_SUBTITLE, BRAND_CTA_TITLE

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
# 编码
# --------------------------------------------------------------------------


def asset_version(path: Any) -> str:
    """素材版本号：修改时间 + 大小。

    与路由层给 URL 加的 `?v=` 是同一套口径。这里存下来是为了能判断
    「视频生成之后配图有没有被改过」—— 改过就说明视频里还是旧画面，
    前端应当提示可以重新合成。
    """
    try:
        stat = Path(path).stat()
    except (OSError, TypeError):
        return "0"
    return f"{int(stat.st_mtime)}-{stat.st_size}"


def is_video_stale(stored_video: dict[str, Any] | None) -> bool:
    """视频是否已经跟不上素材了（配图被人工校正 / 重新提取过）。"""
    if not stored_video:
        return False
    recorded = stored_video.get("asset_versions") or {}
    assets = stored_video.get("assets") or {}
    if not recorded or not assets:
        return False
    for asset_id, version in recorded.items():
        path = assets.get(asset_id)
        if not path:
            continue
        if not Path(path).exists():
            return True
        if asset_version(path) != version:
            return True
    return False


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


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


# --------------------------------------------------------------------------
# 动效：推镜 / 字幕分句 / 进度条
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


def plan_transitions(
    scenes: list[Scene],
    image_ids: list[str] | None = None,
    *,
    style: str = TRANSITION_STYLE,
    seconds: float = TRANSITION_SEC,
    cards: list[Path | None] | None = None,
    slides: list[Path] | None = None,
) -> list[Transition]:
    """算出每一段开头该做什么转场。

    规则（都是「有理由才动」）：
    - 第一段 → `fade_in`（从白底淡入，比「啪」地出现自然）；
    - 与上一段**同一张图** → `none`：图没变就别动，无意义的溶解只会让人以为卡了一下；
    - 换图 → `dissolve`（`style="push"` 时改成横推）；
    - 片尾品牌卡 → `to_card`：上一整页淡出，露出底下的深色卡；
    - 片段太短（`seconds` 被压到 `TRANSITION_MIN_SEC` 以下）→ `none`。
    """
    ids = image_ids or [str(scene.image) for scene in scenes]
    plan: list[Transition] = []
    for index, scene in enumerate(scenes):
        if scene.brand == "outro":
            plan.append(
                Transition(
                    kind="to_card",
                    seconds=_clamp_transition(seconds, scene, minimum=0.0),
                    previous=(slides[index - 1] if slides and index else None),
                )
            )
            continue

        if index == 0:
            plan.append(
                Transition(kind="fade_in", seconds=_clamp_transition(FADE_IN_SEC, scene))
            )
            continue

        same_image = index < len(ids) and ids[index] == ids[index - 1]
        previous_card = cards[index - 1] if cards and index - 1 < len(cards) else None
        if same_image or style == "none" or previous_card is None:
            plan.append(Transition())
            continue

        allowed = _clamp_transition(seconds, scene)
        # 压到 0 就是「不做」：kind 也要跟着变成 none，否则日志和计划读起来自相矛盾
        plan.append(
            Transition(kind=style if allowed else "none", seconds=allowed, previous=previous_card)
        )
    return plan


def _clamp_transition(seconds: float, scene: Scene, *, minimum: float = TRANSITION_MIN_SEC) -> float:
    """过渡时长不能超过这一段的三分之一，太短就干脆不做。"""
    allowed = min(seconds, scene.duration * TRANSITION_MAX_RATIO)
    return allowed if allowed >= minimum else 0.0


def split_caption_beats(text: str, *, max_beats: int = CAPTION_BEAT_MAX) -> list[str]:
    """把一段字幕按句子切成最多 `max_beats` 句（字幕逐句出现）。

    不切的情况：太短（< `CAPTION_BEAT_MIN_CHARS`）就原样返回一句 ——
    二十来字再切成两半，每句剩几个字，闪来闪去比不切更难看。

    句末标点优先；没有标点（模型偶尔会写成一长串）就在中间最近的逗号处切；
    超过 `max_beats` 句时**按字数均衡合并**（不是丢弃后面的内容）。
    """
    clean = " ".join((text or "").split())
    if not clean:
        return []
    if len(clean) < CAPTION_BEAT_MIN_CHARS:
        return [clean]

    chunks: list[str] = []
    current = ""
    for char in clean:
        current += char
        if char in _SENTENCE_END:
            chunks.append(current.strip())
            current = ""
    if current.strip():
        chunks.append(current.strip())

    if len(chunks) <= 1:
        # 没有句末标点：优先在中点附近的逗号处切；连逗号都没有（模型偶尔会写成一长串）
        # 就按中点切一刀 —— 一整段糊上去比切得略生硬更糟。
        #
        # ⚠️ 这条路径**也要过一遍行数规则**：英文句号 `.` 不在 `_SENTENCE_END` 里
        # （加上它会把 "3.5" 这种小数切开），所以英文长句全都走到这里 ——
        # 实测残留的 15 条三行字幕**全是英文**，就是因为这里直接 return 了。
        mid = len(clean) // 2
        candidates = [i for i, ch in enumerate(clean) if ch in "，,、"]
        if candidates:
            cut = min(candidates, key=lambda i: abs(i - mid))
        else:
            spaces = [i for i, ch in enumerate(clean) if ch == " "]
            cut = min(spaces, key=lambda i: abs(i - mid)) if spaces else mid
        left, right = clean[: cut + 1].strip(), clean[cut + 1 :].strip()
        if not left or not right:
            return _split_beats_by_lines([clean])
        return _split_beats_by_lines([left, right])

    if len(chunks) <= max_beats:
        return _split_beats_by_lines(chunks)

    # 合并到 max_beats 份：按总字数均分目标，逐句累加到接近目标就断一份
    total = sum(len(chunk) for chunk in chunks)
    target = total / max_beats
    merged: list[str] = []
    buffer = ""
    for chunk in chunks:
        buffer += chunk
        if len(buffer) >= target and len(merged) < max_beats - 1:
            merged.append(buffer.strip())
            buffer = ""
    if buffer.strip():
        merged.append(buffer.strip())
    return _split_beats_by_lines(merged)


def _beat_lines(text: str) -> int:
    """这句字幕实际会渲染成几行（用真正上屏的那套字号/行宽算）。"""
    return len(_fit_subtitle(text)[1])


def _split_at_break(text: str) -> tuple[str, str] | None:
    """在最靠近中间的逗号处切一刀；没有逗号就退到最近的空格；都不行返回 None。"""
    mid = len(text) // 2
    for chars in ("，,、", " "):
        cuts = [index for index, char in enumerate(text) if char in chars]
        if not cuts:
            continue
        cut = min(cuts, key=lambda index: abs(index - mid))
        left, right = text[: cut + 1].strip(), text[cut + 1 :].strip()
        if left and right:
            return left, right
    return None


def _split_beats_by_lines(
    beats: list[str],
    *,
    max_lines: int = CAPTION_MAX_LINES,
    hard_max: int = CAPTION_BEAT_HARD_MAX,
) -> list[str]:
    """把「会排成三行以上」的那一句继续拆，直到每句 ≤ `max_lines` 行。

    不拆的两种情况：拆出来的碎片太短（几个字一闪而过），或者本来就切不动
    （没有逗号/空格）。这时宁可留一句三行的，也不要拆成一串碎片。
    """
    out = list(beats)
    while len(out) < hard_max:
        grown = False
        pieces: list[str] = []
        for beat in out:
            if _beat_lines(beat) <= max_lines:
                pieces.append(beat)
                continue
            pair = _split_at_break(beat)
            if pair is None or min(len(pair[0]), len(pair[1])) < CAPTION_SPLIT_MIN_CHARS:
                pieces.append(beat)
                continue
            pieces.extend(pair)
            grown = True
        out = pieces
        if not grown or len(out) >= hard_max:
            break
    return out


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


# encode_video / capture_poster 搬去了 `video_encode.py`（本文件末尾再导出一次，
# 所以 `from app.services.video import encode_video` 仍然照旧可用）。


def probe_media_duration(path: Path) -> float | None:
    """用 ffprobe 读容器时长。读不到就返回 None。"""
    if not shutil.which("ffprobe"):
        return None
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "csv=p=0", str(path),
            ],
            capture_output=True, text=True, timeout=60,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    try:
        return round(float(result.stdout.strip()), 3)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# 总入口
# --------------------------------------------------------------------------


def build_asset_pool(
    *,
    cover_path: str | None,
    figures: list[dict[str, Any]],
    illustration_png: str | None,
) -> dict[str, ImageAsset]:
    """收集所有可用作画面的图。

    封面(id=cover) 和 信息图(id=illustration) 也要进池子：它们是**中性选项**——
    当某段脚本和任何一张论文原图都对不上时，用信息图比硬塞一张不相关的图好。
    """
    pool: dict[str, ImageAsset] = {}

    if cover_path and Path(cover_path).exists():
        pool["cover"] = ImageAsset(
            id="cover", path=Path(cover_path), kind="cover", caption="论文首页"
        )

    for index, figure in enumerate(figures):
        path = figure.get("path")
        if not path or not Path(path).exists():
            continue
        figure_id = str(figure.get("id") or f"f{index + 1}")
        pool[figure_id] = ImageAsset(
            id=figure_id,
            path=Path(path),
            kind=str(figure.get("kind") or "figure"),
            caption=str(figure.get("caption") or figure.get("label") or ""),
        )

    if illustration_png and Path(illustration_png).exists():
        pool["illustration"] = ImageAsset(
            id="illustration",
            path=Path(illustration_png),
            kind="illustration",
            caption="论文核心机制信息图",
        )

    return pool


def default_asset_id(pool: dict[str, ImageAsset]) -> str:
    """没有明确匹配时用哪张图。信息图最中性（它概括全文，不会文不对题）。"""
    for candidate in ("illustration", "cover"):
        if candidate in pool:
            return candidate
    return next(iter(pool))


# ---------------------------------------------------------------------------
# 渲染路由：HTML（Chrome）优先，用不了就退回 SVG + resvg
# ---------------------------------------------------------------------------
#
# 两条路径**同时在**：HTML 是新的默认，SVG 是保底。这样「这台机器没装 Chrome」
# 不会变成「出不了片」——那是比画面差一点严重得多的失败。
#
# 为什么要换：老路径的排版全是 Python 手算的（折行靠近似字宽表、放不下就试六档字号、
# 数字高亮的底色靠手工累加每个字的宽度）。这些浏览器都做得更准，而且**代码少一个数量级**。
#
# 实测（这台机器，Chrome 154 / 936×1210）：整页 125ms、图片卡 106ms、强调行/聚光灯约 60ms、
# 字幕带 80ms（其中截图本身占 42ms）。对照 **SVG 路径的字幕带只要 14ms**。
#
# 一条真实片子的图层数量（量过库里 7 集，跨语言）：整页/卡片/强调行各 30~90 张、
# **字幕带只有 26~72 条**（`CAPTION_BEAT_MAX = 2`，绝大多数段不需要拆）。
# 所以全部搬过来只比原来多 2~3 秒 —— 这个代价换「只剩一个渲染器」是划算的。
#
# 反过来说，如果哪天字幕带真的涨到几百条（比如把 `CAPTION_MAX_LINES` 放宽到 3 行、
# 或切成更碎的句子），代价就变成几十秒，那时再上「一场戏所有句子拼一张长图 +
# ffmpeg crop 取每一条」（能省掉每条的固定开销，约 2 倍）——**别提前做**。
RENDER_HTML_LAYERS_ENV = "RENDER_HTML_LAYERS"


def _html_layers_enabled() -> bool:
    """图层要不要走 HTML。环境变量可以单独关掉（出问题时不必回滚代码）。"""
    raw = (os.environ.get(RENDER_HTML_LAYERS_ENV) or "").strip().lower()
    if raw in ("0", "false", "no", "off", "svg"):
        return False
    from .htmlframe import html_rendering_enabled

    return html_rendering_enabled()


@contextmanager
def render_session(work_dir: Path):
    """一次出片共用一个 Chrome；HTML 不可用时 yield None，调用方走 SVG。

    为什么是「一次出片一个会话」：Chrome 启动约 1.9 秒，一页起一个的话 90 张图层页
    光启动就是三分钟。会话在渲染循环结束时立刻关掉（不等编码），
    免得一个空转的浏览器陪着 ffmpeg 跑完整条流程。
    """
    if not _html_layers_enabled():
        yield None
        return
    from .htmlframe import ChromeSession

    try:
        session = ChromeSession.start()
    except Exception as exc:  # noqa: BLE001 - 起不来就该降级，不是让出片失败
        logger.warning("Chrome 起不来，这次出片退回 SVG 渲染：%s", exc)
        yield None
        return
    try:
        logger.info("画面渲染走 HTML（Chrome 无头）")
        yield session
    finally:
        session.close()


def _render_chrome_layer(
    scene: Scene,
    path: Path,
    *,
    include_subtitle: bool,
    include_point: bool,
    layout: Layout,
    session: Any,
) -> Path:
    """骨架层：白底 + logo + 图注 + 字幕带（**不含图片**，图片卡另外一层叠上来）。"""
    if session is None:
        return render_chrome(
            scene,
            path,
            include_subtitle=include_subtitle,
            include_point=include_point,
            layout=layout,
        )
    from .htmlpage import scene_page

    session.render(
        scene_page(
            scene,
            layout,
            include_image=False,
            include_subtitle=include_subtitle,
            include_point=include_point,
        ),
        path,
        work_dir=path.parent,
    )
    return path


def _render_card_layer(scene: Scene, path: Path, *, layout: Layout, session: Any) -> Path:
    """图片卡层：图片 + 贴着它的细边框（普通页）或毛玻璃封面（片头）。"""
    if session is None:
        return render_image_card(scene, path, layout=layout)
    from .htmlpage import image_card_page

    session.render(image_card_page(scene, layout), path, work_dir=path.parent)
    return path


def _render_point_layer(point: str, path: Path, *, layout: Layout, session: Any) -> Path:
    """强调行层：单独一层是为了让它滑入（ffmpeg 的逐帧 x 表达式）。"""
    if session is None:
        return render_point_row(point, path, layout=layout)
    from .htmlpage import point_row_page

    session.render(point_row_page(point, layout), path, work_dir=path.parent)
    return path


def _render_band_layer(beat: str, path: Path, *, layout: Layout, session: Any) -> Path:
    """字幕带层：单独一层是为了「字幕逐句出现」（每句一条带子按时序叠上去）。"""
    if session is None:
        return render_caption_band(beat, path, layout=layout)
    from .htmlpage import caption_band_page

    session.render(caption_band_page(beat, layout), path, work_dir=path.parent)
    return path


def _render_focus_layer(
    scene: Scene, path: Path, *, focus: dict[str, Any], layout: Layout, session: Any
) -> Path:
    """聚光灯层：图卡画幅、与图片卡同一坐标系，叠在图片卡之上、强调行/字幕之下。"""
    if session is None:
        return render_focus_overlay(scene, path, focus=focus, layout=layout)
    from .htmlpage import focus_overlay_page

    session.render(focus_overlay_page(scene, layout, focus), path, work_dir=path.parent)
    return path


def _render_endcard_layer(scene: Scene, path: Path, *, layout: Layout, session: Any) -> Path:
    """片尾品牌卡：深色底 + logo + 关注引导（整页一张，没有「图片层」）。"""
    if session is None:
        return render_endcard(scene, path, layout=layout)
    from .htmlpage import endcard_page

    session.render(endcard_page(scene, layout), path, work_dir=path.parent)
    return path


def _focus_morph_origin(
    scenes: list[Scene], index: int
) -> dict[str, Any] | None:
    """这一段的聚光灯该**从哪儿移过来**；没有可移的就返回 None。

    条件缺一不可：
    - 上一段也框了同一张图（图都不一样，就没有「移动」可言）；
    - 两处位置**确实不同**（差不到半个像素就没什么可移的，白花 14 次渲染）。

    返回的是上一段的 focus（相对坐标），`focus_overlay_page` 会自己换算成像素。
    """
    if index <= 0:
        return None
    current, previous = scenes[index], scenes[index - 1]
    if not current.focus or not previous.focus:
        return None
    if previous.image != current.image:
        return None
    if previous.brand == "outro" or current.brand == "outro":
        return None
    a, b = previous.focus, current.focus
    moved = sum(
        abs(float(a.get(key, 0)) - float(b.get(key, 0))) for key in ("x", "y", "w", "h")
    )
    if moved < FOCUS_MORPH_MIN_MOVE:
        return None
    return dict(a)


# 位置差小于这个数（x/y/w/h 四个量加起来）就不做「移动」——
# 差一两个像素的移动看不出来，却要花十几次渲染
FOCUS_MORPH_MIN_MOVE = 0.02


def _render_focus_animation(
    scene: Scene,
    focus: dict[str, Any],
    origin: dict[str, Any],
    *,
    index: int,
    layout: Layout,
    work_dir: Path,
    session: Any,
) -> list[Path]:
    """把「聚光灯从上一处移到这一处」逐帧抓下来（**Tier B**）。

    只抓动画窗口那点帧（`FOCUS_MORPH_SEC × 30`），之后由 ffmpeg 让序列停在末帧
    （`overlay` 的 `eof_action` 默认就是 repeat）。整片逐帧是 7500 帧 ≈ 15 分钟，
    这里只要 14 帧 ≈ 1 秒 —— 这就是「只在动画窗口逐帧」的意思。
    """
    from .htmlpage import FOCUS_MORPH_SEC, focus_overlay_page

    page = focus_overlay_page(scene, layout, focus, origin=origin)
    count = max(int(round(FOCUS_MORPH_SEC * FPS)), 2)
    times = [step / FPS for step in range(count)]
    prefix = f"focus-{index:04d}"
    frames = session.render_frames(page, times, work_dir, prefix=prefix, work_dir=work_dir)
    logger.info(
        "聚光灯移动：第 %d 段从上一处移过来（%d 帧 / %.2f 秒）",
        index,
        len(frames),
        FOCUS_MORPH_SEC,
    )
    return frames


@dataclass
class RenderedLayers:
    """一次出片渲染出来的所有图层文件（编码阶段要用）。

    单独一个结构是因为它已经有十来个列表了：位置参数排到第 8 个就没人看得懂谁是谁，
    而错一个顺序（比如把 `cards` 和 `slides` 调个个儿）在画面上不一定立刻看得出来。
    """

    slides: list[Path]
    caption_bands: list[list[CaptionBand]]
    band_paths: list[Path]
    point_rows: list[Path | None]
    point_paths: list[Path]
    cards: list[Path | None]
    card_paths: list[Path]
    focus_rows: list[Path | None]
    focus_paths: list[Path]
    # 逐帧抓下来的聚光灯动画（Tier B）：第 i 段是这一段的帧序列，没有就是空列表
    focus_frames: list[list[Path]]
    image_ids: list[str]
    endcard_count: int = 0
    beat_count: int = 0
    backend: str = "svg"


def _render_scene_layers(
    scenes: list[Scene], layout: Layout, work_dir: Path
) -> RenderedLayers:
    """把每一段画面渲染成「骨架 + 图片卡 + 字幕带 + 强调行 + 聚光灯」几层文件。

    分层的意义（见 AGENTS.md「转场与语音驱动的动效」一节）：骨架全程不动、
    只有图片卡在换，画面上就是「同一页里图在变」，而不是每段重画一整页。
    字幕再单独成层，才能做到「字幕逐句出现」。
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    result = RenderedLayers(
        slides=[], caption_bands=[], band_paths=[], point_rows=[], point_paths=[],
        cards=[], card_paths=[], focus_rows=[], focus_paths=[], focus_frames=[],
        image_ids=[],
    )

    with render_session(work_dir) as session:
        if session is not None:
            result.backend = "html"
        for index, scene in enumerate(scenes):
            slide_path = work_dir / f"slide-{index:04d}.png"
            beats = split_caption_beats(scene.text)
            bands: list[CaptionBand] = []
            card_path: Path | None = None

            if scene.brand == "outro":
                # 片尾用品牌卡（深色 + logo + 关注引导），正文用普通白底页。
                # 品牌卡上的字是大号引导语，不参与「字幕逐句出现」——那会把它切碎。
                # 它整页都是牌子，没有「图片层」，转场用 to_card（上一整页淡出）代替。
                _render_endcard_layer(scene, slide_path, layout=layout, session=session)
                result.endcard_count += 1
            else:
                # 正文：**骨架 + 图片卡** 两层。骨架（图注/强调行/字幕带）全程不动，
                # 只有图片卡在换 —— 这就是「连贯」的来源（见 render_chrome 的说明）。
                _render_chrome_layer(
                    scene,
                    slide_path,
                    include_subtitle=len(beats) <= 1,
                    include_point=False,
                    layout=layout,
                    session=session,
                )
                card_path = work_dir / f"card-{index:04d}.png"
                _render_card_layer(scene, card_path, layout=layout, session=session)
                result.card_paths.append(card_path)
                if len(beats) > 1:
                    for beat_index, beat in enumerate(beats):
                        band_path = work_dir / f"band-{index:04d}-{beat_index}.png"
                        _render_band_layer(beat, band_path, layout=layout, session=session)
                        bands.append(CaptionBand(text=beat, image=band_path))
                        result.band_paths.append(band_path)
                    result.beat_count += 1

            result.slides.append(slide_path)
            result.caption_bands.append(bands)
            result.cards.append(card_path)
            # 「换了没有」按图片素材判断，而不是按渲染出来的文件：同一张图连着讲两段时
            # 不该做无意义的溶解（观众只会觉得画面卡了一下）
            result.image_ids.append(str(scene.image))

            # 聚光灯只框**论文原图**：我们生成的信息图/段落图是矢量示意图，
            # 它们有自己的动效路线，两套叠在一起反而乱。
            wants_focus = (
                scene.focus
                and scene.brand != "outro"
                and card_path is not None
                and scene.kind in ("figure", "table", "cover")
            )
            frames: list[Path] = []
            if wants_focus:
                # Tier B：上一段也框了同一张图、而且位置不同 → 让聚光灯**移过去**
                # （只有 HTML 路径做得到；退回 SVG 时就退回原来的淡入）
                origin = _focus_morph_origin(scenes, index) if session is not None else None
                if origin is not None:
                    frames = _render_focus_animation(
                        scene,
                        scene.focus,
                        origin,
                        index=index,
                        layout=layout,
                        work_dir=work_dir,
                        session=session,
                    )
                    # 帧序列自己带结尾状态（末帧就是目标位置），所以不再叠静态图：
                    # 多叠一层会在序列播完后「跳」一下
                    result.focus_rows.append(None)
                    result.focus_paths.extend(frames)
                else:
                    focus_path = work_dir / f"focus-{index:04d}.png"
                    _render_focus_layer(
                        scene,
                        focus_path,
                        focus=scene.focus,
                        layout=layout,
                        session=session,
                    )
                    result.focus_rows.append(focus_path)
                    result.focus_paths.append(focus_path)
            else:
                result.focus_rows.append(None)
            result.focus_frames.append(frames)

            if scene.point and scene.brand != "outro":
                point_path = work_dir / f"point-{index:04d}.png"
                _render_point_layer(scene.point, point_path, layout=layout, session=session)
                result.point_rows.append(point_path)
                result.point_paths.append(point_path)
            else:
                result.point_rows.append(None)

    return result


def compose_video(
    *,
    segments: list[dict[str, Any]],
    timings: list[Any],
    audio_path: Path,
    audio_duration: float,
    cover_path: str | None,
    figures: list[dict[str, Any]],
    illustration_png: str | None,
    work_dir: Path,
    output_path: Path,
    title: str = "",
    llm: Any | None = None,
    analysis: dict[str, Any] | None = None,
    max_topic_images: int = 4,
    preset_scenes: list[dict[str, Any]] | None = None,
    preset_assets: dict[str, str] | None = None,
    preset_hook: str = "",
    allow_point_llm: bool | None = None,
    language: str = "zh",
    orientation: str = "portrait",
) -> VideoResult:
    """合成视频解读播客。任何一步失败都抛 VideoError，由调用方降级。

    preset_scenes / preset_assets 用于**重新合成**：上一次已经把「哪一段用哪张图」
    算好了并存了下来，重做时直接复用，不再问模型 —— 既省钱省时间，
    也保证重做前后画面选择一致（否则再问一次结果可能就不一样了）。

    `preset_hook` 是上次写的封面标题，`allow_point_llm` 同时管「要点文案」和
    「封面标题」这两件**可以单独补一次**的模型输出（见下面强调行那一段）。
    """
    pool = build_asset_pool(
        cover_path=cover_path, figures=figures, illustration_png=illustration_png
    )
    if not pool:
        raise VideoError("没有任何可用配图，无法生成视频")

    default_id = default_asset_id(pool)
    figure_ids = [i for i in pool if i not in ("cover", "illustration")]

    # 复用模式：把上次用到的素材（含现场生成的主题图）补回池子里
    if preset_scenes:
        for asset_id, asset_path in (preset_assets or {}).items():
            if asset_id in pool:
                continue
            if asset_path and Path(asset_path).exists():
                pool[asset_id] = ImageAsset(
                    id=asset_id,
                    path=Path(asset_path),
                    kind="illustration" if asset_id.startswith("topic") else "figure",
                    caption="",
                )
        default_id = default_asset_id(pool)
        figure_ids = [i for i in pool if i not in ("cover", "illustration")]

    # 逐段语义匹配：优先让模型判断「这一段在讲什么、哪张图正好在讲同一件事」
    image_for_segment: list[str] | None = None
    # 逐段「为什么配这张图」（模型写的，或从上次合成里复用）。只入库、不渲染。
    assignment_whys: list[str] = [""] * len(segments)
    strategy = "heuristic"

    if preset_scenes:
        # 复用上次的选择，只做有效性校验（图可能被删了/文件丢了）
        stored = {sc.get("index"): sc for sc in preset_scenes if isinstance(sc, dict)}
        picked: list[str] = []
        last_valid = default_id
        for index in range(len(segments)):
            candidate = (stored.get(index) or {}).get("image")
            if candidate and candidate in pool and pool[candidate].exists:
                last_valid = candidate
            picked.append(last_valid)
        image_for_segment = picked
        # 复用分配时**理由一起复用**：它跟的是「哪一段用什么图」，图没换理由就还成立。
        # 丢掉的后果是「重新合成一次，复查依据全没了」，而这并不是用户改了什么。
        assignment_whys = [
            str((stored.get(index) or {}).get("why") or "")
            for index in range(len(segments))
        ]
        strategy = "reused"
        missing = sum(
            1 for index in range(len(segments)) if (stored.get(index) or {}).get("image") not in pool
        )
        logger.info("重新合成：复用上次的画面分配（%d 段）", len(picked))
        if missing:
            logger.info("其中有 %d 段引用的图已失效，回退到中性图", missing)

    elif llm is not None and not getattr(llm, "mock", True):
        assets = [
            {"id": asset.id, "caption": asset.caption}
            for asset in pool.values()
        ]
        try:
            data = llm._chat_json(
                build_assign_messages(segments, assets),
                # 每段一项，现在每项除了 id 还多一句 why（中文 20 字 ≈ 25 token），
                # 所以预算是 110/段 —— 卡着 60 会让长稿的尾部整段缺项，
                # 而缺项的表现是「后半程画面回到中性图」，看起来像模型没干活。
                max_tokens=max(2500, len(segments) * 110),
                temperature=0.2,
            )
            candidate = _normalize_per_segment(
                data.get("assignments"),
                count=len(segments),
                valid_ids=set(pool),
                default_id=default_id,
            )
            if candidate:
                image_for_segment = candidate
                strategy = "model"
                assignment_whys = _assignment_notes(
                    data.get("assignments"),
                    count=len(segments),
                    valid_ids=set(pool),
                )
                distinct = len(set(candidate))
                logger.info(
                    "逐段配图由模型完成：%d 段用了 %d 张不同的图", len(candidate), distinct
                )
                # 抽查几条理由：这是「图文到底符不相符」在日志里唯一看得见的证据
                # （以前只能靠人重读全稿）。空着的位置不打，所以只报「几段有」。
                filled = [w for w in assignment_whys if w]
                if filled:
                    sample = "；".join(
                        f"[{i}] {w}" for i, w in enumerate(assignment_whys) if w
                    )
                    logger.info(
                        "配图理由（%d/%d 段有）：%s",
                        len(filled),
                        len(segments),
                        sample[:300],
                    )
        except Exception as exc:  # noqa: BLE001
            logger.warning("模型逐段配图失败，退回均匀分布：%s", exc)

    if image_for_segment is None:
        image_for_segment = heuristic_per_segment(
            len(segments), figure_ids, default_id
        )
        logger.info("逐段配图使用均匀分布（退化方案，不保证图文相符）")

    # ---- 没有对应原图的段落：现场生成专门的配图 ----
    #
    # 以前这些段落统一挂同一张概括全图的信息图，实测 21 段里有 10 段都是它，
    # 画面单调且并不真的对应内容。改成按话题生成后，每张图都只讲那一段的事。
    runs = group_generate_runs(image_for_segment)
    generated = 0
    if runs:
        capped = merge_runs_to_cap(runs, max_topic_images)
        if len(capped) < len(runs):
            logger.info(
                "需要生成配图的段落组有 %d 个，超过上限 %d，已合并为 %d 组",
                len(runs), max_topic_images, len(capped),
            )
        topic_assets = generate_topic_images(
            llm,
            segments=segments,
            runs=capped,
            paper_title=title,
            analysis=analysis,
            output_dir=work_dir / "topics",
            stem=output_path.stem,
        )
        for start, asset in topic_assets.items():
            pool[asset.id] = asset
            generated += 1

        # 把 generate 的段落指向它所属那一组生成出来的图
        for start_segment, end_segment in capped:
            asset = topic_assets.get(start_segment)
            if asset is None:
                continue  # 这一组生成失败 → 保持 generate，下面会退回中性图
            for index in range(start_segment, min(end_segment, len(image_for_segment))):
                image_for_segment[index] = asset.id

    # 生成失败或未生成的部分退回中性图，保证不会出现空画面
    image_for_segment = [
        pick if pick != GENERATE_ID else default_id for pick in image_for_segment
    ]
    logger.info(
        "配图构成：原图/封面 %d 张，现场生成 %d 张，中性兜底 %d 段",
        len(figure_ids),
        generated,
        sum(1 for pick in image_for_segment if pick == default_id),
    )

    # ---- 「本段要点」强调行 ----
    #
    # 三级来源，优先级从高到低：
    # 1. **上次存下来的**（`video.scenes[i].point`）—— 重新合成时必须复用，
    #    否则用户只是转了个配图，整段强调文案就全变了。
    # 2. **让模型写**（`POINTS_SYSTEM`）。这是唯一能给出「这一段最该记住什么」的来源：
    #    实测本地抽取够不着 —— 只有 30%~47% 的段落含阿拉伯数字，关键词命中接近 0。
    # 3. **本地兜底**（`local_point`，只抠数字），没有就留空、不显示强调行。
    #
    # **复用画面时默认不碰模型**（`tests/test_video.py::TestPresetReuse` 钉着这条），
    # 因为「用户只是转了个配图，画面和文案不该全变」。但旧视频里没有强调行文案，
    # 需要能**单独补一次**：调用方显式传 `allow_point_llm=True` 即可（见维护脚本）。
    if allow_point_llm is None:
        allow_point_llm = not preset_scenes
    point_limit = point_char_limit(language)

    points: list[str] = [""] * len(segments)
    focuses: list[dict[str, Any] | None] = [None] * len(segments)
    if preset_scenes:
        reused = 0
        reused_focus = 0
        for entry in preset_scenes:
            index = entry.get("index")
            point = str(entry.get("point") or "").strip()
            if isinstance(index, int) and 0 <= index < len(points):
                if point:
                    points[index] = point
                    reused += 1
                # 聚光灯也复用：重新合成时画面分配与「框哪里」都不该变
                focus = normalize_focus(entry.get("focus"))
                if focus:
                    focuses[index] = focus
                    reused_focus += 1
        if reused:
            logger.info("强调行：复用上次的 %d 段", reused)
        if reused_focus:
            logger.info("聚光灯：复用上次的 %d 段", reused_focus)

    if (
        allow_point_llm
        and any(not point for point in points)
        and llm is not None
        and not getattr(llm, "mock", True)
    ):
        # 这几张图有子图清单可给模型挑（几何已经由像素量好了，见 build_panel_catalog）
        panel_catalog = build_panel_catalog(figures, pool)
        panel_options = [
            panel_catalog[asset_id].options() if asset_id in panel_catalog else []
            for asset_id in image_for_segment
        ]
        panel_letters = [
            panel_catalog[asset_id].letters if asset_id in panel_catalog else []
            for asset_id in image_for_segment
        ]
        # 重试一次：这一路失败不会让视频整体失败（下面有本地兜底），
        # 但结果会从「每段都有要点」退化成「只有含数字的段有」——
        # 实测踩到过一次（中文那版整集没有要点，英文那版正常），所以值得多试一次。
        for attempt in (1, 2):
            try:
                data = llm._chat_json(
                    build_points_messages(
                        segments,
                        title=title,
                        language=language,
                        image_captions=[
                            pool[asset_id].caption if asset_id in pool else None
                            for asset_id in image_for_segment
                        ],
                        panel_options=panel_options,
                    ),
                    max_tokens=max(900, len(segments) * 60),
                    temperature=0.3,
                )
                written = normalize_point_items(
                    data.get("points"),
                    count=len(segments),
                    max_chars=point_limit,
                    language=language,
                    panel_letters=panel_letters,
                )
                filled = 0
                boxed = 0
                for index, item in enumerate(written):
                    if item["point"] and not points[index]:
                        points[index] = item["point"]
                        filled += 1
                    if focuses[index] is not None:
                        continue
                    focus = None
                    if item["panel"] and image_for_segment[index] in panel_catalog:
                        focus = panel_catalog[image_for_segment[index]].focus_for(item["panel"])
                    # 旧数据里存的「模型给的坐标框」还能读，但不能把空间让给它：
                    # 面板框是量出来的，坐标框是猜的（见 POINTS_SYSTEM 的说明）。
                    focus = focus or item["focus"]
                    if focus:
                        focuses[index] = focus
                        boxed += 1
                if filled:
                    logger.info("强调行：模型写了 %d 段", filled)
                if boxed:
                    logger.info("聚光灯：模型指了 %d 段", boxed)
                break
            except Exception as exc:  # noqa: BLE001
                if attempt == 1:
                    logger.warning("强调行生成失败，重试一次：%s", exc)
                else:
                    logger.warning("强调行生成失败，退回本地抽取：%s", exc)

    fallback_used = 0
    for index, segment in enumerate(segments):
        if not points[index]:
            local = local_point(
                str(segment.get("text") or ""), max_chars=point_limit, language=language
            )
            if local:
                points[index] = local
                fallback_used += 1
    logger.info(
        "强调行：共 %d 段有内容（其中本地兜底 %d 段）",
        sum(1 for point in points if point),
        fallback_used,
    )

    # ---- 封面标题（爆款标题）----
    #
    # 只出现在片头那一帧上。三级来源：
    # 1. **上次写的**（`video.hook`）—— 重新合成时必须复用（跟强调行同一个道理）；
    # 2. **让模型写一次**（`HOOK_SYSTEM`，挑风格、不许编数字）；
    # 3. **论文原题兜底** —— 没有标题的封面很怪，原题至少交代了这是什么。
    hook = normalize_hook(preset_hook, language=language)
    if not hook and allow_point_llm and llm is not None and not getattr(llm, "mock", True):
        try:
            raw = llm._chat(
                build_hook_messages(title=title, analysis=analysis, language=language),
                max_tokens=80,
                temperature=0.8,
                json_mode=False,  # 要的就是一行纯文本，塞进 JSON 反而更容易被引号包起来
            )
            hook = normalize_hook(raw, language=language)
        except Exception as exc:  # noqa: BLE001
            logger.warning("封面标题生成失败，改用论文原题：%s", exc)
    cover_headline = hook or _cover_fallback_headline(title)
    if hook:
        logger.info("封面标题：%s", hook)
    elif cover_headline:
        logger.info("封面标题：没有模型输出，用论文原题兜底（%s…）", cover_headline[:16])

    scenes = build_scenes(
        segments=segments,
        timings=timings,
        audio_duration=audio_duration,
        assets=pool,
        image_for_segment=image_for_segment,
        fallback_id=default_id,
        points=points,
        focuses=focuses,
        whys=assignment_whys,
        headline=cover_headline,
        cover_caption=title,
    )

    layout = layout_for(orientation)
    layers = _render_scene_layers(scenes, layout, work_dir)
    image_ids = layers.image_ids

    transitions = plan_transitions(scenes, image_ids, cards=layers.cards, slides=layers.slides)
    # 有「聚光灯移过来」的那一段**取消溶解**：入场交给聚光灯的运动。
    # 两套动效同时演（图在溶、框在走）看着就是「画面在抖」，而且观众不知道该看哪。
    for frame_index, frames in enumerate(layers.focus_frames):
        if frames and frame_index < len(transitions):
            transitions[frame_index] = Transition()
    moved = sum(1 for item in transitions if item.kind not in ("", "none"))
    logger.info(
        "已渲染 %d 帧画面（其中片尾品牌卡 %d 帧，字幕分句 %d 段，转场 %d 处）",
        len(layers.slides), layers.endcard_count, layers.beat_count, moved,
    )

    # 说话停顿：用来把字幕换句对准真实的停顿（检测失败就是空列表 → 退回按字数）
    pauses = detect_pauses(audio_path)
    if pauses:
        logger.info("检测到 %d 处说话停顿，字幕换句将对齐到停顿", len(pauses))

    result = encode_video(
        scenes,
        layers.slides,
        audio_path,
        output_path,
        target_duration=audio_duration,
        caption_bands=layers.caption_bands,
        point_rows=layers.point_rows,
        pauses=pauses,
        image_cards=layers.cards,
        transitions=transitions,
        focus_overlays=layers.focus_rows,
        focus_animations=layers.focus_frames,
        layout=layout,
    )
    result.assignment = strategy
    result.renderer = layers.backend
    result.scenes = [
        {
            "index": index,
            "image": image_for_segment[index],
            # 强调文案与聚光灯都存下来，重新合成时复用（见上面三级来源的说明）
            "point": points[index] if index < len(points) else "",
            "focus": focuses[index] if index < len(focuses) else None,
            # 「为什么配这张图」（模型写的）。**只存不渲染**：它是复查图文相符的依据，
            # 也让「同一张图连讲多段」这种判断有迹可循，不必再重读全稿。
            "why": assignment_whys[index] if index < len(assignment_whys) else "",
        }
        for index in range(min(len(segments), len(image_for_segment)))
    ]
    result.assets = {asset_id: str(asset.path) for asset_id, asset in pool.items()}
    result.asset_versions = {
        asset_id: asset_version(asset.path) for asset_id, asset in pool.items()
    }
    result.hook = hook
    result.poster_path = str(
        capture_poster(
            output_path,
            output_path.with_name(output_path.stem + ".poster.png"),
            # 片头第一帧带 fade_in（从白底淡入 0.35 秒），所以取第 1 段画面**中间**的时刻：
            # 既躲过淡入，也不会滑到第二段画面里（片头段通常有十几秒，足够）
            at=min(1.5, max(scenes[0].duration / 2, 0.4)) if scenes else 1.0,
        )
    )

    for temp_path in [
        *layers.slides,
        *layers.band_paths,
        *layers.point_paths,
        *layers.card_paths,
        *layers.focus_paths,
    ]:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass

    return result


# --------------------------------------------------------------------------
# 「没有对应原图」的段落：现场生成专门的配图
# --------------------------------------------------------------------------


def group_generate_runs(picks: list[str]) -> list[tuple[int, int]]:
    """把连续的 generate 段落合并成 [起, 止) 区间。

    必须合并：一段一张图的话，21 段脚本会生成十几张，每张一次模型调用
    （实测单张约 10 秒、3000+ token），成本和耗时都不可控。
    连续几段通常本来就在讲同一件事，共用一张图反而更贴切。
    """
    runs: list[tuple[int, int]] = []
    start: int | None = None
    for index, pick in enumerate(picks):
        if pick == GENERATE_ID:
            if start is None:
                start = index
        elif start is not None:
            runs.append((start, index))
            start = None
    if start is not None:
        runs.append((start, len(picks)))
    return runs


def merge_runs_to_cap(
    runs: list[tuple[int, int]], cap: int
) -> list[tuple[int, int]]:
    """区间数超过上限时，把相邻区间合并到只剩 cap 个。

    按顺序均分成 cap 组，每组覆盖原来若干个区间 —— 这样覆盖范围不变，
    只是每张图负责更多段落。比「丢掉超出的区间」更合理：
    丢掉的段落会退回中性图，又变回图文不符。
    """
    if len(runs) <= cap or cap <= 0:
        return runs

    merged: list[tuple[int, int]] = []
    total = len(runs)
    for slot in range(cap):
        first = slot * total // cap
        last = (slot + 1) * total // cap - 1
        merged.append((runs[first][0], runs[last][1]))
    return merged


def generate_topic_images(
    llm: Any,
    *,
    segments: list[dict[str, Any]],
    runs: list[tuple[int, int]],
    paper_title: str,
    analysis: dict[str, Any] | None,
    output_dir: Path,
    stem: str,
    progress: Any | None = None,
) -> dict[int, ImageAsset]:
    """为每个区间生成一张配图，返回 {起始段号: ImageAsset}。

    用线程池并行：串行的话 4 张图要 40 秒，会把流水线明显拖长。
    LLMClient 每次调用都是独立的 httpx 请求，没有共享状态，并行是安全的。
    """
    if not runs or getattr(llm, "mock", True):
        return {}

    from .illustration import generate_topic_illustration

    def one(index: int, run: tuple[int, int]) -> tuple[int, ImageAsset | None]:
        start, end = run
        lines = [
            f"主播{(segments[i].get('speaker') or 'A')}：{(segments[i].get('text') or '').strip()}"
            for i in range(start, min(end, len(segments)))
        ]
        try:
            result = generate_topic_illustration(
                llm,
                script_lines=lines,
                paper_title=paper_title,
                analysis=analysis,
                output_dir=output_dir,
                stem=f"{stem}-topic{index + 1}",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("段落配图异常（第 %d 组）：%s", index + 1, exc)
            return start, None

        if result is None:
            return start, None
        return start, ImageAsset(
            id=f"topic{index + 1}",
            path=Path(result.png_path),
            kind="illustration",
            caption="本段内容的示意图",
        )

    from concurrent.futures import ThreadPoolExecutor

    assets: dict[int, ImageAsset] = {}
    workers = min(len(runs), 4)
    with ThreadPoolExecutor(max_workers=max(workers, 1)) as pool:
        for start, asset in pool.map(lambda pair: one(*pair), enumerate(runs)):
            if asset is not None:
                assets[start] = asset
                if progress:
                    progress(start)

    logger.info("段落配图：%d 组中成功 %d 张", len(runs), len(assets))
    return assets


# ffmpeg 编码搬到了 video_encode.py；为了不让几十处老调用点改动，这里在文件末尾再导出一次。
from .video_encode import capture_poster, encode_video  # noqa: E402,F401
