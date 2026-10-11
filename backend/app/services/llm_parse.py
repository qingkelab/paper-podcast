"""模型输出的解析与归一化：把「模型不听话」的那些返回捏成能用的结构。

`parse_json_response` 从带围栏 / 带客套话的输出里抠出 JSON，`LLMError` 也在这里
（解析失败要抛它，所以它必须住在最底层）；`_normalize_*` 负责把缺字段、类型不对的
解读与脚本段兜底成固定形状。`build_script_payload` / `split_long_segments` 是对
**归一化之后的段落**做后处理（编号、字数、TTS 单轮上限），`llm_mock` 要用它们。

依赖方向：本模块在底层，**不 import `llm.py` / `llm_mock.py`**。
"""

from __future__ import annotations

import json
import re
from typing import Any

from . import prompts


class LLMError(Exception):
    """模型调用失败，调用方应把 message 展示给用户。"""


# --------------------------------------------------------------------------
# JSON 容错解析
# --------------------------------------------------------------------------


_JSON_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


def parse_json_response(raw: str) -> dict[str, Any]:
    """从模型输出里抠出 JSON。

    即使要求了 json_object，模型偶尔仍会包一层 ```json 围栏或加一句客套话，
    所以这里做三层兜底：直接解析 → 去围栏 → 取第一个平衡的 {...}。
    """
    text = (raw or "").strip()
    if not text:
        raise LLMError("模型返回了空内容")

    for candidate in (text, _JSON_FENCE.sub("", text).strip()):
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    balanced = _extract_balanced_object(text)
    if balanced:
        try:
            parsed = json.loads(balanced)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    raise LLMError(f"模型输出不是合法 JSON：{text[:200]}")


def _extract_balanced_object(text: str) -> str | None:
    """扫描出第一个括号平衡的 JSON 对象，跳过字符串内的花括号。"""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


# --------------------------------------------------------------------------
# 归一化：模型输出可能缺字段/类型不对，这里统一兜底
# --------------------------------------------------------------------------


def _as_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = [p.strip(" -•\t") for p in re.split(r"[\n;；]", value)]
        return [p for p in parts if p]
    if isinstance(value, list):
        return [_as_str(v) for v in value if _as_str(v)]
    return [_as_str(value)]


def _normalize_meta(value: Any) -> dict[str, Any]:
    data = value if isinstance(value, dict) else {}
    year = data.get("year")
    try:
        year = int(year) if year not in (None, "", "null") else None
    except (TypeError, ValueError):
        year = None
    return {
        "title": _as_str(data.get("title")) or None,
        "authors": _as_list(data.get("authors")),
        # 作者机构（首页单位署名）。**取不到就是空数组**，绝不从别处补 ——
        # 猜错的机构比不写更糟（解读里会照它讲「这是谁的工作」）。
        "affiliations": _as_list(data.get("affiliations")),
        "abstract": _as_str(data.get("abstract")) or None,
        "year": year,
        "venue": _as_str(data.get("venue")) or None,
        "arxiv_id": _as_str(data.get("arxiv_id")) or None,
        "keywords": _as_list(data.get("keywords")),
    }


def _normalize_analysis(value: Any) -> dict[str, Any]:
    data = value if isinstance(value, dict) else {}
    return {
        "background": _as_str(data.get("background")),
        "question": _as_str(data.get("question")),
        "innovations": _as_list(data.get("innovations")),
        "method": _as_str(data.get("method")),
        "experiments": _as_str(data.get("experiments")),
        "conclusion": _as_str(data.get("conclusion")),
        "concepts": _as_list(data.get("concepts")),
        "strengths": _as_list(data.get("strengths")),
        "limitations": _as_list(data.get("limitations")),
        "value": _as_str(data.get("value")),
        "future": _as_list(data.get("future")),
    }


