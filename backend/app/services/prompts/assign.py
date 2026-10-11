"""逐段选图：为脚本的每一段挑一张配图，并写一句「为什么配这张」。

`_normalize_per_segment` / `heuristic_*` 是兜底：模型给不出、或者返回的段数对不上时，
不能让视频缺画面，所以有确定性的启发式分配。
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
    "heuristic_per_segment",
]


# --------------------------------------------------------------------------

# 「这一段为什么配这张图」的长度上限。prompt 里写的是 20 个字，这里留一倍余量：
# 它是**给人看的复查依据**，模型偶尔写长一点不该被拦；但也不能放任它塞一整段进来
# （那会把 `scenes` 撑大，而 `video.scenes` 是随单集走数据库的）。


ASSIGN_WHY_MAX_CHARS = 40


ASSIGN_SYSTEM = """你在为「论文解读播客」的视频版做图文编排。

听众能听到主播的对话，同时看到画面。你的任务是让**每一段话配上意思相符的图**：
听众听到某个概念时，屏幕上正好是讲这个概念的图。图不对文比没有图更糟。

你会拿到：
- 脚本分段列表（编号、内容）
- 可用图片列表（id 与图注）

请为**每一段**指定一张图。判断依据是这一段在讲什么，以及哪张图正好在讲同一件事。

【重要规则】
- 图注可能是英文，脚本是中文，你要按**语义**判断，不要按字面。
- 若某一段的内容和任何一张图都对不上，就填 `generate` —— 系统会为这一段
  **现场画一张专门的示意图**。连续几段讲同一件事时都填 `generate`，
  系统会把它们合并成一张图，不要每段都标。
- `illustration` 是一张概括全文的信息图，只在「需要一个中性过渡画面」时用
  （例如开场收尾）。**不要为了填满而硬塞一张不相关的原图。**
- `cover` 是论文首页，适合开场介绍论文时用；正文讨论具体内容时不要用它。
- 能对上论文原图的段落**优先用原图**（原图最准确）；原图对不上才填 `generate`。
- 相邻段落如果确实在讲同一件事，用同一张图是正常的；话题变了就换图。
- 图片 id 只能从给出的列表里选，不要编造。
- 每一项还要写一句 `why`：**这一段和这张图的共同点是什么**。
  它是给人看的检查依据 —— 复查时只看 `why` 就能判断选得对不对，不必重读全稿。
  一句话，不超过 20 个字，不要复述图注、不要写「内容相关」这种空话。

只输出 JSON：
{"assignments": [{"segment": 0, "image_id": "cover", "why": "开场点出论文主题"},
                 {"segment": 1, "image_id": "f1", "why": "都在讲量化误差分布"}, ...]}
每个脚本段都要有一项，`why` 不要遗漏。除了这个 JSON 不要输出任何解释。"""


def build_assign_messages(
    segments: list[dict[str, Any]], assets: list[dict[str, Any]]
) -> list[dict[str, str]]:
    script_lines = []
    for index, segment in enumerate(segments):
        text = (segment.get("text") or "").strip().replace("\n", " ")
        speaker = segment.get("speaker") or "A"
        script_lines.append(f"[{index}] 主播{speaker}：{text}")

    asset_lines = []
    for asset in assets:
        caption = (asset.get("caption") or asset.get("label") or "").strip()
        asset_lines.append(f"- {asset['id']}: {caption[:160]}")

    user = f"""【脚本分段】共 {len(segments)} 段（编号 0 到 {len(segments)-1}）
{chr(10).join(script_lines)}

【可用图片】共 {len(assets)} 张
{chr(10).join(asset_lines)}

请为每一段脚本指定一张意思相符的图，输出 JSON。"""
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
