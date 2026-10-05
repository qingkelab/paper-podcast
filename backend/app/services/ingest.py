"""论文预处理：PDF / 链接 / 纯文本 → 干净的、适配模型输入长度的正文。

目标是把「杂乱原文」压成「有效正文」：去掉参考文献、致谢、图表注释、页眉页脚、
断词连字符，并约束总长度。
"""

from __future__ import annotations

import html as html_lib
import io
import re
import unicodedata

import httpx

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)

# 正文里出现这些标题，认为其后内容不是正文主体
# 两种形态都要认：
#   1) 标题独占一行（网页、Markdown 稿件常见）
#   2) 标题后面紧跟正文（PDF 抽取的常见形态，如 "Acknowledgements We are grateful to..."）
_TAIL_HEADINGS = re.compile(
    r"^\s*(?:\d+[\.\s]*)?(references|bibliography|acknowledg(?:e)?ments?|"
    r"参考文献|引用文献|致\s*谢|附录|appendix)\s*$",
    re.IGNORECASE,
)
_TAIL_HEADING_PREFIX = re.compile(
    r"^\s*(?:\d+[\.\s]*)?(references|bibliography|acknowledg(?:e)?ments?|"
    r"参考文献|引用文献|致\s*谢|附录|appendix)\b",
    re.IGNORECASE,
)

# 图表、公式、页码类的行
_NOISE_LINE = re.compile(
    r"^\s*(?:figure|fig\.?|table|tab\.?|algorithm|listing|eq(?:uation)?\.?)\s*"
    r"\d+\s*[:.\-—)]?\s*$"
    r"|^\s*(?:图|表|公式|算法)\s*\d+\s*[:.\-—)]?\s*$"
    r"|^\s*\d{1,4}\s*$"
    r"|^\s*arxiv:\s*\S+\s*$",
    re.IGNORECASE,
)

_HYPHEN_BREAK = re.compile(r"(\w)-\n(\w)")
_MULTI_NEWLINE = re.compile(r"\n{3,}")
_MULTI_SPACE = re.compile(r"[ \t\u00a0]{2,}")
_CJK = re.compile(r"[\u4e00-\u9fff]")


class IngestError(Exception):
    """预处理失败，调用方应转成 HTTP 400。"""


# --------------------------------------------------------------------------
# 文本清洗
# --------------------------------------------------------------------------


def clean_text(raw: str) -> str:
    """通用文本清洗，对 PDF 抽取结果和网页正文都适用。"""
    if not raw:
        return ""

    # 1. 统一 unicode，把各类花式空格/连字符拉平
    text = unicodedata.normalize("NFKC", raw)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u00ad", "")  # soft hyphen

    # 2. 修复跨行断词
    text = _HYPHEN_BREAK.sub(r"\1\2", text)

    # 3. 逐行过滤
    kept: list[str] = []
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            kept.append("")
            continue
        if _NOISE_LINE.match(stripped):
            continue
        kept.append(stripped)

    text = "\n".join(kept)

    # 4. 砍掉正文之后的参考文献 / 致谢 / 附录
    #    判定用「字符位置过半」而不是「行号过半」：PDF 抽取出的正文经常是
    #    少数超长行，按行号算的话参考文献会落在前半段而被漏掉。
    lines = text.split("\n")
    total_chars = len(text)
    offset = 0
    for index, line in enumerate(lines):
        stripped = line.strip()
        if offset > total_chars * 0.5 and (
            _TAIL_HEADINGS.match(stripped) or _TAIL_HEADING_PREFIX.match(stripped)
        ):
            lines = lines[:index]
            break
        offset += len(line) + 1  # +1 是换行符
    text = "\n".join(lines)

    # 5. 收尾规范化
    text = _MULTI_SPACE.sub(" ", text)
    text = _MULTI_NEWLINE.sub("\n\n", text)
    return text.strip()


def truncate_smart(text: str, max_chars: int) -> str:
    """超长时保留开头和结尾。

    论文的开头（背景/方法）和结尾（结论/局限）信息量最大，中间实验细节
    可以牺牲，所以这里做「掐头去尾保两端」而不是简单截断。
    """
    if len(text) <= max_chars:
        return text
    head_len = int(max_chars * 0.62)
    tail_len = max_chars - head_len
    return (
        text[:head_len].rstrip()
        + "\n\n[……此处省略中间部分……]\n\n"
        + text[-tail_len:].lstrip()
    )


# --------------------------------------------------------------------------
# 元信息启发式抽取（粗提取，精确信息交给大模型）
# --------------------------------------------------------------------------


def guess_title(text: str, fallback: str = "未命名论文") -> str:
    """取正文前若干行里最像标题的一行。"""
    for line in text.split("\n")[:25]:
        candidate = line.strip()
        if not (8 <= len(candidate) <= 200):
            continue
        lowered = candidate.lower()
        if lowered.startswith(("abstract", "摘要", "arxiv:", "doi:", "http")):
            continue
        if re.match(r"^[\d\s.,]+$", candidate):
            continue
        # 标题一般不以句号结尾，且词数不至于太少
        if candidate.endswith((".", "。")):
            continue
        return candidate
    return fallback


