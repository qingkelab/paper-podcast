"""两处共用的文字收尾：把句尾的虚词去掉。

单独一个模块是为了避免循环依赖：封面标题（`cover`）和画面强调行（`points`）
都要用同一套「收短之后别以虚词结尾」的规则，而这两个模块互不依赖。
"""

from __future__ import annotations

__all__ = [
    "strip_trailing_stopwords",
]


_TRAILING_STOPWORDS: dict[str, frozenset[str]] = {
    "en": frozenset(
        """and or but the a an to of with for in on that is are was were be been being as by at
        from it its this these those than then so if when while which who whom whose not no into
        over under about after before""".split()
    ),
    "zh": frozenset("的 了 和 与 或 而 就 还 也 在 是 把 被 对 从 到 这 那 并 且 以 及 有 会".split()),
}


def strip_trailing_stopwords(text: str, *, language: str, floor: int) -> str:
    """反复去掉结尾的虚词，直到不是虚词或剩下的太短为止。"""
    stopwords = _TRAILING_STOPWORDS.get(language, frozenset())
    if not stopwords:
        return text
    # 英文按空格分词；中文按字判断（中文虚词就是单字）
    if language == "zh":
        while text and text[-1] in stopwords and len(text) - 1 >= floor:
            text = text[:-1]
        return text.rstrip("，,、 ")
    words = text.split()
    while words and words[-1].strip(".,!?;:").lower() in stopwords:
        if len(" ".join(words[:-1])) < floor:
            break
        words = words[:-1]
    return " ".join(words).rstrip(" ,;:")
