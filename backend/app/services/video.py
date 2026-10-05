"""把配图和播客音频合成「视频解读播客」。

## 同步是怎么做到的

不去猜语速、也不做静音检测。豆包在合成时每个轮次都会返回
`start_time` / `end_time`（见 podcast_tts 的 RoundTiming），
这就是每段脚本在最终音频里的**确切时间区间**。视频的时间轴直接由它驱动，
所以画面切换和字幕出现天然和声音对齐。

## 画面结构

```
[0, 第一段开始)      片头音乐   → 封面（PDF 第一页）
[第 i 段时间区间]     第 i 段脚本 → 该段配图 + 该段字幕
[最后一段结束, 结尾]  片尾音乐   → 信息图 + 结束卡
```

配图分配优先让大模型判断「哪张图适合出现在哪一段」（图的图注 + 脚本内容
都在手里，一次调用就够），失败则退回均匀分布——保证任何情况下都有画面。

## 为什么用 resvg 渲染每一帧而不是 ffmpeg drawtext

字幕是中文，ffmpeg 的 drawtext 需要指定字体文件且转义规则很坑。
用 SVG 渲染（resvg 已验证能正确解析中文字形）排版完全可控，
也复用了信息图那套渲染链路。
"""

from __future__ import annotations

import base64
import html
import logging
import shutil
import subprocess
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 画幅 = 论文 PDF 首页渲染图的尺寸（935x1210）。
#
# ⚠️ 宽度必须取 936 而不是 935：H.264 的 yuv420p 是 4:2:0 色度抽样，
# 要求宽高都能被 2 整除。直接用 935 会被 libx264 拒掉：
#   "width not divisible by 2 (935x1210)" —— 编码器根本打不开。
# 多出的 1 像素肉眼不可见，封面居中放置即可。
COVER_ASPECT_W = 935
COVER_ASPECT_H = 1210
VIDEO_W = 936
VIDEO_H = 1210
FPS = 30

# 竖版布局：标题条 → 图片区 → 图注 → 字幕面板
TITLE_BASELINE = 46
IMAGE_TOP = 74
IMAGE_BOX_W = VIDEO_W - 40      # 896
IMAGE_BOX_H = 812
CAPTION_TOP = IMAGE_TOP + IMAGE_BOX_H + 6

SUBTITLE_TOP = 968
SUBTITLE_LEFT = 40
SUBTITLE_WIDTH = VIDEO_W - SUBTITLE_LEFT * 2
SUBTITLE_TEXT_TOP = 1052        # 第一行文字的基线
SUBTITLE_MAX_HEIGHT = 148       # 留给文字的总高度

FONT_STACK = "PingFang SC, Hiragino Sans GB, Microsoft YaHei, Noto Sans CJK SC, sans-serif"

# 主播配色（与前端一致：A 冷蓝、B 暖橙）
SPEAKER_COLORS = {"A": "#5b9bd5", "B": "#e8a33d"}
SPEAKER_NAMES = {"A": "主播A", "B": "主播B"}

BG_COLOR = "#0c1524"

# 伪 id：模型用它表示「这一段没有对应原图，需要现场生成一张」
GENERATE_ID = "generate"


class VideoError(Exception):
    """视频合成失败。属于可降级错误——没有视频也要能听播客。"""


@dataclass
class Scene:
    """一个画面片段：在 [start, end) 期间显示 image，并叠加字幕。"""

    start: float
    end: float
    image: Path
    kind: str          # "cover" | "figure" | "illustration"
    speaker: str = ""
    text: str = ""
    caption: str = ""  # 图片说明（图注），显示在图片下方

    @property
    def duration(self) -> float:
        return max(self.end - self.start, 0.05)


@dataclass
class ImageAsset:
    """一张可用作画面的图。"""

    id: str
    path: Path
    kind: str      # "cover" | "figure" | "table" | "illustration"
    caption: str = ""

    @property
    def exists(self) -> bool:
        return self.path.exists()


@dataclass
class VideoResult:
    video_path: Path
    duration_sec: float
    scene_count: int
    bytes_written: int
    assignment: str  # "model" | "heuristic"

    def to_dict(self) -> dict:
        return {
            "duration_sec": round(self.duration_sec, 2),
            "scene_count": self.scene_count,
            "bytes": self.bytes_written,
            "assignment": self.assignment,
        }


