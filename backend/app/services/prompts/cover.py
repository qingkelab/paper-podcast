"""封面上的大字标题（爆款标题）：提示词 + 清洗 + 兜底。

`normalize_hook` 是**唯一**的清洗入口：模型写的、用户在界面上填的，都从这里过一遍，
所以「引号 + 前缀」这类装饰、超长结尾的标点断句，两条写入路径的行为必然一致。
"""

from __future__ import annotations

import re
from typing import Any

from .text import strip_trailing_stopwords
from .units import output_language_rule

__all__ = [
    "HOOK_MAX_CHARS",
    "HOOK_BREAK_KEEP",
    "HOOK_TAIL_GRACE",
    "HOOK_MAX_WORDS_EN",
    "HOOK_SYSTEM",
    "HOOK_SYSTEM_EN",
    "build_hook_messages",
    "normalize_hook",
]


HOOK_MAX_CHARS = 20
# 断在标点处至少要保留多少比例的内容，否则宁可不按标点断
HOOK_BREAK_KEEP = 0.6
# 超出上限**这么多字以内**就整句留着，不做任何截断。
# 中文词之间没有空格，截断必然落在词中间；而封面的字号是自适应的，
# 多两三个字只是缩一点字号。宁可略长，也不要砍出半个词。
HOOK_TAIL_GRACE = 3
HOOK_MAX_WORDS_EN = 9

HOOK_SYSTEM = """你在给一个「论文解读视频」写**封面标题**。

观众在信息流里刷到这段视频，画面一闪而过，只有封面上的这一行字能让人停下来。
你要写的不是论文的名字，而是**让人想点开的那一句话**。

规则：
- **不超过 18 个字**（写到 20 个字就已经是上限，超了会被我们截断 —— 与其被截，
  不如一开始就把话说短）。封面上的字号很大，长一点就挤成两行小字。
- 必须来自这篇论文**真实的内容**：它解决了什么、发现了什么、结果有多反常。
  **不许编造**数字或结论，论文里没有的别写。
- 四档风格，**挑最贴这篇论文的那一档**，不要四档混着写：
  1. **数字结论型**：「6.93 倍压缩，精度不掉」
  2. **悬念提问型**：「并发到 70，模型就装不下了？」
  3. **反差型**：「省了显存，却更容易崩」
  4. **代价型**：「精度换速度，这笔账划不划算」
- 可以口语、可以带问号，但**不要**标题党到失真（观众点进来发现不是那么回事就划走了）。
- **不要**出现「论文」「本文」「研究」「一种」这类学术腔开头，也不要引号。
- 语言与脚本一致（中文脚本写中文，英文脚本写英文），结尾不加标点。
- 只输出这一行字本身，不要解释、不要 JSON、不要换行。"""

HOOK_SYSTEM_EN = """You write the **cover headline** for a paper-explainer video.

A viewer scrolls past this video in a feed. The only thing that can stop the scroll is the
one line on the cover. Write that line — not the paper's title, but the sentence that makes
someone want to watch.

Rules:
- **At most 8 words** (9 is the hard ceiling — anything longer gets trimmed, and a trimmed
  headline reads like a cut-off sentence). The cover uses a very large font; longer wraps.
- It must come from the paper's **actual content**: what problem it solves, what it found,
  how surprising the result is. **Never invent** numbers or claims that are not in the paper.
- Pick **one** of these four styles — whichever fits this paper best, do not mix them:
  1. **number/result**: "6.9x compression, same accuracy"
  2. **question/hook**: "70 concurrent chats and the model is full?"
  3. **contrast**: "saves memory, breaks accuracy"
  4. **trade-off**: "what speed actually costs"
- Stay honest: a headline that oversells loses the viewer in the first ten seconds.
- No academic throat-clearing ("This paper", "We study", "A novel"), no quotation marks,
  no trailing punctuation. Output the line only — no explanation, no JSON, no line breaks."""


