"""可用音色目录。

这些 ID 来自豆包语音播客接口的官方配对音色（PodcastTTS 专用），
格式为 `zh_{gender}_{name}_v2_saturn_bigtts`。播客接口要求正好两个音色，
且不支持混音/多情感音色。

要增删音色时改这里即可，前端从 `GET /api/options` 动态读取。
"""

from __future__ import annotations

VOICES: list[dict[str, str]] = [
    {
        "id": "zh_male_dayixiansheng_v2_saturn_bigtts",
        "label": "大义先生（男声 · 沉稳讲解）",
        "gender": "male",
        "pair": "mizai-dayi",
    },
    {
        "id": "zh_female_mizaitongxue_v2_saturn_bigtts",
        "label": "米仔同学（女声 · 活泼提问）",
        "gender": "female",
        "pair": "mizai-dayi",
    },
    {
        "id": "zh_male_liufei_v2_saturn_bigtts",
        "label": "刘飞（男声 · 温和叙述）",
        "gender": "male",
        "pair": "liufei-xiaolei",
    },
    {
        "id": "zh_male_xiaolei_v2_saturn_bigtts",
        "label": "小雷（男声 · 轻快对话）",
        "gender": "male",
        "pair": "liufei-xiaolei",
    },
]

VOICE_IDS = {voice["id"] for voice in VOICES}

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