# --------------------------------------------------------------------------
# 配图分配
# --------------------------------------------------------------------------

ASSIGN_SYSTEM = """你在为「论文解读播客」的视频版做图文编排。

听众能听到主播的对话，同时看到画面。你的任务是让**每一段话配上意思相符的图**：
听众听到某个概念时，屏幕上正好是讲这个概念的图。图不对文比没有图更糟。

你会拿到：
- 脚本分段列表（编号、内容）
- 可用图片列表（id 与图注）

请为**每一段**指定一张图。判断依据是这一段在讲什么，以及哪张图正好在讲同一件事。

【重要规则】
- 图注可能是英文，脚本是中文，你要按**语义**判断，不要按字面。
- 若某一段的内容和任何一张图都对不上，就填 `generate` —— 系统会为这一段
  **现场画一张专门的示意图**。连续几段讲同一件事时都填 `generate`，
  系统会把它们合并成一张图，不要每段都标。
- `illustration` 是一张概括全文的信息图，只在「需要一个中性过渡画面」时用
  （例如开场收尾）。**不要为了填满而硬塞一张不相关的原图。**
- `cover` 是论文首页，适合开场介绍论文时用；正文讨论具体内容时不要用它。
- 能对上论文原图的段落**优先用原图**（原图最准确）；原图对不上才填 `generate`。
- 相邻段落如果确实在讲同一件事，用同一张图是正常的；话题变了就换图。
- 图片 id 只能从给出的列表里选，不要编造。

只输出 JSON：
{"assignments": [{"segment": 0, "image_id": "cover"}, {"segment": 1, "image_id": "f1"}, ...]}
每个脚本段都要有一项，不要遗漏。不要输出任何解释。"""


def build_assign_messages(
    segments: list[dict[str, Any]], assets: list[dict[str, Any]]
) -> list[dict[str, str]]:
    script_lines = []
    for index, segment in enumerate(segments):
        text = (segment.get("text") or "").strip().replace("\n", " ")
        speaker = segment.get("speaker") or "A"
        script_lines.append(f"[{index}] 主播{speaker}：{text}")

    asset_lines = []
    for asset in assets:
        caption = (asset.get("caption") or asset.get("label") or "").strip()
        asset_lines.append(f"- {asset['id']}: {caption[:160]}")

    user = f"""【脚本分段】共 {len(segments)} 段（编号 0 到 {len(segments)-1}）
{chr(10).join(script_lines)}

【可用图片】共 {len(assets)} 张
{chr(10).join(asset_lines)}

请为每一段脚本指定一张意思相符的图，输出 JSON。"""
    return [
        {"role": "system", "content": ASSIGN_SYSTEM},
        {"role": "user", "content": user},
    ]


def _normalize_per_segment(
    raw: Any, *, count: int, valid_ids: set[str], default_id: str
) -> list[str] | None:
    """把模型给的逐段分配整理成「每段一个图片 id」。

    与旧版「只给起始段号」不同：那种做法下，一张图会一直挂到下一张图开始，
    中间几段可能已经换了话题。这里要求逐段明确，才能保证图文一致。
    """
    if not isinstance(raw, list):
        return None

    picks: dict[int, str] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        image_id = str(item.get("image_id") or "").strip()
        try:
            segment = int(item.get("segment"))
        except (TypeError, ValueError):
            continue
        # "generate" 是伪 id：表示「这一段需要现场生成一张专门的图」
        if image_id == GENERATE_ID or image_id in valid_ids:
            if 0 <= segment < count:
                picks[segment] = image_id

    if not picks:
        return None

    # 模型漏掉的段落（尤其是开头）沿用最近一次有效选择，避免出现空图
    result: list[str] = []
    last = picks.get(0, default_id)
    for index in range(count):
        if index in picks:
            last = picks[index]
        result.append(last)
    return result


def heuristic_assignment(count: int, figure_count: int) -> list[int]:
    """均匀分布：把图铺到正文段上。模型不可用时的兜底（给的是起始段号）。

    这是**退化**方案：它只保证每张图都有露面，不保证图文相符。
    正常的语义匹配由模型逐段完成；模型不可用时至少画面不会空着。
    """
    if figure_count <= 0:
        return []
    head = max(int(count * 0.06), 0)
    tail = max(int(count * 0.12), 1)
    span = max(count - head - tail, 1)
    step = max(span / figure_count, 1)
    result = [min(head + int(i * step), count - 1) for i in range(figure_count)]

    for index in range(1, len(result)):
        if result[index] <= result[index - 1]:
            result[index] = min(result[index - 1] + 1, count - 1)
    for index in range(len(result) - 2, -1, -1):
        if result[index] >= result[index + 1]:
            result[index] = max(result[index + 1] - 1, 0)
    return result


