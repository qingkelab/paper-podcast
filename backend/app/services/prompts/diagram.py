"""动态图解镜头的**草案生成**（模型只提结构，画面由代码画）。

这一条线和别处最大的不同：**模型的输出是要被机器核验的**。
它写下的每个数字、每个术语，都要能在我们给它的证据里找到 —— 找不到就丢掉那一条，
节点不够就整条镜头作废、退回原来的画面（见 `services/diagram.py::verify_shot`）。

所以 prompt 里反复强调三件事：
1. **只写材料里出现过的数字与术语，一个都不许推算、不许凑整**；
2. 每条都要带 `source`，写清它是从哪句来的（我们会逐条核验）；
3. 只在「确实有结构或数字可讲」的段落提镜头，别为了好看硬造一张图。
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from ..design import (
    DIAGRAM_MAX_METRICS,
    DIAGRAM_MAX_NODES,
    DIAGRAM_MAX_SHOTS,
    DIAGRAM_MAX_TITLE_CHARS,
)
from ..video_core import (
    DiagramLink,
    DiagramMetric,
    DiagramNode,
    DiagramShot,
)
from .units import output_language_rule

__all__ = [
    "DIAGRAM_SYSTEM",
    "DIAGRAM_KINDS",
    "build_diagram_messages",
    "build_diagram_repair_note",
    "normalize_diagram_shots",
]

DIAGRAM_KINDS = ("hook", "flow", "compare", "metric", "summary")


DIAGRAM_SYSTEM = """你在给一个「论文解读视频」设计**动态图解镜头**：画面上出现一张会长的图，
节点依次出现、连线按顺序画出来、数字滚到目标值。图不是自由创作的插画，而是**论文信息的结构化呈现**。

【最重要的规矩：不许编造】
- 只使用我给你的材料里**已经出现过的数字与术语**。不许推算、不许四舍五入、不许换单位写成别的数。
- 每一张卡片、每个数字，都要在 `source` 里写清来自哪一句（把原句抄进去，短一点没关系）。
- 我这边会**逐条核验**：数字对不上、术语查不到的条目会被丢掉；剩下不足两个节点的镜头直接作废。
  所以「少写一点、每条都真」远好过「写满、有一半是猜的」。

【只在合适的段落提镜头】
- `metric`：这一段出现了明确的量化结果（指标、倍数、内存、吞吐、准确率……）；
- `flow`：这一段在讲**步骤或因果链**（先做什么、再做什么、为什么）；
- `compare`：这一段在对比两个方案/两个配置（谁处理什么、谁好谁差）；
- `hook`：开场或一节的开头，用一句话抛出问题或结论；
- `summary`：**收尾**用，一句话总结 + `fallback_image` 填论文原图 id（我给你的可用图 id 之一）。
  我会在候选段落里用「← **收尾段**」标出哪一段该放它；那一段**优先**提 `summary`。
- **没有合适内容的段落就不要提镜头**，宁缺毋滥：作废的镜头会退回原来的画面，写了也没用。

【输出格式（严格 JSON，不要解释文字）】
{
  "shots": [
    {
      "segment": 12,                        // 这段脚本的段号（我给你的编号，必须原样抄）
      "kind": "flow",
      "title": "他们凭什么稳住精度",          // 不超过 16 字
      "title_source": "脚本原句：…",
      "subtitle": "",                        // 可选
      "nodes": [                             // flow / compare 用；2~4 个
        {"id": "a", "label": "时间维度", "detail": "活得越久误差越大", "source": "脚本原句：…"}
      ],
      "links": [{"src": "a", "dst": "b"}],   // 可选；只会按顺序一条条画出来
      "metrics": [                           // metric 用；1~3 个
        {"label": "状态内存压缩", "value": 5, "unit": " 倍", "source": "脚本原句：…"}
      ],
      "bullets": ["…"],                      // 可选，最多 2 条；也要写 source
      "bullet_sources": ["脚本原句：…"],
      "fallback_image": ""                   // 可选：有依据的图 id；没有就留空
    }
  ]
}

