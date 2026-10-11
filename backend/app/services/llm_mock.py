"""无密钥时的假数据：让整条流水线在零密钥下也能端到端跑通。

`mock_analysis` / `mock_script` 是 `LLMClient` 在 Mock 模式下走的路径；
`_MOCK_META_POOL` 是三篇经典论文的示例元数据，按正文关键词挑一篇，离线演示才不突兀。
中英文各一份，英文那份是**真正的英文对谈**（不是中文的翻译副本），否则看不出语言切换。

依赖方向：本模块**不 import `llm.py`** —— `LLMClient.analyze_paper` 会调
`mock_analysis`，反过来 import 就成环了。共用的小工具从 `llm_parse` 取。
"""

from __future__ import annotations

from typing import Any

from . import prompts
from .llm_parse import build_script_payload


# --------------------------------------------------------------------------
# Mock 实现：零密钥时让整条流水线可跑通
# --------------------------------------------------------------------------


def _mock_script_en(
    *,
    title: str,
    innovations: list[str],
    limitations: list[str],
    duration_min: int,
    speech_rate: int,
    padding_sec: float,
    language: str,
) -> dict[str, Any]:
    """英文版 Mock 脚本（真正的英文对谈，不是中文的翻译副本）。"""
    first = innovations[0] if innovations else "it swapped out the whole approach"
    # 同中文 Mock：作者视角 + 费曼技巧，离线演示的文案要和线上一个风格
    lines: list[tuple[str, str]] = [
        ("A", f"\u201c{title}\u201d is our paper. The one-line version: we turned something that had to be computed step by step into something computed all at once."),
        ("B", "Wait, you're one of the authors? Good — then start with the naive question. Is this an engineering win, or does it actually work better?"),
        ("A", "Both, but not in the order you'd expect. We started out just wanting training to be faster; somewhere along the way we noticed it also did better at equal compute."),
        ("B", "So what's the actual change? I don't want to hear \u201ca novel architecture\u201d."),
        ("A", f"{first} That's still jargon. Think of it this way: the old model is a relay race where every runner waits for the baton. We let everyone watch the whole field at once and give more weight to whoever matters."),
        ("B", "Okay — so the point isn't that it does less work, it's that the steps that used to wait on each other can now happen in parallel."),
        ("A", "Right, and not only parallel. Because any two positions can connect directly, information travels a shorter path, so long-range dependencies are easier to learn. We didn't expect that — the ablations are what convinced us."),
        ("B", "Here's my problem with it: once the sequence gets long, you're comparing everything to everything. Didn't you worry the cost would explode?"),
        ("A", f"We worried about it constantly, and it's the weakness we're most clear-eyed about. {(limitations[0] if limitations else 'Cost grows quadratically with length')} A whole line of later work is about that one cut."),
        ("B", "So it's a clear win on short sequences and a real cost on long ones — not something you use blindly."),
        ("A", "That's fair. We traded long-sequence compute for generality and scalability. Whether that trade is worth it depends on what you're building — we wouldn't decide that for you."),
        ("B", "If you were starting again today, where would you put the effort?"),
        ("A", "Two places: bringing that quadratic cost down, and making positional representations hold up over much longer context. Both are still active."),
        ("B", "Got it. So it isn't the finish line — it moved the track, and everyone kept running on the new one."),
        ("A", "That's a good way to end it. Thanks for listening."),
    ]

    target = prompts.target_chars(duration_min, speech_rate, padding_sec, language)
    total = sum(len(t.split()) for _, t in lines)
    while total > target * 1.15 and len(lines) > 6:
        remove_at = len(lines) - 3
        total -= len(lines[remove_at][1].split())
        lines.pop(remove_at)

    segments = [{"speaker": speaker, "text": text} for speaker, text in lines]
    return build_script_payload(
        segments, speech_rate=speech_rate, padding_sec=padding_sec, language=language
    )