def guess_arxiv_id(url: str) -> str | None:
    match = re.search(r"arxiv\.org/(?:abs|pdf)/([0-9]{4}\.[0-9]{4,5})(?:v\d+)?", url, re.I)
    return match.group(1) if match else None


def looks_chinese(text: str) -> bool:
    return len(_CJK.findall(text[:2000])) > 50


# --------------------------------------------------------------------------
# PDF
# --------------------------------------------------------------------------


def extract_pdf_text(data: bytes) -> str:
    """从 PDF 字节流抽取文本。"""
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover
        raise IngestError("服务端缺少 pypdf 依赖，无法解析 PDF") from exc

    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception as exc:
        raise IngestError(f"PDF 解析失败，文件可能已损坏或被加密：{exc}") from exc

    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception as exc:
            raise IngestError("PDF 已加密，无法读取内容") from exc

    pages: list[str] = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:
            # 单页失败不影响整体
            continue

    text = "\n".join(pages)
    if len(text.strip()) < 200:
        raise IngestError(
            "这个 PDF 几乎提取不到文字，可能是扫描版（图片型 PDF）。"
            "请改用「文本粘贴」导入，或先做 OCR。"
        )
    return text


# --------------------------------------------------------------------------
# 链接
# --------------------------------------------------------------------------


def _html_to_text(html: str) -> str:
    """极简 HTML → 文本。够用于 arXiv 摘要页这类结构规整的页面。"""
    text = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?is)<!--.*?-->", " ", text)
    # 块级元素转换为换行
    text = re.sub(
        r"(?i)</?(p|div|br|li|tr|h[1-6]|section|article|blockquote|table)[^>]*>",
        "\n",
        text,
    )
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    return clean_text(text)


def arxiv_pdf_url(url: str) -> str | None:
    """把 arXiv 的 /abs/ 链接换成对应的 /pdf/ 链接。

    arXiv 摘要页虽然有标题、作者、摘要，但**没有正文**，而正文是解读方法、
    实验和结论的唯一来源。靠摘要页只能写出一篇空泛甚至编造的解读，
    所以 arXiv 链接一律优先去抓 PDF。
    """
    arxiv_id = guess_arxiv_id(url)
    return f"https://arxiv.org/pdf/{arxiv_id}" if arxiv_id else None


def _get(client: httpx.Client, url: str, headers: dict[str, str]) -> httpx.Response:
    try:
        return client.get(url, headers=headers)
    except httpx.TimeoutException as exc:
        raise IngestError("抓取链接超时，请检查网络或稍后重试") from exc
    except httpx.HTTPError as exc:
        raise IngestError(f"抓取链接失败：{exc}") from exc


def fetch_url_text(url: str, timeout: float = 25.0) -> tuple[str, str, bytes | None]:
    """抓取链接正文。

    返回 (清洗后文本, 内容类型标识, PDF 原始字节)。
    内容类型为 'pdf' 或 'html'；只有拿到 PDF 时第三项才非空——
    配图提取需要原始 PDF 字节，所以这里一并带出来，避免二次下载。
    """
    if not re.match(r"^https?://", url, re.I):
        raise IngestError("链接必须以 http:// 或 https:// 开头")

    headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}

    with httpx.Client(follow_redirects=True, timeout=timeout) as client:
        # arXiv 链接直接抓 PDF，拿全文而不是摘要页
        pdf_url = arxiv_pdf_url(url)
        if pdf_url:
            try:
                pdf_response = client.get(pdf_url, headers=headers)
            except httpx.HTTPError as exc:
                raise IngestError(f"抓取 arXiv PDF 失败：{exc}") from exc
            if pdf_response.status_code < 400 and pdf_response.content[:5] == b"%PDF-":
                return (
                    clean_text(extract_pdf_text(pdf_response.content)),
                    "pdf",
                    pdf_response.content,
                )
            # 抓不到 PDF 就退回摘要页，至少还有摘要可用
            response = _get(client, url, headers)
        else:
            response = _get(client, url, headers)

    if response.status_code >= 400:
        raise IngestError(
            f"抓取链接失败（HTTP {response.status_code}）。"
            "如果这是知网等需要登录的站点，请改为下载 PDF 后上传。"
        )

    content_type = (response.headers.get("content-type") or "").lower()
    if "application/pdf" in content_type or response.content[:5] == b"%PDF-":
        return clean_text(extract_pdf_text(response.content)), "pdf", response.content

    body = _html_to_text(response.text)
    if len(body) < 400:
        raise IngestError(
            "这个页面提取不到足够正文，可能是动态渲染或需要登录的页面。"
            "请改用 PDF 上传或文本粘贴。"
        )
    return body, "html", None
