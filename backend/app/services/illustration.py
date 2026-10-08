"""生成配图：让大模型写一张 SVG 信息图，再转成 PNG。

## 为什么是 SVG 而不是「HTML 转图片」

原始设想是「生成 HTML → 截图成图片」。实测这条路在本项目里不可行：
Chrome headless 在本机会挂死，Playwright 装浏览器超时。而且 HTML 截图产物
是死图，没法动。

SVG 反而更贴合需求：
- **原生支持动画**（SMIL 的 animate/animateMotion、CSS animation），
  直接把 SVG 交给浏览器就是动态的，不需要录屏或 GIF 编码
- 矢量，任意尺寸都清晰
- 不依赖浏览器，用 resvg（Rust 实现，自包含）就能栅格化

所以：详情页直出 SVG（动画可播），列表/缩略图用栅格化的 PNG。

## 安全

SVG 是要交给浏览器渲染的，而它是模型生成的。这里做两层防护：
1. 解析后**白名单式清洗**：删掉 script/foreignObject/事件属性/外部引用
2. 只通过同源接口返回，不允许内联到页面 HTML 里

另外，模型偶尔会输出非法 SVG，所以始终有一个本地生成的兜底图，
保证任何情况下封面都不为空。
"""

from __future__ import annotations

import html
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SVG_NS = "http://www.w3.org/2000/svg"
ET.register_namespace("", SVG_NS)

# 信息图尺寸（16:9），详情页头部和列表封面都用这个比例
CANVAS_W = 1280
CANVAS_H = 720

# 危险元素：直接移除
_FORBIDDEN_TAGS = {"script", "foreignobject", "iframe", "embed", "object", "animateTransform"}

# 危险属性：以这些前缀开头的全部移除（事件处理器、外链命名空间等）
_FORBIDDEN_ATTR_PREFIXES = ("on",)

# 明确禁止的属性
_FORBIDDEN_ATTRS = {
    "href",
    "xlink:href",
    "src",
    "action",
    "formaction",
    "style",  # 交由白名单重建，避免 url() 外链
}

_ALLOWED_URL_SCHEMES = ("data:image/",)


class IllustrationError(Exception):
    """配图生成失败。属于可降级错误。"""


@dataclass
class Illustration:
    svg_path: str
    png_path: str
    width: int
    height: int
    source: str  # "model" | "fallback"

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------
# Prompt
# --------------------------------------------------------------------------

ILLUSTRATION_SYSTEM = """你是信息图设计师，专门把论文的核心机制画成一张 SVG 信息图。\
你的作品风格克制、学术、深色底，信息密度高但不拥挤。

【输出要求】
- 只输出一个完整的 `<svg>` 元素，不要 markdown 代码围栏，不要任何解释文字。
- 必须有 `xmlns="http://www.w3.org/2000/svg"` 和 `viewBox="0 0 1280 720"`。
- 画布 1280x720，深色底（例如 #101a2b），四角圆润。
- 所有样式写成**元素属性**（fill / stroke / font-size / font-family / opacity），\
不要用 <style> 标签，不要用 class。
- 字体统一写 `font-family="PingFang SC, Hiragino Sans GB, Microsoft YaHei, sans-serif"`。
- 文字用简体中文，术语可保留英文原词。字号：主标题 40-48，副标题 22-26，\
方框内文字 20-24，注释 16-18。
- 文字必须落在画布内，留出至少 48px 边距。中文一个字约占一个字号宽度，\
先用这个估算再定位，宁可留白也不要让文字超出画布或互相重叠。

【画什么】
- 用**方框 + 箭头 + 分组**画出论文的核心机制或流程，让不懂的人一眼看懂数据怎么流动。
- 至少体现 2-3 个关键设计点，并在图上直接标注关键数字（论文里的指标、倍数、参数量）。
- 右下角用小字标注论文标题与年份。

【可以做动画，但要有节制】
- 可以用 SMIL 让关键流程发光、圆点沿路径流动，帮助表达「信息如何传递」。
- 动画必须 `repeatCount="indefinite"`，单个动画周期 2-4 秒。
- 不要用 `<animateTransform>`，不要做闪烁或快速位移。

【绝对禁止】
- 不要 `<script>`、`<foreignObject>`、`<image>`、任何外部链接或 `url(...)` 引用。
- 不要用 Markdown、不要输出 `<svg>` 之外的任何字符。"""


