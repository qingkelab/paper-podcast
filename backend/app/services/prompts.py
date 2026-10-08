"""Prompt 工程：论文解读 + 双人播客脚本。

这是本产品的核心资产。设计原则：

1. **两段式生成**。先出结构化解读（JSON），再基于解读写脚本。分两步的原因：
   一次性输出「解读+脚本」时模型会把注意力放在文风上，解读深度明显下降；
   分开后每一步的约束都更紧，且任一步失败可以单独重试。
2. **强制 JSON**。解读用 JSON 输出，前端才能渲染成卡片、才能做关键词高亮。
   脚本也用 JSON，避免解析「主播A：」这种自由文本时被换行/冒号变体搞崩。
3. **反 AI 腔**。播客脚本最怕出现「首先…其次…最后」「综上所述」「让我们深入探讨」
   这类书面语。Prompt 里用负面清单显式禁止，并给出正面的口语示例。
4. **长度可控**。中文播客语速约 250 字/分钟，按分钟数反推字数，写进 prompt。
"""

from __future__ import annotations

from typing import Any

# 豆包播客音色的真实语速（字/分钟）。
#
# ⚠️ 这个值是**实测反推**出来的，不是拍的。用两次真实合成结果算：
#   - 238 字 → 总时长 58.49s，扣掉 17s 片头片尾音乐 → 41.5s → 344 字/分钟
#   - 1787 字 → 总时长 319.01s，扣掉 17s → 302.0s       → 355 字/分钟
# 早先按「中文播客常识」填了 250，导致时长控制偏差 20~26%：
# 用户选 10 分钟只会拿到约 7.4 分钟的音频。
CHARS_PER_MINUTE = 350

# 服务端自带片头/片尾音乐时的实测时长（各约 7.0s / 9.95s）。
# 现在默认**关掉**了服务端音乐（见 config.podcast_head_music），
# 换成社区自己的品牌话术，所以这个常量只在开关打开时才用得上。
MUSIC_PADDING_SEC = 17.0

# 各难度档的表达约束
LEVEL_GUIDE: dict[str, str] = {
    "intro": (
        "面向「非本专业的聪明听众」。禁止使用未经解释的术语；必须用生活中的类比"
        "解释核心机制；数学公式一律用文字描述其直觉含义，不出现公式符号。"
        "目标是让一个文科生也能听懂并复述出这篇论文在干什么。"
    ),
    "advanced": (
        "面向「同领域研究生」。可以直接使用领域标准术语，但要在第一次出现时用"
        "半句话点明含义；可以讨论方法的技术取舍，不需要从零铺垫基础概念。"
    ),
    "expert": (
        "面向「同方向的科研人员」。可以直接讨论方法的实现细节、与同期工作的差异、"
        "实验设计的严谨性；允许指出论文在假设、基线选择、评估指标上的可疑之处。"
        "不要浪费篇幅做科普。"
    ),
}

# 英文版的难度档说明（英文输出配英文说明，见 _build_script_messages_en）
LEVEL_GUIDE_EN: dict[str, str] = {
    "intro": (
        "For a smart listener outside the field. No unexplained jargon; explain every "
        "core mechanism with an everyday analogy; describe any formula in plain words "
        "instead of symbols. The goal: a non-specialist can follow it and repeat back "
        "what the paper actually did."
    ),
    "advanced": (
        "For a graduate student in the same field. Standard terminology is fine, but "
        "gloss each term the first time it appears; discuss technical trade-offs "
        "without rebuilding the basics from scratch."
    ),
    "expert": (
        "For researchers working in this direction. Go straight into implementation "
        "detail, differences from contemporaneous work, and the rigour of the "
        "experimental setup; point at questionable assumptions, baselines or metrics. "
        "Do not spend time on background."
    ),
}


# 语言标识
LANGUAGES = ("zh", "en")

LANGUAGE_LABEL = {"zh": "简体中文", "en": "English"}

