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
from dataclasses import dataclass, field, replace
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
from .design import content_layout
from .design import DIAGRAM_FALLBACK_HOLD
from .design import ENABLE_CLAUSE_REVEAL
from .video_core import (
    diagram_shot_from_dict,
    diagram_shot_to_dict,
    plan_reveals,
    _SENTENCE_END,
    Beat,
    CaptionBand,
    ImageAsset,
    Scene,
    Transition,
    VideoError,
    VideoResult,
)
from .video_render import (  # noqa: F401
    emphasis_clause_index,
    BeatLayers,
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
    fitted_figure_height,
    _wrap,
    beat_windows,
    beat_windows_aligned,
    build_panel_catalog,
    image_card_markup,
    progress_bar_svg,
    render_beat_layers,
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
from .video_qa import qa_video

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
    _beat_offsets,
    _beat_notes_by_segment,
    _flat_sentence_items,
    _normalize_per_beat,
    _normalize_per_segment,
    build_assign_messages,
    heuristic_assignment,
    heuristic_per_beat,
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


# --------------------------------------------------------------------------
# 动态图解镜头：把讲稿句子与论文证据分配到镜头（见 docs/DIAGRAM_SHOTS.md）
# --------------------------------------------------------------------------


def diagram_evidence(
    analysis: dict[str, Any] | None,
    segments: list[dict[str, Any]] | None,
    figures: list[dict[str, Any]] | None = None,
) -> list[str]:
    """允许作为依据的全部文本：解读字段 + 脚本原句 + 图注。

    **核验只认这一份**（`diagram.verify_shot`）—— 所以它必须包含脚本原句：
    模型写草案时最容易犯的错是「把解说里的数字记错一位」，而解说原句就是裁判。
    """
    out: list[str] = []
    for value in (analysis or {}).values():
        if isinstance(value, list):
            out.extend(str(item) for item in value if str(item).strip())
        elif isinstance(value, (str, int, float)):
            out.append(str(value))
    out.extend(str(seg.get("text") or "") for seg in segments or [])
    out.extend(str(fig.get("caption") or "") for fig in figures or [])
    return [item for item in out if item.strip()]


def diagram_candidates(
    segments: list[dict[str, Any]],
    timings: list[Any],
    *,
    min_seconds: float | None = None,
    max_candidates: int = 24,
) -> list[int]:
    """挑**值得画图解**的候选段：够长、而且话里确实有东西可结构化。

    判据是「提到数字（指标/倍数/内存/准确率…）」或「在讲步骤/对比/结论」——
    不筛的话模型会在每一段上都硬造一张图（它的默认行为就是「尽量满足要求」）。
    """
    from .design import DIAGRAM_MIN_SEGMENT_SEC

    min_seconds = DIAGRAM_MIN_SEGMENT_SEC if min_seconds is None else min_seconds
    markers = (
        "%", "倍", "比特", "bit", "内存", "显存", "吞吐", "延迟", "加速", "准确率", "精度",
        "第一步", "第二步", "先", "再", "然后", "相比", "对比", "结论", "意味着", "所以",
    )
    picked: list[int] = []
    for index, segment in enumerate(segments):
        text = str(segment.get("text") or "")
        if len(text) < 12:
            continue
        if index < len(timings):
            timing = timings[index]
            start = getattr(timing, "start", None)
            end = getattr(timing, "end", None)
            if start is not None and end is not None and float(end) - float(start) < min_seconds:
                continue
        if any(marker in text for marker in markers):
            picked.append(index)
        if len(picked) >= max_candidates:
            break
    # **收尾段始终进候选**：结论镜头该落在哪儿是位置问题，不是「这段话里有没有数字」。
    # （它长度不够就算了 —— 一两秒的收尾挂一张图反而更差。）
    for index in summary_candidates(segments):
        if index in picked:
            continue
        if index >= len(timings):
            continue
        timing = timings[index]
        start, end = getattr(timing, "start", None), getattr(timing, "end", None)
        if start is not None and end is not None and float(end) - float(start) < min_seconds:
            continue
        picked.append(index)
    return sorted(picked)


def summary_candidates(segments: list[dict[str, Any]]) -> list[int]:
    """「收尾段」的段号：**紧挨片尾品牌段的那一段正文**。

    为什么要有它：`summary` 镜头（一句话总结 + 切回论文原图）在整集的**最后**最自然，
    但模型拿到一堆候选段时并不知道「哪一段是结尾」—— 它只看到编号和文字。
    这里把位置信息显式喂给它，规则本身是确定的（不依赖模型判断）。
    """
    outro_index = next(
        (
            index
            for index, segment in enumerate(segments)
            if str(segment.get("brand") or "") == "outro"
        ),
        None,
    )
    if outro_index is None:
        return []
    for index in range(outro_index - 1, -1, -1):
        if str(segments[index].get("brand") or "") not in ("intro", "outro"):
            return [index]
    return []


def _verify_diagram_drafts(
    shots: list[tuple[int, Any]], *, evidence: list[str]
) -> tuple[dict[int, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """逐条核验草案：返回（通过的镜头、**部分被删**的、整条作废的）。

    三种结果都要分开 —— 它们后面要做的事不一样：通过的照用；部分被删的按删完的结果用
    （**核验就是安全网，删掉是对的**），但要记进报告让人看得见；整条作废的才值得**再问一次**。
    """
    from .diagram import verify_shot

    kept: dict[int, Any] = {}
    partial: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for segment_index, shot in shots:
        result = verify_shot(shot, evidence=evidence)
        if result.ok:
            kept[segment_index] = result.shot
            if result.dropped:
                partial.append(
                    {"segment": segment_index, "title": shot.title, "dropped": list(result.dropped)}
                )
            logger.info("图解镜头：第 %d 段「%s」（%s）", segment_index, shot.title, shot.kind)
        else:
            rejected.append(
                {
                    "segment": segment_index,
                    "title": shot.title,
                    "kind": shot.kind,
                    "dropped": list(result.dropped),
                }
            )
            logger.info("图解镜头作废：第 %d 段 %s", segment_index, result.summary())
    return kept, partial, rejected


def assign_diagram_shots(
    *,
    segments: list[dict[str, Any]],
    timings: list[Any],
    evidence: list[str],
    llm: Any | None,
    available_images: list[tuple[str, str]] | None = None,
    max_shots: int | None = None,
    language: str = "zh",
    report: dict[str, Any] | None = None,
) -> dict[int, Any]:
    """调模型提草案 → 归一化 → **逐条核验** →（核验不过的）**带原因再问一次** → 返回镜头表。

    核验不过的条目会被丢掉；剩下不足两个节点的镜头整条作废（不返回）——
    这样一来「无依据的内容不渲染，退回现有画面流程」是**默认行为**，不是异常分支。

    **第二轮修复**（`report["repair"]`）：整条作废的镜头会带**机器给出的具体原因**
    再问一次模型（「节点「Gemini-3-Pro 标注」查不到依据」这种原文），只重提那几条、
    不复述已经通过的。参考做法里最硬的一条就是「修复的输入是真实错误」——
    原来只知道「这条没过」，模型下一次还是同样的写法。

    `report` 是出参（会被填上）：几次草案、通过几条、**哪几条被删了/被删掉了什么**、
    修复又救回来几条。存进 `video.diagram_report` —— 只打日志的话，
    「这一集为什么没有图解」事后查不到。
    """
    from .design import DIAGRAM_MAX_SHOTS
    from .prompts.diagram import build_diagram_messages, normalize_diagram_shots

    def record(**fields: Any) -> None:
        if report is not None:
            report.update(fields)

    if llm is None or getattr(llm, "mock", True):
        record(skipped="没有可用的模型")
        return {}
    candidates = diagram_candidates(segments, timings)
    if not candidates:
        record(skipped="没有够长的候选段")
        return {}
    max_shots = DIAGRAM_MAX_SHOTS if max_shots is None else max_shots
    summary = summary_candidates(segments)
    valid_images = [item[0] for item in (available_images or [])]

    # 一次草案最多要几个 token：`1600` 实测会**被截断**（6 个镜头、每个带节点/连线/指标，
    # 输出跑到一半 JSON 就断了 → `parse_json_response` 报「不是合法 JSON」→ 整集没有图解）。
    # 这里给宽一点，并且截断/解析失败时**带着真实错误再问一次**（见下面 `draft(..., truncated=True)`）。
    draft_tokens = 3000

    def draft(repair: dict | None, *, truncated: bool = False) -> list[tuple[int, Any]]:
        messages = build_diagram_messages(
            segments=segments,
            evidence=evidence,
            available_images=available_images or [],
            candidates=candidates,
            summary_candidates=summary,
            max_shots=max_shots,
            language=language,
            timings=timings,
            repair=repair,
        )
        # ⚠️ 签名要和 `LLMClient._chat_json(messages, *, max_tokens, temperature)` 对齐：
        # 我第一版按别的调用点写了 `what=`/`retries=`，结果**每次生成都在视频合成这一步炸**
        # （被 `_compose_video_safely` 兜住 → 用户看到的是「音频有、视频没有」）。
        # 而且模型调用失败**不该拖垮出片**：图解只是锦上添花，问不到就当没有。
        if truncated:
            # **修复的输入是真实错误**：告诉它上次输出被截断了，这次少提几个、提短的
            messages = [
                messages[0],
                {
                    "role": "user",
                    "content": messages[1]["content"]
                    + "\n\n【上一次的输出被截断了（不是合法 JSON，没法解析）】"
                    "这次请**只提最重要的 3 个镜头**，每个镜头的节点/指标/要点各不超过 3 条，"
                    "标题不超过 12 个字，务必输出**完整合法**的 JSON。",
                },
            ]
        try:
            raw = llm._chat_json(messages, max_tokens=draft_tokens, temperature=0.3) or {}
        except Exception:  # noqa: BLE001  —— 拿不到草案就退回原画面，别让出片失败
            if not truncated:
                logger.warning("图解镜头草案生成失败，带原因再问一次（其余画面不受影响）")
                return draft(repair, truncated=True)
            logger.warning("图解镜头草案生成失败，本集不画图解（其余画面不受影响）", exc_info=True)
            return []
        return normalize_diagram_shots(raw, max_shots=max_shots, valid_images=valid_images)

    shots = draft(None)
    out, partial, rejected = _verify_diagram_drafts(shots, evidence=evidence)
    repair_record: dict[str, Any] = {"called": False}
    if rejected:
        # 只问一次、只问被作废的那几条（已经通过的镜头一个字都不重写）
        repair_record["called"] = True
        fixed: list[int] = []
        still: list[dict[str, Any]] = []
        repaired = draft({"shots": rejected})
        candidates_repair = [
            (index, shot) for index, shot in repaired if index in {item["segment"] for item in rejected}
        ]
        repaired_kept, repaired_partial, repaired_rejected = _verify_diagram_drafts(
            candidates_repair, evidence=evidence
        )
        out.update(repaired_kept)
        partial.extend(repaired_partial)
        fixed.extend(sorted(repaired_kept))
        still.extend(repaired_rejected)
        repair_record.update(
            {
                "attempted": [item["segment"] for item in rejected],
                "fixed": fixed,
                "still_rejected": [{"segment": item["segment"], "dropped": item["dropped"]} for item in still],
            }
        )
        logger.info(
            "图解镜头修复：问了 %d 条，救回 %d 条（%s）",
            len(rejected),
            len(fixed),
            "、".join(f"第 {i} 段" for i in fixed) or "无",
        )
    record(
        drafts=len(shots),
        kept=sorted(out),
        partial=partial,
        rejected=[
            {"segment": item["segment"], "title": item["title"], "dropped": item["dropped"]}
            for item in rejected
            if item["segment"] not in out
        ],
        repair=repair_record,
    )
    return out


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
    beats_for_segment: list[list[str]] | None = None,
    # **动态图解镜头**：`{段号: DiagramShot}`（由 `assign_diagram_shots` 产出，
    # 或从 `preset_scenes[i]["diagram"]` 读回来复用）。缺省/空 = 全部走原来的画面。
    diagram_shots: dict[int, Any] | None = None,
    beat_focuses: list[list[dict[str, Any] | None]] | None = None,
    headline: str = "",
    cover_caption: str = "",
    language: str = "zh",
) -> list[Scene]:
    """把脚本、时间戳和逐段配图拼成画面时间轴。

    `timings` 是 RoundTiming 列表（顺序与脚本段一一对应），
    里面已经包含了片头音乐的偏移，可以直接当绝对时间用。

    `image_for_segment[i]` 是第 i 段脚本要显示的图片 id —— 逐段指定而不是
    「从某段开始一直用到下一张图」，这样才能保证画面跟着话题走。

    `beats_for_segment[i][j]` 是第 i 段**第 j 句**选到的图片 id（句子由
    `split_caption_beats` 切出来，与编码侧同一套规则）。给了它就落成 `Scene.beats`，
    逐句换画面才有依据；不给（老数据 / 启发式兜底）就是空列表 = 整段一张图。
    **这一段第一句那张图必须等于 `image_for_segment[i]`**（`scene.image` 由后者来）。

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
                language=language,
                caption=head_asset.caption,
            )
        )

    # ---- 正文：逐段按语义配图 ----
    #
    # 逐句的选图结果先挂着（`body_beat_picks`），等整条时间轴定型之后再落成
    # `Scene.beats` —— 「封面段」与「这一句太短」这两条守卫要看的是**最终**的
    # headline 与时长（见函数末尾）。
    body_beat_picks: list[tuple[Scene, list[str], list[dict[str, Any] | None]]] = []
    for index, timing in enumerate(ordered):
        segment = segments[index] if index < len(segments) else {}
        asset = asset_for(index)
        scene = Scene(
            start=float(timing.start),
            end=float(timing.end),
            image=asset.path,
            kind=asset.kind,
            language=language,
            speaker=str(segment.get("speaker") or getattr(timing, "speaker", "") or "A"),
            text=str(segment.get("text") or ""),
            caption=asset.caption,
            point=(points[index] if points and index < len(points) else ""),
            focus=(focuses[index] if focuses and index < len(focuses) else None),
            why=(whys[index] if whys and index < len(whys) else ""),
            brand=str(segment.get("brand") or ""),
            # 图解镜头：有就画图解，没有就是原来的论文图/示意图。
            # 片尾品牌段不画（那里是牌子）；核验不过的镜头根本不会出现在这里。
            diagram=(
                (diagram_shots or {}).get(index)
                if str(segment.get("brand") or "") != "outro"
                else None
            ),
        )
        scenes.append(scene)
        if beats_for_segment is not None and index < len(beats_for_segment):
            picks = list(beats_for_segment[index])
            focus_picks = (
                list(beat_focuses[index])
                if beat_focuses is not None and index < len(beat_focuses)
                else [None] * len(picks)
            )
            body_beat_picks.append((scene, picks, focus_picks))

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
                language=language,
                caption=tail_caption,
                text="以上就是这篇论文的解读，感谢收听。",
                brand="outro",  # 收尾一律用品牌卡，不要用普通配图页结尾
            )
        )

    # 补齐空隙：相邻场景之间若有缝，前一个延长到下一个开始
    for index in range(len(scenes) - 1):
        if scenes[index].end < scenes[index + 1].start:
            scenes[index].end = scenes[index + 1].start

    # ---- 逐句画面：把逐句选图落成 `Scene.beats` ----
    #
    # 放在最后一步是因为守卫要的是**最终**值：封面段得等 headline 落到那几帧上
    # （见上面「封面帧」那一段），「这一句太短」得按补齐空隙之后的时长估。
    # 没有逐句选图（`beats_for_segment is None`：老数据复用、启发式兜底）时一句都不动，
    # `beats` 保持空列表 = 整段一张图，老视频的行为一点不变。
    for scene, picks, focus_picks in body_beat_picks:
        scene.beats = build_scene_beats(
            text=scene.text,
            picks=picks,
            duration=scene.duration,
            headline=scene.headline,
            brand=scene.brand,
            focuses=focus_picks,
        )

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


def build_scene_beats(
    *,
    text: str,
    picks: list[str] | None,
    duration: float,
    headline: str = "",
    brand: str = "",
    focuses: list[dict[str, Any] | None] | None = None,
) -> list[Beat]:
    """把**逐句**选图落成 `Scene.beats`，三条守卫在这里一次性施加。

    ⚠️ **耦合**：句子必须用 `split_caption_beats` 切 —— 编码侧（`_render_scene_layers`
    的字幕带、`beat_windows_aligned` 的换句时刻）用的是**同一个函数**，
    只有两边切法一致，`Scene.beats[i]` 才真的对应第 i 句。切句规则一改两边一起错位。

    三条守卫（见 `docs/BEAT_MOTION.md`「什么时候不切画面」，全部有测试）：

    - **封面段与片尾品牌段一律不切**（`headline` 非空 / `brand == "outro"`）：
      封面要始终是「论文首页 + 标题」，第一帧必须是论文首页是产品要求。
    - **和上一句同一张图 → 空**：图没变就别动，无意义的溶解只会让人以为卡了一下。
    - **太短的句子 → 空**：按字数估的时长 < `BEAT_IMAGE_MIN_SEC` 时几个字一闪而过，
      比不切更难看。时长估算用编码侧同一个 `beat_windows`。

    第 0 句**永远**是空：它就是这一段原本那张卡（段级转场负责它和上一段的差别），
    所以这里没有「第 0 句单独一层」这回事。

    `picks` 为 None（没有逐句数据）或句数对不上时返回**空列表** = 整段一张图。
    句数对不上说明调用方的切句与这里不一致（稿子变了 / 切句规则改了），
    这时宁可整段不切，也不能让第 i 句配到别人那一句的图上。
    """
    beats_text = split_caption_beats(text)
    if not beats_text:
        return []
    if picks is None:
        return []
    if len(picks) != len(beats_text):
        logger.warning(
            "逐句配图的句数对不上（图 %d / 句 %d），这一段不切画面", len(picks), len(beats_text)
        )
        return []

    # 封面段与片尾品牌段：整段不切（beats 照旧逐句给出来，image 全空）
    frozen = bool(headline) or brand == "outro"
    windows = beat_windows(beats_text, duration)

    beats: list[Beat] = []
    for index, beat_text in enumerate(beats_text):
        image = ""
        if not frozen and index > 0:
            changed = picks[index] != picks[index - 1]
            span = windows[index][1] - windows[index][0]
            if changed and span >= BEAT_IMAGE_MIN_SEC:
                image = picks[index]
        focus = focuses[index] if focuses and index < len(focuses) else None
        beats.append(Beat(text=beat_text, image=image, focus=focus))
    return beats


def _normalize_beat_focuses(
    raw: Any,
    *,
    beat_counts: list[int],
    image_picks: list[list[str]],
    panel_catalog: dict[str, FigurePanels],
) -> list[list[dict[str, Any] | None]]:
    """Map model-selected panel letters to measured, per-sentence focus boxes."""
    result = [[None for _ in range(max(int(count), 0))] for count in beat_counts]
    items = _flat_sentence_items(raw, beat_counts=beat_counts)
    if not items:
        return result
    offsets = _beat_offsets(beat_counts)
    for sentence, item in items:
        segment_index = next(
            (i for i, start in enumerate(offsets)
             if start <= sentence < start + max(int(beat_counts[i]), 0)),
            None,
        )
        if segment_index is None:
            continue
        beat_index = sentence - offsets[segment_index]
        picks = image_picks[segment_index] if segment_index < len(image_picks) else []
        image_id = picks[beat_index] if beat_index < len(picks) else ""
        catalog = panel_catalog.get(image_id)
        letter = str(item.get("panel") or "").strip().lower()
        if catalog is not None and letter:
            result[segment_index][beat_index] = catalog.focus_for(letter)
    return result


def _visual_quality_summary(
    *,
    segments: list[dict[str, Any]],
    timings: list[Any],
    image_picks: list[str],
    beat_picks: list[list[str]] | None,
    beat_focuses: list[list[dict[str, Any] | None]] | None,
    panel_focuses: list[dict[str, Any] | None],
    assignment_whys: list[str],
    assets: dict[str, ImageAsset],
    fallback_id: str,
) -> dict[str, Any]:
    """Summarize visual evidence coverage and surface weak assignments for review."""
    total_seconds = 0.0
    role_seconds: dict[str, float] = {}
    focused_seconds = 0.0
    fallback_seconds = 0.0
    review_samples: list[dict[str, Any]] = []

    for index, timing in enumerate(timings):
        if index >= len(segments):
            break
        duration = max(float(timing.end) - float(timing.start), 0.0)
        if duration <= 0:
            continue
        total_seconds += duration
        picks = beat_picks[index] if beat_picks and index < len(beat_picks) else []
        texts = split_caption_beats(str(segments[index].get("text") or ""))
        weights = [len(text) for text in texts]
        weight_total = sum(weights) or len(picks) or 1
        if picks and len(picks) == len(weights):
            rows = list(zip(picks, weights))
        else:
            image_id = image_picks[index] if index < len(image_picks) else ""
            rows = [(image_id, weight_total)]

        local_focuses = beat_focuses[index] if beat_focuses and index < len(beat_focuses) else []
        has_segment_focus = index < len(panel_focuses) and bool(panel_focuses[index])
        focus_weight = 0
        for beat_index, (asset_id, weight) in enumerate(rows):
            asset = assets.get(asset_id)
            kind = asset.kind if asset else "unknown"
            role = {
                "figure": "paper_figure",
                "table": "paper_table",
                "illustration": "generated_explanation",
                "cover": "paper_cover",
            }.get(kind, "fallback")
            seconds = duration * weight / weight_total
            role_seconds[role] = role_seconds.get(role, 0.0) + seconds
            if asset_id == fallback_id:
                fallback_seconds += seconds
            if beat_index < len(local_focuses) and local_focuses[beat_index]:
                focus_weight += weight
            if asset_id == fallback_id or kind == "unknown":
                review_samples.append({
                    "start_sec": round(float(timing.start), 2),
                    "text": str(segments[index].get("text") or "")[:100],
                    "visual_role": role,
                    "why": assignment_whys[index] if index < len(assignment_whys) else "",
                })
        if any(local_focuses):
            focused_seconds += duration * focus_weight / weight_total
        elif has_segment_focus:
            focused_seconds += duration
        if index < len(assignment_whys) and not assignment_whys[index]:
            review_samples.append({
                "start_sec": round(float(timing.start), 2),
                "text": str(segments[index].get("text") or "")[:100],
                "visual_role": "missing_assignment_rationale",
                "why": "",
            })

    ratio = lambda value: round(value / total_seconds, 3) if total_seconds else 0.0
    return {
        "basis": "duration_weighted_visual_plan",
        "speaking_seconds": round(total_seconds, 2),
        "role_seconds": {key: round(value, 2) for key, value in sorted(role_seconds.items())},
        "role_ratios": {key: ratio(value) for key, value in sorted(role_seconds.items())},
        "subfigure_focus_ratio": ratio(focused_seconds),
        "fallback_ratio": ratio(fallback_seconds),
        "assignment_rationale_ratio": round(
            sum(bool(str(why).strip()) for why in assignment_whys[:len(segments)])
            / max(len(segments), 1),
            3,
        ),
        "review_samples": review_samples[:12],
    }


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


def _render_diagram_card(
    scene: Scene, path: Path, *, layout: Layout, session: Any, state: int = 0
) -> Path:
    """图解镜头的「图片卡」= 图解的一个状态（有 Chrome 走 Chrome，没有就走 SVG）。"""
    from .diagram import render_diagram_state_html

    assert scene.diagram is not None
    return render_diagram_state_html(
        scene.diagram, path, layout=layout, state=state, session=session
    )


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


def _chip_delay_for(text: str, plan: list[tuple[int, float]]) -> float:
    """强调层要等它自己那个小句出现之后再点亮：算出相对本句起点的延迟。

    没有显露计划（短句、或 SVG 降级）时就是原来的 `CHIP_POP_DELAY`。
    有计划时：看被强调的数字落在第几个小句，取「第一个会显示到它的那个状态」的时刻，
    再加一个 `CHIP_POP_DELAY`（先读到那句话，再看到数字被点出来）。
    """
    from .design import CHIP_POP_DELAY

    if not plan:
        return CHIP_POP_DELAY
    clause = emphasis_clause_index(text)
    # ⚠️ 第 0 张带子也是一个「状态」，它的时刻是 0：那张带子的状态是「从第 1 个小句起藏起来」，
    # 所以第 0 个小句**从一开始就看得见**。少了这一步，数字落在第一小句里的句子
    # 会被推迟到第一次显露之后才点亮（实测：数字明明在第一句，却要等 4.2 秒）。
    for hidden_from, moment in [(1, 0.0), *plan]:
        if hidden_from > clause:
            return moment + CHIP_POP_DELAY
    return CHIP_POP_DELAY


def _diagram_fallback_card(
    scene: Scene, pool: dict[str, ImageAsset], *, layout: Layout, session: Any
) -> Path | None:
    """图解镜头收尾要切回的**论文原图**那一张卡（没有就返回 None）。

    只有 `DiagramShot.fallback_image` 指到一个**真实存在**的素材时才画：
    指到不存在的 id 就什么都不做（宁可一直停在图解的最后一个状态，也不要画一张空卡）。
    """
    if scene.diagram is None or not scene.diagram.fallback_image:
        return None
    asset = pool.get(scene.diagram.fallback_image)
    if asset is None or not Path(asset.path).exists():
        logger.info("图解收尾要切回 %s，但那张图不在素材池里，跳过", scene.diagram.fallback_image)
        return None
    card_path = Path(scene.image).parent / f"diagram-fallback-{Path(scene.image).stem}.png"
    render_image_card(
        replace(scene, image=Path(asset.path), headline="", diagram=None),
        card_path,
        layout=layout,
    )
    return card_path


def _render_band_layer(
    beat: str,
    path: Path,
    *,
    layout: Layout,
    session: Any,
    chip: bool = True,
    hidden_from: int | None = None,
) -> Path:
    """字幕带层：单独一层是为了「字幕逐句出现」（每句一条带子按时序叠上去）。

    `chip=False` = 这一句**先不要把数字胶囊画出来**（数字用普通文字颜色，位置不变）：
    它留给逐句强调层晚 0.12 秒在同一处点亮，否则强调层叠上去等于没叠
    （见 docs/BEAT_MOTION.md）。默认 `True`，一处没给就是原来的观感。

    `hidden_from=N` = 从第 N 个小句起先不显示（同一套排版、只把颜色变透明）——
    「按小句依次出现」用的就是它的 N 个变体（见 `video_core.plan_reveals`）。
    **SVG 降级路径不实现它**（理由见 `render_caption_band` 的注释）。
    """
    if session is None:
        return render_caption_band(beat, path, layout=layout, chip=chip)
    from .htmlpage import caption_band_page

    session.render(
        caption_band_page(beat, layout, chip=chip, hidden_from=hidden_from),
        path,
        work_dir=path.parent,
    )
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
    # **逐句**的两层（见 docs/BEAT_MOTION.md）：第 i 段第 j 句的一张图片卡 / 一个数字强调层。
    # 空列表 = 这一段没有逐句图层（老数据 `beats=[]` 天然如此）—— 编码侧拿到空列表
    # 走的就是改动前那条滤镜链，所以这两个字段的默认值必须是空列表而不是 `None`。
    beat_cards: list[list[Path | None]] = field(default_factory=list)
    beat_chips: list[list[Path | None]] = field(default_factory=list)
    # 逐句胶囊的**帧序列**（弹簧「弹一下」）。有序列时 `beat_chips` 那一格是 None：
    # 静态图和帧序列不能同时叠（序列播完会被静态图顶掉，看起来像跳了一下）。
    beat_chip_frames: list[list[list[Path]]] = field(default_factory=list)
    # 图解镜头的状态图：第 i 段的第 k 个状态（相对本段起点的秒数 + 图层）。
    # 与逐句卡分开：状态数由内容决定（几个节点就几步），与字幕句数无关。
    diagram_states: list[list[tuple[Path, float]]] = field(default_factory=list)
    beat_focuses: list[list[Path | None]] = field(default_factory=list)
    # 每一段的**版式**：图注/强调行跟着图的下沿走（`design.content_layout`）。
    # 渲染各层用它、编码叠层也用它 —— 「跟着图走」只有一个来源。
    # 空列表 = 全部用传入的基准版式（老调用点不受影响）。
    layouts: list[Any] = field(default_factory=list)
    endcard_count: int = 0
    beat_count: int = 0
    chip_count: int = 0
    backend: str = "svg"


def _scene_layout(scene: Scene, layout: Layout) -> Layout:
    """这一段的版式：**图注与强调行跟着图的下沿走**（`design.content_layout`）。

    图片卡本身不动 —— 动的只有它下面的两行字。这样：
    - 高瘦的图（铺满卡片）**逐字段不变**，老片子重合成一模一样；
    - 宽图（只占卡片中间一条）图注从「卡片底边下 6px」挪到「图下沿下 `FIGURE_CAPTION_GAP`」，
      不再出现「图在上面、说明在一百多像素之外」；
    - 图注跟着放大一档（18 → 22px）：反正那块竖空间也没别的用处，字大一点更好读。

    返回的版式会同时交给**渲染各层**和**编码叠层**，两边必须是同一个数。
    """
    return content_layout(
        layout,
        fitted_figure_height(scene, layout),
        has_caption=bool((scene.caption or "").strip()),
        has_point=bool((scene.point or "").strip()),
    )


def _render_scene_layers(
    scenes: list[Scene],
    layout: Layout,
    work_dir: Path,
    *,
    assets: dict[str, ImageAsset] | None = None,
    pauses: list[Any] | None = None,
) -> RenderedLayers:
    """把每一段画面渲染成「骨架 + 图片卡 + 字幕带 + 强调行 + 聚光灯」几层文件。

    分层的意义（见 AGENTS.md「转场与语音驱动的动效」一节）：骨架全程不动、
    只有图片卡在换，画面上就是「同一页里图在变」，而不是每段重画一整页。
    字幕再单独成层，才能做到「字幕逐句出现」。

    逐句那两层（`render_beat_layers`）也在这里渲染：它要**先**算出来，
    因为字幕带要不要画那颗数字胶囊取决于这一句有没有强调层（上面 `chip=`）。

    `assets` 是素材池（`Beat.image` 是素材 id，要换成真实文件路径），
    `pauses` 是说话停顿 —— `render_beat_layers` 算逐句窗口用的是和编码侧
    **同一个** `beat_windows_aligned`，不给就会按字数估，两边的「太短不切」判据会错位。
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    result = RenderedLayers(
        slides=[], caption_bands=[], band_paths=[], point_rows=[], point_paths=[],
        cards=[], card_paths=[], focus_rows=[], focus_paths=[], focus_frames=[],
        image_ids=[], beat_focuses=[],
    )

    with render_session(work_dir) as session:
        if session is not None:
            result.backend = "html"
        for index, scene in enumerate(scenes):
            # 这一段的版式：图注/强调行跟着**图的下沿**走（宽图让出来的竖空间不用钉在卡片底边）
            lay = _scene_layout(scene, layout)
            result.layouts.append(lay)
            slide_path = work_dir / f"slide-{index:04d}.png"
            beats = split_caption_beats(scene.text)
            bands: list[CaptionBand] = []
            card_path: Path | None = None
            # 逐句图层（见 docs/BEAT_MOTION.md）：**先算出来**，因为下面那条字幕带要不要
            # 画数字胶囊取决于这一句有没有强调层。片尾品牌卡既不分句也不叠图层，跳过。
            beat_layers = BeatLayers()
            if scene.brand != "outro":
                beat_layers = render_beat_layers(
                    scene,
                    index=index,
                    work_dir=work_dir,
                    layout=lay,
                    assets=assets,
                    pauses=pauses,
                    session=session,
                )
            result.beat_cards.append(list(beat_layers.cards))
            result.beat_chips.append(list(beat_layers.chips))
            result.beat_chip_frames.append([list(row) for row in beat_layers.chip_frames])
            beat_focus_paths: list[Path | None] = []
            for beat_index, focus in enumerate(beat_layers.focus_specs):
                if not focus:
                    beat_focus_paths.append(None)
                    continue
                source = beat_layers.focus_sources[beat_index]
                if source is None:
                    beat_focus_paths.append(None)
                    continue
                source_image, source_kind = source
                focus_scene = replace(
                    scene,
                    image=source_image,
                    kind=source_kind,
                )
                focus_path = work_dir / f"beat-focus-{index:04d}-{beat_index}.png"
                _render_focus_layer(
                    focus_scene, focus_path, focus=focus, layout=lay, session=session
                )
                beat_focus_paths.append(focus_path)
                result.focus_paths.append(focus_path)
            result.beat_focuses.append(beat_focus_paths)
            result.chip_count += sum(1 for chip in beat_layers.chips if chip is not None)
            result.chip_count += sum(
                1 for row in beat_layers.chip_frames if row
            )

            if scene.brand == "outro":
                # 片尾用品牌卡（深色 + logo + 关注引导），正文用普通白底页。
                # 品牌卡上的字是大号引导语，不参与「字幕逐句出现」——那会把它切碎。
                # 它整页都是牌子，没有「图片层」，转场用 to_card（上一整页淡出）代替。
                _render_endcard_layer(scene, slide_path, layout=lay, session=session)
                result.diagram_states.append([])
                result.endcard_count += 1
            else:
                # 正文：**骨架 + 图片卡** 两层。骨架（图注/强调行/字幕带）全程不动，
                # 只有图片卡在换 —— 这就是「连贯」的来源（见 render_chrome 的说明）。
                _render_chrome_layer(
                    scene,
                    slide_path,
                    include_subtitle=len(beats) <= 1,
                    include_point=False,
                    layout=lay,
                    session=session,
                )
                card_path = work_dir / f"card-{index:04d}.png"
                if scene.diagram is not None:
                    # 图解镜头：状态 0 是「标题 + 第一个节点」，后面的状态按时间点叠上去
                    _render_diagram_card(scene, card_path, layout=lay, session=session)
                    from .diagram import (
                        render_diagram_state_html,
                        shot_state_count,
                        state_moments,
                    )

                    states: list[tuple[Path, float]] = []
                    total = shot_state_count(scene.diagram)
                    # 状态**跟着讲稿的句子**出现（句子边界与字幕带同一组窗口，含真实停顿）
                    moments = state_moments(
                        scene.text,
                        scene.duration,
                        total,
                        pauses=pauses,
                        start=scene.start,
                    )
                    for state in range(1, total):
                        state_path = work_dir / f"diagram-{index:04d}-{state}.png"
                        render_diagram_state_html(
                            scene.diagram,
                            state_path,
                            layout=lay,
                            state=state,
                            session=session,
                        )
                        states.append((state_path, moments[state]))
                    # `fallback_image`：镜头讲完之后**切回论文原图**（只有摘要类镜头会给）。
                    # 这是产品要求的一条：图解讲的是「我们用它的数据画的东西」，
                    # 收尾得让观众看一眼**原文那张图**，知道这些数字是从哪来的。
                    # ⚠️ 这一层的素材池参数叫 `assets`（不是 `pool` —— `pool` 是
                    # `compose_video` 里的局部名）。写成 `pool` 会让**每次真出片**都在
                    # 这里 `NameError`，而它被 `_compose_video_safely` 兜住 →
                    # 用户看到的又是「音频有、视频没有」。
                    fallback = _diagram_fallback_card(
                        scene, assets or {}, layout=lay, session=session
                    )
                    if fallback is not None and states:
                        tail = max(0.0, scene.duration - DIAGRAM_FALLBACK_HOLD)
                        states.append((fallback, round(max(states[-1][1] + 0.6, tail), 2)))
                    result.diagram_states.append(states)
                else:
                    _render_card_layer(scene, card_path, layout=lay, session=session)
                    # ⚠️ **没有图解也要占一行**：编码侧是按**段号**取 `diagram_states[index]` 的，
                    # 少一行就整体错位 —— 实测踩过：这一集第 11 段的图解被叠到了**第 1 段**上
                    # （用户看到的是「第二个画面有问题，而且又跳回封面」：第一个图解把封面盖住，
                    # 下一段又是封面）。行数和段数必须永远 1:1。
                    result.diagram_states.append([])
                result.card_paths.append(card_path)
                if len(beats) > 1:
                    # 小句时间表和编码侧用的是**同一个** `beat_windows_aligned`
                    # （字幕带、逐句卡、强调层三处都是它），所以这里算出来的相对时刻
                    # 在编码那一侧加上 `beat_start` 就是绝对时刻，不会差一帧。
                    windows = beat_windows_aligned(
                        beats, scene.duration, start=scene.start, pauses=pauses
                    )
                    for beat_index, beat in enumerate(beats):
                        band_path = work_dir / f"band-{index:04d}-{beat_index}.png"
                        # 这一句有强调层 → 带子里先不画胶囊，留给强调层点亮（见 BEAT_MOTION）
                        has_chip = (
                            beat_index < len(beat_layers.chips)
                            and beat_layers.chips[beat_index] is not None
                        ) or (
                            beat_index < len(beat_layers.chip_frames)
                            and bool(beat_layers.chip_frames[beat_index])
                        )
                        # 字幕：**整句一次出现**（1~2 行），不再按小句分块。
                        # 开关在 `design.ENABLE_CLAUSE_REVEAL`（用户反馈「字幕不要分块」）。
                        beat_start, beat_end = windows[beat_index]
                        plan = (
                            plan_reveals(beat, beat_end - beat_start)
                            if (session is not None and ENABLE_CLAUSE_REVEAL)
                            else []
                        )
                        _render_band_layer(
                            beat,
                            band_path,
                            layout=lay,
                            session=session,
                            chip=not has_chip,
                            # 有显露计划时，第 0 张只显示第 0 个小句
                            hidden_from=1 if plan else None,
                        )
                        reveals: list[tuple[Path, float]] = []
                        for reveal_index, (hidden_from, moment) in enumerate(plan):
                            reveal_path = (
                                work_dir / f"band-{index:04d}-{beat_index}-r{reveal_index}.png"
                            )
                            _render_band_layer(
                                beat,
                                reveal_path,
                                layout=lay,
                                session=session,
                                chip=not has_chip,
                                hidden_from=hidden_from,
                            )
                            reveals.append((reveal_path, moment))
                            result.band_paths.append(reveal_path)
                        bands.append(
                            CaptionBand(
                                text=beat,
                                image=band_path,
                                reveals=tuple(reveals),
                                chip_delay=(
                                    _chip_delay_for(beat, plan) if has_chip else None
                                ),
                            )
                        )
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
                and not any(beat_layers.focus_specs)
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
                        layout=lay,
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
                        layout=lay,
                        session=session,
                    )
                    result.focus_rows.append(focus_path)
                    result.focus_paths.append(focus_path)
            else:
                result.focus_rows.append(None)
            result.focus_frames.append(frames)

            if scene.point and scene.brand != "outro":
                point_path = work_dir / f"point-{index:04d}.png"
                _render_point_layer(scene.point, point_path, layout=lay, session=session)
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
    max_topic_images: int = 12,
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
    panel_catalog: dict[str, FigurePanels] = {}

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

    # 逐句切分：**必须**用 split_caption_beats。
    #
    # ⚠️ 耦合：编码侧（`_render_scene_layers` 的字幕带、`beat_windows_aligned` 的换句时刻）
    # 用的是**同一个**函数，所以两边句数与顺序天然一致，`Scene.beats[i]` 就是第 i 句。
    # 切句规则一改两边一起错位 —— 切句这件事只能有这一个出处（见 docs/BEAT_MOTION.md）。
    beat_texts = [
        split_caption_beats(str(segment.get("text") or "")) for segment in segments
    ]
    beat_counts = [len(texts) for texts in beat_texts]

    # 逐句语义匹配：优先让模型判断「这句话在讲什么、哪张图正好在讲同一件事」
    image_for_segment: list[str] | None = None
    # 逐句选图：`beat_picks[i][j]` = 第 i 段第 j 句选到的图。
    # **None 表示没有逐句数据**（老数据复用 / 启发式不猜）。这时 `build_scenes` 一句都不切，
    # `Scene.beats` 保持空列表 = 整段一张图（老行为）。
    beat_picks: list[list[str]] | None = None
    beat_focuses: list[list[dict[str, Any] | None]] | None = None
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

        # 逐句画面**一起复用**（规矩 3：重新合成不许调模型）。逐句配图是「选图」那一步的
        # 产物，跟 `scene.image` 一样存下来复用，否则「只是转了个配图」会让画面分配全变。
        # **老数据没有 beats** → 保持 None，`build_scenes` 走整段一张图那条路，一点不变。
        if any(isinstance(sc, dict) and sc.get("beats") for sc in preset_scenes):
            reused_beats: list[list[str]] = []
            reused_focuses: list[list[dict[str, Any] | None]] = []
            dropped = 0
            for index in range(len(segments)):
                raw_beats = (stored.get(index) or {}).get("beats")
                picks: list[str] = []
                focus_picks: list[dict[str, Any] | None] = []
                if isinstance(raw_beats, list) and len(raw_beats) == beat_counts[index]:
                    for beat_index, beat in enumerate(raw_beats):
                        if not isinstance(beat, dict):
                            continue
                        # 稿子/切句规则变了的话，存下来的句子对不上，整段不切（宁可不动）
                        if str(beat.get("text") or "") != beat_texts[index][beat_index]:
                            picks = []
                            dropped += 1
                            break
                        asset_id = str(beat.get("image") or "")
                        # 图被删了就按「这一句不切」处理（沿用本段那张），
                        # 比塞一张别的图更贴近当初的选择
                        picks.append(
                            asset_id if asset_id in pool and pool[asset_id].exists else ""
                        )
                        focus_picks.append(normalize_focus(beat.get("focus")))
                elif beat_counts[index]:
                    dropped += 1
                reused_beats.append(picks or [""] * beat_counts[index])
                reused_focuses.append(focus_picks or [None] * beat_counts[index])
            beat_picks = reused_beats
            beat_focuses = reused_focuses
            reused_switches = sum(1 for picks in reused_beats for pick in picks if pick)
            logger.info(
                "逐句画面：复用上次的 %d 处换图（%d 段）", reused_switches, len(reused_beats)
            )
            if dropped:
                logger.info("其中有 %d 段的逐句分配已经对不上当前稿子，退回整段一张图", dropped)

    elif llm is not None and not getattr(llm, "mock", True):
        panel_catalog = build_panel_catalog(figures, pool)
        assets = [
            {"id": asset.id, "caption": asset.caption}
            for asset in pool.values()
        ]
        total_beats = sum(beat_counts)
        panel_options = {
            asset_id: catalog.options() for asset_id, catalog in panel_catalog.items()
        }
        try:
            data = llm._chat_json(
                build_assign_messages(
                    segments,
                    assets,
                    beats_per_segment=beat_texts,
                    panel_options=panel_options,
                ),
                # 单位是**句**（以前是段）：每句一项，每项除了 id 还多一句 why
                # （中文 20 字 ≈ 25 token），所以预算按**句数**给 110/句 ——
                # 卡着 60 会让长稿的尾部整段缺项，而缺项的表现是「后半程画面回到中性图」，
                # 看起来像模型没干活。
                max_tokens=max(2500, total_beats * 110),
                temperature=0.2,
            )
            candidate_beats = _normalize_per_beat(
                data.get("assignments"),
                beat_counts=beat_counts,
                valid_ids=set(pool),
                default_id=default_id,
            )
            if candidate_beats:
                beat_picks = candidate_beats
                beat_focuses = _normalize_beat_focuses(
                    data.get("assignments"),
                    beat_counts=beat_counts,
                    image_picks=candidate_beats,
                    panel_catalog=panel_catalog,
                )
                # `scene.image` 仍然是这一段**第一句**选到的那张：老逻辑
                # （video.scenes[i].image）、复用/重建那条链、TestPresetReuse 全都依赖它，
                # 逐句的差别只落在 `Scene.beats` 上。
                image_for_segment = [
                    picks[0] if picks else default_id for picks in candidate_beats
                ]
                strategy = "model"
                assignment_whys = _beat_notes_by_segment(
                    data.get("assignments"),
                    beat_counts=beat_counts,
                    valid_ids=set(pool),
                )
                distinct = len(set(image_for_segment))
                switches = sum(
                    1
                    for picks in candidate_beats
                    for index in range(1, len(picks))
                    if picks[index] != picks[index - 1]
                )
                logger.info(
                    "逐句配图由模型完成：%d 段 %d 句用了 %d 张不同的图（%d 句与前一句不同）",
                    len(image_for_segment),
                    total_beats,
                    distinct,
                    switches,
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
            logger.warning("模型逐句配图失败，退回均匀分布：%s", exc)

    # ---- 同一张论文原图不许连续覆盖太久（确定性兜底） ----
    #
    # 用户报「文本和图像还是无法对应」，量出来的根因是 f2 一张图盖了 14 段 / 101 秒，
    # 而这 101 秒里解说走过了 8 个话题。模型每段的 why 都具体且不同（它认真判了），
    # 但 6 张原图对 41 段，它只能反复挑最像的那张。prompt 那边收紧判据，
    # 这里保证**上限**：超出的段落改判 `generate`，交给现场生成那条线去画。
    if image_for_segment is not None:
        image_for_segment, beat_picks = _apply_figure_run_cap(image_for_segment, beat_picks)

    if image_for_segment is None:
        image_for_segment = heuristic_per_segment(
            len(segments), figure_ids, default_id
        )
        # 逐句兜底：**沿用本段那一张**，不在本地猜哪句该换图（那是编，见 heuristic_per_beat）。
        beat_picks = heuristic_per_beat(image_for_segment, beat_counts)
        beat_focuses = [[None] * count for count in beat_counts]
        logger.info("逐句配图使用均匀分布（退化方案，不保证图文相符，逐句沿用本段那张）")

    # ---- 现场生成的配图：单位仍然是**段** ----
    #
    # ⚠️ 单位变了：`generate` 现在是**逐句**选出来的（以前只有逐段）。
    # 同一段里「一句有原图、一句没原图」很常见，直接按句喂给 group_generate_runs
    # 会让生成组数随句数放大 —— 每组一次模型调用（实测约 10 秒 / 3000+ token），
    # 成本就失控了。所以先把**同一段内**的 generate 合并到整段：同一段里的句子本来
    # 就在讲同一件事（选图就是按「这句在讲什么」选的），整段共用一张现场生成的图。
    # 合并之后 `group_generate_runs` / `merge_runs_to_cap` / `max_topic_images`(默认 12)
    # 的「单位」仍然是**段**，量级与合并前一致。
    if beat_picks is not None:
        collapsed = collapse_generate_within_segments(beat_picks)
        if collapsed != beat_picks:
            logger.info(
                "同一段内的 generate 已合并到整段：%d 段（按句算有 %d 句选了 generate）",
                sum(1 for before, after in zip(beat_picks, collapsed) if before != after),
                sum(1 for picks in beat_picks for pick in picks if pick == GENERATE_ID),
            )
            # 整段改成 generate 的，段级那张图（`scene.image`）也跟着改成 generate，
            # 否则 `scene.image` 会和 `beats` 打架（段级那张图与逐句的安排对不上）。
            # ⚠️ 不能拿 `beats[0]` 反推段级那张图：第 0 句的 image **恒为空**
            # （它就是这一段原本那张卡），拿它反推会把 scene.image 清成空串。
            for index, (before, after) in enumerate(zip(beat_picks, collapsed)):
                if before != after and after and index < len(image_for_segment):
                    image_for_segment[index] = after[0]
        beat_picks = collapsed

    # ---- 没有对应原图的段落：现场生成专门的配图 ----
    #
    # 以前这些段落统一挂同一张概括全图的信息图，实测 21 段里有 10 段都是它，
    # 画面单调且并不真的对应内容。改成按话题生成后，每张图都只讲那一段的事。
    runs = group_generate_runs(image_for_segment)
    generated = 0
    if runs:
        runs = split_long_runs(runs)
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
                if beat_picks is not None and index < len(beat_picks):
                    # 这一段的每一句都指向同一张（上面已经按段合并过，句里那几张是同一个话题）
                    beat_picks[index] = [asset.id] * len(beat_picks[index])

    # 生成失败或未生成的部分退回中性图，保证不会出现空画面
    image_for_segment = [
        pick if pick != GENERATE_ID else default_id for pick in image_for_segment
    ]
    if beat_picks is not None:
        beat_picks = [
            [pick if pick != GENERATE_ID else default_id for pick in picks]
            for picks in beat_picks
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

    # 图解镜头：**重建时复用**（规矩同 point/hook：重建不该换画面，也不该再花一次调用）。
    # 只有「没有 preset」（= 第一次生成）时才可能去问模型。
    diagram_shots: dict[int, Any] = {}
    # 「这一集为什么有/没有图解」的台账：存进 `video.diagram_report`（只打日志是查不到的）
    diagram_report: dict[str, Any] = {}
    if preset_scenes:
        for entry in preset_scenes:
            index = entry.get("index")
            shot = diagram_shot_from_dict(entry.get("diagram"))
            if shot is not None and isinstance(index, int) and 0 <= index < len(segments):
                diagram_shots[index] = shot
        if diagram_shots:
            logger.info("图解镜头：复用上次的 %d 段", len(diagram_shots))
    elif allow_point_llm and llm is not None:
        diagram_shots = assign_diagram_shots(
            segments=segments,
            timings=timings,
            evidence=diagram_evidence(analysis, segments, figures),
            llm=llm,
            available_images=[
                (asset_id, str(asset.caption or "")) for asset_id, asset in pool.items()
            ],
            report=diagram_report,
        )

    if (
        allow_point_llm
        and any(not point for point in points)
        and llm is not None
        and not getattr(llm, "mock", True)
    ):
        # 这几张图有子图清单可给模型挑（几何已经由像素量好了，见 build_panel_catalog）
        if not panel_catalog:
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
        diagram_shots=diagram_shots,
        fallback_id=default_id,
        points=points,
        focuses=focuses,
        whys=assignment_whys,
        # 逐句选图 → `Scene.beats`（三条守卫在 build_scene_beats 里施加）
        beats_for_segment=beat_picks,
        beat_focuses=beat_focuses,
        headline=cover_headline,
        cover_caption=title,
        language=language,
    )

    layout = layout_for(orientation)
    # 说话停顿：用来把字幕换句对准真实的停顿（检测失败就是空列表 → 退回按字数）。
    # **必须在渲染图层之前拿到**：`render_beat_layers` 里的「这一句太短不切」用的是
    # `beat_windows_aligned` —— 编码侧同一个函数、同一组停顿，两边算出的窗口必须一致，
    # 否则会出现「渲染时判它够长、编码时又跳过」（白渲染一堆用不上的卡）。
    pauses = detect_pauses(audio_path)
    if pauses:
        logger.info("检测到 %d 处说话停顿，字幕换句将对齐到停顿", len(pauses))

    layers = _render_scene_layers(scenes, layout, work_dir, assets=pool, pauses=pauses)
    image_ids = layers.image_ids

    transitions = plan_transitions(scenes, image_ids, cards=layers.cards, slides=layers.slides)
    # 有「聚光灯移过来」的那一段**取消溶解**：入场交给聚光灯的运动。
    # 两套动效同时演（图在溶、框在走）看着就是「画面在抖」，而且观众不知道该看哪。
    for frame_index, frames in enumerate(layers.focus_frames):
        if frames and frame_index < len(transitions):
            transitions[frame_index] = Transition()
    moved = sum(1 for item in transitions if item.kind not in ("", "none"))
    logger.info(
        "已渲染 %d 帧画面（其中片尾品牌卡 %d 帧，字幕分句 %d 段，逐句卡 %d 个，"
        "数字强调 %d 个，转场 %d 处）",
        len(layers.slides), layers.endcard_count, layers.beat_count,
        sum(1 for row in layers.beat_cards for card in row if card is not None),
        layers.chip_count, moved,
    )

    # 说话停顿在上面（渲染图层之前）已经算过了
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
        # **逐句**的两层（见 docs/BEAT_MOTION.md）：少了这两个参数，上面渲染出来的
        # 逐句卡与数字强调层就只是躺在磁盘上 —— 这一步才是「动效真的进片」的那根线。
        beat_cards=layers.beat_cards,
        beat_chips=layers.beat_chips,
        beat_chip_frames=layers.beat_chip_frames,
        diagram_states=layers.diagram_states,
        beat_focus_overlays=layers.beat_focuses,
        layout=layout,
        # 每段自己的版式：图注/强调行跟着图的下沿走（渲染用的是同一份，见 `_scene_layout`）
        scene_layouts=layers.layouts,
    )
    result.assignment = strategy
    result.renderer = layers.backend
    # 正文场景与 segments 一一对应：片头封面帧与片尾品牌卡都没有主播（`speaker` 为空），
    # 所以「有主播」这个选择器筛出来的就是正文（老测试里也是这么筛的）。
    body_scenes = [scene for scene in scenes if scene.speaker]
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
            # **动态图解镜头也要存**：`build_scenes` 会从 `preset_scenes[i]["diagram"]` 读回来
            # 复用（和 `point` / `hook` 同一条规矩）。漏了它，重新合成就会**静默丢掉图解** ——
            # 少的不只是几张图，还有「重新合成不该换画面」这条保证。
            "diagram": (
                diagram_shot_to_dict(body_scenes[index].diagram)
                if index < len(body_scenes) and body_scenes[index].diagram is not None
                else None
            ),
            # **逐句画面**一起存：它和 image 一样是「选图」那一步的产物，
            # 重新合成时复用（规矩 3：重新合成不许调模型），画面才不会因为重做而全变。
            # 存的是 build_scene_beats 施加过守卫之后的结果（真正的画面安排）。
            "beats": [
                {
                    "text": beat.text,
                    "image": beat.image,
                    **({"focus": beat.focus} if beat.focus is not None else {}),
                }
                for beat in (body_scenes[index].beats if index < len(body_scenes) else [])
            ],
        }
        for index in range(min(len(segments), len(image_for_segment)))
    ]
    visual_summary = _visual_quality_summary(
        segments=segments,
        timings=timings,
        image_picks=image_for_segment,
        beat_picks=beat_picks,
        beat_focuses=beat_focuses,
        panel_focuses=focuses,
        assignment_whys=assignment_whys,
        assets=pool,
        fallback_id=default_id,
    )
    # 「图解镜头这一集的台账」：提了几条、哪几条被核验删了些什么、修复救回几条。
    # 只打日志的话，「这一集为什么一个图解都没有」事后查不到（`diagram_report` 进 video_json）。
    if diagram_report:
        result.diagram_report = diagram_report
    logger.info(
        "画面质量概览：论文图表 %.0f%%，生成示意 %.0f%%，子图聚焦 %.0f%%，配图理由 %.0f%%",
        100 * sum(
            visual_summary["role_ratios"].get(role, 0.0)
            for role in ("paper_figure", "paper_table")
        ),
        100 * visual_summary["role_ratios"].get("generated_explanation", 0.0),
        100 * visual_summary["subfigure_focus_ratio"],
        100 * visual_summary["assignment_rationale_ratio"],
    )
    result.assets = {asset_id: str(asset.path) for asset_id, asset in pool.items()}
    result.asset_versions = {
        asset_id: asset_version(asset.path) for asset_id, asset in pool.items()
    }
    result.hook = hook
    # **成片自查**（最长静止 / 响度 / 音画长度差）。它扫一遍片子约 1.7 秒（2fps 抽样），
    # 相对 40~95 秒的编码可以忽略；而这三件事**只有成片里看得出来** ——
    # 分层测试能证明「字幕会换」，证明不了「整条片子里有 10 秒什么也没发生」。
    # 自查绝不让出片失败：`qa_video` 自己吞掉所有异常，只回报测得的部分。
    try:
        report = qa_video(output_path, audio_path=audio_path)
        result.qa = report.to_dict()
        logger.info("成片自查：%s", report.summary())
        for issue in report.issues():
            logger.info("成片自查 · 待改：%s", issue)
    except Exception:  # pragma: no cover - 自查本身不该影响出片
        logger.warning("成片自查失败（不影响出片）", exc_info=True)

    # ⚠️ **顺序有讲究**：上面那一步是 `result.qa = report.to_dict()`（**整份替换**），
    # 所以「画面计划概览」和「接缝审计」都必须写在它**之后**。
    # 原来 `visual_plan` 写在它之前 —— 算出来了、日志也打了，但**从来没进过库**
    # （同一类：算过、打过日志、没存下来，和 `video.scenes[i].diagram` 那个 bug 一个模子）。
    result.qa["visual_plan"] = visual_summary

    # **接缝审计**（只读）：逐切点量「切点两侧的动对不对得上」。
    # `video_qa` 只能回答「画面有没有卡住」（≥4 秒无变化），回答不了「切得顺不顺」——
    # 而用户报过的「一直在反复切换、和语音对不上、有抖动」全在后者。
    # 判据来自 HyperFrames 的影片法则（见 `docs/VIDEO_MOTION.md`），**先只量不拦**：
    # 我们目前是「静态卡 + 叠化」的讲解风格，它会在多数切点命中，得先在更多成片上标定。
    # 成本实测：304 秒的片子整趟解码 + 量 39 处切点 ≈ 2.0 秒。
    try:
        from .seam_audit import audit_seams

        cuts = [
            scene.start
            for scene in scenes[1:]
            if scene.start > 0.7 and scene.start < (audio_duration - 0.7)
        ]
        if cuts:
            seam_report = audit_seams(output_path, cuts)
            result.qa["seams"] = seam_report.to_dict()
            logger.info("%s", seam_report.summary())
            # 最差的几处点名（只提示，不拦）：整套 13 集实测下来 398/398 处切点
            # **两侧都没在动**，所以这里现在几乎每集都会点满 3 条 —— 它不是判决，
            # 是「画面语法有没有变」的基线（运动幅度中位数 0.09，判「没在动」的阈值 1.0）。
            for seam in [item for item in seam_report.seams if item.issues()][:3]:
                logger.info("接缝审计 · 待改：%.1fs %s（%s）", seam.at, seam.kind,
                            "；".join(seam.issues()[:2]))
    except Exception:  # pragma: no cover - 审计本身不该影响出片
        logger.warning("接缝审计失败（不影响出片）", exc_info=True)

    result.poster_path = str(
        capture_poster(
            output_path,
            output_path.with_name(output_path.stem + ".poster.png"),
            # 片头第一帧带 fade_in（从白底淡入 0.35 秒），所以取第 1 段画面**中间**的时刻：
            # 既躲过淡入，也不会滑到第二段画面里（片头段通常有十几秒，足够）
            at=min(1.5, max(scenes[0].duration / 2, 0.4)) if scenes else 1.0,
        )
    )

    # 中间图层编码完就没用了，删掉省磁盘。**逐句的那两层也要删** ——
    # 它们是这一轮新增的中间产物，一段两句就是两三个文件，一集几十段攒下来不少
    # （`chip-*.png` 之前没人删，实测跑一次测试就留下好几个）。
    for temp_path in [
        *layers.slides,
        *layers.band_paths,
        *layers.point_paths,
        *layers.card_paths,
        *layers.focus_paths,
        *(card for row in layers.beat_cards for card in row if card is not None),
        *(chip for row in layers.beat_chips for chip in row if chip is not None),
        *(frame for row in layers.beat_chip_frames for frames in row for frame in frames),
        # ⚠️ 生成器的元素表达式要用**同一个**变量名：写成 `path`（而循环变量是 `_path`）
        # 会在这一步 `NameError`。它发生在 `compose_video` 的**最后一步（清理临时文件）**，
        # 也就是说 MP4 其实已经编好了，却被这个异常连累：视频没写进库、
        # 页面显示「没有视频」。实测就是这么连着丢掉好几集的视频的。
        *(item[0] for row in layers.diagram_states for item in row),
    ]:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass

    return result


