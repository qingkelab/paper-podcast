"""画面文案：本段要点（强调行）+ 子图聚光灯的选择与归一化。

两条线都遵循同一个原则：**模型只做「选」和「写」，坐标与几何由我们用像素定**。
`normalize_focus` 因此只接受 0~1 的比例，给像素值一律丢掉（猜错会框到角落）。
"""

from __future__ import annotations

import math
import re
from typing import Any

from ..design import (
    FOCUS_LABEL_CHARS,
    FOCUS_MAX_AREA,
    FOCUS_MIN_AREA,
    FOCUS_MIN_SIDE,
    POINT_MAX_CHARS,
    point_char_limit,
)
from .script import _bullets
from .text import strip_trailing_stopwords
from .units import output_language_rule

__all__ = [
    "POINTS_SYSTEM",
    "POINTS_SYSTEM_EN",
    "local_point",
    "shorten_point",
    "build_points_messages",
    "normalize_point_items",
    "normalize_focus",
]


POINTS_SYSTEM = """你在给一个「论文解读视频」做**画面强调**。

观众在听双人播客，画面上除了字幕，还需要一句**大字强调**：这一段最该被记住的是什么。
你要为每一段脚本写一句强调文案。

规则：
- **不超过 14 个字**（英文不超过 8 个词）。画面上是一行大字，超了就挤。
- 必须是**这一段自己的内容**：关键的结论、数字、机制名、对比结果。
  有数字就优先给数字（「成功率 67%」「比基线高 3 倍」）—— 这是观众最记得住的东西。
- **不要**写「很重要」「值得关注」这类空话，也不要把整段话缩写一遍。
- **不要编造**原文没有的数字或结论。
- **每一段都要有一句**，包括过渡段：过渡段就写这一段**要讲的话题**（例如「误差为什么会滚雪球」），
  而不是「我们接着看」这种本身没有信息量的话。
  （第一版允许留空，结果整条视频只有 18% 的时间有强调行 —— 观众感觉「这行时有时无」。）
- 语言与原文一致（原文中文就中文，英文就英文）。
- **每一段都必须带 `panel` 字段，一个都不能省**（省略字段和写 null 是两回事）：
  - 如果这一段配的图**下面给了子图清单**（形如 `(a) …；(b) …`），并且这一段讲的就是
    其中某一个子图 → 写那个字母，例如 `"b"`；
  - 讲的是整张图、清单里没有对得上的、或者这一段没有子图清单 → 写 `null`。
  **不要输出坐标**：图片里的区域位置由我们从图上量出来，你只需要指出是哪个子图。
  指错子图比不指更糟，拿不准就写 `null`。

只输出 JSON：
{"points": [{"segment": 0, "point": "成功率 67%", "panel": "b"},
            {"segment": 1, "point": "误差会滚雪球", "panel": null}]}
每个脚本段都要有一项。不要输出任何解释。"""

POINTS_SYSTEM_EN = """You write the big on-screen emphasis line for a paper-explainer video.

The viewer is listening to a two-host podcast. Besides the captions, the screen shows one
line of large text: the single thing from this segment worth remembering. Write that line
for every segment.

Rules:
- **At most 8 words.** It is one large line on a 936px-wide canvas; longer will not fit.
- Use **this segment's own content**: the key result, number, mechanism name, or comparison.
  Prefer a number when there is one ("67% success rate", "3x over baseline") — that is what
  viewers remember.
- Do **not** write vague filler like "this matters" or "worth noting", and do not paraphrase
  the whole segment.
- Do **not** invent numbers or conclusions that are not in the text.
- **Every segment gets a line**, transitions included: for a transition, name the topic it
  is introducing ("why errors snowball"), not "let's move on" (which says nothing).
  (The first version allowed empty lines; only 18% of the video ended up with an emphasis
  line, which reads as "this line flickers on and off".)
- **Write in English** (the whole video is English), **start with a capital letter**,
  and do not end with a period — it is a headline, not a sentence.
- **Every segment must carry a `panel` field — never omit it**:
  - if the image for this segment comes with a **panel list** (like `(a) …; (b) …`) and
    this segment is genuinely about one of those panels → write that letter, e.g. `"b"`;
  - if it is about the whole figure, nothing in the list matches, or there is no panel
    list → write `null`.
  **Never give coordinates**: the region of the image is measured from the pixels on our
  side; you only say which panel. Pointing at the wrong panel is worse than not pointing.

Output JSON only:
{"points": [{"segment": 0, "point": "67% success rate", "panel": "b"},
            {"segment": 1, "point": "errors snowball", "panel": null}]}
Include one entry per segment. No explanations."""