# 英文播客的语速。中文按「字/分钟」算，英文按「词/分钟」算 ——
# 两者量纲不同，不能用同一个常量。
#
# 这个数字是**实测反推**的，不是「英文播客常识」：真实跑完一期 3 分钟英文播客，
# 正文 496 词对应 211.0 秒语音（总时长 238.2 秒减去 27.2 秒品牌片头片尾），
# 得 141 词/分钟。早先按 150 估，估出来的时长比实际短 6%。
WORDS_PER_MINUTE_EN = 141

# 豆包播客 TTS 对**单个 round 的文本长度**有硬上限。实测越界会被直接拒绝：
#   40000010 "PodcastTTS invalid param, errMsg: nlp_texts round text length
#             309 is greater than max char length 300"
# 注意它数的是**字符**，不是词、也不是字：
#   中文一段 120 字 ≈ 120 字符，几乎撞不到上限；
#   英文一段 50 个词就有 300 字符上下 —— 同样的「一段话」在英文里字符数多 5 倍。
# 所以双语上线后英文脚本第一个撞线。取 280 留出余量。
MAX_ROUND_CHARS = 280

# 各语言的「AI 腔」负面清单。中文那套（综上所述/值得注意的是）
# 在英文输出里毫无意义，必须换成英文的对应物。
AI_CLICHES = {
    "zh": ["首先", "其次", "综上所述", "值得注意的是", "让我们深入探讨", "不难发现", "总而言之"],
    "en": [
        "In conclusion",
        "It's worth noting",
        "It is worth noting",
        "Let's dive in",
        "Let's dive into",
        "delve into",
        "Furthermore",
        "Moreover",
        "Additionally",
        "In today's rapidly evolving",
        "game-changer",
        "unlock the potential",
        "In summary",
    ],
}


def effective_rate(speech_rate: int = 0, language: str = "zh") -> float:
    """把语速档位换算成实际语速。

    中文是字/分钟，英文是词/分钟 —— 两者量纲不同，不能用同一个常量。
    """
    base = WORDS_PER_MINUTE_EN if language == "en" else CHARS_PER_MINUTE
    return base * (1.0 + speech_rate / 100.0)


def output_language_rule(language: str) -> str:
    """给模型的输出语言要求，并附上该语言的套话负面清单。

    英文版这条规则也必须用英文写：整个 prompt 里夹杂中文指令，
    模型偶尔会跟着中文指令的语感走（实测把「120 字」当成 120 个词）。
    """
    if language == "en":
        cliches = ", ".join(f'"{c}"' for c in AI_CLICHES["en"])
        return (
            "\n\n【Output language】Write everything in **English**.\n"
            "Keep technical terms in their original English form; do not translate them.\n"
            f"These podcast clichés are banned: {cliches}."
        )
    cliches = "、".join(f"「{c}」" for c in AI_CLICHES["zh"])
    return (
        "\n\n【输出语言】全部输出**简体中文**。\n"
        f"禁止出现这些套话：{cliches}。"
    )





def brand_padding_sec(brand_chars: int, speech_rate: int = 0, language: str = "zh") -> float:
    """品牌片头/片尾话术占用的时长。

    它们和音乐一样是**固定开销**：每期都有、不随正文字数变化。
    所以要从时长预算里扣掉，否则正文写满之后整期就超时了。
    """
    if brand_chars <= 0:
        return 0.0
    return brand_chars / effective_rate(speech_rate, language) * 60


def compute_padding_sec(
    *,
    head_music: bool,
    tail_music: bool,
    brand_chars: int = 0,
    speech_rate: int = 0,
    language: str = "zh",
) -> float:
    """正片之外的所有固定开销（音乐 + 品牌话术）。"""
    total = 0.0
    if head_music or tail_music:
        # 实测片头约 7.0s、片尾约 9.95s，只开一个就按对应那半算
        total += MUSIC_PADDING_SEC * (
            1.0
            if (head_music and tail_music)
            else (0.41 if head_music else 0.59)
        )
    total += brand_padding_sec(brand_chars, speech_rate, language)
    return total