def _normalize_segments(value: Any) -> list[dict[str, Any]]:
    """把模型给的 segments 归一化成 [{speaker, text}]，并丢掉空段。"""
    if not isinstance(value, list):
        return []
    segments: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        speaker = _as_str(item.get("speaker")).upper()
        # 容忍模型写成 "主播A" / "a" / "A："
        if "B" in speaker:
            speaker = "B"
        elif "A" in speaker:
            speaker = "A"
        else:
            speaker = "A" if len(segments) % 2 == 0 else "B"
        text = _as_str(item.get("text"))
        text = re.sub(r"^\s*[（(]?\s*主播\s*[ABab]\s*[）)]?\s*[:：]\s*", "", text)
        if text:
            segments.append({"speaker": speaker, "text": text})
    return segments


def split_long_segments(
    segments: list[dict[str, Any]],
    *,
    max_chars: int = prompts.MAX_ROUND_CHARS,
) -> list[dict[str, Any]]:
    """把超过 TTS 单轮上限的发言切成多轮。

    ⚠️ 为什么必须有这个兜底：豆包播客 TTS 对**单个 round 的文本长度**有硬上限
    （实测 >300 字符直接拒绝，错误码 40000010）。prompt 里写了「每段 30-120 字」，
    中文基本守规矩；**英文不守** —— 模型把「120 字」当成 120 个词，
    写出一段 50 词 = 309 字符，于是整个任务在 TTS 阶段失败。
    实测双语首次运行就这么挂的，而且失败后 worker 会整条流水线重跑一遍
    （重新解读、重新生成配图、重新合成中文音频），代价很大。

    切分点优先选句末标点，其次逗号，其次硬切 —— 尽量落在自然的停顿处，
    听感上就是同一人换了口气。speaker 保持不变，但会变成连续两个 round。
    输出顺序与输入一致，round 由 build_script_payload 重新编号。
    """
    if max_chars <= 0:
        return list(segments)

    out: list[dict[str, Any]] = []
    for segment in segments:
        text = str(segment.get("text") or "")
        if len(text) <= max_chars:
            out.append(dict(segment))
            continue
        for piece in _split_text(text, max_chars):
            out.append({**segment, "text": piece})
    return out


# 切分优先在这些标点之后断开（从「最自然」到「最勉强」）
_BREAK_TOKENS = ("。", "！", "？", "…", ". ", "! ", "? ", "；", "; ", "，", ", ", "、", " ")


def _split_text(text: str, max_chars: int) -> list[str]:
    """把一段长文本切成若干不超过 max_chars 的片段。"""
    pieces: list[str] = []
    remaining = text.strip()
    while len(remaining) > max_chars:
        window = remaining[:max_chars]
        cut = -1
        for token in _BREAK_TOKENS:
            index = window.rfind(token)
            # 别切出一个只有几个字的碎片
            if index >= max_chars // 2:
                cut = index + len(token)
                break
        if cut <= 0:
            cut = max_chars
        piece = remaining[:cut].strip()
        if piece:
            pieces.append(piece)
        remaining = remaining[cut:].strip()
    if remaining:
        pieces.append(remaining)
    return pieces or [text]


def build_script_payload(
    segments: list[dict[str, Any]],
    *,
    speech_rate: int = 0,
    padding_sec: float = 0.0,
    language: str = "zh",
) -> dict[str, Any]:
    """补上 round 编号与字数/时长统计。

    `word_count` 是**时长预算的单位数**：中文数字符，英文数词。
    两者量纲不同（350 字/分 vs 150 词/分），必须跟 `effective_rate` 对齐，
    否则英文的时长会被算错（按字符数当年词数会高估 4~5 倍）。
    """
    total_chars = 0
    payload_segments = []
    for index, segment in enumerate(segments):
        text = segment["text"]
        total_chars += len(text.split()) if language == "en" else len(text)
        entry: dict[str, Any] = {
            "speaker": segment["speaker"],
            "text": text,
            "round": index,
        }
        # 保留品牌标记（intro / outro）：视频层要靠它把片尾渲染成品牌卡片
        if segment.get("brand"):
            entry["brand"] = segment["brand"]
        payload_segments.append(entry)
    return {
        "segments": payload_segments,
        "word_count": total_chars,
        "est_duration_sec": prompts.estimate_duration_sec(
            total_chars, speech_rate, padding_sec, language
        ),
    }
