"""青稞社区的品牌片头 / 片尾。

每期播客都会自动加上这两段，所以措辞要能反复听而不腻，也要经得起
「这是社区在说话」的分量。

设计依据（来自社区公开表述，不是编的）：
- 价值观表述：「只推我们真正读过的东西」
- 口径：「不站技术路线队，站价值观队」
- 内容方向：具身智能、强化学习 / Post-training、模型架构、语音多模态
- 已有产品线：青稞 Talk（直播）、青稞解读（论文解读）

几条自己定的约束：
1. **不说空话**。社区的价值主张就是反注水，片头自己先注水就自相矛盾了。
2. **不喊口号**。不用「欢迎来到」「让我们一起」这类套话。
3. **片头给预期**：告诉听众这期会怎么讲（先讲清楚、再说靠不靠谱），
   而不是自我介绍三句。
4. **片尾给去处**，不煽情、不升华。
5. 短。片头片尾加起来控制在 20 秒左右 —— 每期都听，长了就烦。
"""

from __future__ import annotations

# (主播, 台词)。主播 "A" 是主讲（男声 · 沉稳），"B" 是提问（女声 · 活泼）。
BRAND_INTRO: list[tuple[str, str]] = [
    ("A", "这里是青稞社区。我们只讲自己真正读过的东西。"),
    ("B", "所以每期就一件事：先把这篇论文讲明白，再说它到底站不站得住。"),
    ("A", "不吹，也不绕。开始吧。"),
]

BRAND_OUTRO: list[tuple[str, str]] = [
    ("A", "这篇就聊到这儿。"),
    ("B", "想找原文、想跟着读更多论文，来青稞社区。"),
    ("A", "关注青稞，每天学习最新论文。下期见。"),
]

# 片尾卡片上的行动号召。语音里也会说，但画面上单独留一行大字，
# 因为听众往往是「听到结尾才决定要不要关注」，这时候画面必须给到明确指引。
BRAND_CTA_TITLE = "关注青稞，每天学习最新论文"
BRAND_CTA_SUBTITLE = "青稞社区 · 只推我们真正读过的东西"

# 内置的社区 logo（来源：qingkelab/qingke-video/assets/logo.png，与社区自己的
# 视频用的是同一份）。它是**浅色**字标，为深色底设计 —— 所以片尾卡片做成深色，
# 而正文保持白底（论文配图本身就是白底图表）。
LOGO_FILENAME = "qingke-logo.png"


# --------------------------------------------------------------------------
# 英文版
# --------------------------------------------------------------------------
#
# 不是直译。中文那几句的设计约束（不喊口号、片头给预期、片尾给去处）
# 在英文里要重新措辞才成立 —— 直译过来会变成典型的英文播客套话。
# 比如中文「不吹，也不绕」直译成 "No hype, no beating around the bush"
# 既啰嗦又像推销；英文里 "No hype." 这种短句就够了。

BRAND_INTRO_EN: list[tuple[str, str]] = [
    ("A", "This is Qingke Community. We only talk about papers we've actually read."),
    ("B", "So each episode is one job: explain the paper properly, then say whether it holds up."),
    ("A", "No hype. Let's get into it."),
]

BRAND_OUTRO_EN: list[tuple[str, str]] = [
    ("A", "That's this one wrapped up."),
    ("B", "If you want the paper itself, or more of what we've read, find us at Qingke Community."),
    ("A", "Follow Qingke — a new paper every day. See you next time."),
]

BRAND_CTA_TITLE_EN = "Follow Qingke — a new paper every day"
BRAND_CTA_SUBTITLE_EN = "Qingke Community · We only recommend what we've actually read"


def intro_segments(language: str = "zh") -> list[dict]:
    """片头，转成脚本段格式。"""
    lines = BRAND_INTRO_EN if language == "en" else BRAND_INTRO
    return [
        {"speaker": speaker, "text": text, "round": index, "brand": "intro"}
        for index, (speaker, text) in enumerate(lines)
    ]


def outro_segments(language: str = "zh") -> list[dict]:
    """片尾。"""
    lines = BRAND_OUTRO_EN if language == "en" else BRAND_OUTRO
    return [
        {"speaker": speaker, "text": text, "round": index, "brand": "outro"}
        for index, (speaker, text) in enumerate(lines)
    ]


def logo_path() -> "pathlib.Path":
    """内置 logo 的绝对路径。"""
    import pathlib

    return pathlib.Path(__file__).resolve().parent / "assets" / LOGO_FILENAME


def cta_text(language: str = "zh") -> tuple[str, str]:
    """片尾卡上的关注引导（主句, 副句）。"""
    if language == "en":
        return BRAND_CTA_TITLE_EN, BRAND_CTA_SUBTITLE_EN
    return BRAND_CTA_TITLE, BRAND_CTA_SUBTITLE


def brand_units(language: str = "zh") -> int:
    """片头 + 片尾的「语速单位数」，用于算进时长预算。

    中文是字数，英文是词数 —— 两者量纲不同，所以按语言分别统计：
    英文按空格切词，中文按字符数（标点也算，和语速换算口径一致）。
    """
    lines = (BRAND_INTRO_EN, BRAND_OUTRO_EN) if language == "en" else (BRAND_INTRO, BRAND_OUTRO)
    text = " ".join(t for group in lines for _, t in group)
    if language == "en":
        return len(text.split())
    return len(text)


def brand_char_count(language: str = "zh") -> int:
    """兼容旧调用名。"""
    return brand_units(language)