def target_chars(
    duration_min: int, speech_rate: int = 0, padding_sec: float = 0.0, language: str = "zh"
) -> int:
    """把目标时长换算成正文字数。

    要扣掉正片之外的固定开销（音乐、品牌话术），
    否则估出来的字数偏多、实际时长会超目标。
    """
    speech_seconds = max(duration_min * 60 - padding_sec, 30.0)
    return int(speech_seconds / 60 * effective_rate(speech_rate, language))


def estimate_duration_sec(
    chars: int, speech_rate: int = 0, padding_sec: float = 0.0, language: str = "zh"
) -> int:
    """字数/词数 → 预期音频总时长（含音乐与品牌话术）。"""
    return round(chars / effective_rate(speech_rate, language) * 60 + padding_sec)


# --------------------------------------------------------------------------
# 第一步：结构化解读
# --------------------------------------------------------------------------

ANALYSIS_SYSTEM = """你是资深学术播客制作人，长期为科研听众解读各领域论文。你的解读以\
「准确、具体、不注水」著称：你从不写「本文提出了一个新颖的方法」这种无信息量的句子，\
而是直接说清楚新在哪里、和谁比、好在多少。

现在你要精读用户提供的论文，输出一份结构化解读。要求：

- 只依据论文本身的内容。论文没写的东西不要编造。如果某项信息论文中没有（比如\
没有做消融实验），就明确写「论文未涉及」，不要用常识填补。
- 具体到数字。实验部分要引用论文里的关键指标、对比基线、提升幅度；有具体数值就用数值，\
不要只说「显著提升」。
- 创新点要指出「相对于什么」。孤立地说「提出了X」没有意义，要说「相比A方法，X改进了B」。
- 不足要从科研视角挑刺：假设是否过强、基线是否公平、评估指标是否片面、\
实验规模是否足以支撑结论、是否有未讨论的失败场景。
- 所有文本字段使用要求的输出语言（见下），学术但通顺，不用 Markdown 语法（不要 **加粗**、# 标题）。

严格输出 JSON，不要输出任何 JSON 之外的解释文字。格式：

{
  "paper_meta": {
    "title": "论文原始英文/中文标题",
    "authors": ["作者1", "作者2"],
    "year": 2023,
    "venue": "发表会议或期刊，不确定则填 null",
    "arxiv_id": "如 1706.03762，没有则 null",
    "abstract": "用中文 2-3 句话概括论文摘要，不要直译",
    "keywords": ["关键词1", "关键词2", "关键词3"]
  },
  "analysis": {
    "background": "研究背景与要解决的问题。3-5 句，说清这个问题为什么难、之前的方法卡在哪。",
    "innovations": [
      "创新点1：具体说明新在哪、相对谁而言",
      "创新点2",
      "创新点3"
    ],
    "method": "研究方法的实质内容。6-10 句，讲清技术路线的关键设计，以及每个设计为什么这么做。",
    "experiments": "实验设置与结果。6-10 句，包含数据集、对比基线、关键指标数值、消融实验结论。",
    "conclusion": "核心结论。3-4 句，说明作者认为什么被证明了。",
    "limitations": [
      "不足1：从科研视角指出具体缺陷",
      "不足2",
      "不足3"
    ],
    "value": "行业应用价值。3-5 句，说清这个成果能用在什么实际场景、落地还缺什么。",
    "future": [
      "未来方向1",
      "未来方向2",
      "未来方向3"
    ]
  }
}

字数要求：innovations / limitations / future 各 3-5 条；其余字段按上述句数。
如果论文信息不足以支撑某个字段，该字段填「论文未涉及」（英文版填 "Not covered in the paper."）
或空数组。"""


def build_analysis_messages(
    paper_text: str, *, title_hint: str = "", language: str = "zh"
) -> list[dict[str, str]]:
    user_parts = []
    if title_hint:
        user_parts.append(f"（预解析标题，仅供参考，以正文为准：{title_hint}）")
    user_parts.append("以下是论文正文（已去除参考文献与致谢）：\n\n" + paper_text)
    return [
        {"role": "system", "content": ANALYSIS_SYSTEM + output_language_rule(language)},
        {"role": "user", "content": "\n".join(user_parts)},
    ]