def heuristic_per_segment(
    count: int, figure_ids: list[str], default_id: str
) -> list[str]:
    """把「均匀分布的起始段号」展开成逐段图片 id。"""
    if not figure_ids:
        return [default_id] * count
    starts = heuristic_assignment(count, len(figure_ids))
    result: list[str] = []
    current = default_id
    for index in range(count):
        starts_here = [j for j, start in enumerate(starts) if start <= index]
        if starts_here:
            current = figure_ids[starts_here[-1]]
        result.append(current)
    return result


# --------------------------------------------------------------------------
# 时间轴
# --------------------------------------------------------------------------


def build_scenes(
    *,
    segments: list[dict[str, Any]],
    timings: list[Any],
    audio_duration: float,
    assets: dict[str, ImageAsset],
    image_for_segment: list[str],
    fallback_id: str,
) -> list[Scene]:
    """把脚本、时间戳和逐段配图拼成画面时间轴。

    `timings` 是 RoundTiming 列表（顺序与脚本段一一对应），
    里面已经包含了片头音乐的偏移，可以直接当绝对时间用。

    `image_for_segment[i]` 是第 i 段脚本要显示的图片 id —— 逐段指定而不是
    「从某段开始一直用到下一张图」，这样才能保证画面跟着话题走。
    """
    if not segments or not timings:
        raise VideoError("缺少脚本或时间戳，无法建立视频时间轴")
    if not assets:
        raise VideoError("没有任何可用配图，无法生成视频")

    def asset_for(index: int) -> ImageAsset:
        if index < len(image_for_segment):
            asset = assets.get(image_for_segment[index])
            if asset and asset.exists:
                return asset
        asset = assets.get(fallback_id)
        if asset and asset.exists:
            return asset
        return next(a for a in assets.values() if a.exists)

    ordered = sorted(timings, key=lambda t: t.start)
    scenes: list[Scene] = []

    # ---- 片头：封面 ----
    head_end = ordered[0].start
    cover = assets.get("cover")
    head_asset = cover if (cover and cover.exists) else asset_for(0)
    if head_end > 0.3:
        scenes.append(
            Scene(
                start=0.0,
                end=head_end,
                image=head_asset.path,
                kind=head_asset.kind,
                caption=head_asset.caption,
            )
        )

    # ---- 正文：逐段按语义配图 ----
    for index, timing in enumerate(ordered):
        segment = segments[index] if index < len(segments) else {}
        asset = asset_for(index)
        scenes.append(
            Scene(
                start=float(timing.start),
                end=float(timing.end),
                image=asset.path,
                kind=asset.kind,
                speaker=str(segment.get("speaker") or getattr(timing, "speaker", "") or "A"),
                text=str(segment.get("text") or ""),
                caption=asset.caption,
            )
        )

    # ---- 片尾：信息图 + 结束卡 ----
    tail_start = ordered[-1].end
    if audio_duration > tail_start + 0.3:
        illustration = assets.get("illustration")
        if illustration and illustration.exists:
            tail_image, tail_kind, tail_caption = (
                illustration.path,
                illustration.kind,
                illustration.caption,
            )
        else:
            # 没有信息图就沿用最后一段的画面，别让片尾变成黑屏
            tail_image = scenes[-1].image
            tail_kind = scenes[-1].kind
            tail_caption = scenes[-1].caption

        scenes.append(
            Scene(
                start=tail_start,
                end=audio_duration,
                image=tail_image,
                kind=tail_kind,
                caption=tail_caption,
                text="以上就是这篇论文的解读，感谢收听。",
            )
        )

    # 补齐空隙：相邻场景之间若有缝，前一个延长到下一个开始
    for index in range(len(scenes) - 1):
        if scenes[index].end < scenes[index + 1].start:
            scenes[index].end = scenes[index + 1].start

    return scenes


# --------------------------------------------------------------------------
# 幻灯片渲染
# --------------------------------------------------------------------------