# --------------------------------------------------------------------------
# 「没有对应原图」的段落：现场生成专门的配图
# --------------------------------------------------------------------------


def collapse_generate_within_segments(
    beat_picks: list[list[str]],
) -> list[list[str]]:
    """把**同一段内**的 `generate` 合并到整段：一段里只要有一句要现场生成，整段都算它。

    为什么需要这一步：单位从「段」变成「句」之后，`generate` 是**逐句**选的 ——
    同一段里「一句挑到原图、另一句挑不到」很常见。按句喂给 `group_generate_runs`
    会切出一堆只在段内连续的小组，组数随句数放大，而**每组一次模型调用**
    （实测每张约 10 秒 / 3000+ token），成本和耗时立刻失控。

    同一段里的句子本来就在讲同一件事（选图就是按「这句在讲什么」挑的），
    所以整段共用一个「现场生成的图」是合理的近似；合并之后组数 ≤ 段数，
    与逐段时代的量级一致（见 `docs/BEAT_MOTION.md`）。
    """
    collapsed: list[list[str]] = []
    for picks in beat_picks:
        if GENERATE_ID in picks:
            collapsed.append([GENERATE_ID] * len(picks))
        else:
            collapsed.append(list(picks))
    return collapsed


def _apply_figure_run_cap(
    picks: list[str], beat_picks: list[list[str]] | None
) -> tuple[list[str], list[list[str]] | None]:
    """把「一张原图连续盖太久」兜住：超出的段落改判 `generate`，并让逐句那层跟着段走。

    抽成函数是为了能直接测（`TestOneFigureCannotCoverAHundredSeconds`）——
    内联在 `compose_video` 里就只能靠跑一整条出片去验证，那太慢也太脆。
    """
    capped = cap_figure_runs(picks)
    if capped == picks:
        return picks, beat_picks
    changed = [i for i, (before, after) in enumerate(zip(picks, capped)) if before != after]
    logger.info(
        "同一张原图连续覆盖超过 %d 段，%d 段改判现场生成配图：%s",
        FIGURE_RUN_MAX_SEGMENTS,
        len(changed),
        "、".join(f"第 {i} 段" for i in changed[:6]) + ("…" if len(changed) > 6 else ""),
    )
    # 逐句那层跟着段走（否则 `scene.image` 会和 `beats` 打架：段级说生成、
    # 逐句还指着原图，画面就按原图走，等于没改）
    if beat_picks is not None:
        for index in changed:
            if index < len(beat_picks):
                beat_picks[index] = [GENERATE_ID] * len(beat_picks[index])
    return capped, beat_picks


