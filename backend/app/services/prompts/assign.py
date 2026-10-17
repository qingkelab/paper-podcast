"""逐句选图：为脚本的**每一句**挑一张配图，并写一句「为什么配这张」。

单位是**句**，不再是段（一段 2~4 句、10~20 秒，以前整段只有一张图，
于是「一句话讲完了画面还停在那儿」，见 `docs/BEAT_MOTION.md`）。
选图仍然只调一次模型，只是脚本行从「逐段」换成「逐句」。
`scene.image` 仍然是这一段**第一句**选到的那张（老逻辑、复用/重建那条链都依赖它），
逐句的差别落在 `Scene.beats` 上。

句子怎么切不在这个文件里：`build_assign_messages` 收调用方切好的句子
（`video.split_caption_beats`，纯文本、确定性）—— **编码侧用的是同一个函数**，
两侧句数必须一致，所以切句这件事只能有一个出处。

`_normalize_per_beat` / `_normalize_per_segment` / `heuristic_*` 是兜底：
模型给不出、或者返回的对不上时，不能让视频缺画面，所以有确定性的启发式分配。
"""

from __future__ import annotations

from typing import Any

from ..design import GENERATE_ID
from .units import output_language_rule

__all__ = [
    "ASSIGN_WHY_MAX_CHARS",
    "ASSIGN_SYSTEM",
    "build_assign_messages",
    "heuristic_assignment",
    "heuristic_per_beat",
    "heuristic_per_segment",
]


# --------------------------------------------------------------------------

# 「这一段为什么配这张图」的长度上限。prompt 里写的是 20 个字，这里留一倍余量：
# 它是**给人看的复查依据**，模型偶尔写长一点不该被拦；但也不能放任它塞一整段进来
# （那会把 `scenes` 撑大，而 `video.scenes` 是随单集走数据库的）。


ASSIGN_WHY_MAX_CHARS = 40


ASSIGN_SYSTEM = """你在为「论文解读播客」的视频版做图文编排。

听众能听到主播的对话，同时看到画面。你的任务是让**每一段里的每一句**都配上意思相符的图：
听众听到某个概念时，屏幕上正好是讲这个概念的图。图不对文比没有图更糟。

你会拿到：
- 脚本**逐句**列表（每行前面是**句号**；行里标着这句属于第几段、第几句，以及主播）
- 可用图片列表（id 与图注）
- 部分论文原图附有可确认的子图清单（字母与说明）

请为**每一句**指定一张图。判断依据是这句话在讲什么，以及哪张图正好在讲同一件事。

【重要规则】
- 图注可能是英文，脚本是中文，你要按**语义**判断，不要按字面。
- 相邻几句如果确实在讲同一件事，就**填同一张图**：图没变的那一句本来就不会切画面，
  系统会自己跳过，所以重复填一张图不花任何代价；**话题变了就换图** ——
  画面跟着话走是这条流水线的目的，这一句换了对象/环节/结论时就别再沿用上一张。

- **「对得上」的判据是「这句话讲的东西能不能在这张图上看见」，不是「话题属于这篇论文」。**
  只要这一句在讲具体机制、具体数字、具体某一步，就要问自己：**那一步/那个数在图上画出来了吗？**
  没有画出来就是**对不上**，要填 `generate`，不要因为「这是同一篇论文的总览图」就沿用。
  典型的错法（真实例子）：讲「相关性分数怎么算」时沿用了一张**算法流程示意图** ——
  流程图上只有「打分 → 选子树」两个方块，分数怎么算在图上根本没有。这种要填 `generate`。
- **同一张论文原图最多连续覆盖 3 段**（约 20 秒）。再往后即使话题仍然相关，也要填 `generate`
  换一张专门画的图 —— 观众盯着一张不变的图听三个以上的话题，就会觉得「图文对不上」。
  系统还会做一次确定性的兜底：连续超过 3 段的部分一律改判 `generate`，所以你不如自己判准。
- 若某一句的内容和任何一张图都对不上，就填 `generate` —— 系统会为它
  **现场画一张专门的示意图**。连续几句讲同一件事时都填 `generate`，
  系统会把它们合并成一张图，不要隔一句就标一个。
- `illustration` 是一张概括全文的信息图，只在「需要一个中性过渡画面」时用
  （例如开场收尾）。**不要为了填满而硬塞一张不相关的原图。**
- `cover` 是论文首页，适合开场介绍论文时用；正文讨论具体内容时不要用它。
- 能对上论文原图的句子**优先用原图**（原图最准确）；原图对不上才填 `generate`。
- 若这句话明确讲到某张原图中的某个子图，填该子图字母；只有清单里存在且语义明确时才填，
  不确定就留空。不要根据图注以外的信息猜字母。信息图和现场生成图不填子图。
- 片头那几句（画面上顶着封面标题）与片尾的品牌收尾段**不用换图**：
  全部填这一段第一句用的那张就行（系统在那几帧本来就不切画面）。
- 图片 id 只能从给出的列表里选，不要编造。
- 每一项还要写一句 `why`：**这一句和这张图的共同点是什么**。
  它是给人看的检查依据 —— 复查时只看 `why` 就能判断选得对不对，不必重读全稿。
  一句话，不超过 20 个字，不要复述图注、不要写「内容相关」这种空话。

只输出 JSON：
{"assignments": [{"sentence": 0, "image_id": "cover", "panel": "", "why": "开场点出论文主题"},
                 {"sentence": 1, "image_id": "f1", "why": "都在讲量化误差分布"}, ...]}
每一句都要有一项（`sentence` 用脚本行前面的**句号**），`why` 不要遗漏。
除了这个 JSON 不要输出任何解释。"""