# 强调行里允许出现的数字形态（用于本地兜底）


_NUMBER_TOKEN = re.compile(r"\d+(?:\.\d+)?\s*(?:%|％|倍|万|亿|千|个百分点|个|条|张|次|秒|分钟|层|维|B|M|K)?")


def local_point(text: str, *, max_chars: int = POINT_MAX_CHARS, language: str = "zh") -> str:
    """没有模型可用时的兜底强调行：从这一段的原文里抠出一句带数字的短语。

    为什么只认数字：实测这几集的脚本里只有 **30%~47%** 的段落含阿拉伯数字
    （有一集 0%），而「论文关键词命中」几乎是 0 —— 脚本是口语化的，不会照抄术语。
    所以本地抽取给不出稳定的「要点」，真正的要点得让模型写（见 `POINTS_SYSTEM`）。
    兜底只做最有把握的一件事：**把数字拎出来**，其余情况返回空串（画面就不显示强调行）。
    """
    clean = " ".join((text or "").split())
    candidates = list(_NUMBER_TOKEN.finditer(clean))
    if not candidates:
        return ""

    # 一句里常有多个数字（「16 个任务上平均成功率 67%」）——要挑**信息量最大**的那个：
    # 百分比 > 带单位 > 光秃秃的整数。挑第一个的话会拎出「16 个任务」，
    # 而观众真正该记住的是「67%」。
    def importance(match: re.Match[str]) -> int:
        token = match.group(0)
        if "%" in token or "％" in token or "倍" in token or "百分点" in token:
            return 3
        if any(unit in token for unit in ("万", "亿", "千", "秒", "分钟", "层", "维", "B", "M", "K")):
            return 2
        return 1 if len(token) >= 2 else 0

    match = max(candidates, key=importance)

    # 以数字为中心，向左右扩到句子边界，再截到 max_chars
    left, right = match.start(), match.end()
    for index in range(match.start() - 1, -1, -1):
        if clean[index] in "。！？；，、,.;:":
            left = index + 1
            break
        left = index
    for index in range(match.end(), len(clean)):
        if clean[index] in "。！？；，、,.;:":
            right = index
            break
        right = index + 1

    phrase = clean[left:right].strip()
    # 短语里若还夹着标点（模型写的问句「Should we go to 8 bits? People tried…」），
    # 就在第一个标点处收掉，别把下一句的头几个词带进来（实测出现过「to 8 bits? People」）
    for position, char in enumerate(phrase):
        if char in "。！？；，、,.;:!?":
            phrase = phrase[:position]
            break
    phrase = phrase.strip()
    if not phrase:
        return ""
    if len(phrase) > max_chars:
        # 太长就只留数字和它前后的少量上下文
        head = max(0, match.start() - left - 4)
        phrase = clean[left + head : right]
    return shorten_point(phrase, max_chars=max_chars, language=language)


# 收短之后可能停在虚词上（「Quantization error feeds back and」「Memory problem is」）。
# 画面上最抢眼的一行以虚词结尾很扎眼，把它们去掉 —— 只在剩下的长度还够时去。