# --------------------------------------------------------------------------
# 第二步：双人播客脚本
# --------------------------------------------------------------------------

SCRIPT_SYSTEM = """你是顶级中文播客的撰稿人，专门把学术论文改编成双人对谈节目。\
你的稿子听起来像两个真人聊天，而不是两个人轮流念论文。

【角色设定】
- 主播A（主讲）：懂论文的人。负责讲清楚内容，语气松弛、有热情，会主动打比方。
- 主播B（提问者）：代表听众。负责追问、质疑、要求举例、把跑偏的话题拉回来。\
B不是捧哏，B要真的会问出听众心里的疑问，偶尔可以说「这个我没听明白」「等一下，\
那你刚才说的那个和这个不是矛盾了吗」。

【必须做到】
1. 口语化。写出来的句子要能直接说出口。用短句。允许不完整句、语气词。
2. B的提问要具体。禁问「能详细说说吗」这种空问题，要问「那它在小样本上也是这样吗」。
3. A回答后，B要有真实反应：可以是「哦——所以其实就是…」的复述确认，可以是补充，\
也可以是提出反例。不要每轮都以「原来如此，那……」开头。
4. 至少安排一次「难点解释」：A用一个生活化的类比讲清一个核心机制。
5. 至少安排一次「质疑与回应」：B对论文的某个不足（来自解读里的 limitations）提出\
真实质疑，A承认局限并说明影响范围。
6. 开场不要念标题。用一句能勾起好奇心的话切入。结尾不要喊口号式升华，\
要说清「所以这篇论文对我们意味着什么」，然后自然收尾。

【禁止出现】
- 「首先/其次/再次/最后」这种条目式过渡
- 「综上所述」「总而言之」「值得注意的是」「让我们深入探讨」「不难发现」
- 「本文」「该研究」「作者指出」这类书面第三人称（改成「他们」「这篇」）
- 任何 Markdown 标记、括号注释、括号里的英文
- 直接念公式。公式必须转化成语言描述
- 每个话题都做小总结。真人聊天不做总结

【格式要求】
严格输出 JSON，不要输出 JSON 之外的解释文字：

{
  "segments": [
    {"speaker": "A", "text": "开场白第一段"},
    {"speaker": "B", "text": "第二段"},
    {"speaker": "A", "text": "第三段"}
  ]
}

- speaker 只能是 "A" 或 "B"。
- 每个 segment 是**一次连续发言**，长度 30-120 字，且**绝不能超过 260 个字符**\
（语音合成接口会对单轮超长的发言直接报错，而且真人也不会一口气说那么长）。\
不要把一个意思拆成好几个短 segment，也不要把好几轮并成一大段。
- A 和 B 大体交替，但允许 A 连续说两段（展开一个复杂点），也允许 B 连续追问。
- 不要输出 round 字段，后端会自行编号。"""