def _prepare_image(path: Path, max_w: int, max_h: int) -> tuple[str, int, int]:
    """把图缩放到展示尺寸并转成 data URI。

    先缩放再内嵌是有必要的：原始配图可能接近 1000x1200，直接 base64 内嵌会让
    每张幻灯片的 SVG 膨胀到几百 KB，resvg 每次都要重新解码整张大图。
    缩放后单帧渲染从 ~200ms 降到 ~40ms，30 帧就是好几秒的差别。
    """
    try:
        import pymupdf
    except ImportError as exc:  # pragma: no cover
        raise VideoError("缺少 pymupdf，无法处理配图") from exc

    try:
        pix = pymupdf.Pixmap(str(path))
    except Exception as exc:  # noqa: BLE001
        raise VideoError(f"配图无法读取：{path.name}（{exc}）") from exc

    scale = min(max_w / pix.width, max_h / pix.height, 1.0)
    if scale < 1.0:
        try:
            target_w = max(int(pix.width * scale), 1)
            target_h = max(int(pix.height * scale), 1)
            pix = pymupdf.Pixmap(pix, target_w, target_h)
        except Exception as exc:  # noqa: BLE001
            logger.warning("配图缩放失败，使用原尺寸（会慢一些）：%s", exc)

    data = pix.tobytes("png")
    return (
        "data:image/png;base64," + base64.b64encode(data).decode("ascii"),
        pix.width,
        pix.height,
    )


def _wrap(text: str, max_units: float) -> list[str]:
    """按全角单位宽度折行（西文按 0.55 计）。"""
    from .illustration import _char_width, _tokenize

    text = " ".join((text or "").split())
    if not text:
        return []

    lines: list[str] = []
    current = ""
    width = 0.0
    for token in _tokenize(text):
        token_width = sum(_char_width(c) for c in token)
        if current and width + token_width > max_units:
            lines.append(current.strip())
            current = ""
            width = 0.0
        if not current and token == " ":
            continue
        current += token
        width += token_width
    if current.strip():
        lines.append(current.strip())
    return lines


def _fit_subtitle(text: str, *, max_lines: int = 5) -> tuple[float, list[str]]:
    """选一个既能放下、又不至于太小的字号。

    竖版画幅只有 896px 宽，比横版窄很多，所以必须**同时**检查行数和总高度：
    光看行数会在字号偏大时让文字溢出到画面外。
    """
    from .illustration import _char_width, _tokenize

    text = " ".join((text or "").split())
    if not text:
        return 24.0, []

    def wrap_at(size: float) -> list[str]:
        units = SUBTITLE_WIDTH / size
        lines: list[str] = []
        current = ""
        width = 0.0
        for token in _tokenize(text):
            token_width = sum(_char_width(c) for c in token)
            if current and width + token_width > units:
                lines.append(current.strip())
                current = ""
                width = 0.0
            if not current and token == " ":
                continue
            current += token
            width += token_width
        if current.strip():
            lines.append(current.strip())
        return lines

    for size in (30.0, 28.0, 26.0, 24.0, 22.0, 20.0):
        lines = wrap_at(size)
        if len(lines) <= max_lines and len(lines) * size * 1.36 <= SUBTITLE_MAX_HEIGHT:
            return size, lines

    # 还是放不下：用最小字号，超出部分截断加省略号
    size = 20.0
    lines = wrap_at(size)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1][:-1] + "…"
    return size, lines


