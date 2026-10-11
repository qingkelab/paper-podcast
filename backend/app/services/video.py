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

## 分层：改哪里，开哪个文件

```
video_core.py    类型与错误类型（Scene / CaptionBand / VideoResult / VideoError）
video_render.py  画面渲染（骨架、图片卡、字幕带、聚光灯、片尾卡、字幕时间窗）
video_encode.py  ffmpeg 编码与封面静帧（每段滤镜链、帧数、音频封装）
video.py         ← 本文件：场景编排、转场分句、素材池，以及对外门面
```

方向是**单向**的：本文件依赖上面三层，上面三层**谁都不许** import 本文件。
所以「画一帧画面」「编一段视频」都能单独 import 进来用，不必先加载本模块。
本文件顶部把上面三层的东西再导出一次，老写法 `from app.services.video import ...` 照旧可用。
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .audio_track import detect_pauses

# 版式与配色令牌搬去了 `design.py`（HTML 渲染路径也要用同一套数值）。
# 这里 `import *` 是为了**一个字都不改地**再导出给现有调用方 ——
# tests 和别的模块都写着 `from app.services.video import PORTRAIT` 之类。
from .design import *  # noqa: F401,F403

# --------------------------------------------------------------------------
# 分层：design → video_core → video_render → video_encode → video（本文件）
# --------------------------------------------------------------------------
#
# 类型在 `video_core.py`、画面渲染在 `video_render.py`、ffmpeg 编码在 `video_encode.py`。
# 这里是**正常 import**（不再是「文件末尾再导出」）—— 末尾再导出会让
# `import app.services.video_encode` 撞上正在初始化的 `video`（partially initialized
# module），那正是这一轮拆分要修掉的东西。所以顺序是死的、单向的：
# 下面三个模块**谁都不许** import 本文件。
#
# 这些名字同时是**对外门面**：老调用点（含 tests 里那 60 多个 import）继续写
# `from app.services.video import encode_video` 就行，一个字都不用改。
# 新代码请直接用上面对应的模块（类型 → video_core，画面 → video_render，ffmpeg → video_encode）。
from .video_core import (
    _SENTENCE_END,
    CaptionBand,
    ImageAsset,
    Scene,
    Transition,
    VideoError,
    VideoResult,
)
from .video_render import (  # noqa: F401
    FigurePanels,
    _caption_line_parts,
    _fit_subtitle,
    _image_data_uri,
    _logo_data_uri,
    _number_runs,
    _panel_label,
    _prepare_image,
    _rasterize_custom,
    _render_slide_layers,
    _wrap,
    beat_windows,
    beat_windows_aligned,
    build_panel_catalog,
    image_card_markup,
    progress_bar_svg,
    render_caption_band,
    render_chrome,
    render_endcard,
    render_focus_overlay,
    render_image_card,
    render_point_row,
    render_slide,
)
from .video_encode import (  # noqa: F401
    capture_poster,
    encode_video,
    ffmpeg_available,
    probe_media_duration,
)

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
# 素材版本与「视频过时」判断
# --------------------------------------------------------------------------
#
# ffmpeg 的可用性探测（`ffmpeg_available`）与容器时长探测（`probe_media_duration`）
# 跟着编码一起搬去了 `video_encode.py`，这里仍然再导出给老调用点。


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


# --------------------------------------------------------------------------
# 转场与字幕分句
# --------------------------------------------------------------------------
#
# 字幕**时间窗**（哪一句什么时候出现）跟着画面渲染一起搬去了 `video_render.py`；
# 这里留的是「一段稿子怎么切成几句」与「每段开头做什么转场」——两者的规则都属于编排。


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


# encode_video / capture_poster / ffmpeg_available / probe_media_duration 都在
# `video_encode.py`（本文件顶部正常 import 之后再导出，所以老调用点照旧可用）。


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
