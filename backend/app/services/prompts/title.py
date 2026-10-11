"""从论文正文里认标题。"""

from __future__ import annotations

__all__ = [
    "build_title_messages",
]


def build_title_messages(paper_text: str) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": "你从论文正文中提取标题。只输出标题本身，不要引号、不要解释、不要句号。",
        },
        {"role": "user", "content": paper_text[:2000]},
    ]