def build_illustration_messages(
    analysis: dict[str, Any],
    paper_meta: dict[str, Any] | None,
    language: str = "zh",
) -> list[dict[str, str]]:
    meta = paper_meta or {}
    bullets = analysis.get("innovations") or []
    brief = [
        f"论文标题：{meta.get('title') or '未知'}",
        f"发表年份：{meta.get('year') or '未知'}",
        "",
        f"研究背景：{analysis.get('background', '')}",
        "",
        "核心创新点：",
        *[f"- {item}" for item in bullets[:4]],
        "",
        f"研究方法：{analysis.get('method', '')}",
        "",
        f"关键结果：{analysis.get('experiments', '')}",
        "",
        "请为这篇论文画一张信息图，把上面这些要点组织成一张能被一眼读懂的图。",
        "重点画「机制是怎么运作的」，而不是罗列文字。",
    ]
    # 图上的文字跟随这一版的语言：英文版配一张满是中文标注的信息图会很割裂。
    lang_rule = (
        "\n\n【图上文字】全部用 **英文**（English）。"
        '字体写 `font-family="Inter, Helvetica Neue, Arial, sans-serif"`。'
        if language == "en"
        else ""
    )
    return [
        {"role": "system", "content": ILLUSTRATION_SYSTEM + lang_rule},
        {"role": "user", "content": "\n".join(brief)},
    ]


# --------------------------------------------------------------------------
# 清洗
# --------------------------------------------------------------------------


def _strip_bad_attributes(element: ET.Element) -> None:
    for name in list(element.attrib):
        lowered = name.lower()
        if lowered in _FORBIDDEN_ATTRS:
            del element.attrib[name]
            continue
        if any(lowered.startswith(prefix) for prefix in _FORBIDDEN_ATTR_PREFIXES):
            del element.attrib[name]
            continue
        value = element.attrib[name]
        if isinstance(value, str):
            # 只放行内嵌 data:image 之类的安全引用
            if re.search(r"(javascript|vbscript|file|https?)\s*:", value, re.I):
                if not value.strip().lower().startswith(_ALLOWED_URL_SCHEMES):
                    del element.attrib[name]


