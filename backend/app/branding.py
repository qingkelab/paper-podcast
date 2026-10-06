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
    ("B", "如果你想找原文、想看我们读过的其他论文，来青稞社区。"),
    ("A", "我们只推真正读过的东西。下期见。"),
]


def intro_segments() -> list[dict]:
    """片头，转成脚本段格式。"""
    return [
        {"speaker": speaker, "text": text, "round": index, "brand": "intro"}
        for index, (speaker, text) in enumerate(BRAND_INTRO)
    ]


def outro_segments() -> list[dict]:
    """片尾。"""
    return [
        {"speaker": speaker, "text": text, "round": index, "brand": "outro"}
        for index, (speaker, text) in enumerate(BRAND_OUTRO)
    ]


def brand_char_count() -> int:
    """片头 + 片尾的总字数，用于把它算进时长预算。"""
    return sum(len(text) for _, text in BRAND_INTRO + BRAND_OUTRO)