约束：`title` ≤ 16 字；`nodes` ≤ 4 个且至少 2 个；`metrics` ≤ 3 个；
`label` ≤ 12 字、`detail` ≤ 24 字；`kind` 只能是 hook / flow / compare / metric / summary。
整篇最多提 %(max_shots)d 个镜头，**挑最值得画的那些段落**。
"""


def build_diagram_repair_note(repair: dict) -> str:
    """把「上一次哪些条目没通过核验、为什么」写成给模型的修复线索。

    参考做法（Fibona agent）里最硬的一条规矩：**修复的输入是真实的错误，不是「再试一次」**。
    我们这边的错误是机器给的、逐条带原文的 —— `verify_shot` 会写下
    「节点「Gemini-3-Pro 标注」查不到依据」这种句子，正好可以直接喂回去。

    没有它的代价是具体的：核验不过的镜头原来**整条被丢掉**（退回论文原图），
    而模型根本不知道自己写的哪个数字不在材料里 —— 下一次提还是同样的写法。
    """
    shots = list(repair.get("shots") or [])
    if not shots:
        return ""
    lines = ["", "【上一次的草案里，下面这些条目没有通过机器核验】**这一次只需要重提这几条**："]
    for item in shots:
        reasons = "；".join(str(reason) for reason in (item.get("dropped") or [])) or "整条镜头未通过"
        title = str(item.get("title") or "")[:24]
        lines.append(
            f"- 第 {item.get('segment')} 段「{title}」（{item.get('kind') or 'flow'}）：{reasons}"
        )
    lines.append(
        "这些条目要么改成**材料里原样出现过的**说法（数字、术语一个字都不要自己改），"
        "要么直接删掉那一条 —— 不要凑整、不要推算、不要换一个相近的数字。"
        "已经通过核验的镜头不要重写，段落编号必须和上面一致。"
    )
    return "\n".join(lines)


def build_diagram_messages(
    *,
    segments: Sequence[dict[str, Any]],
    evidence: Sequence[str],
    available_images: Sequence[tuple[str, str]] = (),
    candidates: Iterable[int] | None = None,
    summary_candidates: Iterable[int] = (),
    max_shots: int = DIAGRAM_MAX_SHOTS,
    language: str = "zh",
    timings: Sequence[Any] = (),
    repair: dict | None = None,
) -> list[dict[str, str]]:
    """构造草案请求。

    - `segments`：脚本逐段（只列**候选段**，不让模型面对整篇 —— 它会在无关段落上硬造镜头）；
    - `evidence`：允许作为依据的文本（解读字段 + 脚本原句 + 图注）；
    - `available_images`：`(id, 图注)`，给 `fallback_image` 用；
    - `timings`：可选，带上每段的时长，让模型知道哪几段够长、值得画。
    """
    wanted = list(candidates) if candidates is not None else list(range(len(segments)))
    summary_set = {int(index) for index in summary_candidates}
    lines: list[str] = []
    for index in wanted:
        if index < 0 or index >= len(segments):
            continue
        text = str(segments[index].get("text") or "").strip()
        speaker = str(segments[index].get("speaker") or "")
        seconds = ""
        if index < len(timings):
            timing = timings[index]
            start = getattr(timing, "start", None)
            end = getattr(timing, "end", None)
            if start is not None and end is not None:
                seconds = f"（{float(end) - float(start):.1f} 秒）"
        # 标出收尾段：位置信息由我们给（模型看不到「这是第几段里的最后一段」）
        tag = "　← **收尾段**（这一段优先提 `summary`，`fallback_image` 填论文原图 id）" if index in summary_set else ""
        lines.append(f"[{index}] {speaker}：{text}{seconds}{tag}")

    evidence_block = "\n".join(f"- {item}" for item in evidence if str(item).strip())
    images_block = "\n".join(f"- {image_id}：{caption}" for image_id, caption in available_images)
    user = (
        "【候选段落（只在这些段里提镜头）】\n"
        + "\n".join(lines)
        + "\n\n【可用作依据的材料（数字与术语只许来自这里）】\n"
        + (evidence_block or "（没有）")
        + "\n\n【可用图 id（`fallback_image` 只能填这里的）】\n"
        + (images_block or "（没有）")
        + f"\n\n请挑最值得画的段落，最多 {max_shots} 个镜头，按严格 JSON 输出。"
        + (build_diagram_repair_note(repair) if repair else "")
    )
    return [
        {"role": "system", "content": DIAGRAM_SYSTEM % {"max_shots": max_shots}},
        {"role": "user", "content": user + output_language_rule(language)},
    ]


# --------------------------------------------------------------------------
# 归一化：模型给什么都要能吃得下（残缺/多余/类型不对都不该抛异常）
# --------------------------------------------------------------------------


def _text(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.strip().replace(",", "").replace("%", "")
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def _node(raw: Any, index: int) -> DiagramNode | None:
    if not isinstance(raw, dict):
        return None
    label = _text(raw.get("label"), 12)
    if not label:
        return None
    return DiagramNode(
        id=_text(raw.get("id"), 16) or chr(ord("a") + index),
        label=label,
        detail=_text(raw.get("detail"), 24),
        source=_text(raw.get("source"), 120),
    )


def _metric(raw: Any) -> DiagramMetric | None:
    if not isinstance(raw, dict):
        return None
    value = _number(raw.get("value"))
    label = _text(raw.get("label"), 16)
    if value is None or not label:
        return None
    return DiagramMetric(
        label=label,
        value=value,
        unit=_text(raw.get("unit"), 6),
        source=_text(raw.get("source"), 120),
    )


def normalize_diagram_shots(
    raw: Any,
    *,
    max_shots: int = DIAGRAM_MAX_SHOTS,
    valid_images: Iterable[str] = (),
) -> list[tuple[int, DiagramShot]]:
    """把模型输出归一化成 `[(段号, DiagramShot)]`。**认不出的部分直接丢**，不抛异常。

    为什么返回「段号 + 镜头」而不是只返回镜头：后面要把它塞回**对应的那一段画面**
    （这一段画图解、前后段照旧），段号就是那条线。
    """
    payload = raw
    if isinstance(raw, dict):
        payload = raw.get("shots") or raw.get("items") or []
    if not isinstance(payload, list):
        return []
    allowed_images = {str(item) for item in valid_images}

    out: list[tuple[int, DiagramShot]] = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        try:
            segment = int(entry.get("segment"))
        except (TypeError, ValueError):
            continue
        kind = str(entry.get("kind") or "").strip().lower()
        if kind not in DIAGRAM_KINDS:
            continue
        title = _text(entry.get("title"), DIAGRAM_MAX_TITLE_CHARS)
        if not title:
            continue
        nodes = [
            node
            for node in (_node(item, index) for index, item in enumerate(entry.get("nodes") or []))
            if node is not None
        ][:DIAGRAM_MAX_NODES]
        metrics = [
            metric
            for metric in (_metric(item) for item in (entry.get("metrics") or []))
            if metric is not None
        ][:DIAGRAM_MAX_METRICS]
        ids = {node.id for node in nodes}
        links = tuple(
            DiagramLink(
                src=_text(link.get("src"), 16),
                dst=_text(link.get("dst"), 16),
                label=_text(link.get("label"), 8),
            )
            for link in (entry.get("links") or [])
            if isinstance(link, dict)
            and _text(link.get("src"), 16) in ids
            and _text(link.get("dst"), 16) in ids
        )
        bullets = tuple(_text(item, 30) for item in (entry.get("bullets") or []) if _text(item, 30))[:2]
        sources = tuple(_text(item, 120) for item in (entry.get("bullet_sources") or []))
        fallback = _text(entry.get("fallback_image"), 24)
        out.append(
            (
                segment,
                DiagramShot(
                    kind=kind,
                    title=title,
                    title_source=_text(entry.get("title_source"), 120),
                    subtitle=_text(entry.get("subtitle"), 30),
                    subtitle_source=_text(entry.get("subtitle_source"), 120),
                    nodes=tuple(nodes),
                    links=links,
                    metrics=tuple(metrics),
                    bullets=bullets,
                    bullet_sources=sources,
                    # 图 id 必须是**我们给过的**：模型自己编一个 id 只会指向不存在的图
                    fallback_image=fallback if fallback in allowed_images else "",
                ),
            )
        )
        if len(out) >= max_shots:
            break
    return out