def cap_figure_runs(picks: list[str], *, max_run: int = FIGURE_RUN_MAX_SEGMENTS) -> list[str]:
    """同一张**论文原图**连续覆盖超过 `max_run` 段时，多出来的段落改判 `generate`。

    这是**确定性兜底**，和 prompt 里那条「同一张图最多连续三段」是同一件事的两道保险：
    模型层负责判「这段话说的东西这张图有没有」，这里负责保证「一张图不会讲一百秒」——
    后者是能算的，前者不能。

    三条豁免（都是有意的，不是漏掉）：
    - `cover`：片头那几帧顶着封面标题，本来就该停在同一张首页上；
    - `illustration`：概括全文的信息图，只做中性过渡画面；
    - `generate`：已经是现场生成，它自己按「连续几段合并成一张」处理。
    另外片头 / 片尾那些段（`brand` 非空）不参与 —— 那是品牌话术，画面本来就不切。
    """
    out = list(picks)
    index = 0
    while index < len(out):
        pick = out[index]
        end = index
        while end + 1 < len(out) and out[end + 1] == pick:
            end += 1
        run = end - index + 1
        if (
            run > max_run
            and pick not in ("cover", "illustration", GENERATE_ID)
        ):
            for position in range(index + max_run, end + 1):
                out[position] = GENERATE_ID
        index = end + 1
    return out