def render_slide(scene: Scene, output_path: Path, *, title: str = "") -> Path:
    """渲染一帧画面：配图 + 图注 + 字幕条（竖版，与论文首页同尺寸）。"""
    data_uri, img_w, img_h = _prepare_image(scene.image, IMAGE_BOX_W, IMAGE_BOX_H)
    img_x = (VIDEO_W - img_w) / 2
    img_y = IMAGE_TOP + (IMAGE_BOX_H - img_h) / 2

    font_size, lines = _fit_subtitle(scene.text)
    color = SPEAKER_COLORS.get(scene.speaker, SPEAKER_COLORS["A"])
    speaker_name = SPEAKER_NAMES.get(scene.speaker, "")

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{VIDEO_W}" height="{VIDEO_H}" '
        f'viewBox="0 0 {VIDEO_W} {VIDEO_H}">',
        f'<rect width="{VIDEO_W}" height="{VIDEO_H}" fill="{BG_COLOR}"/>',
    ]

    # 顶部标题条
    if title:
        parts.append(
            f'<text x="40" y="{TITLE_BASELINE}" font-family="{FONT_STACK}" font-size="22" '
            f'fill="#7f9dc4">{html.escape(title[:40])}</text>'
        )

    # 配图（淡边框 + 居中）
    parts.append(
        f'<rect x="{img_x - 2:.1f}" y="{img_y - 2:.1f}" width="{img_w + 4}" height="{img_h + 4}" '
        f'rx="10" fill="none" stroke="#22314d" stroke-width="2"/>'
    )
    parts.append(
        f'<image x="{img_x:.1f}" y="{img_y:.1f}" width="{img_w}" height="{img_h}" '
        f'href="{data_uri}"/>'
    )

    # 图注
    if scene.caption:
        caption_lines = _wrap(scene.caption, SUBTITLE_WIDTH / 18)[:2]
        for offset, line in enumerate(caption_lines):
            parts.append(
                f'<text x="{SUBTITLE_LEFT}" y="{CAPTION_TOP + 22 + offset * 23}" '
                f'font-family="{FONT_STACK}" font-size="18" fill="#6a86ab">'
                f"{html.escape(line)}</text>"
            )

    # 字幕面板
    parts.append(
        f'<rect x="0" y="{SUBTITLE_TOP}" width="{VIDEO_W}" '
        f'height="{VIDEO_H - SUBTITLE_TOP}" fill="#0a1220"/>'
    )
    parts.append(
        f'<rect x="0" y="{SUBTITLE_TOP}" width="{VIDEO_W}" height="2" fill="#1d2c46"/>'
    )

    if speaker_name and lines:
        badge_w = 42 + len(speaker_name) * 19
        parts.append(
            f'<rect x="{SUBTITLE_LEFT}" y="{SUBTITLE_TOP + 20}" width="{badge_w}" height="32" '
            f'rx="16" fill="{color}" opacity="0.18"/>'
        )
        parts.append(
            f'<text x="{SUBTITLE_LEFT + badge_w / 2:.0f}" y="{SUBTITLE_TOP + 43}" '
            f'text-anchor="middle" font-family="{FONT_STACK}" font-size="19" '
            f'fill="{color}">{html.escape(speaker_name)}</text>'
        )

    line_height = font_size * 1.36
    for offset, line in enumerate(lines):
        parts.append(
            f'<text x="{SUBTITLE_LEFT}" y="{SUBTITLE_TEXT_TOP + offset * line_height:.0f}" '
            f'font-family="{FONT_STACK}" font-size="{font_size:.0f}" '
            f'fill="#e8f0fb">{html.escape(line)}</text>'
        )

    parts.append("</svg>")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    _rasterize_custom("\n".join(parts), output_path)
    return output_path


def _rasterize_custom(svg_text: str, output_path: Path) -> None:
    """按 SVG 自带的宽高渲染（illustration.rasterize_svg 会强制套用信息图的画布尺寸）。"""
    try:
        import resvg_py
    except ImportError as exc:  # pragma: no cover
        raise VideoError("缺少 resvg-py，无法渲染画面") from exc

    try:
        png = bytes(
            resvg_py.svg_to_bytes(
                svg_string=svg_text,
                width=VIDEO_W,
                height=VIDEO_H,
                languages=["zh-Hans", "en"],
            )
        )
    except Exception as exc:  # noqa: BLE001
        raise VideoError(f"画面渲染失败：{exc}") from exc

    if not png:
        raise VideoError("画面渲染返回空内容")
    output_path.write_bytes(png)


