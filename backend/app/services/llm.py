"""豆包方舟（Volcengine Ark）大模型客户端。

两件事：论文结构化解读、双人播客脚本生成。没有配置密钥时自动降级为 Mock，
使整条流水线在零密钥情况下也能端到端跑通。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from ..config import Settings
from . import prompts

logger = logging.getLogger(__name__)

_JSON_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


class LLMError(Exception):
    """模型调用失败，调用方应把 message 展示给用户。"""


# --------------------------------------------------------------------------
# JSON 容错解析
# --------------------------------------------------------------------------


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
# 客户端
# --------------------------------------------------------------------------


class LLMClient:
    """论文解读 + 播客脚本生成。

    支持两个提供方，它们都是 OpenAI 兼容的 /chat/completions：
    - DeepSeek（默认）：https://api.deepseek.com
    - 豆包方舟（Ark）：https://ark.cn-beijing.volces.com/api/v3
    切换只改配置，调用代码完全一致。都没配密钥时降级为 Mock。
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.mode = settings.llm_mode
        self.mock = self.mode == "mock"
        self.base_url, self.api_key, self.model, self.timeout = settings.llm_credentials
        self.provider_label = {"deepseek": "DeepSeek", "doubao": "豆包方舟"}.get(
            self.mode, "Mock"
        )
        if self.mock:
            logger.warning(
                "大模型未配置（缺少 DEEPSEEK_API_KEY 或 ARK_API_KEY），"
                "解读与脚本将使用 Mock 数据"
            )
        else:
            logger.info("文本解读使用 %s（model=%s）", self.provider_label, self.model)

    # ---------- 对外 ----------

    def analyze_paper(
        self, paper_text: str, *, title_hint: str = ""
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """返回 (paper_meta, analysis)。"""
        if self.mock:
            return mock_analysis(paper_text, title_hint=title_hint)

        data = self._chat_json(
            prompts.build_analysis_messages(paper_text, title_hint=title_hint),
            max_tokens=4000,
        )
        meta = _normalize_meta(data.get("paper_meta"))
        analysis = _normalize_analysis(data.get("analysis"))
        if not analysis["innovations"] and not analysis["method"]:
            raise LLMError("模型未能给出有效的论文解读内容")
        return meta, analysis

    def generate_script(
        self,
        analysis: dict[str, Any],
        paper_meta: dict[str, Any] | None,
        *,
        duration_min: int,
        level: str,
    ) -> dict[str, Any]:
        """返回 script dict：{segments, word_count, est_duration_sec}。"""
        if self.mock:
            return mock_script(analysis, paper_meta, duration_min=duration_min, level=level)

        data = self._chat_json(
            prompts.build_script_messages(
                analysis, paper_meta, duration_min=duration_min, level=level
            ),
            max_tokens=8000,
            temperature=0.9,  # 脚本需要文采，适度放开
        )
        segments = _normalize_segments(data.get("segments"))
        if len(segments) < 4:
            raise LLMError("模型生成的播客脚本过短，无法合成")
        return build_script_payload(segments)

    # ---------- 内部 ----------

    def _chat_json(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int,
        temperature: float = 0.6,
    ) -> dict[str, Any]:
        raw = self._chat(messages, max_tokens=max_tokens, temperature=temperature)
        return parse_json_response(raw)

    def _chat(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int,
        temperature: float,
    ) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            # 两个提供方都支持 JSON 输出模式。开启后模型不会在外面裹客套话，
            # 解析成功率显著提升（parse_json_response 仍保留兜底）。
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        url = f"{self.base_url.rstrip('/')}/chat/completions"

        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(url, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            raise LLMError(f"{self.provider_label} 请求超时，请稍后重试") from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"{self.provider_label} 请求失败：{exc}") from exc

        if response.status_code == 401:
            raise LLMError(
                f"{self.provider_label} 鉴权失败（401），请检查 API Key 是否正确/是否欠费"
            )
        if response.status_code == 402:
            raise LLMError(f"{self.provider_label} 账户余额不足（402）")
        if response.status_code == 404:
            raise LLMError(
                f"{self.provider_label} 接口或模型不存在（404）：model={self.model!r}，"
                f"base_url={self.base_url}"
            )
        if response.status_code == 429:
            raise LLMError(f"{self.provider_label} 触发限流（429），请稍后重试")
        if response.status_code >= 400:
            raise LLMError(
                f"{self.provider_label} 返回错误 {response.status_code}：{response.text[:300]}"
            )

        try:
            body = response.json()
        except ValueError as exc:
            raise LLMError(f"{self.provider_label} 返回了非 JSON 响应") from exc

        choices = body.get("choices") or []
        if not choices:
            raise LLMError(f"{self.provider_label} 返回中没有 choices：{str(body)[:200]}")
        content = (choices[0].get("message") or {}).get("content") or ""
        if not isinstance(content, str) or not content.strip():
            raise LLMError(f"{self.provider_label} 返回了空 message")

        usage = body.get("usage") or {}
        if usage:
            logger.info(
                "%s 用量：prompt=%s completion=%s",
                self.provider_label,
                usage.get("prompt_tokens"),
                usage.get("completion_tokens"),
            )
        return content


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
        "innovations": _as_list(data.get("innovations")),
        "method": _as_str(data.get("method")),
        "experiments": _as_str(data.get("experiments")),
        "conclusion": _as_str(data.get("conclusion")),
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


def build_script_payload(segments: list[dict[str, Any]]) -> dict[str, Any]:
    """补上 round 编号与字数/时长统计。"""
    total_chars = 0
    payload_segments = []
    for index, segment in enumerate(segments):
        text = segment["text"]
        total_chars += len(text)
        payload_segments.append(
            {"speaker": segment["speaker"], "text": text, "round": index}
        )
    return {
        "segments": payload_segments,
        "word_count": total_chars,
        "est_duration_sec": round(total_chars / prompts.CHARS_PER_MINUTE * 60),
    }


# --------------------------------------------------------------------------
# Mock 实现：零密钥时让整条流水线可跑通
# --------------------------------------------------------------------------

_MOCK_META_POOL = [
    {
        "title": "Attention Is All You Need",
        "authors": ["Ashish Vaswani", "Noam Shazeer", "Niki Parmar"],
        "year": 2017,
        "venue": "NeurIPS",
        "arxiv_id": "1706.03762",
        "abstract": "论文提出完全基于注意力机制的 Transformer 架构，抛弃了循环与卷积结构，"
        "在机器翻译任务上取得当时最优效果，并显著提升了训练并行度。",
        "keywords": ["Transformer", "Self-Attention", "机器翻译"],
    },
    {
        "title": "Deep Residual Learning for Image Recognition",
        "authors": ["Kaiming He", "Xiangyu Zhang", "Shaoqing Ren", "Jian Sun"],
        "year": 2016,
        "venue": "CVPR",
        "arxiv_id": "1512.03385",
        "abstract": "论文提出残差学习框架，通过恒等捷径连接缓解深层网络退化问题，"
        "使 152 层甚至上千层网络的训练成为可能，并在 ImageNet 上取得冠军成绩。",
        "keywords": ["ResNet", "残差学习", "图像分类"],
    },
    {
        "title": "LoRA: Low-Rank Adaptation of Large Language Models",
        "authors": ["Edward J. Hu", "Yelong Shen", "Phillip Wallis"],
        "year": 2022,
        "venue": "ICLR",
        "arxiv_id": "2106.09685",
        "abstract": "论文提出低秩适配方法，冻结预训练权重并注入可训练的低秩分解矩阵，"
        "把可训练参数量降低数个数量级，同时保持与全量微调相当的效果。",
        "keywords": ["LoRA", "参数高效微调", "大语言模型"],
    },
]


def _pick_mock_meta(paper_text: str, title_hint: str = "") -> dict[str, Any]:
    """按正文关键词挑一个最贴近的示例论文，让 Mock 数据看起来不突兀。"""
    haystack = (paper_text[:6000] + " " + title_hint).lower()
    if any(k in haystack for k in ("attention", "transformer", "self-attention")):
        return dict(_MOCK_META_POOL[0])
    if any(k in haystack for k in ("residual", "resnet", "image recognition")):
        return dict(_MOCK_META_POOL[1])
    if any(k in haystack for k in ("lora", "low-rank", "adapter")):
        return dict(_MOCK_META_POOL[2])
    meta = dict(_MOCK_META_POOL[0])
    if title_hint:
        meta["title"] = title_hint
    return meta


def mock_analysis(paper_text: str, *, title_hint: str = "") -> tuple[dict[str, Any], dict[str, Any]]:
    meta = _pick_mock_meta(paper_text, title_hint)
    topic = meta["title"]
    analysis = {
        "background": (
            f"围绕「{topic}」这一问题，此前的主流做法依赖逐步骤的串行计算或人工设计的结构先验，"
            "导致训练难以并行、规模难以扩大。论文要回答的是：能否用一种更通用的机制，"
            "在同等甚至更少的计算量下取得更好的效果。"
        ),
        "innovations": [
            "用统一的注意力机制替换了原有的串行结构，使计算路径长度从线性降到常数级，训练可完全并行。",
            "通过多头机制让模型在不同表示子空间并行建模，相比单头注意力在同等参数下效果更稳定。",
            "给出了与既有方法在同等计算预算下的公平对比，验证收益来自结构本身而非参数量堆叠。",
        ],
        "method": (
            "方法的核心是把输入映射成查询、键、值三组表示，用查询与键的相似度决定每个位置应当"
            "关注哪些位置，再据此对值加权求和。多头设计让模型同时从多个角度做这件事，"
            "最后拼接并线性变换。相比串行结构，这种设计的关键好处是所有位置可以并行计算，"
            "并且任意两个位置之间的交互都只需要一步。论文还加入位置编码来补回顺序信息，"
            "以及残差连接与归一化来稳定深层训练。"
        ),
        "experiments": (
            "在标准机器翻译基准上，模型在 WMT14 英德任务取得 28.4 BLEU，超出此前最优结果约 2 BLEU，"
            "同时训练成本明显下降。消融实验显示，去掉多头机制或位置编码都会造成可测量的性能损失，"
            "其中位置编码的影响在长句上更显著。论文还验证了模型在语法成分分析等任务上的可迁移性。"
        ),
        "conclusion": (
            "论文证明了序列建模不必依赖串行结构，注意力机制本身足以支撑强性能，"
            "并且这种结构在并行度和可扩展性上具有结构性优势。"
        ),
        "limitations": [
            "自注意力的计算与内存开销随序列长度呈平方增长，长文档场景下代价迅速变得不可接受。",
            "论文的主要验证集中在机器翻译，向其他模态的泛化能力在当时尚未充分验证。",
            "位置编码采用固定形式，对训练时未见过的更长序列外推能力有限。",
        ],
        "value": (
            "该结构后来成为大语言模型的基础组件，直接支撑了预训练加微调的技术范式。"
            "在工程上，它让大规模并行训练成为可能，是当前主流模型能够扩展到千亿参数的前提之一。"
        ),
        "future": [
            "降低注意力对序列长度的平方复杂度，例如稀疏化、线性近似或分块计算。",
            "改进位置表示以提升长度外推能力，适应超长上下文场景。",
            "把该结构迁移到视觉、语音、多模态等非文本模态并验证其通用性。",
        ],
    }
    return meta, analysis


def mock_script(
    analysis: dict[str, Any],
    paper_meta: dict[str, Any] | None,
    *,
    duration_min: int,
    level: str,
) -> dict[str, Any]:
    title = (paper_meta or {}).get("title") or "这篇论文"
    innovations = analysis.get("innovations") or []
    limitations = analysis.get("limitations") or []
    first_innovation = innovations[0] if innovations else "它换了一条技术路线"

    lines: list[tuple[str, str]] = [
        ("A", f"今天聊的这篇《{title}》，一句话说结论：它把过去必须一步步算的东西，变成了一次算完。"),
        ("B", "等一下，这个听起来像是纯工程优化？我想知道的是，它到底是省了时间，还是真的效果也变好了。"),
        ("A", "两个都有。这也就是它最值钱的地方——在同等计算预算下效果更好，这就不只是快了。"),
        ("B", f"那核心改动是什么？我不想听「提出了一个新架构」这种话。"),
        ("A", f"{first_innovation}这么说还是抽象，我打个比方：以前的模型像接力赛，一棒传一棒，必须等前一棒跑完；现在改成所有人同时看全场，谁重要谁就多给点注意力。"),
        ("B", "哦——所以关键不是计算变少了，而是本来要串起来等的那些步骤，现在可以并行了。"),
        ("A", "对，而且不只是并行。因为它允许任意两个位置直接建立联系，信息传递的路径变短了，长距离依赖也更好学。"),
        ("B", "那问题来了，这样算的话，序列一长，两两之间都要算一遍，开销不是爆炸了吗？"),
        ("A", f"这正是它被诟病的地方。{(limitations[0] if limitations else '复杂度随长度平方增长')}所以后来一大堆工作都在解决长度问题。"),
        ("B", "也就是说，这个方法在短序列上是占优的，长序列上是有代价的，不能无脑用。"),
        ("A", "可以这么理解。它换来的通用性和可扩展性，是要用长序列上的计算代价来买的。这笔账划不划算，取决于你的场景。"),
        ("B", "那对做研究的人来说，这篇的东西现在还有什么可挖的？"),
        ("A", "主要是两件事：一是怎么把那个平方复杂度降下来，二是怎么让位置表示支持更长的上下文。这两个方向到现在还是活跃的。"),
        ("B", "明白了。所以它不是终点，是把赛道换了一条，然后大家都在新赛道上继续跑。"),
        ("A", "这个说法挺准。今天就聊到这儿。"),
    ]

    # 按目标时长裁剪：保留首尾，删掉中间对谈
    target = prompts.target_chars(duration_min)
    total = sum(len(t) for _, t in lines)
    while total > target * 1.15 and len(lines) > 6:
        remove_at = len(lines) - 3
        total -= len(lines[remove_at][1])
        lines.pop(remove_at)

    segments = [{"speaker": speaker, "text": text} for speaker, text in lines]
    return build_script_payload(segments)