def shorten_point(text: str, *, max_chars: int = POINT_MAX_CHARS, language: str = "zh") -> str:
    """把过长的强调文案收短到上限，**在词/标点边界处断开**。

    按字符硬切会切出「at 4 and 6 bi」「INT6 errors a」这种半截话（实测踩到过），
    画面上最抢眼的一行出现半截英文比不显示还糟。
    优先切成句标点，其次空格，实在没有才硬截。
    """
    clean = " ".join((text or "").split())
    floor = int(max_chars * 0.5)
    if len(clean) <= max_chars:
        return strip_trailing_stopwords(clean, language=language, floor=floor)
    window = clean[:max_chars]
    for separator in ("，", "。", "、", "；", "：", "！", "？", ",", ".", ";", ":", "!", "?", " "):
        cut = window.rfind(separator)
        if cut >= max_chars * 0.6:
            return strip_trailing_stopwords(
                window[:cut].strip(), language=language, floor=floor
            )
    return strip_trailing_stopwords(window.strip(), language=language, floor=floor)


def build_points_messages(
    segments: list[dict[str, Any]],
    *,
    title: str = "",
    language: str = "zh",
    image_captions: list[str | None] | None = None,
    panel_options: list[list[tuple[str, str]]] | None = None,
) -> list[dict[str, str]]:
    """为每一段脚本要一句「大字强调」。

    单独一次调用（而不是塞进逐段配图那次）的原因：重新合成视频时必须**复用**画面分配、
    不能重新问模型，但强调文案是可以补的、而且补一次就存下来。两件事的生命周期不同。

    `panel_options[i]` 是第 i 段那张图的**子图清单**（`[(字母, 这一块讲什么), …]`）。
    给出来是为了让模型做一件它做得对的事：**从文字里挑**「这一段在讲哪个子图」。
    它做不对的是「给坐标」—— 它看不到图，实测两次真实调用分别只给出 8/23 和 4/16 个
    可用框（有一集 4 个框全是同一块「左半张」，只是标签不同）。

    **英文版必须整段用英文 prompt**（`POINTS_SYSTEM_EN`）：中文 system 里就算写了
    「语言与原文一致」，模型给英文脚本写的强调行仍然是中文 —— 实测踩到过，
    英文那一版的画面上整行中文。这跟当初解读/脚本 prompt 踩的是同一个坑。
    """
    system = POINTS_SYSTEM if language == "zh" else POINTS_SYSTEM_EN
    captions = image_captions or []
    script_lines = []
    for index, segment in enumerate(segments):
        text = (segment.get("text") or "").strip().replace("\n", " ")
        caption = (captions[index] if index < len(captions) else None) or ""
        caption = " ".join(str(caption).split())[:120]
        # 把「这一段配的是哪张图」一并给模型，否则它没法判断该框图里的哪一块
        suffix = f"（这一段配的图：{caption}）" if caption else "（这一段没有配图）"
        if language != "zh":
            suffix = f" (image for this segment: {caption})" if caption else " (no image)"

        options = panel_options[index] if panel_options and index < len(panel_options) else []
        if options:
            items = "；".join(f"({letter}) {body}" for letter, body in options)
            suffix += (
                f"（这张图有 {len(options)} 个子图：{items}）"
                if language == "zh"
                else f" (this figure has {len(options)} panels: {items})"
            )
        script_lines.append(f"[{index}] {text}{suffix}")

    if language == "zh":
        user = f"""【论文标题】{title or "（未提供）"}

【脚本分段】共 {len(segments)} 段（编号 0 到 {len(segments) - 1}）
{chr(10).join(script_lines)}

请为每一段写一句不超过 14 个字的大字强调，并给出 `panel`（拿不准就写 `null`，但**字段不能省**），输出 JSON。"""
    else:
        user = f"""[PAPER TITLE] {title or "(not provided)"}

[SCRIPT SEGMENTS] {len(segments)} segments (numbered 0 to {len(segments) - 1})
{chr(10).join(script_lines)}

Write one emphasis line of at most 8 words per segment, and include a `panel` field for
every segment (use `null` when unsure — but never omit the field). Output JSON only."""
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def normalize_point_items(
    raw: Any,
    *,
    count: int,
    max_chars: int = POINT_MAX_CHARS,
    language: str = "zh",
    panel_letters: list[list[str]] | None = None,
) -> list[dict[str, Any]]:
    """把模型给的整份 payload 整理成每段一项：`{"point", "panel", "focus"}`。

    要点文案与「讲的是哪个子图」来自**同一次调用**（模型一边说「这段最该记住什么」，
    一边指出在图的哪一块），所以这里一起归一化 —— 分两次调用会多花一次钱，
    而且两者可能对不上。

    `panel` 只在**这一段那张图真的有这些字母**时才保留：模型偶尔会编一个图里
    不存在的字母（比如那张图根本没有子图），那种一律丢掉。

    `focus` 是**兼容字段**：早期版本让模型直接给坐标，实测不可靠（见 `POINTS_SYSTEM`
    的说明），现在只剩「读旧数据里已存下来的框」这一条用途。
    """
    items: list[dict[str, Any]] = [
        {"point": "", "panel": None, "focus": None} for _ in range(max(count, 0))
    ]
    if not isinstance(raw, list):
        return items
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        try:
            index = int(entry.get("segment", entry.get("index")))
        except (TypeError, ValueError):
            continue
        if not 0 <= index < count:
            continue
        text = shorten_point(str(entry.get("point") or ""), max_chars=max_chars, language=language)
        allowed = panel_letters[index] if panel_letters and index < len(panel_letters) else []
        letter = str(entry.get("panel") or "").strip().lower().strip("()")
        items[index] = {
            "point": text,
            "panel": letter if letter and letter in allowed else None,
            "focus": normalize_focus(entry.get("focus")),
        }
    return items