# 英文版的格式段。英文不能用「30-120 字」这种说法：模型会把「字」理解成「词」，
# 于是一段写出 50 个词、300 多字符，直接超过 TTS 的单轮上限。
SCRIPT_SYSTEM_EN = """You are a scriptwriter for a top-tier podcast that turns research papers into
two-host conversations. Your script must sound like two real people talking, not two
people reading a paper out loud.

【Roles】
- Host A (the explainer): knows the paper. Explains clearly, relaxed and enthusiastic,
  reaches for analogies unprompted.
- Host B (the listener's voice): asks, pushes back, demands examples, steers back when
  a thread drifts. B is not a hype man — B asks what the listener is actually wondering,
  and may say "I don't follow" or "wait, doesn't that contradict what you just said?"

【Required】
1. Spoken language. Sentences must be sayable out loud. Short sentences. Fragments and
   discourse markers are fine.
2. B's questions must be specific. Never "can you elaborate?" — ask
   "does that still hold with only a handful of examples?"
3. After A answers, B reacts like a person: paraphrase to confirm, add something, or
   raise a counterexample. Don't start every turn with "I see, so...".
4. Include at least one "hard part explained": A makes a core mechanism clear with an
   everyday analogy.
5. Include at least one "challenge and response": B genuinely challenges a limitation
   from the analysis, A admits the scope and explains what it means.
6. Don't open by reading the title. Open with a line that creates curiosity. Don't end
   with a slogan — say what the paper means for us and land it naturally.

【Banned】
- Enumeration transitions: "first / second / finally"
- "In conclusion", "It's worth noting", "Let's dive in", "delve into", "game-changer"
- Third-person academic distancing ("the paper states", "the authors point out") —
  say "they", "this paper"
- Any Markdown, bracketed asides, or parenthetical translations
- Reading formulas aloud. Turn formulas into plain language
- Summarising every topic. Real conversation doesn't summarise

【Format】
Output strict JSON, nothing outside the JSON:

{
  "segments": [
    {"speaker": "A", "text": "opening line"},
    {"speaker": "B", "text": "second turn"},
    {"speaker": "A", "text": "third turn"}
  ]
}

- speaker is only "A" or "B".
- Each segment is ONE continuous turn. **HARD LIMIT: at most 280 characters per
  segment** — the speech engine rejects any single turn longer than that. Aim for
  15-45 words per turn; if a thought is longer, split it across two segments.
  Never merge several turns into one long paragraph.
- A and B mostly alternate, but A may take two turns in a row (unpacking a complex
  point) and B may ask two questions in a row.
- Do not output a round field; the backend numbers them."""


def script_system(language: str) -> str:
    """这一版的 system prompt。英文不能用中文那套格式约束（字/词的陷阱）。"""
    return SCRIPT_SYSTEM_EN if language == "en" else SCRIPT_SYSTEM


def build_script_messages(
    analysis: dict[str, Any],
    paper_meta: dict[str, Any] | None,
    *,
    duration_min: int,
    level: str,
    speech_rate: int = 0,
    padding_sec: float = 0.0,
    language: str = "zh",
) -> list[dict[str, str]]:
    if language == "en":
        return _build_script_messages_en(
            analysis,
            paper_meta,
            duration_min=duration_min,
            level=level,
            speech_rate=speech_rate,
            padding_sec=padding_sec,
        )

    chars = target_chars(duration_min, speech_rate, padding_sec, language)
    rate = effective_rate(speech_rate, language)
    level_text = LEVEL_GUIDE.get(level, LEVEL_GUIDE["intro"])
    meta = paper_meta or {}

    brief = [
        f"【目标时长】{duration_min} 分钟（这套音色的实测语速约 {rate:.0f} 字/分钟，"
        f"另有约 {int(padding_sec)} 秒的片头片尾，所以正文总字数控制在 "
        f"{int(chars * 0.92)}-{int(chars * 1.05)} 字之间，这是硬约束，超了会被裁掉）",
        f"【讲解难度】{level_text}",
        f"【论文标题】{meta.get('title') or '未知'}",
    ]
    if meta.get("venue") or meta.get("year"):
        brief.append(f"【发表信息】{meta.get('venue') or ''} {meta.get('year') or ''}".strip())

    body = f"""请基于下面这份论文解读稿，写一期 {duration_min} 分钟的双人播客脚本。

{chr(10).join(brief)}

【解读稿】
研究背景：{analysis.get('background', '')}

创新点：
{_bullets(analysis.get('innovations'))}

研究方法：{analysis.get('method', '')}

实验结果：{analysis.get('experiments', '')}

核心结论：{analysis.get('conclusion', '')}

存在不足：
{_bullets(analysis.get('limitations'))}

行业价值：{analysis.get('value', '')}

未来方向：
{_bullets(analysis.get('future'))}

请按前述全部要求输出 JSON。注意：这是 {duration_min} 分钟的节目，不要把解读稿\
逐条念一遍——要挑最有信息量的部分展开，其余一笔带过或干脆不提。"""

    return [
        {"role": "system", "content": script_system(language) + output_language_rule(language)},
        {"role": "user", "content": body},
    ]


