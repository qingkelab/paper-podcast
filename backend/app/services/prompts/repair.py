"""长度修补：脚本写短了扩写、写长了压缩（生成后的兜底，不靠 prompt 自觉）。"""

from __future__ import annotations

from .units import output_language_rule

__all__ = [
    "EXPAND_SYSTEM",
    "build_expand_messages",
    "TRIM_SYSTEM",
    "build_trim_messages",
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


def build_trim_messages(
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
    excess = max(current_chars - target, 0)
    ratio = int(excess / max(current_chars, 1) * 100)
    user = f"""下面这版播客脚本共 {current_chars} 字，目标长度是 {target} 字左右，\
需要精简掉约 {excess} 字（相当于砍掉 {ratio}%）。这个缩减量是硬指标，
请务必达到；如果只减掉几个字，这次精简就是失败的。

【当前脚本】
{script_text}

请输出精简后的完整脚本 JSON（是完整替换，不是只输出要删的部分）。"""
    return [
        {"role": "system", "content": TRIM_SYSTEM + output_language_rule(language)},
        {"role": "user", "content": user},
    ]