_MOCK_META_POOL = [
    {
        "title": "Attention Is All You Need",
        "authors": ["Ashish Vaswani", "Noam Shazeer", "Niki Parmar"],
        "affiliations": ["Google Brain", "Google Research", "University of Toronto"],
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
        "affiliations": ["Microsoft Research Asia"],
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
        "affiliations": ["Microsoft", "Microsoft Azure AI"],
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


def _mock_analysis_en(topic: str) -> dict[str, Any]:
    """英文版 Mock 解读。

    刻意写成真正的英文，而不是把中文那段复制过去 —— 离线演示要能看出
    语言切换确实换了内容，否则等于没验证。
    """
    return {
        "background": (
            f"Work on \u201c{topic}\u201d previously relied on step-by-step sequential computation "
            "or hand-designed structural priors. That made training hard to parallelise and hard "
            "to scale. The paper asks a blunt question: can a more general mechanism do better "
            "at the same or lower compute cost?"
        ),
        "question": (
            "The paper asks whether attention alone, without any sequential recurrence, can match or "
            "beat the best sequence models at equal or lower compute."
        ),
        "innovations": [
            "It replaces the sequential structure with a single attention mechanism, cutting the "
            "path length between any two positions from linear to constant and making training "
            "fully parallel.",
            "A multi-head design lets the model attend in several representation subspaces at once, "
            "which is measurably more stable than a single head at equal parameter count.",
            "It reports a like-for-like comparison against existing methods at equal compute budget, "
            "so the gain can be attributed to the architecture rather than to extra parameters.",
        ],
        "method": (
            "The input is projected into queries, keys and values. The similarity between a query "
            "and each key decides which positions to attend to, and those weights are used to "
            "combine the values. Multiple heads do this in parallel from different angles; the "
            "results are concatenated and linearly projected. The key benefit over sequential "
            "structures is that every position can be computed at once, and any two positions "
            "interact in a single step. Positional encodings restore order information, while "
            "residual connections and normalisation keep deep stacks trainable."
        ),
        "experiments": (
            "On standard translation benchmarks the model reaches 28.4 BLEU on WMT14 English-German, "
            "roughly 2 BLEU above the previous best, at materially lower training cost. Ablations "
            "show measurable losses when either the multi-head mechanism or the positional encoding "
            "is removed, with the positional encoding mattering more on longer sentences. The paper "
            "also checks transfer to constituency parsing."
        ),
        "conclusion": (
            "The paper shows sequence modelling does not require sequential structure: attention "
            "alone is enough for strong results, and it has a structural advantage in parallelism "
            "and scalability."
        ),
        "concepts": [
            "Self-attention: every position looks at every other position and pulls back a weighted "
            "average of their information, so any two positions connect in one step instead of "
            "propagating through a chain.",
            "Multi-head: the same attention is run several times in parallel, each head tracking a "
            "different kind of relation, and the results are concatenated.",
            "Positional encoding: attention itself is order-blind, so the index of each token has to "
            "be injected separately or word order becomes invisible.",
        ],
        "strengths": [
            "Complexity analysis, a like-for-like compute-budget comparison and ablations are reported "
            "together, so the gain is attributable to the architecture rather than to scale.",
            "Each component (multi-head, positional encoding) is ablated, showing none of them is decorative.",
            "The setup is reproducible: data, compute and metrics are specified, so later work can compare "
            "on the same footing.",
        ],
        "limitations": [
            "Self-attention cost grows quadratically with sequence length, which becomes prohibitive "
            "for long documents.",
            "Validation is concentrated on machine translation; generalisation to other modalities "
            "was not established at the time.",
            "Fixed positional encodings extrapolate poorly to sequences longer than those seen in training.",
        ],
        "value": (
            "The architecture later became a building block for large language models and underpins "
            "the pretrain-then-finetune paradigm. In engineering terms it made large-scale parallel "
            "training practical, which is what allowed models to grow to hundreds of billions of parameters."
        ),
        "future": [
            "Reducing the quadratic cost of attention through sparsity, linear approximations or chunking.",
            "Better positional representations for length extrapolation and very long context.",
            "Transferring the architecture to vision, speech and multimodal settings and testing how general it really is.",
        ],
    }


def mock_analysis(
    paper_text: str, *, title_hint: str = "", language: str = "zh"
) -> tuple[dict[str, Any], dict[str, Any]]:
    meta = _pick_mock_meta(paper_text, title_hint)
    if language == "en":
        return meta, _mock_analysis_en(meta.get("title") or "this paper")
    topic = meta["title"]
    analysis = {
        "background": (
            f"围绕「{topic}」这一问题，此前的主流做法依赖逐步骤的串行计算或人工设计的结构先验，"
            "导致训练难以并行、规模难以扩大。论文要回答的是：能否用一种更通用的机制，"
            "在同等甚至更少的计算量下取得更好的效果。"
        ),
        "question": (
            "论文要回答的是：在不依赖串行结构的前提下，只用注意力机制能否在同等或更低的计算量下，"
            "取得不低于此前最优序列模型的翻译质量。"
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
        "concepts": [
            "自注意力：让序列里每个位置都去看一遍其他所有位置，然后按「有多相关」加权把信息取回来，"
            "所以任意两处的联系都只需要一步，不必像循环网络那样一步步传递。",
            "多头：把同一套注意力并排做几份，每份关注不同的关系（比如语法搭配、指代、位置远近），"
            "最后拼起来，比只做一份更稳。",
            "位置编码：注意力本身分不出先后顺序，得额外把「第几个词」注入进去，"
            "否则「狗咬人」和「人咬狗」在模型眼里是一样的。",
        ],
        "strengths": [
            "把复杂度分析、公平计算预算下的对比和消融放在一起做，收益能归因到结构本身，而不是参数量堆叠。",
            "消融实验逐个拆掉部件（多头、位置编码），说明每个设计都不是装饰。",
            "结论可复现：训练配置、数据规模和指标都给了，后来的工作能在同一套设置上继续比。",
        ],
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
    speech_rate: int = 0,
    padding_sec: float = 0.0,
    language: str = "zh",
) -> dict[str, Any]:
    title = (paper_meta or {}).get("title") or "这篇论文"
    innovations = analysis.get("innovations") or []
    limitations = analysis.get("limitations") or []
    first_innovation = innovations[0] if innovations else "它换了一条技术路线"

    if language == "en":
        return _mock_script_en(
            title=title,
            innovations=innovations,
            limitations=limitations,
            duration_min=duration_min,
            speech_rate=speech_rate,
            padding_sec=padding_sec,
            language=language,
        )

    # Mock 也按「作者视角 + 费曼技巧」写：离线演示里看到的文案要和线上同一个风格，
    # 否则改完 prompt 打开演示页还以为是没生效（这个坑在本项目里踩过好几次）。
    lines: list[tuple[str, str]] = [
        ("A", f"《{title}》这篇是我们做的。先说最要紧的一句：我们把过去必须一步一步算的东西，变成了一次算完。"),
        ("B", "等一下，你们是作者？那正好。我想先问最朴素的：这是纯工程优化，还是效果真的变好了？"),
        ("A", "两个都有，但顺序不是你想的那样。我们一开始只是想让训练跑得快一点，做着做着发现：同等计算量下效果反而更好了。"),
        ("B", "那核心改动到底是什么？我不想听「提出了一个新架构」这种话。"),
        ("A", f"{first_innovation}这句还是术语。打个比方，以前的模型像接力赛，一棒传一棒，必须等前一棒跑完；我们把它改成所有人同时看全场，谁重要谁就多分点注意力。"),
        ("B", "哦——所以关键不是算得更少，而是本来要排队等的那些步骤，现在能一起做了。"),
        ("A", "对，但还不止。因为任意两个位置可以直接建立联系，信息不用绕路，长距离的依赖也更好学。这一点我们当时没预料到，是消融实验做出来才确认的。"),
        ("B", "那问题来了：序列一长，两两之间都要算一遍，开销不是要爆炸吗？你们想过这个吗？"),
        ("A", f"当然想过，这也是我们最清楚的一个短板。{(limitations[0] if limitations else '复杂度随长度平方增长')}后来一大堆工作都在补这一刀。"),
        ("B", "也就是说，短序列上你们占优，长序列上要付代价，不能无脑用。"),
        ("A", "可以这么理解。通用性和可扩展性是用长序列上的计算代价换来的。这笔账划不划算，取决于你的场景，我们不敢替你下结论。"),
        ("B", "那如果现在让你们继续做，会往哪儿使劲？"),
        ("A", "主要是两件事：一是把那个平方复杂度压下来，二是让位置表示撑得住更长的上下文。这两个方向到现在都还是活跃的。"),
        ("B", "明白了。所以它不是终点，是把赛道换了一条，然后大家都在新赛道上继续跑。"),
        ("A", "这个说法挺准。今天就聊到这儿。"),
    ]

    # 按目标时长裁剪：保留首尾，删掉中间对谈
    target = prompts.target_chars(duration_min, speech_rate, padding_sec, language)
    total = sum(len(t) for _, t in lines)
    while total > target * 1.15 and len(lines) > 6:
        remove_at = len(lines) - 3
        total -= len(lines[remove_at][1])
        lines.pop(remove_at)

    segments = [{"speaker": speaker, "text": text} for speaker, text in lines]
    return build_script_payload(
        segments, speech_rate=speech_rate, padding_sec=padding_sec, language=language
    )