def _build_script_messages_en(
    analysis: dict[str, Any],
    paper_meta: dict[str, Any] | None,
    *,
    duration_min: int,
    level: str,
    speech_rate: int,
    padding_sec: float,
) -> list[dict[str, str]]:
    """英文版的 user brief。

    必须写成英文，而且**长度单位要说 "words"**：中文版那句「正文总字数控制在
    X-Y 字之间」在英文输出里会被模型当成词的个数，实测英文脚本因此比目标长 30%
    （目标 382 词、实际写了 496 词，成片 238 秒 vs 目标 180 秒）。
    """
    words = target_chars(duration_min, speech_rate, padding_sec, "en")
    rate = effective_rate(speech_rate, "en")
    level_text = LEVEL_GUIDE_EN.get(level, LEVEL_GUIDE_EN["intro"])
    meta = paper_meta or {}

    brief = [
        f"【Target length】{duration_min} minutes. These voices measured at about "
        f"{rate:.0f} words per minute, and roughly {int(padding_sec)} seconds are taken "
        f"by the fixed intro/outro, so the BODY must total "
        f"{int(words * 0.92)}-{int(words * 1.05)} words. This is a hard limit — "
        f"anything longer gets cut.",
        f"【Depth】{level_text}",
        f"【Paper title】{meta.get('title') or 'unknown'}",
    ]
    if meta.get("venue") or meta.get("year"):
        brief.append(f"【Published】{meta.get('venue') or ''} {meta.get('year') or ''}".strip())

    body = f"""Write a {duration_min}-minute two-host podcast script based on the paper analysis below.

{chr(10).join(brief)}

【Analysis】
Background: {analysis.get('background', '')}

Key contributions:
{_bullets(analysis.get('innovations'))}

Method: {analysis.get('method', '')}

Experiments: {analysis.get('experiments', '')}

Conclusion: {analysis.get('conclusion', '')}

Limitations:
{_bullets(analysis.get('limitations'))}

Why it matters: {analysis.get('value', '')}

Open questions:
{_bullets(analysis.get('future'))}

Output the JSON described above. Remember: this is a {duration_min}-minute episode, so do not
walk through the analysis point by point — expand the most informative parts and drop the rest."""  # noqa: E501

    return [
        {"role": "system", "content": script_system("en") + output_language_rule("en")},
        {"role": "user", "content": body},
    ]


def _bullets(items: Any) -> str:
    if not items:
        return "（论文未涉及）"
    if isinstance(items, str):
        return items
    return "\n".join(f"- {item}" for item in items)


# --------------------------------------------------------------------------
# 独立调用：从纯文本推断脚本（当只需要脚本时用，MVP 未启用）
# --------------------------------------------------------------------------


def build_title_messages(paper_text: str) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": "你从论文正文中提取标题。只输出标题本身，不要引号、不要解释、不要句号。",
        },
        {"role": "user", "content": paper_text[:2000]},
    ]


# --------------------------------------------------------------------------
# 第三步（按需）：脚本长度修复
# --------------------------------------------------------------------------
#
# 为什么需要这一步：模型并不总是遵守 prompt 里的字数预算。实测同一套 prompt
# 下，3 分钟档超出 7%、5 分钟档欠了 35%（1065 字 vs 目标 1650 字，产出只有
# 3.27 分钟）。靠措辞约束是不可靠的，所以在生成后做一次确定性检查，
# 不达标才补一次针对性的扩写。
#
# 扩写与「注水」的区别在于：要求模型把抽象讲具体、补例子和类比、把追问写细，
# 而不是加过渡句和总结。prompt 里对此有明确约束。