def build_assign_messages(
    segments: list[dict[str, Any]],
    assets: list[dict[str, Any]],
    *,
    beats_per_segment: list[list[str]] | None = None,
    panel_options: dict[str, list[tuple[str, str]]] | None = None,
) -> list[dict[str, str]]:
    """组「逐句选图」这一次调用的两条消息。

    `beats_per_segment[i]` 是第 i 段切出来的句子**文本**（调用方用
    `video.split_caption_beats` 切好传进来）。给了它就按**句**出行，每行标出
    「第几段、第几句」和行首的**句号**（模型按句号作答，见 `_normalize_per_beat`）。

    没给（或者所有段都没有文本）就退回原来的**逐段**行 —— 老调用点与老单测照旧能用。
    """
    script_lines: list[str] = []
    total_beats = (
        sum(len(beats) for beats in beats_per_segment) if beats_per_segment else 0
    )
    if beats_per_segment and total_beats > 0:
        cursor = 0
        for index, segment in enumerate(segments):
            beats = beats_per_segment[index] if index < len(beats_per_segment) else []
            speaker = segment.get("speaker") or "A"
            for beat_index, beat in enumerate(beats):
                text = " ".join(str(beat or "").split())
                script_lines.append(
                    f"[{cursor}] 第{index + 1}段 第{beat_index + 1}/{len(beats)}句"
                    f" 主播{speaker}：{text}"
                )
                cursor += 1
        header = (
            f"【脚本逐句】共 {len(segments)} 段、{total_beats} 句"
            f"（行首编号是**句号**，0 到 {total_beats - 1}）"
        )
        ask = "请为每一句脚本指定一张意思相符的图（几句讲同一件事就填同一张，这是正常的），输出 JSON。"
    else:
        for index, segment in enumerate(segments):
            text = (segment.get("text") or "").strip().replace("\n", " ")
            speaker = segment.get("speaker") or "A"
            script_lines.append(f"[{index}] 主播{speaker}：{text}")
        header = f"【脚本分段】共 {len(segments)} 段（编号 0 到 {len(segments)-1}）"
        ask = "请为每一段脚本指定一张意思相符的图，输出 JSON。"

    asset_lines = []
    for asset in assets:
        caption = (asset.get("caption") or asset.get("label") or "").strip()
        asset_lines.append(f"- {asset['id']}: {caption[:160]}")
        options = (panel_options or {}).get(str(asset["id"]), [])
        for letter, body in options:
            asset_lines.append(f"  - 子图 {letter}: {body[:100]}")

    user = f"""{header}
{chr(10).join(script_lines)}

【可用图片】共 {len(assets)} 张
{chr(10).join(asset_lines)}

{ask}"""
    return [
        {"role": "system", "content": ASSIGN_SYSTEM},
        {"role": "user", "content": user},
    ]


# 封面标题（爆款标题）的长度上限。
# 为什么不照抄论文原题：原题（「STEPQuant: When and Where Errors Matter in Deep
# Quantization」）在封面大字上既长又没有钩子，观众扫一眼就走了。封面要的是
# 「**跟我有关 / 有意思**」的一句话，原题放在它下面那行小字里（见 build_scenes 的
# cover_caption）—— 两行各司其职：一行抓人，一行交代出处。
# 20 是「竖版封面大字 @54px 一行放得下」的上限附近：面板内宽 732px，
# 54px 的中文一行约 13 字、两行约 27 字 —— 20 个字排两行（每行 10 字）绰绰有余，
# 而且标题长了还能靠 HTML 的 `fitText` 自动缩字号，不会挤破面板。
# 提示词里写 18、这里写 20，是**故意留的余量**：模型偶尔多写一两个字不该被截。