# --------------------------------------------------------------------------
# 编码
# --------------------------------------------------------------------------


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def encode_video(
    scenes: list[Scene],
    slide_paths: list[Path],
    audio_path: Path,
    output_path: Path,
    *,
    target_duration: float | None = None,
) -> VideoResult:
    """把幻灯片序列和音频合成 MP4。

    ## 为什么要分两步，而不是一条 ffmpeg 命令搞定

    一条命令（concat 幻灯片 + 音频一起编码）**无法把视频长度对齐到音频**：

    | 写法 | 视频流时长 | 问题 |
    |---|---|---|
    | `-r 30 -shortest`（单步） | 209.23s | 比音频 206.86s 长 2.4 秒，结尾只有画面没声音 |
    | 不加 `-r`（单步） | 194.60s | 短 12 秒，结尾画面提前冻住 |
    | 输入端 `-r 30` | 0.80s | concat 会忽略每段 duration，直接崩掉 |

    原因是 concat demuxer 的时间戳与输出帧率不匹配，且**逐文件都有舍入累积**
    （23 个文件累积出 12 秒偏差），`-shortest` 在单步编码里也裁不掉。

    拆成两步就干净了：先出纯视频轨（不关心它多长），再用 `-c:v copy` 只封装
    音频，此时 `-shortest` 能正确把总长裁到音频长度。实测视频 206.77s /
    音频 206.86s，误差 0.09 秒。

    注意：**不要**用 `-t` 去钳总长。实测 `-t 4.000` 会把 4 秒的片子砍成 2.03 秒。
    """
    if not ffmpeg_available():
        raise VideoError("系统未安装 ffmpeg，无法合成视频")
    if len(scenes) != len(slide_paths):
        raise VideoError("画面数与幻灯片数不一致")
    if not slide_paths:
        raise VideoError("没有可用的画面")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    list_path = output_path.parent / f"{output_path.stem}-slides.txt"
    silent_path = output_path.parent / f"{output_path.stem}-video-only.mp4"

    # 每段时长量化成整帧，避免 duration 落在帧边界之外被额外舍入
    frame_counts = [max(int(round(scene.duration * FPS)), 1) for scene in scenes]

    # concat demuxer 要求重复最后一个文件，否则最后一帧时长会丢
    lines: list[str] = []
    for slide, frames in zip(slide_paths, frame_counts):
        lines.append(f"file '{slide.resolve()}'")
        lines.append(f"duration {frames / FPS:.6f}")
    lines.append(f"file '{slide_paths[-1].resolve()}'")
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def run(command: list[str], what: str) -> None:
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=1800)
        except subprocess.TimeoutExpired as exc:
            raise VideoError(f"{what}超时（超过 30 分钟）") from exc
        except OSError as exc:
            raise VideoError(f"无法调用 ffmpeg：{exc}") from exc
        if result.returncode != 0:
            raise VideoError(f"{what}失败：{(result.stderr or '')[-400:]}")

    try:
        # ---- 第一步：纯视频轨 ----
        run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "concat", "-safe", "0", "-i", str(list_path),
                "-an",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-pix_fmt", "yuv420p", "-r", str(FPS),
                str(silent_path),
            ],
            "视频轨编码",
        )

        # ---- 第二步：只封装音频，视频流直接 copy ----
        mux = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(silent_path),
            "-i", str(audio_path),
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",   # 让浏览器能边下边播、可拖动
            "-shortest",
            str(output_path),
        ]
        run(mux, "音视频封装")
    finally:
        for temp in (list_path, silent_path):
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass

    if not output_path.exists() or output_path.stat().st_size == 0:
        raise VideoError("ffmpeg 没有产出视频文件")

    duration = probe_media_duration(output_path) or sum(s.duration for s in scenes)
    return VideoResult(
        video_path=output_path,
        duration_sec=duration,
        scene_count=len(scenes),
        bytes_written=output_path.stat().st_size,
        assignment="pending",
    )


def probe_media_duration(path: Path) -> float | None:
    """用 ffprobe 读容器时长。读不到就返回 None。"""
    if not shutil.which("ffprobe"):
        return None
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "csv=p=0", str(path),
            ],
            capture_output=True, text=True, timeout=60,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    try:
        return round(float(result.stdout.strip()), 3)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# 总入口
# --------------------------------------------------------------------------


def build_asset_pool(
    *,
    cover_path: str | None,
    figures: list[dict[str, Any]],
    illustration_png: str | None,
) -> dict[str, ImageAsset]:
    """收集所有可用作画面的图。

    封面(id=cover) 和 信息图(id=illustration) 也要进池子：它们是**中性选项**——
    当某段脚本和任何一张论文原图都对不上时，用信息图比硬塞一张不相关的图好。
    """
    pool: dict[str, ImageAsset] = {}

    if cover_path and Path(cover_path).exists():
        pool["cover"] = ImageAsset(
            id="cover", path=Path(cover_path), kind="cover", caption="论文首页"
        )

    for index, figure in enumerate(figures):
        path = figure.get("path")
        if not path or not Path(path).exists():
            continue
        figure_id = str(figure.get("id") or f"f{index + 1}")
        pool[figure_id] = ImageAsset(
            id=figure_id,
            path=Path(path),
            kind=str(figure.get("kind") or "figure"),
            caption=str(figure.get("caption") or figure.get("label") or ""),
        )

    if illustration_png and Path(illustration_png).exists():
        pool["illustration"] = ImageAsset(
            id="illustration",
            path=Path(illustration_png),
            kind="illustration",
            caption="论文核心机制信息图",
        )

    return pool