EXPAND_SYSTEM = """你是播客撰稿人，正在把一版偏短的稿子改写成目标长度。\
听众反馈「内容太赶、很多地方没听懂」，所以你要把话讲透，而不是把话说多。

【怎么做】
- 抽象的地方讲具体：凡是出现「效果好」「有提升」这类说法，都要落到具体数字、具体场景。
- 给每个技术点补一个例子或类比，尤其是机制性内容。
- 把主播B的追问写得更具体、更像真听众会问的问题，A的回答随之展开。
- 可以补充方法的技术细节、实验设置、适用边界，但必须来自给定的解读稿，不许编造。
- 保持原有的观点、结论和事实不变。

【不要做】
- 不要加过渡句、铺垫句、小总结来凑字数。
- 不要重复已经说过的内容换种说法。
- 不要新增原文里没有的数据、结论或评价。
- 保持原有的开场方式和收尾方式，不要改成另一套。

输出 JSON：{"segments": [{"speaker": "A", "text": "..."}]}，speaker 只能是 A 或 B，\
每段 30-120 字，禁止 Markdown。"""


def build_expand_messages(
    segments: list[dict],
    *,
    current_chars: int,
    target: int,
    language: str = "zh",
) -> list[dict[str, str]]:
    script_text = "\n".join(
        f"主播{'A' if seg.get('speaker') == 'A' else 'B'}：{seg.get('text', '')}"
        for seg in segments
    )
    need = max(target - current_chars, 0)
    user = f"""下面这版播客脚本共 {current_chars} 字，目标长度是 {target} 字左右，\
还差约 {need} 字。请在保持结构、观点、事实完全不变的前提下把它扩写到目标长度。

【当前脚本】
{script_text}

请输出扩写后的完整脚本 JSON（是完整替换，不是只输出新增部分）。"""
    return [
        {"role": "system", "content": EXPAND_SYSTEM + output_language_rule(language)},
        {"role": "user", "content": user},
    ]


TRIM_SYSTEM = """你是播客剪辑师，正在把一版偏长的稿子压到目标长度。\
时长超标会让听众觉得拖沓，所以要真的砍，而不是把话说快一点。

【必须达到缩减目标】
用户会给出需要缩减的**具体字数**。这是硬指标：如果只删掉几个字就交回来，
等于没做这件事。宁可舍弃一个次要话题，也要把字数压到位。

【优先砍这些】
- 重复表达同一件事的段落，只留信息量最高的那一版。
- 铺垫句、客套话、不承担信息功能的过渡和附和。
- 为了解释同一个点而连续举的两个例子，留一个够好的。
- 次要的限定条件和补充说明（保留主结论即可）。

【必须保留】
- 开场方式与收尾方式。
- 所有关键数字、核心结论、主要技术要点。
- 主播B 的核心质疑（可以缩短措辞，但不能删掉问题本身）。

【不要做】
- 不要改写观点、结论或数据。
- 不要因为压缩就让句子变得书面化，口语感必须保留。
- 不要输出「（略）」之类的省略标记，要输出可以直接用的完整台词。
- 不要靠删掉开场或收尾来凑数。

输出 JSON：{"segments": [{"speaker": "A", "text": "..."}]}，speaker 只能是 A 或 B，\
每段 30-120 字，禁止 Markdown。"""


def build_trim_messages(
    segments: list[dict],
    *,
    current_chars: int,
    target: int,
    language: str = "zh",
) -> list[dict[str, str]]:
    script_text = "\n".join(
        f"主播{'A' if seg.get('speaker') == 'A' else 'B'}：{seg.get('text', '')}"
        for seg in segments
    )
    excess = max(current_chars - target, 0)
    ratio = int(excess / max(current_chars, 1) * 100)
    user = f"""下面这版播客脚本共 {current_chars} 字，目标长度是 {target} 字左右，\
需要精简掉约 {excess} 字（相当于砍掉 {ratio}%）。这个缩减量是硬指标，
请务必达到；如果只减掉几个字，这次精简就是失败的。

【当前脚本】
{script_text}

请输出精简后的完整脚本 JSON（是完整替换，不是只输出要删的部分）。"""
    return [
        {"role": "system", "content": TRIM_SYSTEM + output_language_rule(language)},
        {"role": "user", "content": user},
    ]