def _normalize_per_segment(
    raw: Any, *, count: int, valid_ids: set[str], default_id: str
) -> list[str] | None:
    """把模型给的逐段分配整理成「每段一个图片 id」。

    与旧版「只给起始段号」不同：那种做法下，一张图会一直挂到下一张图开始，
    中间几段可能已经换了话题。这里要求逐段明确，才能保证图文一致。
    """
    if not isinstance(raw, list):
        return None

    picks: dict[int, str] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        image_id = str(item.get("image_id") or "").strip()
        try:
            segment = int(item.get("segment"))
        except (TypeError, ValueError):
            continue
        # "generate" 是伪 id：表示「这一段需要现场生成一张专门的图」
        if image_id == GENERATE_ID or image_id in valid_ids:
            if 0 <= segment < count:
                picks[segment] = image_id

    if not picks:
        return None

    # 模型漏掉的段落（尤其是开头）沿用最近一次有效选择，避免出现空图
    result: list[str] = []
    last = picks.get(0, default_id)
    for index in range(count):
        if index in picks:
            last = picks[index]
        result.append(last)
    return result


def _assignment_notes(
    raw: Any, *, count: int, valid_ids: set[str]
) -> list[str]:
    """把模型给的 `why`（「这一段为什么配这张图」）整理成逐段一列表。

    和 `_normalize_per_segment` 收的是同一份 JSON，但**刻意分成两个函数**：
    选图的规则被测试钉着（`TestPerSegmentAssignment`），不该为了加一个「顺带存下来的
    理由」去动它。这里只做整理，**不对缺失项做延续** —— 上一段的理由套到这一段上
    是**假信息**，比空着更坏（复查时会以为模型真的解释过这一段）。

    认下的条件和选图一致（段号在范围内、image_id 合法），这样理由和实际分配
    永远对得上；模型给了理由但我们没采纳它那张图时，理由一并丢掉。
    """
    if not isinstance(raw, list):
        return [""] * count

    notes: dict[int, str] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        image_id = str(item.get("image_id") or "").strip()
        if image_id != GENERATE_ID and image_id not in valid_ids:
            continue
        try:
            segment = int(item.get("segment"))
        except (TypeError, ValueError):
            continue
        if not 0 <= segment < count:
            continue
        note = " ".join(str(item.get("why") or "").split())
        if note:
            notes[segment] = note[:ASSIGN_WHY_MAX_CHARS]
    return [notes.get(index, "") for index in range(count)]


def _beat_offsets(beat_counts: list[int]) -> list[int]:
    """每段第一句在**全局句号**里的位置。"""
    offsets: list[int] = []
    cursor = 0
    for count in beat_counts:
        offsets.append(cursor)
        cursor += max(int(count), 0)
    return offsets