def _normalize_points(
    raw: Any, *, count: int, max_chars: int = POINT_MAX_CHARS, language: str = "zh"
) -> list[str]:
    """整理模型给的强调文案：丢掉认不出的项、超长的截断，缺的补空串。

    返回的列表长度一定是 `count` —— 调用方按段号取用，缺项要能对上位置。
    """
    points = [""] * max(count, 0)
    if not isinstance(raw, list):
        return points
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            index = int(item.get("segment", item.get("index")))
        except (TypeError, ValueError):
            continue
        if not 0 <= index < count:
            continue
        text = shorten_point(str(item.get("point") or ""), max_chars=max_chars, language=language)
        if text:
            points[index] = text
    return points


def normalize_focus(raw: Any) -> dict[str, Any] | None:
    """整理模型给的「这一段在讲图的哪一块」。

    坐标是**相对图片的 0~1 比例**（不是像素）：图片在画面里是等比缩放居中的，
    用比例才能跟 `_prepare_image` 的结果对齐。认不出的、太小的、越界的一律丢掉 ——
    框错地方比不框更糟（观众会以为那一块真的在讲那个）。
    """
    if not isinstance(raw, dict):
        return None
    try:
        x = float(raw.get("x"))
        y = float(raw.get("y"))
        w = float(raw.get("w"))
        h = float(raw.get("h"))
    except (TypeError, ValueError):
        return None
    if not all(map(math.isfinite, (x, y, w, h))):
        return None
    # 契约是 0~1 的**比例**。给了像素（比如 600）说明模型没按格式来 ——
    # 归一化里没法反推像素对应的图有多大，宁可这一帧不框，也不要框到角落里去。
    if any(value < -1e-6 or value > 1.0 + 1e-6 for value in (x, y, w, h)):
        return None
    x, y = max(0.0, min(x, 0.98)), max(0.0, min(y, 0.98))
    w, h = min(w, 1.0 - x), min(h, 1.0 - y)
    if w < FOCUS_MIN_SIDE or h < FOCUS_MIN_SIDE:
        return None
    if w * h < FOCUS_MIN_AREA:
        return None
    if w * h > FOCUS_MAX_AREA:
        # 覆盖 85% 以上：等于没框（见上面 FOCUS_MAX_AREA 的说明）
        return None
    label = str(raw.get("label") or "").strip().replace("\n", " ")
    return {
        "x": round(x, 4),
        "y": round(y, 4),
        "w": round(w, 4),
        "h": round(h, 4),
        "label": label[:FOCUS_LABEL_CHARS],
    }
