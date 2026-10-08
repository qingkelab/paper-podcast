"""可用音色目录。

这些 ID 来自豆包语音播客接口的官方配对音色（PodcastTTS 专用），
格式为 `zh_{gender}_{name}_v2_saturn_bigtts`。播客接口要求正好两个音色，
且不支持混音/多情感音色。

要增删音色时改这里即可，前端从 `GET /api/options` 动态读取。
"""

from __future__ import annotations

VOICES: list[dict[str, str]] = [
    # ---- 英文 ----
    # 实测这两个音色能直接用于播客接口（不需要 speaker_additions）。
    # 英文播客的自然语速约 150 词/分钟（见 prompts.WORDS_PER_MINUTE_EN）。
    {
        "id": "en_male_alex_uranus_bigtts",
        "label": "Alex（English · 男声，主讲）",
        "gender": "male",
        "pair": "alex-jenny",
        "language": "en",
    },
    {
        "id": "en_female_jenny_uranus_bigtts",
        "label": "Jenny（English · 女声，提问）",
        "gender": "female",
        "pair": "alex-jenny",
        "language": "en",
    },
    # ---- 中文 ----
    {
        "id": "zh_male_dayixiansheng_v2_saturn_bigtts",
        "label": "大义先生（男声 · 沉稳讲解）",
        "gender": "male",
        "pair": "mizai-dayi",
        "language": "zh",
    },
    {
        "id": "zh_female_mizaitongxue_v2_saturn_bigtts",
        "language": "zh",
        "label": "米仔同学（女声 · 活泼提问）",
        "gender": "female",
        "pair": "mizai-dayi",
    },
    {
        "id": "zh_male_liufei_v2_saturn_bigtts",
        "language": "zh",
        "label": "刘飞（男声 · 温和叙述）",
        "gender": "male",
        "pair": "liufei-xiaolei",
    },
    {
        "id": "zh_male_xiaolei_v2_saturn_bigtts",
        "language": "zh",
        "label": "小雷（男声 · 轻快对话）",
        "gender": "male",
        "pair": "liufei-xiaolei",
    },
]

VOICE_IDS = {voice["id"] for voice in VOICES}

# 支持的语言版本。顺序 = 前端新建表单里的展示顺序。
LANGUAGE_LABELS: dict[str, str] = {
    "zh": "中文",
    "en": "English",
}

# 每种语言的展示名（用在列表卡片、下载文件名等地方）
LANGUAGE_NAMES = {"zh": "中文", "en": "English"}

DURATIONS = [
    {"value": 3, "label": "3 分钟"},
    {"value": 5, "label": "5 分钟"},
    {"value": 10, "label": "10 分钟"},
]

LEVELS = [
    {"value": "intro", "label": "入门（面向非专业听众）"},
    {"value": "advanced", "label": "进阶（面向同领域研究生）"},
    {"value": "expert", "label": "专业（面向同方向研究者）"},
]


def normalize_voice(candidate: str | None, fallback: str) -> str:
    """只接受目录内的音色，未知的一律回退到默认值。"""
    if candidate and candidate in VOICE_IDS:
        return candidate
    return fallback


def voices_for(language: str) -> list[dict[str, str]]:
    """某个语言的可用音色。"""
    return [v for v in VOICES if v.get("language", "zh") == language]


def default_voices(language: str, settings) -> tuple[str, str]:
    """某个语言的默认主播 A / B 音色。

    两版必须用各自语言的音色 —— 拿中文音色去念英文虽然也能出声，
    但拿英文音色念中文会明显不对。
    """
    if language == "en":
        return settings.default_voice_a_en, settings.default_voice_b_en
    return settings.default_voice_a, settings.default_voice_b
