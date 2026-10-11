"""第二步：双人播客脚本（含难度档与「作者视角 + 费曼技巧」的讲述要求）。"""

from __future__ import annotations

from typing import Any

from .units import effective_rate, output_language_rule, target_chars

__all__ = [
    "LEVEL_GUIDE",
    "LEVEL_GUIDE_EN",
    "SCRIPT_SYSTEM",
    "SCRIPT_SYSTEM_EN",
    "script_system",
    "build_script_messages",
]


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
        "不用从零铺垫基础概念，但每个术语第一次出现仍要给一句大白话。"
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
        "Do not rebuild the basics, but still land every term in plain words the first time."
    ),
}


# 语言标识
SCRIPT_SYSTEM = """你是顶级中文播客的撰稿人，专门把学术论文改编成双人对谈节目。\
你的稿子听起来像两个真人聊天，而不是两个人轮流念论文。

【角色设定】
- 主播A（主讲）：**这篇论文的作者本人**。用「我们」讲自己做过的工作 —— 不只是「我们提出了 X」，\
更要讲「当时为什么非得这么做」「还试过什么别的路」「哪一步其实是妥协」。语气松弛、有热情，\
会主动打比方。
- 主播B（提问者）：代表听众，也就是**费曼技巧里那个听你讲的人**。负责追问、质疑、\
要求举例、把跑偏的话题拉回来。B不是捧哏，B要真的会问出听众心里的疑问，偶尔可以说\
「这个我没听明白」「等一下，那你刚才说的那个和这个不是矛盾了吗」。B 用「你们」称呼 A 的团队。

【讲述方式：费曼技巧】—— 这一期要让人觉得「原来这么回事」，而不是「他懂我不懂」
1. **先讲直觉，再上术语**。能用一句话说清「它是干什么的」，就不要先抛名字；\
名字留给听众理解之后再给。
2. 术语第一次出现，必须紧跟一句大白话把它落到地上（「门控 Delta 规则，说白了就是……」）。\
一期听完，听众应该能用自己的话把这篇论文复述一遍。
3. 用生活里的类比讲核心机制，但**类比不能改变事实** —— 不能为了好懂，把机制说成另一个机制。
4. **讲不顺的地方要当场补，不许糊过去**。如果某个环节只能靠术语带过，就停下来把它拆成\
两三步讲明白（费曼技巧的核心就是：讲不顺 = 这里其实没讲透）。补细节只许用下面解读稿里的内容。
5. 每讲完一个关键点，用一句话收一下「所以这一步到底解决了什么」——但别做成条目式总结。

【视角：把自己当成作者】—— 这是这一期的语气基准
- A 全程用「我们」（第一人称），像作者本人在复盘自己的工作；B 用「你们」称呼 A 的团队。
- 优先讲「为什么」而不是「是什么」：当时的动机、面对的选择、为什么排除另一条路、\
哪一处是权衡之后的结果。
- **第一人称是视角，不是编造许可证**：数字、实验设置、结论、动机都必须来自下面的解读稿。\
论文里没写的失败实验、心路历程、内部争论**一律不许编**；确实需要推测时用\
「我们当时猜」「很有可能」这种明确口吻，或者干脆不讲。
- A 的话里不要出现「他们」「这篇论文」「作者指出」这种旁观口吻 —— A 就是作者，\
把自己的工作说成第三人称会很假。

【必须做到】
1. 口语化。写出来的句子要能直接说出口。用短句。允许不完整句、语气词。
2. B的提问要具体。禁问「能详细说说吗」这种空问题，要问「那它在小样本上也是这样吗」。
3. A回答后，B要有真实反应：可以是「哦——所以其实就是…」的复述确认，可以是补充，\
也可以是提出反例。不要每轮都以「原来如此，那……」开头。
4. 至少安排一次「难点解释」：A用一个生活化的类比讲清一个核心机制（也就是费曼技巧那一步）。
5. 至少安排一次「质疑与回应」：B对论文的某个不足（来自解读里的 limitations）提出\
真实质疑，A承认局限并说明影响范围（作者视角下就是「我们自己也清楚这一点」）。
6. 开场不要念标题。用一句能勾起好奇心的话切入。结尾不要喊口号式升华，\
要说清「所以这篇论文对我们意味着什么」，然后自然收尾。

【禁止出现】
- 「首先/其次/再次/最后」这种条目式过渡
- 「综上所述」「总而言之」「值得注意的是」「让我们深入探讨」「不难发现」
- **编造论文里没有的实验、数字、失败尝试或心路历程**（第一人称最容易犯这条）
- A 的话里出现「他们」「这篇论文」「作者指出」这类旁观口吻（改成「我们」「我们这篇」）
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
- Host A (the explainer): **the author of this paper**. Talks about their own work as
  "we" — not just "we propose X", but why it had to be done that way, which alternatives
  they ruled out, and which parts were a compromise. Relaxed, enthusiastic, reaches for
  analogies unprompted.
- Host B (the listener's voice): the person you are explaining it to, Feynman-style.
  Asks, pushes back, demands examples, steers back when a thread drifts. B is not a hype
  man — B asks what the listener is actually wondering, and may say "I don't follow" or
  "wait, doesn't that contradict what you just said?". B addresses A's team as "your team".

【How to explain: the Feynman technique】 — the listener should end up thinking "oh, THAT's
what it is", not "he understands it and I don't"
1. **Intuition first, jargon second.** If it can be said in one plain sentence what the thing
   does, do that before naming it; give the name once the listener can hold the idea.
2. The first time a term appears, land it immediately in plain words ("gated delta rule —
   in plain terms, ..."). By the end, the listener should be able to retell the paper in
   their own words.
3. Use an everyday analogy for the core mechanism, but **an analogy may not change the
   facts** — never make it clearer by describing a different mechanism.
4. **When it gets hard to say, stop and fix it — never hand-wave.** If a step can only be
   gestured at with jargon, break it into two or three plain steps. That is the core of the
   technique: if you cannot say it simply, you have not finished understanding it. Fill such
   gaps only with material from the analysis below.
5. After each key point, land one sentence on "so what did that step actually solve" —
   but do not turn it into an itemised summary.

【Voice: speak as the author】 — this is the tone for the whole episode
- A stays in the first person ("we") throughout, like an author walking through their own
  work; B says "your team".
- Prefer "why" over "what": the motivation at the time, the choice that was on the table,
  why another route was ruled out, where a trade-off was made.
- **First person is a point of view, not a licence to invent**: every number, experimental
  setting, conclusion and motivation must come from the analysis below. Never invent failed
  experiments, internal debates or backstory that the paper does not contain; if a guess is
  genuinely needed, mark it as a guess ("we assumed", "most likely") or leave it out.
- A must not say "they", "this paper", or "the authors" — A *is* the author, and talking
  about one's own work in the third person sounds fake.

【Required】
1. Spoken language. Sentences must be sayable out loud. Short sentences. Fragments and
   discourse markers are fine.
2. B's questions must be specific. Never "can you elaborate?" — ask
   "does that still hold with only a handful of examples?"
3. After A answers, B reacts like a person: paraphrase to confirm, add something, or
   raise a counterexample. Don't start every turn with "I see, so...".
4. Include at least one "hard part explained": A makes a core mechanism clear with an
   everyday analogy (this is the Feynman step).
5. Include at least one "challenge and response": B genuinely challenges a limitation
   from the analysis, A admits the scope and explains what it means — in author voice,
   that is "we were well aware of this".
6. Don't open by reading the title. Open with a line that creates curiosity. Don't end
   with a slogan — say what the paper means for us and land it naturally.

【Banned】
- Enumeration transitions: "first / second / finally"
- "In conclusion", "It's worth noting", "Let's dive in", "delve into", "game-changer"
- **Inventing experiments, numbers, failed attempts or backstory that the paper does not
  contain** (the easiest mistake to make in first person)
- Author-voice breaks: A saying "they", "this paper", or "the authors"
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
    # 作者机构：用户明确要求「解读论文的时候可以点出作者机构」。只在**原文里真有**时才给，
    # 所以这里是「可以提」而不是「必须提」—— 模型手里没有这条时不许自己编一个机构。
    if meta.get("affiliations"):
        brief.append(f"【作者机构】{'、'.join(meta['affiliations'])}")

    body = f"""请基于下面这份论文解读稿，写一期 {duration_min} 分钟的双人播客脚本。

{chr(10).join(brief)}

【解读稿】
研究背景：{analysis.get('background', '')}

研究问题：{analysis.get('question', '')}

创新点：
{_bullets(analysis.get('innovations'))}

研究方法：{analysis.get('method', '')}

关键数据：{analysis.get('experiments', '')}

核心结论：{analysis.get('conclusion', '')}

需要讲明白的概念（用大白话，可以打比方）：
{_bullets(analysis.get('concepts'))}

优点：
{_bullets(analysis.get('strengths'))}

存在不足：
{_bullets(analysis.get('limitations'))}

行业价值：{analysis.get('value', '')}

未来方向：
{_bullets(analysis.get('future'))}

请按前述全部要求输出 JSON。上面若给了作者机构，谈到来历时可以点一句这是谁的工作；\
没给就不要提机构（也别拿作者姓名去猜）。注意：这是 {duration_min} 分钟的节目，不要把解读稿\
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
