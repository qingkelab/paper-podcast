"""语速、字数预算与时长模型 —— 「这期节目该写多少字」只在这里算一次。

这些常量都是**实测反推**出来的（见下面各条的注释），不要凭「中文播客常识」改：
它们决定了 prompr 里给模型的字数区间，改错会让成片时长整体偏 20% 以上。
"""

from __future__ import annotations

__all__ = [
    "CHARS_PER_MINUTE",
    "MUSIC_PADDING_SEC",
    "LANGUAGES",
    "LANGUAGE_LABEL",
    "WORDS_PER_MINUTE_EN",
    "MAX_ROUND_CHARS",
    "AI_CLICHES",
    "SPEED_PERCENT_MAX",
    "SPEED_PERCENT_MIN",
    "effective_rate",
    "output_language_rule",
    "brand_padding_sec",
    "compute_padding_sec",
    "target_chars",
    "estimate_duration_sec",
]


CHARS_PER_MINUTE = 350

# 服务端自带片头/片尾音乐时的实测时长（各约 7.0s / 9.95s）。
# 现在默认**关掉**了服务端音乐（见 config.podcast_head_music），
# 换成社区自己的品牌话术，所以这个常量只在开关打开时才用得上。
MUSIC_PADDING_SEC = 17.0

# 各难度档的表达约束
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


# 「语速档位」的合法范围（百分比偏移）。0 = 正常；+10 = 快 10%；-20 = 慢 20%。
# 超出这个范围的一定是**把「字/分钟」当成了档位**（我本人就传过一次 350，
# 于是 `effective_rate` 静默算成 base×4.5，目标字数凭空涨了 4.5 倍）。
SPEED_PERCENT_MIN = -50
SPEED_PERCENT_MAX = 100


def effective_rate(speech_rate: int = 0, language: str = "zh") -> float:
    """把**语速档位**换算成实际语速（字/分钟 或 词/分钟）。

    ⚠️ `speech_rate` 是**百分比偏移**，不是「字/分钟」：0 = 正常，10 = 快 10%。
    名字有歧义（历史上就这么叫的），所以这里加了硬校验 —— 越界直接报错，
    **不许静默算出 4.5 倍**。中文是字/分钟，英文是词/分钟，两者量纲不同，不能共用一个常量。
    """
    if not SPEED_PERCENT_MIN <= speech_rate <= SPEED_PERCENT_MAX:
        raise ValueError(
            f"语速档位 {speech_rate} 越界（合法范围 {SPEED_PERCENT_MIN}~{SPEED_PERCENT_MAX}）。"
            "这是**百分比偏移**（0 = 正常语速），不是「字/分钟」——"
            "想改基础语速请改 `CHARS_PER_MINUTE` / `WORDS_PER_MINUTE_EN`。"
        )
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