def build_hook_messages(
    *,
    title: str = "",
    analysis: dict[str, Any] | None = None,
    language: str = "zh",
) -> list[dict[str, str]]:
    """要一句封面标题。

    只发解读稿里最有信息量的几栏（创新点 / 核心结论 / 结果），不把整份解读塞进去：
    封面标题是个「一句话」的活，材料给多了模型反而写成综述。
    """
    system = HOOK_SYSTEM if language == "zh" else HOOK_SYSTEM_EN
    data = analysis or {}

    def joined(key: str, *, limit: int) -> str:
        value = data.get(key)
        if isinstance(value, list):
            return "；".join(str(item) for item in value[:3])[:limit]
        return str(value or "")[:limit]

    if language == "zh":
        user = f"""【论文原题】{title or "（未提供）"}

【解读稿摘要】
- 创新点：{joined("innovations", limit=160) or "（未提供）"}
- 方法：{joined("method", limit=160) or "（未提供）"}
- 实验与结果：{joined("experiments", limit=200) or "（未提供）"}
- 核心结论：{joined("conclusion", limit=160) or "（未提供）"}

请写一句不超过 18 个字的封面标题，只输出这一行字。"""
    else:
        user = f"""[PAPER TITLE] {title or "(not provided)"}

[ANALYSIS SUMMARY]
- Contributions: {joined("innovations", limit=160) or "(not provided)"}
- Method: {joined("method", limit=160) or "(not provided)"}
- Results: {joined("experiments", limit=200) or "(not provided)"}
- Conclusion: {joined("conclusion", limit=160) or "(not provided)"}

Write the cover headline in at most 8 words. Output that one line only."""
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def normalize_hook(raw: Any, *, language: str = "zh") -> str:
    """整理模型给的封面标题：去掉引号/书名号/序号/换行，按语言卡长度。

    模型经常把标题包在引号里、前面还写个「封面标题：」，这些都直接剥掉 ——
    封面上的大字出现引号非常出戏。
    """
    def clean(line: str) -> str:
        # 先去**外层装饰**（引号、markdown 强调符）：它们会挡住下面那些以「行首」为锚的
        # 前缀规则 —— 实测 `"封面标题：…"` 这种「引号 + 前缀」的组合就是漏网的
        # （模型很爱这么写，而封面上的大字顶着「封面标题：」四个字非常出戏）。
        line = line.strip().strip('"\'“”「」《》【】*_`').strip()
        line = re.sub(
            r"^[\s\-•*\d.、)）:：]*(封面标题|标题|headline|title)\s*[:：]?",
            "",
            line.strip(),
            flags=re.IGNORECASE,
        )
        # 模型爱把风格名一起写出来（「反差型：省了显存却更容易崩」）——那是给它自己看的，
        # 顶到封面上很出戏。只认「XX型」这一种形态，真实标题里极少这么开头。
        # 分隔符要认全：`:：` 之外还有 `|`（「数字结论型 | 6.93 倍压缩」这种也实测出现过）。
        line = re.sub(r"^[\u4e00-\u9fffA-Za-z]{2,5}型\s*[:：|｜]\s*", "", line)
        line = re.sub(r"^[（(]\s*[1-4]\s*[)）]\s*", "", line)
        line = line.strip().strip('"\'“”「」《》【】`').replace("**", "").strip()
        line = re.sub(r"\s+", " ", line).rstrip("。．.,，;；!！?？~～")
        return line.strip()

    text = str(raw or "").strip()
    if not text:
        return ""
    # 多行时逐行清理，取**第一行有内容的**（标题在头一行；「标题」这种标签被剥掉后会是空）
    for line in text.splitlines():
        candidate = clean(line)
        if len(candidate) >= 4:
            text = candidate
            break
    else:
        text = clean(text)
    if not text:
        return ""
    return _trim_hook(text, language=language)