def sanitize_svg(svg_text: str) -> str:
    """白名单式清洗模型生成的 SVG，去掉一切可执行/外链内容。"""
    text = (svg_text or "").strip()

    # 去掉 markdown 围栏
    text = re.sub(r"^```(?:svg|xml)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()

    start = text.find("<svg")
    end = text.rfind("</svg>")
    if start < 0 or end < 0:
        raise IllustrationError("模型输出里没有完整的 <svg> 元素")
    text = text[start : end + 6]

    # 解析前先粗暴剔除注释和 DOCTYPE（可能藏实体炸弹）
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    text = re.sub(r"<!DOCTYPE[^>]*>", "", text, flags=re.IGNORECASE)

    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise IllustrationError(f"模型输出的 SVG 不是合法 XML：{exc}") from exc

    if not root.tag.endswith("svg"):
        raise IllustrationError("模型输出的根元素不是 <svg>")

    for parent in root.iter():
        for child in list(parent):
            tag = child.tag.split("}")[-1].lower()
            if tag in _FORBIDDEN_TAGS:
                parent.remove(child)
                continue
            _strip_bad_attributes(child)
    _strip_bad_attributes(root)

    # 统一补上必需属性，保证任何渲染器都能正确处理
    #
    # ⚠️ 这里**不要**手动 set("xmlns", ...)：因为上面注册了默认命名空间，
    # ElementTree 序列化时会自动输出 xmlns，手动再加一次会变成
    # 重复属性 → XML 非法（resvg 宽容能渲染，但严格解析器和浏览器会报错）。
    root.set("viewBox", f"0 0 {CANVAS_W} {CANVAS_H}")
    root.set("width", str(CANVAS_W))
    root.set("height", str(CANVAS_H))
    root.attrib.pop("style", None)

    # 模型如果漏写 xmlns，标签会没有命名空间。逐个改写回 SVG 命名空间，
    # 否则序列化出来的 <svg> 没有 xmlns，浏览器不会当成 SVG 渲染。
    if not root.tag.startswith("{"):
        for element in root.iter():
            if not element.tag.startswith("{"):
                element.tag = f"{{{SVG_NS}}}{element.tag}"

    output = ET.tostring(root, encoding="unicode")

    # 自校验：清洗后的结果必须能被重新解析，否则宁可退回兜底图
    try:
        ET.fromstring(output)
    except ET.ParseError as exc:
        raise IllustrationError(f"清洗后的 SVG 不合法：{exc}") from exc

    return output


# --------------------------------------------------------------------------
# 栅格化
# --------------------------------------------------------------------------


def rasterize_svg(svg_text: str, output_path: Path, *, scale: float = 1.0) -> tuple[Path, int, int]:
    """用 resvg 把 SVG 渲染成 PNG。不依赖浏览器。"""
    try:
        import resvg_py
    except ImportError as exc:  # pragma: no cover
        raise IllustrationError("服务端缺少 resvg-py 依赖，无法栅格化 SVG") from exc

    width = int(CANVAS_W * scale)
    height = int(CANVAS_H * scale)

    try:
        png_bytes = bytes(
            resvg_py.svg_to_bytes(
                svg_string=svg_text,
                width=width,
                height=height,
                languages=["zh-Hans", "en"],
            )
        )
    except Exception as exc:  # noqa: BLE001
        raise IllustrationError(f"SVG 栅格化失败：{exc}") from exc

    if not png_bytes:
        raise IllustrationError("SVG 栅格化返回了空内容")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(png_bytes)
    return output_path, width, height


# --------------------------------------------------------------------------
# 兜底信息图（不依赖模型，永远可用）
# --------------------------------------------------------------------------


def _char_width(char: str) -> float:
    """按 em 估算单字宽度。中文/全角算 1，西文和空格约 0.55。"""
    code = ord(char)
    if code < 0x2E80:  # 拉丁、数字、标点等基本半角区
        return 0.55
    return 1.0


def _tokenize(text: str) -> list[str]:
    """切成可折行的最小单位：中文单字一个 token，西文按单词。"""
    tokens: list[str] = []
    buffer = ""
    for char in text:
        if _char_width(char) >= 1.0:  # 全角：单字成 token
            if buffer:
                tokens.append(buffer)
                buffer = ""
            tokens.append(char)
        elif char == " ":
            if buffer:
                tokens.append(buffer)
                buffer = ""
            tokens.append(" ")
        else:
            buffer += char
    if buffer:
        tokens.append(buffer)
    return tokens


def _wrap_cjk(text: str, per_line: int, max_lines: int) -> list[str]:
    """按行宽折行。

    `per_line` 的单位是**全角字符数**，西文按 0.55 em 折算。
    折行以「词」为单位，避免英文标题被从单词中间劈开（Lang|uage 那种）。
    """
    text = re.sub(r"\s+", " ", (text or "").strip())
    if not text:
        return []

    lines: list[str] = []
    current = ""
    width = 0.0
    truncated = False

    for token in _tokenize(text):
        token_width = sum(_char_width(c) for c in token)

        if current and width + token_width > per_line:
            lines.append(current.strip())
            current = ""
            width = 0.0
            if len(lines) >= max_lines:
                truncated = True
                break

        if not current and token == " ":
            continue  # 行首不留空格

        # 单个 token 就超过整行宽（超长单词）→ 硬切
        while token_width > per_line:
            head = ""
            head_width = 0.0
            for char in token:
                char_width = _char_width(char)
                if head and head_width + char_width > per_line:
                    break
                head += char
                head_width += char_width
            lines.append(head)
            token = token[len(head):]
            token_width = sum(_char_width(c) for c in token)
            if len(lines) >= max_lines:
                truncated = True
                break

        if len(lines) >= max_lines:
            truncated = True
            break

        current += token
        width += token_width

    if len(lines) < max_lines and current.strip():
        lines.append(current.strip())
    elif truncated and lines:
        lines[-1] = lines[-1][:-1].rstrip() + "…"

    return lines


def fallback_svg(
    analysis: dict[str, Any], paper_meta: dict[str, Any] | None, language: str = "zh"
) -> str:
    """本地拼一张信息图。

    用途：模型没配置、调用失败、或输出不合法时，保证封面不为空。
    纯字符串拼接，没有任何外部依赖，也绝不会失败。
    """
    meta = paper_meta or {}
    title = (meta.get("title") or ("论文解读" if language == "zh" else "Paper explained")).strip()
    year = meta.get("year")
    subtitle_bits = [str(b) for b in (meta.get("venue"), year) if b]
    fallback_subtitle = "AI 播客解读" if language == "zh" else "AI podcast explainer"
    subtitle = " · ".join(subtitle_bits) if subtitle_bits else fallback_subtitle

    innovations = [str(x) for x in (analysis.get("innovations") or [])][:3]
    keywords = [str(k) for k in (meta.get("keywords") or [])][:4]

    parts: list[str] = [
        f'<svg xmlns="{SVG_NS}" viewBox="0 0 {CANVAS_W} {CANVAS_H}" '
        f'width="{CANVAS_W}" height="{CANVAS_H}">',
        "<defs>",
        '<linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">',
        '<stop offset="0%" stop-color="#0e1729"/><stop offset="100%" stop-color="#1d3a63"/>',
        "</linearGradient>",
        "</defs>",
        f'<rect width="{CANVAS_W}" height="{CANVAS_H}" fill="url(#bg)" rx="24"/>',
        '<rect x="48" y="48" width="6" height="120" fill="#5b9bd5" rx="3"/>',
    ]

    font = (
        "Inter, Helvetica Neue, Arial, sans-serif"
        if language == "en"
        else "PingFang SC, Hiragino Sans GB, Microsoft YaHei, sans-serif"
    )
    footer = "论文解读 AI 播客" if language == "zh" else "Paper Podcast"
    y = 86
    for line in _wrap_cjk(title, 22, 3):
        parts.append(
            f'<text x="76" y="{y}" font-family="{font}" font-size="42" '
            f'fill="#eaf1ff" font-weight="600">{html.escape(line)}</text>'
        )
        y += 54
    parts.append(
        f'<text x="76" y="{y + 6}" font-family="{font}" font-size="22" '
        f'fill="#8fb3e0">{html.escape(subtitle)}</text>'
    )

    # 创新点卡片
    card_y = 262
    for index, item in enumerate(innovations):
        lines = _wrap_cjk(item, 30, 2)
        height = 74
        parts.append(
            f'<rect x="76" y="{card_y}" width="1128" height="{height}" rx="12" '
            f'fill="#16243d" stroke="#2f4d7a" stroke-width="1.5"/>'
        )
        parts.append(
            f'<circle cx="112" cy="{card_y + height // 2}" r="15" fill="#5b9bd5"/>'
        )
        parts.append(
            f'<text x="112" y="{card_y + height // 2 + 7}" text-anchor="middle" '
            f'font-family="{font}" font-size="19" fill="#0e1729" font-weight="700">{index + 1}</text>'
        )
        ly = card_y + 30
        for line in lines:
            parts.append(
                f'<text x="146" y="{ly}" font-family="{font}" font-size="21" '
                f'fill="#d6e4f7">{html.escape(line)}</text>'
            )
            ly += 28
        card_y += height + 14

    if keywords:
        kx = 76
        for word in keywords:
            width = 18 * len(word) + 34
            if kx + width > CANVAS_W - 76:
                break
            parts.append(
                f'<rect x="{kx}" y="636" width="{width}" height="38" rx="19" '
                f'fill="#1f3a63" stroke="#3d6ca6" stroke-width="1"/>'
            )
            parts.append(
                f'<text x="{kx + width // 2}" y="661" text-anchor="middle" '
                f'font-family="{font}" font-size="17" fill="#9ec4ea">{html.escape(word)}</text>'
            )
            kx += width + 12

    parts.append(
        f'<text x="{CANVAS_W - 48}" y="664" text-anchor="end" font-family="{font}" '
        f'font-size="17" fill="#5d7ba3">{footer}</text>'
    )
    parts.append("</svg>")
    return "".join(parts)


# --------------------------------------------------------------------------
# 对外入口
# --------------------------------------------------------------------------


def generate_illustration(
    llm,
    analysis: dict[str, Any],
    paper_meta: dict[str, Any] | None,
    output_dir: Path,
    stem: str,
    language: str = "zh",
) -> Illustration:
    """生成配图。返回的 Illustration 一定可用（失败时用本地兜底图）。

    绝不抛异常：配图是增强项，不该让它拖垮整期播客的生成。
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    svg_path = output_dir / f"{stem}.svg"
    png_path = output_dir / f"{stem}.png"

    svg_text: str | None = None
    source = "fallback"

    try:
        if not getattr(llm, "mock", True):
            raw = llm.complete(
                build_illustration_messages(analysis, paper_meta, language),
                max_tokens=8000,
                temperature=0.7,
            )
            svg_text = sanitize_svg(raw)
            source = "model"
            logger.info("模型配图生成成功（%d 字符 SVG）", len(svg_text))
    except Exception as exc:  # noqa: BLE001
        logger.warning("模型配图失败，改用本地兜底图：%s", exc)
        svg_text = None
        source = "fallback"

    if svg_text is None:
        svg_text = sanitize_svg(fallback_svg(analysis, paper_meta, language))
        source = "fallback"

    try:
        svg_path.write_text(svg_text, encoding="utf-8")
        _, width, height = rasterize_svg(svg_text, png_path)
    except IllustrationError as exc:
        # 模型 SVG 能过清洗但还是渲染不出来 → 退回兜底图再试一次
        if source == "model":
            logger.warning("模型 SVG 渲染失败，改用兜底图：%s", exc)
            svg_text = sanitize_svg(fallback_svg(analysis, paper_meta, language))
            source = "fallback"
            svg_path.write_text(svg_text, encoding="utf-8")
            _, width, height = rasterize_svg(svg_text, png_path)
        else:
            raise

    return Illustration(
        svg_path=str(svg_path),
        png_path=str(png_path),
        width=width,
        height=height,
        source=source,
    )


# --------------------------------------------------------------------------
# 按主题生成（视频里「没有对应原图」的段落用）
# --------------------------------------------------------------------------
#
# 背景：论文原图只能覆盖一部分话题。实测一篇论文 21 段脚本里，只有约一半段落
# 能对上原图，其余（背景铺垫、结果讨论、不足与展望）都给同一张概括全图的信息图，
# 画面单调且并不真的对应内容。
#
# 所以这里支持**按话题现场生成**：几段连续的内容讲同一件事，就为它们画一张。
# 连续段落必须合并成一组再生成，否则 21 段会生成十几张，成本和耗时都不可控。

TOPIC_SYSTEM = """你在为学术播客的视频画一张示意图，画的是主持人**这一段正在讲的内容**。

【输出要求】
- 只输出一个完整的 `<svg>` 元素，不要 markdown 围栏，不要任何解释文字。
- 必须带 `xmlns="http://www.w3.org/2000/svg"` 和 `viewBox="0 0 1280 720"`。
- 深色底（例如 #101a2b），四角圆润，风格克制、学术。
- 样式写成**元素属性**（fill / stroke / font-size / font-family），不要 <style>、不要 class。
- 字体统一 `font-family="PingFang SC, Hiragino Sans GB, Microsoft YaHei, sans-serif"`。
- 文字用简体中文，字号：标题 38-46，正文 20-26，注释 16-18。
- 文字必须落在画布内，至少留 48px 边距。中文一个字约占一个字号宽，据此估算位置，
  宁可留白也不要让文字超出画布或互相重叠。
- 可以用 SMIL 动画（`repeatCount="indefinite"`，周期 2-4 秒）帮助表达「信息怎么流动」，
  但要有节制，不要闪烁。不要用 `<animateTransform>`。

【画什么】
- 只画这一段对话讲的那**一个**要点，不要试图概括整篇论文。
- 用方框、箭头、示意图形表达机制或流程，让听众一眼看懂。
- 如果对话里提到了具体数字（指标、倍数、参数量），直接标在图上。

【绝对禁止】
- 不要 `<script>`、`<foreignObject>`、`<image>`、任何外部链接或 `url(...)` 引用。
- 不要输出 `<svg>` 之外的任何字符。"""


def build_topic_messages(
    *,
    script_lines: list[str],
    paper_title: str = "",
    analysis: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    """为一段连续对话构建生成提示词。"""
    analysis = analysis or {}
    context_bits = []
    if paper_title:
        context_bits.append(f"所属论文：{paper_title}")
    if analysis.get("background"):
        context_bits.append(f"论文背景（仅供理解，不要画进去）：{analysis['background'][:160]}")

    user = "\n".join(
        [
            *context_bits,
            "",
            "【这一段对话正在讲】",
            *script_lines,
            "",
            "请为**上面这段对话**画一张示意图，只表达它讲的那个要点。",
        ]
    )
    return [
        {"role": "system", "content": TOPIC_SYSTEM},
        {"role": "user", "content": user},
    ]


def generate_topic_illustration(
    llm: Any,
    *,
    script_lines: list[str],
    paper_title: str,
    analysis: dict[str, Any] | None,
    output_dir: Path,
    stem: str,
) -> Illustration | None:
    """为一段对话生成配图。失败返回 None（调用方会退回中性图）。

    与 generate_illustration 的区别：那个是「概括整篇论文」，这个是「画这一段」。
    这里**不**退回本地兜底图 —— 因为兜底图是整篇的概括，放进某个具体段落里
    反而又是图文不符，不如让调用方继续用中性图。
    """
    if getattr(llm, "mock", True):
        return None

    try:
        raw = llm.complete(
            build_topic_messages(
                script_lines=script_lines, paper_title=paper_title, analysis=analysis
            ),
            max_tokens=6000,
            temperature=0.75,
        )
        svg_text = sanitize_svg(raw)
    except Exception as exc:  # noqa: BLE001
        logger.warning("段落配图生成失败（%s）：%s", stem, exc)
        return None

    svg_path = output_dir / f"{stem}.svg"
    png_path = output_dir / f"{stem}.png"
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        svg_path.write_text(svg_text, encoding="utf-8")
        _, width, height = rasterize_svg(svg_text, png_path)
    except IllustrationError as exc:
        logger.warning("段落配图渲染失败（%s）：%s", stem, exc)
        return None

    logger.info("段落配图已生成：%s（%dx%d）", stem, width, height)
    return Illustration(
        svg_path=str(svg_path),
        png_path=str(png_path),
        width=width,
        height=height,
        source="model",
    )
