"""两处共用的文字收尾：把句尾的虚词去掉。

单独一个模块是为了避免循环依赖：封面标题（`cover`）和画面强调行（`points`）
都要用同一套「收短之后别以虚词结尾」的规则，而这两个模块互不依赖。
"""

from __future__ import annotations

import re

__all__ = [
    "strip_trailing_stopwords",
    "first_person_hits",
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


# ---------------------------------------------------------------------------
# 第一人称/第二人称：**用户明确要求脚本与解读里不要出现「我们」「你们」**
# ---------------------------------------------------------------------------
#
# 为什么要有这个确定性检查：prompt 里早就写着「不要用『我们』冒充作者」，
# 但同一份 prompt 又留了一句「日常口语里表示『我们一起看』的『我们』可以用」——
# 模型就顺着这个口子写「我们做了两件事」「我们这篇论文」「你们怎么解决」
# （实测一集 41 段里 14 处）。**规矩写了不等于模型会守**，所以要能数出来。
#
# 检查器只报数、不改写：改文案是模型的事（真要兜底得带原句去修，那是另一条路）。
# 品牌片头/片尾不查 —— 那几句是 `branding.py` 里写死的社区话术，
# 里面有「我们只讲自己真正读过的东西」，那是品牌语气，不算违规。
_FIRST_PERSON = ("我们", "咱们", "你们")


# 引号内的内容不算：论文标题经常就带第一/第二人称
# （「Attention Is All You Need」里有 You；中文稿子里标题通常写在《》里），
# 报它等于每集都误报一次。
_QUOTED = re.compile(r"[\u201c\u201d\u2018\u2019「」『』《》\"\'][^\u201c\u201d「」『』《》\"\']*[\u201c\u201d\u2018\u2019「」『』《》\"\']")


def _without_quotes(text: str) -> str:
    return _QUOTED.sub(" ", text)


def first_person_hits(
    segments: list[dict], *, language: str = "zh"
) -> list[tuple[int, str]]:
    """扫出脚本里带第一/第二人称的段：返回 `[(段号, 那句话), …]`。

    只认**成词**的「我们/咱们/你们」；英文查 `we/our/us/you` 的独立词形
    （`we\'re`、`you\'ve` 也算）。**引号里的内容跳过**（论文原题常常就带 You），
    品牌片头/片尾也跳过（那是 `branding.py` 写死的社区话术）。
    """
    hits: list[tuple[int, str]] = []
    for index, segment in enumerate(segments):
        if str(segment.get("brand") or ""):
            continue  # 品牌话术不查（那是写死的社区语气）
        text = str(segment.get("text") or "")
        probe = _without_quotes(text)
        if language == "en":
            found = re.search(r"\b(we|our|ours|us|you|your|yours)\b", probe, re.I)
        else:
            found = next((word for word in _FIRST_PERSON if word in probe), None)
        if found:
            hits.append((index, text))
    return hits