def _flat_sentence_items(
    raw: Any, *, beat_counts: list[int]
) -> list[tuple[int, dict[str, Any]]] | None:
    """把模型给的每一项映射到**全局句号**，返回 [(句号, 原项)]。

    认两种写法（两种都可能出现，因为脚本行上「句号」和「第几段第几句」都写着）：
    - `sentence`：全局句号，最直接；
    - `segment` + `beat`：段号 + 段内句序。`beat` 缺失时按该段**第一句**算 ——
      模型偶尔会整段作答，那时它的选择只能落在这一段开头，剩下的句子走「沿用上一句」，
      总比整段丢掉强。
    """
    if not isinstance(raw, list) or not beat_counts:
        return None
    offsets = _beat_offsets(beat_counts)
    total = sum(max(int(count), 0) for count in beat_counts)
    if total <= 0:
        return None

    items: list[tuple[int, dict[str, Any]]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        index: int | None = None
        raw_sentence = item.get("sentence")
        if raw_sentence is not None:
            try:
                candidate = int(raw_sentence)
            except (TypeError, ValueError):
                candidate = -1
            if 0 <= candidate < total:
                index = candidate
        if index is None:
            try:
                segment = int(item.get("segment"))
            except (TypeError, ValueError):
                continue
            if not 0 <= segment < len(beat_counts) or beat_counts[segment] <= 0:
                continue
            try:
                beat = int(item.get("beat", 0))
            except (TypeError, ValueError):
                beat = 0
            beat = max(0, min(beat, beat_counts[segment] - 1))
            index = offsets[segment] + beat
        items.append((index, item))
    return items


def _normalize_per_beat(
    raw: Any,
    *,
    beat_counts: list[int],
    valid_ids: set[str],
    default_id: str,
) -> list[list[str]] | None:
    """把模型给的**逐句**分配整理成「每段一个列表，列表里每句一个图片 id」。

    做法是先把逐句结果**摊平**成一条句子序列，再交给 `_normalize_per_segment` ——
    沿用/纠错规则（漏掉的句子沿用上一句、编造的 id 丢掉、开头缺口用中性图）
    与逐段那条完全一致，不另开一套，两边的行为也就不会漂移。

    返回 `None` 表示模型什么都没给出（调用方回退启发式）；`[]` 不会出现 ——
    只要有一段被采纳，返回的就是每段一一对应的列表。

    句子的切分**不在**这里做：`beat_counts[i]` 是调用方用 `split_caption_beats`
    切出来的句数，与编码侧同一个函数（见模块开头）。
    """
    items = _flat_sentence_items(raw, beat_counts=beat_counts)
    if items is None:
        return None
    total = sum(max(int(count), 0) for count in beat_counts)
    flat = _normalize_per_segment(
        [{"segment": index, "image_id": item.get("image_id")} for index, item in items],
        count=total,
        valid_ids=valid_ids,
        default_id=default_id,
    )
    if flat is None:
        return None
    offsets = _beat_offsets(beat_counts)
    return [
        flat[offsets[index] : offsets[index] + max(int(count), 0)]
        for index, count in enumerate(beat_counts)
    ]


def _beat_notes_by_segment(
    raw: Any, *, beat_counts: list[int], valid_ids: set[str]
) -> list[str]:
    """把逐句的 `why` 收成**每段一句**（取这一段**第一句**的理由）。

    为什么不是「每段一条/每句一条」：存进 `video.scenes[i].why` 的那句话说的是
    「这一段为什么配这张图」，而这一段的画面就是它第一句那张图，理由也应当对应它。
    缺失项**不延续**（和 `_assignment_notes` 同一条规矩：拿别的句子的理由顶上是假信息）。
    """
    count = len(beat_counts)
    if count == 0:
        return []
    items = _flat_sentence_items(raw, beat_counts=beat_counts)
    if items is None:
        return [""] * count
    total = sum(max(int(c), 0) for c in beat_counts)
    flat = _assignment_notes(
        [{**item, "segment": index} for index, item in items],
        count=total,
        valid_ids=valid_ids,
    )
    offsets = _beat_offsets(beat_counts)
    return [
        flat[offsets[index]] if max(int(beat_count), 0) > 0 else ""
        for index, beat_count in enumerate(beat_counts)
    ]


def heuristic_per_beat(
    per_segment: list[str], beat_counts: list[int]
) -> list[list[str]]:
    """逐句的兜底：**沿用本段那一张**，一句也不换。

    为什么不在本地猜「哪一句该换图」：那是编。模型不可用时我们并不知道哪一句在讲什么，
    按字数、按关键词硬切只会造出「画面在换、但和正在讲的话无关」的效果 ——
    正是用户否掉推拉镜头（「跟内容无关的放大移动没有价值」）的那条理由。
    所以逐句这一版宁可整段一张图：`scene.image` 照旧逐段分配，`beats` 的 image 全空。
    """
    result: list[list[str]] = []
    for index, count in enumerate(beat_counts):
        pick = per_segment[index] if index < len(per_segment) else ""
        result.append([pick] * max(int(count), 0))
    return result


def heuristic_assignment(count: int, figure_count: int) -> list[int]:
    """均匀分布：把图铺到正文段上。模型不可用时的兜底（给的是起始段号）。

    这是**退化**方案：它只保证每张图都有露面，不保证图文相符。
    正常的语义匹配由模型逐段完成；模型不可用时至少画面不会空着。
    """
    if figure_count <= 0:
        return []
    head = max(int(count * 0.06), 0)
    tail = max(int(count * 0.12), 1)
    span = max(count - head - tail, 1)
    step = max(span / figure_count, 1)
    result = [min(head + int(i * step), count - 1) for i in range(figure_count)]

    for index in range(1, len(result)):
        if result[index] <= result[index - 1]:
            result[index] = min(result[index - 1] + 1, count - 1)
    for index in range(len(result) - 2, -1, -1):
        if result[index] >= result[index + 1]:
            result[index] = max(result[index + 1] - 1, 0)
    return result


def heuristic_per_segment(
    count: int, figure_ids: list[str], default_id: str
) -> list[str]:
    """把「均匀分布的起始段号」展开成逐段图片 id。"""
    if not figure_ids:
        return [default_id] * count
    starts = heuristic_assignment(count, len(figure_ids))
    result: list[str] = []
    current = default_id
    for index in range(count):
        starts_here = [j for j, start in enumerate(starts) if start <= index]
        if starts_here:
            current = figure_ids[starts_here[-1]]
        result.append(current)
    return result