def group_generate_runs(picks: list[str]) -> list[tuple[int, int]]:
    """把连续的 generate **段落**合并成 [起, 止) 区间。

    ⚠️ **单位是段**：入参是逐段的 `image_for_segment`，不是逐句的选图 ——
    逐句的 `generate` 在调用之前已经被 `collapse_generate_within_segments`
    合并到整段了（否则组数会随句数放大，见那个函数的说明）。

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


def split_long_runs(
    runs: list[tuple[int, int]], *, max_len: int = FIGURE_RUN_MAX_SEGMENTS
) -> list[tuple[int, int]]:
    """把过长的生成区间**切开**，让每张现场生成的图最多覆盖 `max_len` 段。

    为什么必须切：`group_generate_runs` 会把**连续**几段的 `generate` 合并成一组、
    生成一张图 —— 那是对的（连续几段通常在讲同一件事）。但如果那一段连续很长
    （实测把「一张原图盖 14 段」兜住之后，剩下的 11 段会连成一组），
    生成出来的就又是**一张图讲 80 秒**，只不过从「原图对不上」变成「生成的图对不上」。

    `merge_runs_to_cap` 只合并不拆分（它管的是「组数超过上限」），所以这里补上拆分那半边。
    实测单张生成图约 10 秒 / 3000+ token，所以段均 7.3 秒下 3 段 ≈ 22 秒一张，
    成本与「每段一张」不是一个量级。
    """
    if max_len <= 0:
        return runs
    out: list[tuple[int, int]] = []
    for start, end in runs:
        position = start
        while position < end:
            out.append((position, min(position + max_len, end)))
            position += max_len
    return out


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
