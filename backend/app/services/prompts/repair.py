"""长度修补：脚本写短了扩写、写长了压缩（生成后的兜底，不靠 prompt 自觉）。"""

from __future__ import annotations

from .units import output_language_rule

__all__ = [
    "EXPAND_SYSTEM",
    "build_expand_messages",
    "TRIM_SYSTEM",
    "build_trim_messages",
    "trim_feedback_lines",
]


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


def _unit(text: str, language: str) -> int:
    """字数单位：中文数符，英文数词（和 `build_script_payload` 同一套口径）。"""
    return len(text.split()) if language == "en" else len(text)


def trim_feedback_lines(
    current: list[dict],
    *,
    base: list[dict] | None = None,
    language: str = "zh",
    limit: int = 6,
) -> list[str]:
    """「上一版每段砍了多少」/「这一版最长的是这几段」——**具体到段**的精简线索。

    为什么要有这个：第二次精简原来只是加一句「这次必须砍掉 X 字」。模型看的是**原稿**，
    它不知道上一次哪几段没动，于是往往把同样那几段再砍一遍（或者整篇重写一遍、
    字数还是差不多）。带上去的数据分两种：

    - 上一版和原稿段数一致 → 逐段差值，**砍得最少的排前面**（那才是要动的地方）；
    - 段数变了（模型重排过）→ 退一步给出这一版**最长的几段**，也是同一件事的近似。

    两边都是确定性的（不靠模型判断），所以可以断言。
    """
    if not current:
        return []
    counts = [_unit(str(seg.get("text") or ""), language) for seg in current]
    if base and len(base) == len(current):
        deltas = [
            _unit(str(base[index].get("text") or ""), language) - counts[index]
            for index in range(len(current))
        ]
        order = sorted(range(len(current)), key=lambda index: (deltas[index], -counts[index]))
        head = "【上一版每一段砍了多少】（砍得最少的在前面 —— 这些才是这次要动的地方）"
        rows = [
            f"第 {index + 1} 段（{current[index].get('speaker') or 'A'}）："
            f"{_unit(str(base[index].get('text') or ''), language)} → {counts[index]} 字"
            f"（砍了 {deltas[index]} 字）"
            for index in order[:limit]
        ]
        return [head, *rows]

    order = sorted(range(len(current)), key=lambda index: -counts[index])
    head = "【这一版最长的几段】（篇幅就是砍的空间，优先从这里下手）"
    rows = [
        f"第 {index + 1} 段（{current[index].get('speaker') or 'A'}）：{counts[index]} 字，"
        f"开头是「{str(current[index].get('text') or '')[:16]}…」"
        for index in order[:limit]
    ]
    return [head, *rows]


def build_trim_messages(
    segments: list[dict],
    *,
    current_chars: int,
    target: int,
    language: str = "zh",
    attempt: int = 1,
    previous: dict | None = None,
) -> list[dict[str, str]]:
    script_text = "\n".join(
        f"主播{'A' if seg.get('speaker') == 'A' else 'B'}：{seg.get('text', '')}"
        for seg in segments
    )
    excess = max(current_chars - target, 0)
    ratio = int(excess / max(current_chars, 1) * 100)
    retry_note = (
        "\n**注意：这是第二次精简，上一次没有压到位。** 这次必须真正砍掉 "
        f"{excess} 字左右 —— 宁可少讲一个次要细节，也要落到 {target} 字上下。"
        if attempt >= 2
        else ""
    )
    # **修复要带真实的错误**，不是只说一句「再来一次」：上一版砍到多少、还差多少、
    # 哪几段几乎没动 —— 这些数字才算「上一次为什么没成功」。
    feedback = ""
    if previous:
        lines = trim_feedback_lines(
            segments, base=previous.get("base_segments"), language=language
        )
        was = previous.get("chars")
        base_chars = previous.get("base_chars")
        still_over = max(current_chars - int(target * 1.15), 0)
        head = (
            f"\n【上一版的结果】上一版已经是 {was} 字"
            + (f"（原稿 {base_chars} 字）" if base_chars else "")
            + f"，仍比预算上限（{int(target * 1.15)} 字）多 {still_over} 字 —— "
            "**那一次的精简判定为失败**。这次请把差额补齐。"
        )
        feedback = "\n" + head + ("\n" + "\n".join(lines) if lines else "")
    user = f"""下面这版播客脚本共 {current_chars} 字，目标长度是 {target} 字左右，\
需要精简掉约 {excess} 字（相当于砍掉 {ratio}%）。这个缩减量是硬指标，
请务必达到；如果只减掉几个字，这次精简就是失败的。{retry_note}{feedback}

【当前脚本】
{script_text}

请输出精简后的完整脚本 JSON（是完整替换，不是只输出要删的部分）。"""
    return [
        {"role": "system", "content": TRIM_SYSTEM + output_language_rule(language)},
        {"role": "user", "content": user},
    ]