def default_asset_id(pool: dict[str, ImageAsset]) -> str:
    """没有明确匹配时用哪张图。信息图最中性（它概括全文，不会文不对题）。"""
    for candidate in ("illustration", "cover"):
        if candidate in pool:
            return candidate
    return next(iter(pool))


def compose_video(
    *,
    segments: list[dict[str, Any]],
    timings: list[Any],
    audio_path: Path,
    audio_duration: float,
    cover_path: str | None,
    figures: list[dict[str, Any]],
    illustration_png: str | None,
    work_dir: Path,
    output_path: Path,
    title: str = "",
    llm: Any | None = None,
    analysis: dict[str, Any] | None = None,
    max_topic_images: int = 4,
) -> VideoResult:
    """合成视频解读播客。任何一步失败都抛 VideoError，由调用方降级。"""
    pool = build_asset_pool(
        cover_path=cover_path, figures=figures, illustration_png=illustration_png
    )
    if not pool:
        raise VideoError("没有任何可用配图，无法生成视频")

    default_id = default_asset_id(pool)
    figure_ids = [i for i in pool if i not in ("cover", "illustration")]

    # 逐段语义匹配：优先让模型判断「这一段在讲什么、哪张图正好在讲同一件事」
    image_for_segment: list[str] | None = None
    strategy = "heuristic"

    if llm is not None and not getattr(llm, "mock", True):
        assets = [
            {"id": asset.id, "caption": asset.caption}
            for asset in pool.values()
        ]
        try:
            data = llm._chat_json(
                build_assign_messages(segments, assets),
                max_tokens=max(1500, len(segments) * 60),
                temperature=0.2,
            )
            candidate = _normalize_per_segment(
                data.get("assignments"),
                count=len(segments),
                valid_ids=set(pool),
                default_id=default_id,
            )
            if candidate:
                image_for_segment = candidate
                strategy = "model"
                distinct = len(set(candidate))
                logger.info(
                    "逐段配图由模型完成：%d 段用了 %d 张不同的图", len(candidate), distinct
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("模型逐段配图失败，退回均匀分布：%s", exc)

    if image_for_segment is None:
        image_for_segment = heuristic_per_segment(
            len(segments), figure_ids, default_id
        )
        logger.info("逐段配图使用均匀分布（退化方案，不保证图文相符）")

    # ---- 没有对应原图的段落：现场生成专门的配图 ----
    #
    # 以前这些段落统一挂同一张概括全图的信息图，实测 21 段里有 10 段都是它，
    # 画面单调且并不真的对应内容。改成按话题生成后，每张图都只讲那一段的事。
    runs = group_generate_runs(image_for_segment)
    generated = 0
    if runs:
        capped = merge_runs_to_cap(runs, max_topic_images)
        if len(capped) < len(runs):
            logger.info(
                "需要生成配图的段落组有 %d 个，超过上限 %d，已合并为 %d 组",
                len(runs), max_topic_images, len(capped),
            )
        topic_assets = generate_topic_images(
            llm,
            segments=segments,
            runs=capped,
            paper_title=title,
            analysis=analysis,
            output_dir=work_dir / "topics",
            stem=output_path.stem,
        )
        for start, asset in topic_assets.items():
            pool[asset.id] = asset
            generated += 1

        # 把 generate 的段落指向它所属那一组生成出来的图
        for start_segment, end_segment in capped:
            asset = topic_assets.get(start_segment)
            if asset is None:
                continue  # 这一组生成失败 → 保持 generate，下面会退回中性图
            for index in range(start_segment, min(end_segment, len(image_for_segment))):
                image_for_segment[index] = asset.id

    # 生成失败或未生成的部分退回中性图，保证不会出现空画面
    image_for_segment = [
        pick if pick != GENERATE_ID else default_id for pick in image_for_segment
    ]
    logger.info(
        "配图构成：原图/封面 %d 张，现场生成 %d 张，中性兜底 %d 段",
        len(figure_ids),
        generated,
        sum(1 for pick in image_for_segment if pick == default_id),
    )

    scenes = build_scenes(
        segments=segments,
        timings=timings,
        audio_duration=audio_duration,
        assets=pool,
        image_for_segment=image_for_segment,
        fallback_id=default_id,
    )

    work_dir.mkdir(parents=True, exist_ok=True)
    slide_paths: list[Path] = []
    for index, scene in enumerate(scenes):
        slide_path = work_dir / f"slide-{index:04d}.png"
        render_slide(scene, slide_path, title=title)
        slide_paths.append(slide_path)
    logger.info("已渲染 %d 帧画面", len(slide_paths))

    result = encode_video(
        scenes, slide_paths, audio_path, output_path, target_duration=audio_duration
    )
    result.assignment = strategy

    for slide_path in slide_paths:
        try:
            slide_path.unlink(missing_ok=True)
        except OSError:
            pass

    return result


# --------------------------------------------------------------------------
# 「没有对应原图」的段落：现场生成专门的配图
# --------------------------------------------------------------------------


def group_generate_runs(picks: list[str]) -> list[tuple[int, int]]:
    """把连续的 generate 段落合并成 [起, 止) 区间。

    必须合并：一段一张图的话，21 段脚本会生成十几张，每张一次模型调用
    （实测单张约 10 秒、3000+ token），成本和耗时都不可控。
    连续几段通常本来就在讲同一件事，共用一张图反而更贴切。
    """
    runs: list[tuple[int, int]] = []
    start: int | None = None
    for index, pick in enumerate(picks):
        if pick == GENERATE_ID:
            if start is None:
                start = index
        elif start is not None:
            runs.append((start, index))
            start = None
    if start is not None:
        runs.append((start, len(picks)))
    return runs


def merge_runs_to_cap(
    runs: list[tuple[int, int]], cap: int
) -> list[tuple[int, int]]:
    """区间数超过上限时，把相邻区间合并到只剩 cap 个。

    按顺序均分成 cap 组，每组覆盖原来若干个区间 —— 这样覆盖范围不变，
    只是每张图负责更多段落。比「丢掉超出的区间」更合理：
    丢掉的段落会退回中性图，又变回图文不符。
    """
    if len(runs) <= cap or cap <= 0:
        return runs

    merged: list[tuple[int, int]] = []
    total = len(runs)
    for slot in range(cap):
        first = slot * total // cap
        last = (slot + 1) * total // cap - 1
        merged.append((runs[first][0], runs[last][1]))
    return merged


def generate_topic_images(
    llm: Any,
    *,
    segments: list[dict[str, Any]],
    runs: list[tuple[int, int]],
    paper_title: str,
    analysis: dict[str, Any] | None,
    output_dir: Path,
    stem: str,
    progress: Any | None = None,
) -> dict[int, ImageAsset]:
    """为每个区间生成一张配图，返回 {起始段号: ImageAsset}。

    用线程池并行：串行的话 4 张图要 40 秒，会把流水线明显拖长。
    LLMClient 每次调用都是独立的 httpx 请求，没有共享状态，并行是安全的。
    """
    if not runs or getattr(llm, "mock", True):
        return {}

    from .illustration import generate_topic_illustration

    def one(index: int, run: tuple[int, int]) -> tuple[int, ImageAsset | None]:
        start, end = run
        lines = [
            f"主播{(segments[i].get('speaker') or 'A')}：{(segments[i].get('text') or '').strip()}"
            for i in range(start, min(end, len(segments)))
        ]
        try:
            result = generate_topic_illustration(
                llm,
                script_lines=lines,
                paper_title=paper_title,
                analysis=analysis,
                output_dir=output_dir,
                stem=f"{stem}-topic{index + 1}",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("段落配图异常（第 %d 组）：%s", index + 1, exc)
            return start, None

        if result is None:
            return start, None
        return start, ImageAsset(
            id=f"topic{index + 1}",
            path=Path(result.png_path),
            kind="illustration",
            caption="本段内容的示意图",
        )

    from concurrent.futures import ThreadPoolExecutor

    assets: dict[int, ImageAsset] = {}
    workers = min(len(runs), 4)
    with ThreadPoolExecutor(max_workers=max(workers, 1)) as pool:
        for start, asset in pool.map(lambda pair: one(*pair), enumerate(runs)):
            if asset is not None:
                assets[start] = asset
                if progress:
                    progress(start)

    logger.info("段落配图：%d 组中成功 %d 张", len(runs), len(assets))
    return assets