# 封面标题超长时，从哪里断开。
# 硬砍到第 16 个字是不行的 —— 实测砍出来过「4 比特状态量化，反超均匀 IN」（把 INT8 砍成 IN）、
# 「4-bit 反超 INT8，显存」（半句）。封面上的大字出现半句比短一句难看得多，
# 所以优先在**标点/分句处**断开，其次至少不要把一个词砍成两半。
#
# ⚠️ **不要**把 `/`、`|` 放进来。它们看着像分隔符，实际是**词中间的分隔符**
# （「Qwen/Kimi 同款」是一个整体），拿它当断句点会砍出
# 「显存直降 68.7%！Qwen」—— 正好丢掉最有信息量的那半句（实测就是这么砍的）。
# 模型那种「数字结论型 | …」的前缀由 `normalize_hook` 单独剥，不靠这里。


_HOOK_BREAKS = "，。、；：！？,.;:!?"


def _trim_hook(text: str, *, language: str) -> str:
    """超长时在**分句处**断开，而不是数着字数硬砍。"""
    if language != "zh":
        words = text.split()
        if len(words) <= HOOK_MAX_WORDS_EN:
            return text
        head = words[:HOOK_MAX_WORDS_EN]
        # 英文按词截会截到句子中间（实测「…aren't uniform; only some last」）。
        # 词数够多时宁可退回上一个分句，读起来是完整的一句。
        for index in range(len(head) - 1, 2, -1):
            if head[index - 1].endswith((",", ";", ":", "—")):
                return " ".join(head[:index]).rstrip(",;:— ")
        return strip_trailing_stopwords(" ".join(head), language=language, floor=HOOK_MAX_WORDS_EN // 2)

    if len(text) <= HOOK_MAX_CHARS:
        return text
    # 只超出一两个字就**整句留着**：中文的词之间没有空格，硬砍在第 20 个字上
    # 会把结尾那个词砍成两半（实测：「…INT8 基准」→「…INT8 基」）。
    # 而封面上那行大字现在是 HTML 按真实字形自适应的（`fitText`），
    # 多一两个字只会让它缩一点字号 —— 比砍出半个词体面得多。
    if len(text) <= HOOK_MAX_CHARS + HOOK_TAIL_GRACE:
        return text
    window = text[:HOOK_MAX_CHARS]
    cut = max(window.rfind(ch, HOOK_MAX_CHARS // 2) for ch in _HOOK_BREAKS)
    # 只在「断在标点处仍然保留了大部分内容」时才用标点断句：
    # 否则会砍成「4 比特状态量化」这种只剩前半句的标题（实测出现过）。
    if cut >= HOOK_MAX_CHARS * HOOK_BREAK_KEEP:
        return window[:cut].strip()
    # 窗口里没有合适的标点，就看看**后面第一个**标点在哪：断在那儿读起来是一句完整的话，
    # 代价是多几个字。封面那行大字是自适应的，多这三五个字只是缩一点字号 ——
    # 比硬砍在词中间（「…INT8 基准」→「…INT8 基」）体面得多。
    ahead = [index for ch in _HOOK_BREAKS if (index := text.find(ch, HOOK_MAX_CHARS)) >= 0]
    if ahead and min(ahead) <= HOOK_MAX_CHARS + HOOK_TAIL_GRACE * 2:
        return text[: min(ahead)].strip()
    # 后面也没有标点（或者离得太远）：硬砍，至少别把结尾的英文/数字词砍成两半
    # （「反超均匀 IN」→「反超均匀」）
    trimmed = re.sub(r"[A-Za-z0-9.%+\-]+$", "", window).strip()
    return strip_trailing_stopwords(trimmed or window, language=language, floor=HOOK_MAX_CHARS // 2)


def _cover_fallback_headline(title: str) -> str:
    """没有模型输出时的封面大字：用论文原题（会截断）。

    只在这一种情况下用：说话模型不可用（Mock / 调用失败）。封面上什么都不放
    比放个原题更糟 —— 观众总得知道这是哪篇论文。
    """
    text = " ".join((title or "").split())
    if not text:
        return ""
    limit = HOOK_MAX_CHARS * 2  # 封面允许两行，所以比爆款标题宽一倍
    if len(text) > limit:
        text = text[: limit - 1].rstrip("，。、,.;") + "…"
    return text
