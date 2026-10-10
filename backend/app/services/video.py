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
import re
import logging
import math
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .audio_track import Pause, align_boundaries, detect_pauses
from .panels import Panel, detect_panels, panel_count_from_caption, panel_labels

logger = logging.getLogger(__name__)

# 社区品牌配色与素材（取自社区自己的视频合成项目 qingkelab/qingke-video）
BRAND_DARK = "#0a120c"
BRAND_DARK_PANEL = "#0f1a14"
BRAND_GREEN = "#7CB342"
BRAND_GOLD = "#F5C842"
BRAND_TEXT = "#f5f5f0"

# 正文页右上角的 logo 水印尺寸（logo 是 1074x288，宽高比约 3.73）
WATERMARK_W = 120
WATERMARK_PAD = 10

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

# ---------------------------------------------------------------------------
# 画幅与版式
# ---------------------------------------------------------------------------
#
# 两种朝向共用同一套版式逻辑，只是数值不同：**竖版**（936×1210，论文首页的比例，
# 适合手机全屏）与**横版**（1920×1080，适合投屏、B 站/X 这类横屏场景）。
#
# 结构都是「标题条 → 图片区 → 图注 → 强调行 → 字幕带」这一条纵向流，
# 换朝向只换数值，不换阅读顺序 —— 这样两版看起来是同一个产品，而不是两种风格。
@dataclass(frozen=True)
class Layout:
    width: int
    height: int
    title_baseline: int
    image_top: int
    image_box_left: int
    image_box_w: int
    image_box_h: int
    caption_top: int
    subtitle_top: int
    subtitle_left: int
    subtitle_width: int
    subtitle_text_top: int
    subtitle_max_height: int
    point_top: int
    point_left: int
    point_width: int
    point_height: int
    point_max_font: float
    waveform_top: int
    waveform_left: int
    waveform_width: int
    # 图片区之外、字幕带之上那块空间的高矮（强调行要摆进去，横竖版差很多）
    landscape: bool = False


PORTRAIT = Layout(
    width=936,
    height=1210,
    title_baseline=46,
    image_top=74,
    image_box_left=20,
    image_box_w=896,
    image_box_h=742,
    caption_top=822,
    subtitle_top=968,
    subtitle_left=40,
    subtitle_width=856,
    subtitle_text_top=1012,
    subtitle_max_height=186,
    point_top=878,
    point_left=40,
    point_width=856,
    point_height=74,
    point_max_font=40.0,
    waveform_top=1180,
    waveform_left=40,
    waveform_width=856,
)

# 横版：图片区吃满整宽（论文里的图大多是横的，横版正好），底线位置与竖版同构。
LANDSCAPE = Layout(
    width=1920,
    height=1080,
    title_baseline=60,
    image_top=92,
    image_box_left=64,
    image_box_w=1792,
    image_box_h=560,
    caption_top=660,
    subtitle_top=828,
    subtitle_left=64,
    subtitle_width=1792,
    subtitle_text_top=876,
    subtitle_max_height=150,
    point_top=716,
    point_left=64,
    point_width=1792,
    point_height=92,
    point_max_font=44.0,
    waveform_top=1040,
    waveform_left=64,
    waveform_width=1792,
    landscape=True,
)


def layout_for(orientation: str | None) -> Layout:
    """按朝向取版式；认不出的取值一律当竖版（老数据的默认）。"""
    return LANDSCAPE if (orientation or "").lower() in ("landscape", "horizontal", "16:9") else PORTRAIT


# 兼容：老代码与测试直接引用这些常量，它们就是竖版版式的数值
# 竖版布局：标题条 → 图片区 → 图注 → **强调行** → 字幕面板
#
# 图片区从 812 收到 742：腾出来的 70px 给「本段要点」那一行大字。
# 这是刻意的取舍 —— 观众要的是「这段在讲什么」，论文配图是佐证。
# 图注、强调行、字幕三者的位置必须互不重叠，改任何一个都要一起看。
TITLE_BASELINE = PORTRAIT.title_baseline
IMAGE_TOP = PORTRAIT.image_top
IMAGE_BOX_W = PORTRAIT.image_box_w
IMAGE_BOX_H = PORTRAIT.image_box_h
IMAGE_BOX_LEFT = PORTRAIT.image_box_left
CAPTION_TOP = PORTRAIT.caption_top

# 「本段要点」强调行：浅蓝底 + 左侧色条 + 大字，是画面上最抢眼的一行
POINT_TOP = PORTRAIT.point_top
POINT_LEFT = PORTRAIT.point_left
POINT_WIDTH = PORTRAIT.point_width
POINT_HEIGHT = PORTRAIT.point_height
POINT_BAR_W = 6
POINT_BG = "#eef4fb"
POINT_BAR = "#2f6fb5"
POINT_TEXT = "#17416f"
POINT_MAX_FONT = 40.0
POINT_SLIDE_SEC = 0.4     # 强调行滑入的时长：它标记「新的一段开始了」
# 强调文案的长度上限。40px 字号下一行能放约 22 个汉字、约 44 个西文字符，
# 各留一点余量。
#
# **中英文必须分开定**：卡成一个数（比如 20 字符）会把英文切得只剩三四个词 ——
# 实测出现过 "State pool outgrows"、"Memory problem is" 这种断在半句的强调行，
# 而 prompt 里对英文的要求是「8 个词」（≈40 字符）。两边必须对齐。
POINT_MAX_CHARS = 20
POINT_MAX_CHARS_EN = 40


def point_char_limit(language: str) -> int:
    """强调行的长度上限（按语言）。prompt 里的字数要求必须和它对齐。"""
    return POINT_MAX_CHARS_EN if language == "en" else POINT_MAX_CHARS

SUBTITLE_TOP = PORTRAIT.subtitle_top
SUBTITLE_LEFT = PORTRAIT.subtitle_left
SUBTITLE_WIDTH = PORTRAIT.subtitle_width
# 去掉主播标签后，文字可以往上提、可用高度也变大，字幕能放得更大更好读
SUBTITLE_TEXT_TOP = PORTRAIT.subtitle_text_top   # 第一行文字的基线
SUBTITLE_MAX_HEIGHT = PORTRAIT.subtitle_max_height   # 留给文字的总高度

FONT_STACK = "PingFang SC, Hiragino Sans GB, Microsoft YaHei, Noto Sans CJK SC, sans-serif"

# 画面是**白底**：论文配图本身多数是白底图表，深色画布会把它们衬得像贴图，
# 白底更接近读论文的观感，也更适合投屏和打印截图。
BG_COLOR = "#ffffff"
TITLE_COLOR = "#1a2233"

# ---- 封面帧（片头那一帧）的排版 ----
# 为什么单独做一套：正文页的图片区是「一页纸占满」，而封面要有**大字标题**。
# 封面的大字标题 + 论文首页的图都画在图片卡那一层里（见 render_image_card）。
COVER_PAD = 20                  # 卡内留白（卡本身贴在 x=20，所以画面上是 40，与字幕对齐）
COVER_HEADLINE_MAX_FONT = 54    # 竖版最大字号；横版自动降（见 cover_headline_layout）
COVER_HEADLINE_MIN_FONT = 30    # 再小就截断，别缩成小字报
COVER_HEADLINE_LEADING = 1.22   # 行距倍数
COVER_HEADLINE_BASELINE = 0.92  # 首行基线在行盒里的位置
COVER_ACCENT_W = 76             # 标题下面那根品牌绿短杠
COVER_ACCENT_H = 7
COVER_ACCENT_GAP = 14
COVER_TITLE_COLOR = "#121a2b"

# ---- 封面的「毛玻璃」：论文首页整幅铺满 + 模糊 + 白色薄纱，标题压在它上面 ----
#
# 为什么要模糊：论文首页直接当封面背景时，它自己那行大标题、作者、摘要会跟我们的
# 爆款标题抢注意力（两行大字叠在一起，谁也读不清）。模糊 + 薄纱把它变成一层
# 「看得出来是论文、但读不出字」的底，标题就成了画面上唯一的字。
# 高斯模糊半径（卡片像素，竖版；横版按比例缩）。**这个数就是「毛玻璃的浓度」**：
# 实测论文首页上的正文小字约 9px 高，半径 ≥11 时整块底会被糊成均匀的浅灰（明暗跨度 14），
# 那就看不出「这是一篇论文」了；半径 6 时字读不出来、但还留得住灰度结构（跨度 ~25）。
COVER_GLASS_BLUR = 6.0
COVER_GLASS_VEIL = 0.30         # 压在上面的薄纱透明度：太低会看到纸上的字，太高就变纯白板
# 薄纱的**颜色**。用纯白的话整块封面会白得发灰、跟白底画布糊在一起，看不出「玻璃」；
# 一点冷调（浅蓝灰）能让「玻璃面」和「面板」分层，也更像一块真的毛玻璃。
COVER_GLASS_TINT = "#cfdcec"
# 放大倍率：论文首页整页铺进来时，正文区是 9px 的小字，模糊之后几乎全化在白纸里
# （实测明暗跨度只有 15~18，等于一块纯色卡）。放大之后字变大、模糊留下可见的灰块，
# 整块底才像「隔着毛玻璃看一张论文」，而不是「一张浅灰色的卡」。
COVER_GLASS_ZOOM = 1.0
# 放大围绕哪个纵向位置（0~1）：0.5 = 卡片正中。用 0.42 是想让放大后的视野落在
# 论文首页的标题/摘要那一带（那儿墨最多、模糊后纹理最明显）。
COVER_GLASS_ZOOM_CY = 0.42
# 裁满时对齐论文首页的哪一段：`Min` = 顶部（论文自己的标题/作者/摘要那一带）。
# 用 `Mid` 会看到正文中间那片密集小字，模糊之后几乎均匀；顶部那块字大、留白也多，
# 隔着毛玻璃还能看出「这是一篇论文的开头」。
COVER_GLASS_ANCHOR = "Min"
# 竖版卡片里「论文首页」的显示倍率（896/935 ≈ 0.958），模糊半径以它为 1.0 的基准
COVER_GLASS_BLUR_REF_SCALE = 0.958
COVER_PANEL_MAX_W = 820         # 玻璃面板最大宽度（竖版卡片 896 宽）
COVER_PANEL_PAD = 44            # 面板内边距
COVER_PANEL_FILL = 0.82         # 面板本身的那层白（叠在白纱上，比纱更实一点）
COVER_PANEL_RADIUS = 20
COVER_SUBTITLE_FONT = 20        # 面板里的论文原题
COVER_SUBTITLE_COLOR = "#4a5568"
COVER_HEADLINE_MAX_LINES = 2    # 封面上标题最多两行
COVER_SUBTITLE_MAX_LINES = 2    # 论文原题最多两行
CAPTION_COLOR = "#6b7a8f"
FRAME_STROKE = "#d8dfe8"
SUBTITLE_BG = "#f4f7fa"
SUBTITLE_RULE = "#dde5ee"
SUBTITLE_TEXT = "#16202f"

# ---------------------------------------------------------------------------
# 动效
# ---------------------------------------------------------------------------
#
# **动效必须服务于内容，而不是「让画面别太静」。**
#
# 第一版做的是缓慢推近/拉远 + 平移（Ken Burns），被一句「这种图片无意义的放大、
# 移动和缩小没有价值，重点是突出解读的内容」否掉了 —— 而且否得对：
# 那种运动跟正在讲的内容没有任何关系，观众看得出它只是在动。
# 现在留下的是**三件都在讲内容的事**：
#
# 1. **「本段要点」强调行**：一段大字滑入，写的就是这一段最该被记住的结论或数字。
#    这是画面上最抢眼的东西，也是「突出解读」的落点。
# 2. **字幕逐句出现**，并把句子里的**数字**标成强调色 + 浅色底：
#    论文解读里真正有信息量的往往就是那个百分比。
# 3. **顶部进度条**：长内容需要「还剩多少」的锚点。
#
# 三件都靠 ffmpeg 滤镜完成，**不额外渲染帧**（渲染成本不变）：
# 强调行/字幕用 `overlay` + `fade`/`x` 表达式，进度条用 `overlay` 的逐帧 x 表达式。
PROGRESS_BAR_H = 5
PROGRESS_BAR_COLOR = "#2f6fb5"
# 编码质量。**加了动效之后不能再用 23**：静止幻灯片一帧能顶几秒、压缩率极高，
# 而逐帧都在动的画面每一帧都要花比特。实测同一个 250 秒的片子，CRF 23 从 6.4MB
# 涨到 26.4MB（4 倍）；CRF 26 约 14MB，白底图表 + 大字幕在这个码率下看不出差别，
# 而 6.4MB → 14MB 是「有真实运动」应付的代价。
VIDEO_CRF = 26
CAPTION_FADE_SEC = 0.35   # 一句字幕的淡入时长
CAPTION_BEAT_MAX = 2      # 一段最多拆成几句字幕
CAPTION_BEAT_MIN_CHARS = 28   # 短于这个长度就别拆了：每句只剩几个字，闪得更难看
CAPTION_BAND_MAX_FONT = 38.0  # 字幕带单独渲染，字可以比整页大（观众主要在读它）
_SENTENCE_END = "。！？!?；;…"

# ---------------------------------------------------------------------------
# 转场
# ---------------------------------------------------------------------------
#
# 实测参考（一条 40 秒的论文宣传片，白底、16:9）：**没有任何硬切**，
# 每 9~10 秒整页换一次，换的过程约 0.25~0.5 秒 —— 也就是「快速淡过去」。
# 所以这里的默认是溶解（dissolve）、0.35 秒，而不是花哨的擦除/翻页。
#
# **长度对齐不能破**：过渡发生在**新片段内部**（用上一段的图片卡做底层），
# 不跨片段 xfade —— xfade 会重叠、把总长缩短，画面就会比声音早。
# 每段仍是 `-frames:v <整数帧>`，所以拼接总长逐帧不变（实测 45 帧片段仍是 45 帧）。
TRANSITION_STYLE = "dissolve"     # none | dissolve | push
TRANSITION_SEC = 0.35
TRANSITION_MIN_SEC = 0.12         # 短于这个就别做了（短段整段都在过渡更难看）
TRANSITION_MAX_RATIO = 0.35       # 过渡最长占这一段的比例
FADE_IN_SEC = 0.30                # 首段从白底淡入

# ---------------------------------------------------------------------------
# 声波条（跟着语音起伏）
# ---------------------------------------------------------------------------
#
# 用户要的是「根据语音增加一些动画效果」。声波条是这件事的可见落点：
# 贴在字幕带底部，实时显示**当前说话人**的音量起伏，颜色也随说话人走
# （A 麦金 / B 石板蓝，和脚本页的主播配色一致）。
#
# 实现用 ffmpeg 的 `showwaves`，但有两个坑都是实测出来的：
# 1. **某些宽度下 showwaves 什么都不画**（`s=856x60/40/18` 输出全黑，`s=800x18` 正常）
#    → 固定按 800 宽渲染，再用 `scale` 缩到目标尺寸。
# 2. 它画的是「黑底上的彩色波形」，直接叠到浅色字幕带上会留一层黑底
#    → 用 `format=gray` 当 alpha，和纯色源 `alphamerge`，得到干净的上色波形
#    （实测残余暗像素 0，而这正是「够亮」判据看不出来的那种脏东西）。
WAVEFORM_TOP = PORTRAIT.waveform_top          # 贴字幕带底部
WAVEFORM_LEFT = PORTRAIT.waveform_left        # 和字幕左对齐
WAVEFORM_WIDTH = PORTRAIT.waveform_width
WAVEFORM_HEIGHT = 18
WAVEFORM_RENDER_SIZE = "800x60"   # 见上面第 1 条坑：别改这个宽度
WAVEFORM_COLOR = "#2f6fb5"

# ---------------------------------------------------------------------------
# 图内聚光灯：讲到哪一块就把那一块框出来、其余压暗
# ---------------------------------------------------------------------------
#
# 用户否掉过「整张图推拉镜头」，理由是跟内容无关。这个不一样：**框的是正在讲的那一块**，
# 所以它跟「本段要点」是同一类东西 —— 讲内容，不装饰。
# 参考片里的图其实是静止的（每 9~10 秒整页换一次、只做一次快速淡化），
# 所以这一步是超出参考的：让图本身跟着讲解推进。
FOCUS_DIM = "#ffffff"          # 压暗用白：论文图本来就是白底，白蒙版比黑蒙版干净
FOCUS_DIM_ALPHA = 0.72
FOCUS_BORDER = "#2f6fb5"
FOCUS_BORDER_W = 3
FOCUS_FADE_SEC = 0.25          # 聚光灯淡入（跟图片卡一样是「这一段开始了」的信号）
FOCUS_MIN_SIDE = 0.06          # 小于这个比例的区域不值得框（多半是模型瞎给）
# 框上那个小药丸最多放多少字（中英文都按字符算，够放下 2~3 个英文词）
FOCUS_LABEL_CHARS = 16
FOCUS_MIN_AREA = 0.02
# 整张图都框住 = 什么都没突出。实测模型很爱这么干：它给不出「图里的哪一块」，
# 就把整张图框上、再补一个图名（比如 w=1.0,h=1.0,label="四比特精度表"）。
# 那不是聚光灯，是把图压暗一圈，所以直接丢掉。
FOCUS_MAX_AREA = 0.85
SPEAKER_WAVE_COLORS = {"A": "#d3a24a", "B": "#79a9c9"}

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
    # 「本段要点」：一句不超过 14 个字的大字强调（模型写，兜底从原文抠数字）
    point: str = ""
    # 图内聚光灯：这一段在讲图里的哪一块（相对 0~1 比例 + 短标签）。None = 不框
    focus: dict[str, Any] | None = None
    # 封面上的大字标题（**只有片头那一帧用**）。正文页顶部不再放论文标题 ——
    # 一行小字挂在每帧顶上既不抓人、又占掉画面，标题的活儿交给封面。
    headline: str = ""
    # "outro" 表示这是片尾品牌段 —— 视频层会把它渲染成品牌卡片而不是普通配图页
    brand: str = ""

    @property
    def duration(self) -> float:
        return max(self.end - self.start, 0.05)


@dataclass
class CaptionBand:
    """一句字幕：文字（用来按字数分配时长）+ 渲染好的底部字幕带图片。

    字幕带是**独立图层**：底图不画字幕，字幕按句做成带子，用 overlay 按时序叠上去。
    这样同一张静止画面在段中间有一次内容变化 —— 这是「字幕逐句出现」的全部目的。
    """

    text: str
    image: Path


@dataclass
class Transition:
    """一段画面开头的转场。

    `kind`：
    - `none`：直接切（图片没变、或片段太短）
    - `fade_in`：整页从白底淡入（只在第一段）
    - `dissolve`：上一张图淡出、这张淡入（默认）
    - `push`：上一张往左推出去、这张从右推入
    - `to_card`：上一整页淡出、露出底下的片尾品牌卡
    """

    kind: str = "none"
    seconds: float = 0.0
    # dissolve/push 需要上一段的**图片卡**；to_card 需要上一段的**整页**
    previous: Path | None = None


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
    # 每一段用的是哪张图（图片 id，如 f1 / cover / topic2）。
    # 存下来是为了「重新合成视频」时**复用**，而不是再问一次模型 ——
    # 再问一次结果会变、还会把主题图重新生成一遍（4 次调用 + 几十秒）。
    scenes: list[dict[str, Any]] = field(default_factory=list)
    # 用到的素材 id -> 文件路径，重建时按它找图
    assets: dict[str, str] = field(default_factory=dict)
    # 合成时各素材的版本号（mtime-size），用来判断视频是否已经过时：
    # 用户手动旋转了配图、或重新提取过，视频里还是旧画面
    asset_versions: dict[str, str] = field(default_factory=dict)
    # 封面上的大字标题（爆款标题）。和 scenes 里的 point 一样属于
    # 「模型写一次、以后复用」的数据 —— 重新合成时不该换一句。
    hook: str = ""

    def to_dict(self) -> dict:
        return {
            "duration_sec": round(self.duration_sec, 2),
            "scene_count": self.scene_count,
            "bytes": self.bytes_written,
            "assignment": self.assignment,
            "scenes": self.scenes,
            "assets": self.assets,
            "asset_versions": self.asset_versions,
            "hook": self.hook,
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


# 封面标题（爆款标题）的长度上限。
# 为什么不照抄论文原题：原题（「STEPQuant: When and Where Errors Matter in Deep
# Quantization」）在封面大字上既长又没有钩子，观众扫一眼就走了。封面要的是
# 「**跟我有关 / 有意思**」的一句话，原题放在它下面那行小字里（见 build_scenes 的
# cover_caption）—— 两行各司其职：一行抓人，一行交代出处。
HOOK_MAX_CHARS = 16
# 断在标点处至少要保留多少比例的内容，否则宁可不按标点断
HOOK_BREAK_KEEP = 0.6
HOOK_MAX_WORDS_EN = 9

HOOK_SYSTEM = """你在给一个「论文解读视频」写**封面标题**。

观众在信息流里刷到这段视频，画面一闪而过，只有封面上的这一行字能让人停下来。
你要写的不是论文的名字，而是**让人想点开的那一句话**。

规则：
- **不超过 14 个字**（写到 16 个字就已经是上限，超了会被我们截断 —— 与其被截，
  不如一开始就把话说短）。封面上的字号很大，长一点就挤成两行小字。
- 必须来自这篇论文**真实的内容**：它解决了什么、发现了什么、结果有多反常。
  **不许编造**数字或结论，论文里没有的别写。
- 四档风格，**挑最贴这篇论文的那一档**，不要四档混着写：
  1. **数字结论型**：「6.93 倍压缩，精度不掉」
  2. **悬念提问型**：「并发到 70，模型就装不下了？」
  3. **反差型**：「省了显存，却更容易崩」
  4. **代价型**：「精度换速度，这笔账划不划算」
- 可以口语、可以带问号，但**不要**标题党到失真（观众点进来发现不是那么回事就划走了）。
- **不要**出现「论文」「本文」「研究」「一种」这类学术腔开头，也不要引号。
- 语言与脚本一致（中文脚本写中文，英文脚本写英文），结尾不加标点。
- 只输出这一行字本身，不要解释、不要 JSON、不要换行。"""

HOOK_SYSTEM_EN = """You write the **cover headline** for a paper-explainer video.

A viewer scrolls past this video in a feed. The only thing that can stop the scroll is the
one line on the cover. Write that line — not the paper's title, but the sentence that makes
someone want to watch.

Rules:
- **At most 8 words** (9 is the hard ceiling — anything longer gets trimmed, and a trimmed
  headline reads like a cut-off sentence). The cover uses a very large font; longer wraps.
- It must come from the paper's **actual content**: what problem it solves, what it found,
  how surprising the result is. **Never invent** numbers or claims that are not in the paper.
- Pick **one** of these four styles — whichever fits this paper best, do not mix them:
  1. **number/result**: "6.9x compression, same accuracy"
  2. **question/hook**: "70 concurrent chats and the model is full?"
  3. **contrast**: "saves memory, breaks accuracy"
  4. **trade-off**: "what speed actually costs"
- Stay honest: a headline that oversells loses the viewer in the first ten seconds.
- No academic throat-clearing ("This paper", "We study", "A novel"), no quotation marks,
  no trailing punctuation. Output the line only — no explanation, no JSON, no line breaks."""


def build_hook_messages(
    *,
    title: str = "",
    analysis: dict[str, Any] | None = None,
    language: str = "zh",
) -> list[dict[str, str]]:
    """要一句封面标题。

    只发解读稿里最有信息量的几栏（创新点 / 核心结论 / 结果），不把整份解读塞进去：
    封面标题是个「一句话」的活，材料给多了模型反而写成综述。
    """
    system = HOOK_SYSTEM if language == "zh" else HOOK_SYSTEM_EN
    data = analysis or {}

    def joined(key: str, *, limit: int) -> str:
        value = data.get(key)
        if isinstance(value, list):
            return "；".join(str(item) for item in value[:3])[:limit]
        return str(value or "")[:limit]

    if language == "zh":
        user = f"""【论文原题】{title or "（未提供）"}

【解读稿摘要】
- 创新点：{joined("innovations", limit=160) or "（未提供）"}
- 方法：{joined("method", limit=160) or "（未提供）"}
- 实验与结果：{joined("experiments", limit=200) or "（未提供）"}
- 核心结论：{joined("conclusion", limit=160) or "（未提供）"}

请写一句不超过 14 个字的封面标题，只输出这一行字。"""
    else:
        user = f"""[PAPER TITLE] {title or "(not provided)"}

[ANALYSIS SUMMARY]
- Contributions: {joined("innovations", limit=160) or "(not provided)"}
- Method: {joined("method", limit=160) or "(not provided)"}
- Results: {joined("experiments", limit=200) or "(not provided)"}
- Conclusion: {joined("conclusion", limit=160) or "(not provided)"}

Write the cover headline in at most 8 words. Output that one line only."""
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def normalize_hook(raw: Any, *, language: str = "zh") -> str:
    """整理模型给的封面标题：去掉引号/书名号/序号/换行，按语言卡长度。

    模型经常把标题包在引号里、前面还写个「封面标题：」，这些都直接剥掉 ——
    封面上的大字出现引号非常出戏。
    """
    def clean(line: str) -> str:
        # 先去**外层装饰**（引号、markdown 强调符）：它们会挡住下面那些以「行首」为锚的
        # 前缀规则 —— 实测 `"封面标题：…"` 这种「引号 + 前缀」的组合就是漏网的
        # （模型很爱这么写，而封面上的大字顶着「封面标题：」四个字非常出戏）。
        line = line.strip().strip('"\'“”「」《》【】*_`').strip()
        line = re.sub(
            r"^[\s\-•*\d.、)）:：]*(封面标题|标题|headline|title)\s*[:：]?",
            "",
            line.strip(),
            flags=re.IGNORECASE,
        )
        # 模型爱把风格名一起写出来（「反差型：省了显存却更容易崩」）——那是给它自己看的，
        # 顶到封面上很出戏。只认「XX型：」这一种形态，真实标题里极少这么开头。
        line = re.sub(r"^[\u4e00-\u9fffA-Za-z]{2,5}型\s*[:：]\s*", "", line)
        line = re.sub(r"^[（(]\s*[1-4]\s*[)）]\s*", "", line)
        line = line.strip().strip('"\'“”「」《》【】`').replace("**", "").strip()
        line = re.sub(r"\s+", " ", line).rstrip("。．.,，;；!！?？~～")
        return line.strip()

    text = str(raw or "").strip()
    if not text:
        return ""
    # 多行时逐行清理，取**第一行有内容的**（标题在头一行；「标题」这种标签被剥掉后会是空）
    for line in text.splitlines():
        candidate = clean(line)
        if len(candidate) >= 4:
            text = candidate
            break
    else:
        text = clean(text)
    if not text:
        return ""
    return _trim_hook(text, language=language)


# 封面标题超长时，从哪里断开。
# 硬砍到第 16 个字是不行的 —— 实测砍出来过「4 比特状态量化，反超均匀 IN」（把 INT8 砍成 IN）、
# 「4-bit 反超 INT8，显存」（半句）。封面上的大字出现半句比短一句难看得多，
# 所以优先在**标点/分句处**断开，其次至少不要把一个词砍成两半。
_HOOK_BREAKS = "，。、；：！？；,.;:!?/|"


def _trim_hook(text: str, *, language: str) -> str:
    """超长时在**分句处**断开，而不是数着字数硬砍。"""
    if language != "zh":
        words = text.split()
        if len(words) <= HOOK_MAX_WORDS_EN:
            return text
        head = words[:HOOK_MAX_WORDS_EN]
        # 英文按词截会截到句子中间（实测「…aren't uniform; only some last」）。
        # 词数够多时宁可退回上一个分句，读起来是完整的一句。
        for index in range(len(head) - 1, 2, -1):
            if head[index - 1].endswith((",", ";", ":", "—")):
                return " ".join(head[:index]).rstrip(",;:— ")
        return _strip_trailing_stopwords(" ".join(head), language=language, floor=HOOK_MAX_WORDS_EN // 2)

    if len(text) <= HOOK_MAX_CHARS:
        return text
    window = text[:HOOK_MAX_CHARS]
    cut = max(window.rfind(ch, HOOK_MAX_CHARS // 2) for ch in _HOOK_BREAKS)
    # 只在「断在标点处仍然保留了大部分内容」时才用标点断句：
    # 否则会砍成「4 比特状态量化」这种只剩前半句的标题（实测出现过）。
    if cut >= HOOK_MAX_CHARS * HOOK_BREAK_KEEP:
        return window[:cut].strip()
    # 没有合适的标点：至少别把结尾那个词砍成两半（「反超均匀 IN」→「反超均匀」）
    trimmed = re.sub(r"[A-Za-z0-9.%+\-]+$", "", window).strip()
    return _strip_trailing_stopwords(trimmed or window, language=language, floor=HOOK_MAX_CHARS // 2)


def _cover_fallback_headline(title: str) -> str:
    """没有模型输出时的封面大字：用论文原题（会截断）。

    只在这一种情况下用：说话模型不可用（Mock / 调用失败）。封面上什么都不放
    比放个原题更糟 —— 观众总得知道这是哪篇论文。
    """
    text = " ".join((title or "").split())
    if not text:
        return ""
    limit = HOOK_MAX_CHARS * 2  # 封面允许两行，所以比爆款标题宽一倍
    if len(text) > limit:
        text = text[: limit - 1].rstrip("，。、,.;") + "…"
    return text


POINTS_SYSTEM = """你在给一个「论文解读视频」做**画面强调**。

观众在听双人播客，画面上除了字幕，还需要一句**大字强调**：这一段最该被记住的是什么。
你要为每一段脚本写一句强调文案。

规则：
- **不超过 14 个字**（英文不超过 8 个词）。画面上是一行大字，超了就挤。
- 必须是**这一段自己的内容**：关键的结论、数字、机制名、对比结果。
  有数字就优先给数字（「成功率 67%」「比基线高 3 倍」）—— 这是观众最记得住的东西。
- **不要**写「很重要」「值得关注」这类空话，也不要把整段话缩写一遍。
- **不要编造**原文没有的数字或结论。
- **每一段都要有一句**，包括过渡段：过渡段就写这一段**要讲的话题**（例如「误差为什么会滚雪球」），
  而不是「我们接着看」这种本身没有信息量的话。
  （第一版允许留空，结果整条视频只有 18% 的时间有强调行 —— 观众感觉「这行时有时无」。）
- 语言与原文一致（原文中文就中文，英文就英文）。
- **每一段都必须带 `panel` 字段，一个都不能省**（省略字段和写 null 是两回事）：
  - 如果这一段配的图**下面给了子图清单**（形如 `(a) …；(b) …`），并且这一段讲的就是
    其中某一个子图 → 写那个字母，例如 `"b"`；
  - 讲的是整张图、清单里没有对得上的、或者这一段没有子图清单 → 写 `null`。
  **不要输出坐标**：图片里的区域位置由我们从图上量出来，你只需要指出是哪个子图。
  指错子图比不指更糟，拿不准就写 `null`。

只输出 JSON：
{"points": [{"segment": 0, "point": "成功率 67%", "panel": "b"},
            {"segment": 1, "point": "误差会滚雪球", "panel": null}]}
每个脚本段都要有一项。不要输出任何解释。"""

POINTS_SYSTEM_EN = """You write the big on-screen emphasis line for a paper-explainer video.

The viewer is listening to a two-host podcast. Besides the captions, the screen shows one
line of large text: the single thing from this segment worth remembering. Write that line
for every segment.

Rules:
- **At most 8 words.** It is one large line on a 936px-wide canvas; longer will not fit.
- Use **this segment's own content**: the key result, number, mechanism name, or comparison.
  Prefer a number when there is one ("67% success rate", "3x over baseline") — that is what
  viewers remember.
- Do **not** write vague filler like "this matters" or "worth noting", and do not paraphrase
  the whole segment.
- Do **not** invent numbers or conclusions that are not in the text.
- **Every segment gets a line**, transitions included: for a transition, name the topic it
  is introducing ("why errors snowball"), not "let's move on" (which says nothing).
  (The first version allowed empty lines; only 18% of the video ended up with an emphasis
  line, which reads as "this line flickers on and off".)
- **Write in English** (the whole video is English), **start with a capital letter**,
  and do not end with a period — it is a headline, not a sentence.
- **Every segment must carry a `panel` field — never omit it**:
  - if the image for this segment comes with a **panel list** (like `(a) …; (b) …`) and
    this segment is genuinely about one of those panels → write that letter, e.g. `"b"`;
  - if it is about the whole figure, nothing in the list matches, or there is no panel
    list → write `null`.
  **Never give coordinates**: the region of the image is measured from the pixels on our
  side; you only say which panel. Pointing at the wrong panel is worse than not pointing.

Output JSON only:
{"points": [{"segment": 0, "point": "67% success rate", "panel": "b"},
            {"segment": 1, "point": "errors snowball", "panel": null}]}
Include one entry per segment. No explanations."""

# 强调行里允许出现的数字形态（用于本地兜底）
_NUMBER_TOKEN = re.compile(r"\d+(?:\.\d+)?\s*(?:%|％|倍|万|亿|千|个百分点|个|条|张|次|秒|分钟|层|维|B|M|K)?")


def local_point(text: str, *, max_chars: int = POINT_MAX_CHARS, language: str = "zh") -> str:
    """没有模型可用时的兜底强调行：从这一段的原文里抠出一句带数字的短语。

    为什么只认数字：实测这几集的脚本里只有 **30%~47%** 的段落含阿拉伯数字
    （有一集 0%），而「论文关键词命中」几乎是 0 —— 脚本是口语化的，不会照抄术语。
    所以本地抽取给不出稳定的「要点」，真正的要点得让模型写（见 `POINTS_SYSTEM`）。
    兜底只做最有把握的一件事：**把数字拎出来**，其余情况返回空串（画面就不显示强调行）。
    """
    clean = " ".join((text or "").split())
    candidates = list(_NUMBER_TOKEN.finditer(clean))
    if not candidates:
        return ""

    # 一句里常有多个数字（「16 个任务上平均成功率 67%」）——要挑**信息量最大**的那个：
    # 百分比 > 带单位 > 光秃秃的整数。挑第一个的话会拎出「16 个任务」，
    # 而观众真正该记住的是「67%」。
    def importance(match: re.Match[str]) -> int:
        token = match.group(0)
        if "%" in token or "％" in token or "倍" in token or "百分点" in token:
            return 3
        if any(unit in token for unit in ("万", "亿", "千", "秒", "分钟", "层", "维", "B", "M", "K")):
            return 2
        return 1 if len(token) >= 2 else 0

    match = max(candidates, key=importance)

    # 以数字为中心，向左右扩到句子边界，再截到 max_chars
    left, right = match.start(), match.end()
    for index in range(match.start() - 1, -1, -1):
        if clean[index] in "。！？；，、,.;:":
            left = index + 1
            break
        left = index
    for index in range(match.end(), len(clean)):
        if clean[index] in "。！？；，、,.;:":
            right = index
            break
        right = index + 1

    phrase = clean[left:right].strip()
    # 短语里若还夹着标点（模型写的问句「Should we go to 8 bits? People tried…」），
    # 就在第一个标点处收掉，别把下一句的头几个词带进来（实测出现过「to 8 bits? People」）
    for position, char in enumerate(phrase):
        if char in "。！？；，、,.;:!?":
            phrase = phrase[:position]
            break
    phrase = phrase.strip()
    if not phrase:
        return ""
    if len(phrase) > max_chars:
        # 太长就只留数字和它前后的少量上下文
        head = max(0, match.start() - left - 4)
        phrase = clean[left + head : right]
    return shorten_point(phrase, max_chars=max_chars, language=language)


# 收短之后可能停在虚词上（「Quantization error feeds back and」「Memory problem is」）。
# 画面上最抢眼的一行以虚词结尾很扎眼，把它们去掉 —— 只在剩下的长度还够时去。
_TRAILING_STOPWORDS: dict[str, frozenset[str]] = {
    "en": frozenset(
        """and or but the a an to of with for in on that is are was were be been being as by at
        from it its this these those than then so if when while which who whom whose not no into
        over under about after before""".split()
    ),
    "zh": frozenset("的 了 和 与 或 而 就 还 也 在 是 把 被 对 从 到 这 那 并 且 以 及 有 会".split()),
}


def _strip_trailing_stopwords(text: str, *, language: str, floor: int) -> str:
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


def shorten_point(text: str, *, max_chars: int = POINT_MAX_CHARS, language: str = "zh") -> str:
    """把过长的强调文案收短到上限，**在词/标点边界处断开**。

    按字符硬切会切出「at 4 and 6 bi」「INT6 errors a」这种半截话（实测踩到过），
    画面上最抢眼的一行出现半截英文比不显示还糟。
    优先切成句标点，其次空格，实在没有才硬截。
    """
    clean = " ".join((text or "").split())
    floor = int(max_chars * 0.5)
    if len(clean) <= max_chars:
        return _strip_trailing_stopwords(clean, language=language, floor=floor)
    window = clean[:max_chars]
    for separator in ("，", "。", "、", "；", "：", "！", "？", ",", ".", ";", ":", "!", "?", " "):
        cut = window.rfind(separator)
        if cut >= max_chars * 0.6:
            return _strip_trailing_stopwords(
                window[:cut].strip(), language=language, floor=floor
            )
    return _strip_trailing_stopwords(window.strip(), language=language, floor=floor)


def build_points_messages(
    segments: list[dict[str, Any]],
    *,
    title: str = "",
    language: str = "zh",
    image_captions: list[str | None] | None = None,
    panel_options: list[list[tuple[str, str]]] | None = None,
) -> list[dict[str, str]]:
    """为每一段脚本要一句「大字强调」。

    单独一次调用（而不是塞进逐段配图那次）的原因：重新合成视频时必须**复用**画面分配、
    不能重新问模型，但强调文案是可以补的、而且补一次就存下来。两件事的生命周期不同。

    `panel_options[i]` 是第 i 段那张图的**子图清单**（`[(字母, 这一块讲什么), …]`）。
    给出来是为了让模型做一件它做得对的事：**从文字里挑**「这一段在讲哪个子图」。
    它做不对的是「给坐标」—— 它看不到图，实测两次真实调用分别只给出 8/23 和 4/16 个
    可用框（有一集 4 个框全是同一块「左半张」，只是标签不同）。

    **英文版必须整段用英文 prompt**（`POINTS_SYSTEM_EN`）：中文 system 里就算写了
    「语言与原文一致」，模型给英文脚本写的强调行仍然是中文 —— 实测踩到过，
    英文那一版的画面上整行中文。这跟当初解读/脚本 prompt 踩的是同一个坑。
    """
    system = POINTS_SYSTEM if language == "zh" else POINTS_SYSTEM_EN
    captions = image_captions or []
    script_lines = []
    for index, segment in enumerate(segments):
        text = (segment.get("text") or "").strip().replace("\n", " ")
        caption = (captions[index] if index < len(captions) else None) or ""
        caption = " ".join(str(caption).split())[:120]
        # 把「这一段配的是哪张图」一并给模型，否则它没法判断该框图里的哪一块
        suffix = f"（这一段配的图：{caption}）" if caption else "（这一段没有配图）"
        if language != "zh":
            suffix = f" (image for this segment: {caption})" if caption else " (no image)"

        options = panel_options[index] if panel_options and index < len(panel_options) else []
        if options:
            items = "；".join(f"({letter}) {body}" for letter, body in options)
            suffix += (
                f"（这张图有 {len(options)} 个子图：{items}）"
                if language == "zh"
                else f" (this figure has {len(options)} panels: {items})"
            )
        script_lines.append(f"[{index}] {text}{suffix}")

    if language == "zh":
        user = f"""【论文标题】{title or "（未提供）"}

【脚本分段】共 {len(segments)} 段（编号 0 到 {len(segments) - 1}）
{chr(10).join(script_lines)}

请为每一段写一句不超过 14 个字的大字强调，并给出 `panel`（拿不准就写 `null`，但**字段不能省**），输出 JSON。"""
    else:
        user = f"""[PAPER TITLE] {title or "(not provided)"}

[SCRIPT SEGMENTS] {len(segments)} segments (numbered 0 to {len(segments) - 1})
{chr(10).join(script_lines)}

Write one emphasis line of at most 8 words per segment, and include a `panel` field for
every segment (use `null` when unsure — but never omit the field). Output JSON only."""
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def normalize_point_items(
    raw: Any,
    *,
    count: int,
    max_chars: int = POINT_MAX_CHARS,
    language: str = "zh",
    panel_letters: list[list[str]] | None = None,
) -> list[dict[str, Any]]:
    """把模型给的整份 payload 整理成每段一项：`{"point", "panel", "focus"}`。

    要点文案与「讲的是哪个子图」来自**同一次调用**（模型一边说「这段最该记住什么」，
    一边指出在图的哪一块），所以这里一起归一化 —— 分两次调用会多花一次钱，
    而且两者可能对不上。

    `panel` 只在**这一段那张图真的有这些字母**时才保留：模型偶尔会编一个图里
    不存在的字母（比如那张图根本没有子图），那种一律丢掉。

    `focus` 是**兼容字段**：早期版本让模型直接给坐标，实测不可靠（见 `POINTS_SYSTEM`
    的说明），现在只剩「读旧数据里已存下来的框」这一条用途。
    """
    items: list[dict[str, Any]] = [
        {"point": "", "panel": None, "focus": None} for _ in range(max(count, 0))
    ]
    if not isinstance(raw, list):
        return items
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        try:
            index = int(entry.get("segment", entry.get("index")))
        except (TypeError, ValueError):
            continue
        if not 0 <= index < count:
            continue
        text = shorten_point(str(entry.get("point") or ""), max_chars=max_chars, language=language)
        allowed = panel_letters[index] if panel_letters and index < len(panel_letters) else []
        letter = str(entry.get("panel") or "").strip().lower().strip("()")
        items[index] = {
            "point": text,
            "panel": letter if letter and letter in allowed else None,
            "focus": normalize_focus(entry.get("focus")),
        }
    return items


def _normalize_points(
    raw: Any, *, count: int, max_chars: int = POINT_MAX_CHARS, language: str = "zh"
) -> list[str]:
    """整理模型给的强调文案：丢掉认不出的项、超长的截断，缺的补空串。

    返回的列表长度一定是 `count` —— 调用方按段号取用，缺项要能对上位置。
    """
    points = [""] * max(count, 0)
    if not isinstance(raw, list):
        return points
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            index = int(item.get("segment", item.get("index")))
        except (TypeError, ValueError):
            continue
        if not 0 <= index < count:
            continue
        text = shorten_point(str(item.get("point") or ""), max_chars=max_chars, language=language)
        if text:
            points[index] = text
    return points


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
    points: list[str] | None = None,
    focuses: list[dict[str, Any] | None] | None = None,
    headline: str = "",
    cover_caption: str = "",
) -> list[Scene]:
    """把脚本、时间戳和逐段配图拼成画面时间轴。

    `timings` 是 RoundTiming 列表（顺序与脚本段一一对应），
    里面已经包含了片头音乐的偏移，可以直接当绝对时间用。

    `image_for_segment[i]` 是第 i 段脚本要显示的图片 id —— 逐段指定而不是
    「从某段开始一直用到下一张图」，这样才能保证画面跟着话题走。

    `points[i]` 是第 i 段的「要点强调行」文案（可为空串 → 这一段不显示强调行）。

    `headline` 是封面上的大字标题（爆款标题），`cover_caption` 是它下面那行小字
    （论文原题）—— 两者都只作用在片头那一帧。
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
    #
    # 只有「有片头音乐」时才会有这一段独立的封面帧（`head_end > 0.3`）。
    # 社区话术把片头音乐换掉之后，第一句人声就从 0 秒开始了 —— 于是这一段不存在，
    # 视频的**第一帧**变成了第一段脚本的画面。所以封面另有两条保底规则（见函数末尾）：
    # 「第一帧必须是论文首页」和「首页那几帧放大字标题」。
    head_end = ordered[0].start
    cover = assets.get("cover")
    cover_asset = cover if (cover and cover.exists) else None
    head_asset = cover_asset or asset_for(0)
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
                point=(points[index] if points and index < len(points) else ""),
                focus=(focuses[index] if focuses and index < len(focuses) else None),
                brand=str(segment.get("brand") or ""),
            )
        )

    # ---- 封面帧：第一帧必须是论文首页，而且要有大字标题 ----
    #
    # **第一帧必须是论文首页**（PDF 第一页）是产品要求：观众第一眼要看到这是哪篇论文，
    # 而不是某张配图。模型给第一段配了别的图时，这里也要把它换回封面。
    if cover_asset and scenes and scenes[0].kind != "cover":
        scenes[0].image = cover_asset.path
        scenes[0].kind = "cover"
        scenes[0].caption = cover_asset.caption
    # 首页那**连续几帧**（片头话术通常讲两三段，都还停在封面上）一起放大字标题：
    # 只放第一帧的话，标题会在一秒后消失，观众来不及读。
    leading = 0
    for scene in scenes:
        if scene.kind != "cover" or scene.brand == "outro":
            break
        leading += 1
    for scene in scenes[:leading]:
        if headline:
            scene.headline = headline
            # 有大字标题时，下面那行小字换成**论文原题**（观众得知道这是哪篇）
            scene.caption = cover_caption or scene.caption

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
                brand="outro",  # 收尾一律用品牌卡，不要用普通配图页结尾
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


_LOGO_CACHE: dict[int, tuple[str, int, int]] = {}


def _logo_data_uri(width: int) -> tuple[str, int, int] | None:
    """读取内置的社区 logo，缩放到指定宽度后转 data URI。

    每帧都要内嵌一次，所以按宽度缓存 —— 否则每帧都要重新解码缩放同一张 PNG。
    logo 是**浅色**字标（为深色底设计），放在白底正文上必须垫一块深色底，
    否则白字会消失。片尾卡整张就是深色，可以直接用。
    """
    if width in _LOGO_CACHE:
        return _LOGO_CACHE[width]

    from ..branding import logo_path

    path = logo_path()
    if not path.exists():
        logger.warning("未找到社区 logo（%s），跳过水印", path)
        return None

    try:
        import pymupdf

        pix = pymupdf.Pixmap(str(path))
        if width != pix.width:
            target_h = max(int(pix.height * width / pix.width), 1)
            pix = pymupdf.Pixmap(pix, width, target_h)
        uri = "data:image/png;base64," + base64.b64encode(pix.tobytes("png")).decode("ascii")
    except Exception as exc:  # noqa: BLE001
        logger.warning("社区 logo 处理失败，跳过水印：%s", exc)
        return None

    _LOGO_CACHE[width] = (uri, pix.width, pix.height)
    return _LOGO_CACHE[width]


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


def _image_data_uri(path: Path, *, max_side: int = 1400) -> tuple[str, int, int]:
    """把图缩到 `max_side` 以内再 base64 内嵌，返回 data URI。

    封面要用 `preserveAspectRatio="slice"` 裁满整张卡（比例不受控），
    所以不能走 `_prepare_image`（那个按 contain 算尺寸）。模糊之后细节本来就没了，
    缩到 1400 以内既够用又不让 SVG 膨胀。

    返回 `(data URI, 原始宽, 原始高)` —— 宽高要给调用方算**它在卡片里被放大/缩小了多少**，
    模糊半径得按那个倍率走（见 `image_card_markup`）。
    """
    try:
        import pymupdf
    except ImportError as exc:  # pragma: no cover
        raise VideoError("缺少 pymupdf，无法处理配图") from exc
    try:
        pix = pymupdf.Pixmap(str(path))
    except Exception as exc:  # noqa: BLE001
        raise VideoError(f"配图无法读取：{path.name}（{exc}）") from exc
    scale = min(max_side / max(pix.width, pix.height), 1.0)
    if scale < 1.0:
        try:
            pix = pymupdf.Pixmap(pix, max(int(pix.width * scale), 1), max(int(pix.height * scale), 1))
        except Exception as exc:  # noqa: BLE001
            logger.warning("封面底图缩放失败，使用原尺寸：%s", exc)
    return (
        "data:image/png;base64," + base64.b64encode(pix.tobytes("png")).decode("ascii"),
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


def _fit_subtitle(
    text: str,
    *,
    max_lines: int = 6,
    max_size: float = 30.0,
    layout: Layout = PORTRAIT,
) -> tuple[float, list[str]]:
    """选一个既能放下、又不至于太小的字号。

    竖版画幅只有 896px 宽，比横版窄很多，所以必须**同时**检查行数和总高度：
    光看行数会在字号偏大时让文字溢出到画面外。
    """
    from .illustration import _char_width, _tokenize

    text = " ".join((text or "").split())
    if not text:
        return 24.0, []

    def wrap_at(size: float) -> list[str]:
        units = layout.subtitle_width / size
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

    for size in (max_size - 2 * step for step in range(6)):
        lines = wrap_at(size)
        if len(lines) <= max_lines and len(lines) * size * 1.36 <= layout.subtitle_max_height:
            return size, lines

    # 还是放不下：用最小字号，超出部分截断加省略号
    size = min(20.0, max_size)
    lines = wrap_at(size)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1][:-1] + "…"
    return size, lines


def render_slide(
    scene: Scene,
    output_path: Path,
    *,
    include_subtitle: bool = True,
    include_point: bool = True,
    layout: Layout = PORTRAIT,
) -> Path:
    """渲染一帧画面：配图 + 图注 + 字幕（白底竖版，与论文首页同尺寸）。

    `include_subtitle=False` 时只画字幕区的底色和分隔线，不画文字 ——
    这一段的字幕会拆成几句、由 `render_caption_band` 单独渲染、再按时间叠上去
    （见 `CaptionBand`）。底图里若还留着一整段文字，第一句出现之前就会露馅。

    这个函数现在等于「骨架 + 图片卡」两层叠在一起（见下面的
    `render_chrome` / `render_image_card`）。保留它是为了两件事：
    现有的测试与「一页到底」的简单用法，以及需要单张整页图时不必自己拼。
    """
    return _render_slide_layers(
        scene,
        output_path,
        include_subtitle=include_subtitle,
        include_point=include_point,
        include_image=True,
        layout=layout,
    )


def render_chrome(
    scene: Scene,
    output_path: Path,
    *,
    include_subtitle: bool = True,
    include_point: bool = True,
    layout: Layout = PORTRAIT,
) -> Path:
    """只渲染**骨架**：白底 + logo + 图注 + 强调行 + 字幕带（图片区留白）。

    为什么要拆出骨架：转场要的是「图片在变、版面不动」。实测那条 40 秒的论文宣传片
    就是这种观感 —— 白底、每 9~10 秒换一次画面、换的时候整页快速淡过去。
    只有把图片单独成层，才能做到「图片溶解、标题/图注/字幕/进度条一动不动」。
    """
    return _render_slide_layers(
        scene,
        output_path,
        include_subtitle=include_subtitle,
        include_point=include_point,
        include_image=False,
        layout=layout,
    )


def cover_panel_width(layout: Layout) -> float:
    """玻璃面板的宽度：卡片宽减去两侧留白，再压到上限（横版卡片很宽，不能一整条铺满）。"""
    return min(layout.image_box_w - COVER_PAD * 2, COVER_PANEL_MAX_W)


def cover_title_layout(
    headline: str, caption: str, layout: Layout
) -> tuple[float, list[str], list[str], float, float]:
    """封面文字怎么排：返回 `(标题字号, 标题行, 原题行, 标题块高, 面板高)`。

    字号从 `COVER_HEADLINE_MAX_FONT` 往下试，直到标题能塞进两行 ——
    「爆款标题」是我们让模型写的，长一两个字很正常，缩字号比截断句子体面。
    再小到 `COVER_HEADLINE_MIN_FONT` 还放不下就截断（加省略号）。
    """
    text = " ".join((headline or "").split())
    inner = cover_panel_width(layout) - COVER_PANEL_PAD * 2
    if not text:
        return 0.0, [], [], 0.0, 0.0

    size = float(COVER_HEADLINE_MIN_FONT)
    lines: list[str] = []
    for candidate in range(COVER_HEADLINE_MAX_FONT, COVER_HEADLINE_MIN_FONT - 1, -2):
        # `_wrap` 的单位宽度就是字号（一个全角字 ≈ 1 个字号宽），所以像素宽 ÷ 字号 = 单位数
        wrapped = _wrap(text, inner / candidate)[:COVER_HEADLINE_MAX_LINES + 1]
        if len(wrapped) <= COVER_HEADLINE_MAX_LINES:
            size, lines = float(candidate), wrapped
            break
    else:
        size = float(COVER_HEADLINE_MIN_FONT)
        lines = _wrap(text, inner / size)[:COVER_HEADLINE_MAX_LINES]
        if lines:
            lines[-1] = lines[-1].rstrip("，。、,.;") + "…"

    block_h = len(lines) * size * COVER_HEADLINE_LEADING
    sub_lines = _wrap(caption, inner / COVER_SUBTITLE_FONT)[:COVER_SUBTITLE_MAX_LINES]
    sub_h = len(sub_lines) * COVER_SUBTITLE_FONT * 1.4
    panel_h = (
        COVER_PANEL_PAD * 2
        + block_h
        + COVER_ACCENT_GAP
        + COVER_ACCENT_H
        + (COVER_ACCENT_GAP + sub_h if sub_lines else 0)
    )
    return size, lines, sub_lines, block_h, panel_h


def cover_headline_layout(
    headline: str, layout: Layout, *, max_lines: int = COVER_HEADLINE_MAX_LINES
) -> tuple[float, list[str], float]:
    """只要标题那部分（`(字号, 行, 文字块高)`）—— 给测试和别处单独用。"""
    size, lines, _, block_h, _ = cover_title_layout(headline, "", layout)
    return size, lines[:max_lines], block_h


def image_card_markup(scene: Scene, layout: Layout = PORTRAIT) -> str:
    """图片卡那一层的 SVG 内容，**坐标以图片区左上角为原点**。

    为什么要单独抽出来：卡片这一层现在有两个消费者 ——
    分层的编码路径（`render_image_card`，图片单独一层、参与转场）和
    「一页到底」的合成路径（`render_slide` 把它整段贴进骨架里）。
    两处如果各画一份，封面的大字标题就只会在其中一条路径上出现，
    而这种不一致平时看不出来（谁也不会同时跑两条路径对比）。

    ## 两种卡
    - **普通页**：图片 + 贴着它的那圈浅灰细边框（白底上用来界定图片边界）；
    - **封面页**（`scene.headline` 非空）：论文首页整幅铺满 → 高斯模糊 → 白纱 →
      一块玻璃面板，面板里是爆款标题（大字）+ 品牌绿短杠 + 论文原题（小字）。
      模糊的半径按卡片尺寸缩放（横版卡片更大，同一个 stdDeviation 看起来会更清楚）。
    """
    card_w, card_h = layout.image_box_w, layout.image_box_h
    clip_id = "coverclip"

    size, lines, sub_lines, block_h, panel_h = cover_title_layout(
        scene.headline, scene.caption, layout
    )
    if not lines:
        data_uri, img_w, img_h = _prepare_image(scene.image, card_w, card_h)
        img_x = (card_w - img_w) / 2
        img_y = (card_h - img_h) / 2
        return "\n".join(
            [
                f'<rect x="{img_x - 2:.1f}" y="{img_y - 2:.1f}" width="{img_w + 4}" '
                f'height="{img_h + 4}" rx="8" fill="none" stroke="{FRAME_STROKE}" stroke-width="1.5"/>',
                f'<image x="{img_x:.1f}" y="{img_y:.1f}" width="{img_w}" height="{img_h}" href="{data_uri}"/>',
            ]
        )

    # 底图用**裁满**（slice）而不是等比放下：模糊层要铺满整张卡，留白边就露馅了
    data_uri, page_w, page_h = _image_data_uri(scene.image)
    # 模糊半径要跟着**首页在卡片里被放大了多少**走：竖版是把整页缩到 ~0.96 倍，
    # 横版卡片又宽又矮，裁满时首页被放大到近 2 倍 —— 同一个半径在横版上会明显更「清楚」。
    # 实测（同一篇论文）：竖版半径 6 的纹理跨度和横版半径 4.5 对不上，按倍率走才一致。
    page_scale = max(card_w / page_w, card_h / page_h)
    blur = COVER_GLASS_BLUR * (page_scale / COVER_GLASS_BLUR_REF_SCALE)
    panel_w = cover_panel_width(layout)
    panel_x = (card_w - panel_w) / 2
    panel_y = max((card_h - panel_h) / 2, COVER_PAD)

    parts: list[str] = [
        "<defs>",
        f'<clipPath id="{clip_id}"><rect width="{card_w}" height="{card_h}" rx="8"/></clipPath>',
        f'<filter id="coverblur" x="-5%" y="-5%" width="110%" height="110%">'
        f'<feGaussianBlur stdDeviation="{blur:.1f}"/></filter>',
        "</defs>",
        f'<g clip-path="url(#{clip_id})">',
        f'<image x="0" y="0" width="{card_w}" height="{card_h}" preserveAspectRatio="xMidY{COVER_GLASS_ANCHOR} slice" '
        f'transform="translate({card_w / 2:.1f},{card_h * COVER_GLASS_ZOOM_CY:.1f}) '
        f'scale({COVER_GLASS_ZOOM}) translate({-card_w / 2:.1f},{-card_h * COVER_GLASS_ZOOM_CY:.1f})" '
        f'filter="url(#coverblur)" href="{data_uri}"/>',
        # 白纱：把「纸上的字」压成纹理（读不出字，但看得出是论文）
        f'<rect width="{card_w}" height="{card_h}" fill="{COVER_GLASS_TINT}" opacity="{COVER_GLASS_VEIL}"/>',
        # 玻璃面板：比纱再实一点的一层 + 一道亮边，这就是「毛玻璃」的观感
        f'<rect x="{panel_x:.1f}" y="{panel_y:.1f}" width="{panel_w:.1f}" height="{panel_h:.1f}" '
        f'rx="{COVER_PANEL_RADIUS}" fill="#ffffff" opacity="{COVER_PANEL_FILL}" '
        f'stroke="#ffffff" stroke-width="2"/>',
        "</g>",
    ]

    center = card_w / 2
    y = panel_y + COVER_PANEL_PAD + size * COVER_HEADLINE_BASELINE
    for line in lines:
        parts.append(
            f'<text x="{center:.1f}" y="{y:.1f}" text-anchor="middle" font-family="{FONT_STACK}" '
            f'font-size="{size:.0f}" font-weight="700" fill="{COVER_TITLE_COLOR}">{html.escape(line)}</text>'
        )
        y += size * COVER_HEADLINE_LEADING
    bar_y = panel_y + COVER_PANEL_PAD + block_h + COVER_ACCENT_GAP
    parts.append(
        f'<rect x="{center - COVER_ACCENT_W / 2:.1f}" y="{bar_y:.1f}" width="{COVER_ACCENT_W}" '
        f'height="{COVER_ACCENT_H}" rx="{COVER_ACCENT_H / 2:.1f}" fill="{BRAND_GREEN}"/>'
    )
    if sub_lines:
        y = bar_y + COVER_ACCENT_H + COVER_ACCENT_GAP + COVER_SUBTITLE_FONT
        for line in sub_lines:
            parts.append(
                f'<text x="{center:.1f}" y="{y:.1f}" text-anchor="middle" font-family="{FONT_STACK}" '
                f'font-size="{COVER_SUBTITLE_FONT}" fill="{COVER_SUBTITLE_COLOR}">{html.escape(line)}</text>'
            )
            y += COVER_SUBTITLE_FONT * 1.4
    return "\n".join(parts)


def render_image_card(
    scene: Scene, output_path: Path, *, layout: Layout = PORTRAIT
) -> Path:
    """只渲染**图片卡**：图片 + 贴着图片的那圈浅灰细边框，其余透明。

    画幅是图片区（`layout.image_box_w × layout.image_box_h`），叠在骨架的 `(20, layout.image_top)`。
    边框跟着图片走（它是贴着图片量的 2px 内缩），所以它属于这一层 ——
    转场时「带框的图」整体淡入淡出，而不是框留在原地、图在里面换。

    **封面帧（`scene.headline` 非空）走另一套排版**：上面是爆款标题（大字 + 品牌绿短杠），
    下面才是论文首页的图。放在这一层而不是骨架层，是因为这一层已经被转场当成
    「会变的那一块」在用了 —— 标题跟着封面一起淡入、一起溶解掉，不需要动滤镜图里
    任何一处坐标（骨架与图片窗口全程不变）。
    """
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{layout.image_box_w}" height="{layout.image_box_h}" '
        f'viewBox="0 0 {layout.image_box_w} {layout.image_box_h}">',
        image_card_markup(scene, layout),
        "</svg>",
    ]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _rasterize_custom(
        "\n".join(parts), output_path, width=layout.image_box_w, height=layout.image_box_h
    )
    return output_path


def _render_slide_layers(
    scene: Scene,
    output_path: Path,
    *,
    include_subtitle: bool,
    include_point: bool,
    include_image: bool,
    layout: Layout = PORTRAIT,
) -> Path:
    """骨架与整页共用的渲染实现（`include_image` 决定图片层要不要画进来）。"""
    font_size, lines = _fit_subtitle(scene.text)

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{layout.width}" height="{layout.height}" '
        f'viewBox="0 0 {layout.width} {layout.height}">',
        f'<rect width="{layout.width}" height="{layout.height}" fill="{BG_COLOR}"/>',
    ]

    # ⚠️ 这里**没有**顶部标题条：论文标题不再挂在每一帧顶上。
    # 挂在顶上的那行小字有两个问题：一是 22px 的论文全名在一屏里根本读不完，
    # 二是「每帧都有的东西」等于没有信息量。标题现在只出现在封面（见 render_image_card），
    # 而且换成了抓人的爆款标题 + 下面一行的论文原题。

    # 右上角社区 logo 水印。
    # logo 是浅色字标，白底上直接放会看不见，所以垫一块深色圆角底。
    logo = _logo_data_uri(WATERMARK_W)
    if logo:
        uri, lw, lh = logo
        chip_w, chip_h = lw + WATERMARK_PAD * 2, lh + WATERMARK_PAD * 2
        chip_x = layout.width - 40 - chip_w
        chip_y = 18
        parts.append(
            f'<rect x="{chip_x}" y="{chip_y}" width="{chip_w}" height="{chip_h}" '
            f'rx="8" fill="{BRAND_DARK}" opacity="0.9"/>'
        )
        parts.append(
            f'<image x="{chip_x + WATERMARK_PAD}" y="{chip_y + WATERMARK_PAD}" '
            f'width="{lw}" height="{lh}" href="{uri}"/>'
        )

    # 配图（浅灰细边框，白底上用来界定图片边界）；骨架模式下这块留白，由图片卡叠上来。
    # 用的是和分层路径**同一段** markup（`image_card_markup`），所以封面的大字标题
    # 在一页到底的用法里也在 —— 两条路径不会画成两个样子。
    if include_image:
        parts.append(
            f'<g transform="translate({layout.image_box_left}, {layout.image_top})">'
            f"{image_card_markup(scene, layout)}</g>"
        )

    # 图注
    if scene.caption:
        caption_lines = _wrap(scene.caption, layout.subtitle_width / 18)[:2]
        for offset, line in enumerate(caption_lines):
            parts.append(
                f'<text x="{layout.subtitle_left}" y="{layout.caption_top + 22 + offset * 23}" '
                f'font-family="{FONT_STACK}" font-size="18" fill="{CAPTION_COLOR}">'
                f"{html.escape(line)}</text>"
            )

    # 字幕区：极浅底 + 一条分隔线，正文用深色。
    # 这里**不显示「主播A / 主播B」标签** —— 谁在说话听声音就知道，
    # 画面上多一行标签只会分散注意力、也占掉字幕的空间。
    parts.append(
        f'<rect x="0" y="{layout.subtitle_top}" width="{layout.width}" '
        f'height="{layout.height - layout.subtitle_top}" fill="{SUBTITLE_BG}"/>'
    )
    parts.append(
        f'<rect x="0" y="{layout.subtitle_top}" width="{layout.width}" height="2" fill="{SUBTITLE_RULE}"/>'
    )

    if include_point and scene.point:
        # 强调行：浅蓝底 + 左侧色条 + 大字。它是画面上最抢眼的一行，
        # 也是「突出解读内容」的落点（不是装饰，是每一段的核心结论）。
        parts.append(
            f'<rect x="{layout.point_left}" y="{layout.point_top}" width="{layout.point_width}" '
            f'height="{layout.point_height}" rx="10" fill="{POINT_BG}"/>'
        )
        parts.append(
            f'<rect x="{layout.point_left}" y="{layout.point_top}" width="{POINT_BAR_W}" '
            f'height="{layout.point_height}" rx="3" fill="{POINT_BAR}"/>'
        )
        point_size, point_lines = _fit_subtitle(
            scene.point, max_lines=1, max_size=layout.point_max_font
        )
        if point_lines:
            baseline = layout.point_top + layout.point_height / 2 + point_size * 0.36
            parts.append(
                f'<text x="{layout.point_left + POINT_BAR_W + 18}" y="{baseline:.0f}" '
                f'font-family="{FONT_STACK}" font-size="{point_size:.0f}" '
                f'font-weight="600" fill="{POINT_TEXT}">{html.escape(point_lines[0])}</text>'
            )

    if include_subtitle:
        line_height = font_size * 1.36
        for offset, line in enumerate(lines):
            parts.append(
                f'<text x="{layout.subtitle_left}" y="{layout.subtitle_text_top + offset * line_height:.0f}" '
                f'font-family="{FONT_STACK}" font-size="{font_size:.0f}" '
                f'fill="{SUBTITLE_TEXT}">{html.escape(line)}</text>'
            )

    parts.append("</svg>")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    _rasterize_custom(
        "\n".join(parts),
        output_path,
        width=layout.width,
        height=layout.height,
    )
    return output_path


def _number_runs(line: str) -> list[tuple[str, bool]]:
    """把一行字幕切成 [普通文字, 数字, 普通文字, ...]，数字段要单独上色。

    只认阿拉伯数字：「第一」「两倍」这种中文数字太高频，标出来满屏都是重点。
    """
    runs: list[tuple[str, bool]] = []
    cursor = 0
    for match in _NUMBER_TOKEN.finditer(line):
        if match.start() > cursor:
            runs.append((line[cursor : match.start()], False))
        runs.append((match.group(0), True))
        cursor = match.end()
    if cursor < len(line):
        runs.append((line[cursor:], False))
    return runs


def _caption_line_parts(
    line: str, *, baseline: float, font_size: float, layout: Layout = PORTRAIT
) -> list[str]:
    """渲染字幕的一行：数字用强调色 + 浅色底标出来，其余照常。

    为什么值得单独做：一期论文解读里真正有信息量的往往就是那个「67%」，
    而它夹在一整行灰白文字里最容易被读漏。强调它不需要任何模型输出，也不会看错。
    """
    from .illustration import _char_width

    runs = _number_runs(line)
    if not any(is_number for _, is_number in runs):
        return [
            f'<text x="{layout.subtitle_left}" y="{baseline:.0f}" '
            f'font-family="{FONT_STACK}" font-size="{font_size:.0f}" '
            f'fill="{SUBTITLE_TEXT}">{html.escape(line)}</text>'
        ]

    parts: list[str] = []
    cursor = layout.subtitle_left
    for text_run, is_number in runs:
        width = sum(_char_width(char) for char in text_run) * font_size
        if is_number:
            parts.append(
                f'<rect x="{cursor - 3:.1f}" y="{baseline - font_size * 0.92:.1f}" '
                f'width="{width + 6:.1f}" height="{font_size * 1.22:.1f}" rx="4" '
                f'fill="{POINT_BG}"/>'
            )
        parts.append(
            f'<text x="{cursor:.1f}" y="{baseline:.0f}" '
            f'font-family="{FONT_STACK}" font-size="{font_size:.0f}" '
            f'fill="{POINT_TEXT if is_number else SUBTITLE_TEXT}">{html.escape(text_run)}</text>'
        )
        cursor += width
    return parts


def render_point_row(
    point: str, output_path: Path, *, layout: Layout = PORTRAIT
) -> Path:
    """只渲染「本段要点」那一行（画幅 936×layout.point_height，叠在 y=layout.point_top）。

    单独成层是为了让它**滑入**：一段新的内容开始时，这行从左边滑进来 0.4 秒。
    这是有意保留的动效 —— 它标记「内容换了一段」，跟画面在讲什么直接相关，
    与那种「整张图慢慢放大」的无意义运动不是一回事。
    """
    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{layout.width}" height="{layout.point_height}" '
        f'viewBox="0 0 {layout.width} {layout.point_height}">',
        f'<rect x="{layout.point_left}" y="0" width="{layout.point_width}" height="{layout.point_height}" '
        f'rx="10" fill="{POINT_BG}"/>',
        f'<rect x="{layout.point_left}" y="0" width="{POINT_BAR_W}" height="{layout.point_height}" '
        f'rx="3" fill="{POINT_BAR}"/>',
    ]
    size, lines = _fit_subtitle(point, max_lines=1, max_size=layout.point_max_font)
    if lines:
        baseline = layout.point_height / 2 + size * 0.36
        parts.append(
            f'<text x="{layout.point_left + POINT_BAR_W + 18}" y="{baseline:.0f}" '
            f'font-family="{FONT_STACK}" font-size="{size:.0f}" '
            f'font-weight="600" fill="{POINT_TEXT}">{html.escape(lines[0])}</text>'
        )
    parts.append("</svg>")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _rasterize_custom(
        "\n".join(parts),
        output_path,
        width=layout.width,
        height=layout.point_height,
    )
    return output_path


def normalize_focus(raw: Any) -> dict[str, Any] | None:
    """整理模型给的「这一段在讲图的哪一块」。

    坐标是**相对图片的 0~1 比例**（不是像素）：图片在画面里是等比缩放居中的，
    用比例才能跟 `_prepare_image` 的结果对齐。认不出的、太小的、越界的一律丢掉 ——
    框错地方比不框更糟（观众会以为那一块真的在讲那个）。
    """
    if not isinstance(raw, dict):
        return None
    try:
        x = float(raw.get("x"))
        y = float(raw.get("y"))
        w = float(raw.get("w"))
        h = float(raw.get("h"))
    except (TypeError, ValueError):
        return None
    if not all(map(math.isfinite, (x, y, w, h))):
        return None
    # 契约是 0~1 的**比例**。给了像素（比如 600）说明模型没按格式来 ——
    # 归一化里没法反推像素对应的图有多大，宁可这一帧不框，也不要框到角落里去。
    if any(value < -1e-6 or value > 1.0 + 1e-6 for value in (x, y, w, h)):
        return None
    x, y = max(0.0, min(x, 0.98)), max(0.0, min(y, 0.98))
    w, h = min(w, 1.0 - x), min(h, 1.0 - y)
    if w < FOCUS_MIN_SIDE or h < FOCUS_MIN_SIDE:
        return None
    if w * h < FOCUS_MIN_AREA:
        return None
    if w * h > FOCUS_MAX_AREA:
        # 覆盖 85% 以上：等于没框（见上面 FOCUS_MAX_AREA 的说明）
        return None
    label = str(raw.get("label") or "").strip().replace("\n", " ")
    return {
        "x": round(x, 4),
        "y": round(y, 4),
        "w": round(w, 4),
        "h": round(h, 4),
        "label": label[:FOCUS_LABEL_CHARS],
    }


@dataclass(frozen=True)
class FigurePanels:
    """一张论文原图切出来的子图，以及每个子图「讲什么」。

    字母来自**图注**（`(a) … (b) …`），框来自**像素**（见 `panels.detect_panels`）。
    两者必须对得上号（个数相同）才会被用：对不上时字母和块序就可能错位，
    那还不如不框 —— 框错子图比不框更糟。
    """

    letters: list[str]
    bodies: list[str]
    panels: list[Panel]

    def options(self) -> list[tuple[str, str]]:
        return list(zip(self.letters, self.bodies))

    def focus_for(self, letter: str) -> dict[str, Any] | None:
        """字母 → 聚光灯用的框。认不出的字母返回 None（不框）。

        过一遍 `normalize_focus`：聚光灯那几个下限（框太小、覆盖整张图）在这里也要适用，
        否则会出现「存了一个下游必然丢掉的框」，看起来像是聚光灯莫名其妙没生效。
        """
        key = (letter or "").strip().lower()
        if key not in self.letters:
            return None
        index = self.letters.index(key)
        body = self.bodies[index] if index < len(self.bodies) else ""
        return normalize_focus(self.panels[index].to_focus(_panel_label(body)))


def _panel_label(body: str, *, limit: int = FOCUS_LABEL_CHARS) -> str:
    """子图说明里取一小段当框上的标签（框上的小药丸只有十来个字的位置）。

    英文按**词**截：`normalize_focus` 只按字符数砍，砍在词中间会得到
    「Recurrent state 」这种带半截词的标签 —— 实测第一次就出现了。
    判据是「**有没有中日韩字**」而不是 `isascii()`：英文图注里常有
    `6.93×` / `–` 这类非 ASCII 符号，用 isascii 判会把它们当成中文按字符砍，
    实测得到「Mean accuracy ac」。
    """
    clean = " ".join((body or "").split())
    if not clean:
        return ""
    if not any("\u3400" <= char <= "\u9fff" for char in clean):
        picked: list[str] = []
        for word in clean.split():
            if picked and len(" ".join([*picked, word])) > limit:
                break
            picked.append(word)
            if len(picked) >= 4:
                break
        return " ".join(picked)[:limit]
    return clean[:limit]


def build_panel_catalog(
    figures: list[dict[str, Any]] | None, pool: dict[str, "ImageAsset"]
) -> dict[str, FigurePanels]:
    """给每张**有多子图**的论文原图切出子图，返回 `图片 id → FigurePanels`。

    只在图注里写着 `(a) (b) (c)`（`panel_count_from_caption`）时才做这件事 ——
    那是「这张图有几个子图」的唯一可靠来源，而子图个数又决定几何怎么切。
    **切出来的块数必须和图注说的完全一致**，否则整张图放弃（宁可没有聚光灯）。
    """
    catalog: dict[str, FigurePanels] = {}
    for figure in figures or []:
        if not isinstance(figure, dict):
            continue
        asset_id = str(figure.get("id") or "")
        asset = pool.get(asset_id)
        if asset is None or not asset.exists:
            continue
        caption = str(figure.get("caption") or "")
        count = panel_count_from_caption(caption)
        if not count:
            continue
        labels = panel_labels(caption, cap=count)
        if len(labels) != count:
            continue
        marks = [
            (float(mark["x"]), float(mark["y"]))
            for mark in (figure.get("panel_marks") or [])
            if isinstance(mark, dict) and "x" in mark and "y" in mark
        ]
        panels = detect_panels(asset.path, expected=count, marks=marks or None)
        if len(panels) != count:
            logger.info(
                "配图 %s 的图注说有 %d 个子图，但只切出 %d 块 —— 不做聚光灯",
                figure.get("label") or asset_id,
                count,
                len(panels),
            )
            continue
        catalog[asset_id] = FigurePanels(
            letters=[letter for letter, _ in labels],
            bodies=[body for _, body in labels],
            panels=panels,
        )
        logger.info(
            "配图 %s 切成 %d 个子图，可用于聚光灯", figure.get("label") or asset_id, count
        )
    return catalog


def render_focus_overlay(
    scene: Scene,
    output_path: Path,
    *,
    focus: dict[str, Any],
    layout: Layout = PORTRAIT,
) -> Path:
    """渲染「聚光灯」图层：图卡大小，除了要讲的那一块之外全部压上半透明白。

    图层与图片卡**同一个画幅、同一个坐标系**（图片在卡里是等比居中放好的），
    所以这里只要按比例算出那块区域的像素位置即可 —— 不需要回头去动 PDF。
    """
    data_uri, img_w, img_h = _prepare_image(scene.image, layout.image_box_w, layout.image_box_h)
    img_x = (layout.width - img_w) / 2 - layout.image_box_left
    img_y = (layout.image_box_h - img_h) / 2

    fx = img_x + float(focus["x"]) * img_w
    fy = img_y + float(focus["y"]) * img_h
    fw = float(focus["w"]) * img_w
    fh = float(focus["h"]) * img_h
    pad = FOCUS_BORDER_W + 2

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{layout.image_box_w}" '
        f'height="{layout.image_box_h}" viewBox="0 0 {layout.image_box_w} {layout.image_box_h}">',
        # 四条压暗边（围绕那块「洞」），比用 mask 简单，也不依赖渲染器对 mask 的支持
        f'<g fill="{FOCUS_DIM}" fill-opacity="{FOCUS_DIM_ALPHA}">',
        f'<rect x="0" y="0" width="{layout.image_box_w}" height="{max(fy - pad, 0):.1f}"/>',
        f'<rect x="0" y="{fy + fh + pad:.1f}" width="{layout.image_box_w}" '
        f'height="{max(layout.image_box_h - fy - fh - pad, 0):.1f}"/>',
        f'<rect x="0" y="{max(fy - pad, 0):.1f}" width="{max(fx - pad, 0):.1f}" '
        f'height="{fh + pad * 2:.1f}"/>',
        f'<rect x="{fx + fw + pad:.1f}" y="{max(fy - pad, 0):.1f}" '
        f'width="{max(layout.image_box_w - fx - fw - pad, 0):.1f}" height="{fh + pad * 2:.1f}"/>',
        "</g>",
        # 框线：把「正在讲的是这里」说清楚
        f'<rect x="{fx:.1f}" y="{fy:.1f}" width="{fw:.1f}" height="{fh:.1f}" rx="6" '
        f'fill="none" stroke="{FOCUS_BORDER}" stroke-width="{FOCUS_BORDER_W}"/>',
    ]
    label = str(focus.get("label") or "").strip()
    if label:
        size = 26.0 if not layout.landscape else 30.0
        chip_h = size * 1.9
        chip_w = (len(label) + 2) * size * 0.62
        chip_x = min(max(fx, 8), layout.image_box_w - chip_w - 8)
        chip_y = max(fy - chip_h - 6, 6)
        chip_y = chip_y if chip_y + chip_h < layout.image_box_h else min(fy + fh + 6, layout.image_box_h - chip_h - 6)
        parts.append(
            f'<rect x="{chip_x:.1f}" y="{chip_y:.1f}" width="{chip_w:.1f}" height="{chip_h:.1f}" '
            f'rx="8" fill="{FOCUS_BORDER}"/>'
        )
        parts.append(
            f'<text x="{chip_x + size * 0.5:.1f}" y="{chip_y + chip_h * 0.68:.1f}" '
            f'font-family="{FONT_STACK}" font-size="{size:.0f}" fill="#ffffff">{html.escape(label)}</text>'
        )
    parts.append("</svg>")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _rasterize_custom(
        "\n".join(parts), output_path, width=layout.image_box_w, height=layout.image_box_h
    )
    return output_path


def render_caption_band(
    text: str, output_path: Path, *, layout: Layout = PORTRAIT
) -> Path:
    """只渲染底部那条字幕带，用于「字幕逐句出现」。

    画幅是 `layout.width × (layout.height - layout.subtitle_top)`，位置固定叠在 `y=layout.subtitle_top`，
    所以它盖住的就是底图那一条空字幕区。

    字号比整页字幕放大到 38：观众真正在读的是这两行，而一句比一整段短得多，
    放得下更大的字（放不下会自动往小退，见 `_fit_subtitle`）。
    """
    band_h = layout.height - layout.subtitle_top
    font_size, lines = _fit_subtitle(text, max_lines=3, max_size=CAPTION_BAND_MAX_FONT)

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{layout.width}" height="{band_h}" '
        f'viewBox="0 0 {layout.width} {band_h}">',
        f'<rect width="{layout.width}" height="{band_h}" fill="{SUBTITLE_BG}"/>',
        f'<rect width="{layout.width}" height="2" fill="{SUBTITLE_RULE}"/>',
    ]

    if lines:
        line_height = font_size * 1.36
        block_h = line_height * len(lines)
        # 竖直居中：整页字幕是从固定基线往下排的，这里只有一两行，
        # 照搬那个基线会让文字顶在上沿、下面空一大片
        first_baseline = (band_h - block_h) / 2 + font_size * 0.95
        for offset, line in enumerate(lines):
            baseline = first_baseline + offset * line_height
            parts.extend(
                _caption_line_parts(line, baseline=baseline, font_size=font_size)
            )

    parts.append("</svg>")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _rasterize_custom(
        "\n".join(parts),
        output_path,
        width=layout.width,
        height=layout.height - layout.subtitle_top,
    )
    return output_path


def render_endcard(
    scene: Scene, output_path: Path, *, layout: Layout = PORTRAIT
) -> Path:
    """片尾品牌卡：深色底 + 社区 logo + 关注引导。

    ## 为什么片尾要单独做成深色

    logo 是**浅色**字标（社区自己的视频也用深色星空底），白底上会直接消失。
    而正文又必须是白底 —— 论文配图本身就是白底图表，深色画布会把它们衬得像贴图。

    所以两种底各归其位：**正文白底保证可读，片尾深色保证品牌正确**。
    顺带这也让「节目结束」有一个明确的视觉信号，而不是在正文风格里悄悄停掉。

    引导语除了画面上出大字，语音里也会说一遍（见 branding.BRAND_OUTRO）——
    听众往往是听到结尾才决定要不要关注，这时画面和声音必须同时给到指引。
    """
    from ..branding import BRAND_CTA_SUBTITLE, BRAND_CTA_TITLE

    font_size, lines = _fit_subtitle(scene.text, max_lines=4)
    logo = _logo_data_uri(460)

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{layout.width}" height="{layout.height}" '
        f'viewBox="0 0 {layout.width} {layout.height}">',
        f'<rect width="{layout.width}" height="{layout.height}" fill="{BRAND_DARK}"/>',
    ]

    # 片尾也不放论文标题：这张卡是品牌卡，画面上的主角是关注引导，
    # 左上角再挂一行论文全名只会跟引导语抢注意力（正文页的标题条也一并去掉了）。

    if logo:
        uri, lw, lh = logo
        lx = (layout.width - lw) / 2
        ly = 392
        parts.append(f'<image x="{lx:.1f}" y="{ly}" width="{lw}" height="{lh}" href="{uri}"/>')
        divider_y = ly + lh + 60
    else:
        # 没有 logo 也不能空着，直接用社区名顶上
        parts.append(
            f'<text x="{layout.width / 2:.0f}" y="470" text-anchor="middle" '
            f'font-family="{FONT_STACK}" font-size="56" fill="{BRAND_TEXT}" '
            f'font-weight="600">青稞社区</text>'
        )
        divider_y = 530

    # 青稞绿的细分隔线
    parts.append(
        f'<rect x="{layout.width / 2 - 110:.0f}" y="{divider_y}" width="220" height="2" '
        f'fill="{BRAND_GREEN}" opacity="0.85"/>'
    )

    # 关注引导：主句用麦金强调，副句用青稞绿
    parts.append(
        f'<text x="{layout.width / 2:.0f}" y="{divider_y + 88}" text-anchor="middle" '
        f'font-family="{FONT_STACK}" font-size="44" font-weight="600" '
        f'fill="{BRAND_GOLD}">{html.escape(BRAND_CTA_TITLE)}</text>'
    )
    parts.append(
        f'<text x="{layout.width / 2:.0f}" y="{divider_y + 140}" text-anchor="middle" '
        f'font-family="{FONT_STACK}" font-size="24" '
        f'fill="{BRAND_GREEN}">{html.escape(BRAND_CTA_SUBTITLE)}</text>'
    )

    # 字幕区：深色面板 + 浅色字（与其他页的浅底深字相反）
    parts.append(
        f'<rect x="0" y="{layout.subtitle_top}" width="{layout.width}" '
        f'height="{layout.height - layout.subtitle_top}" fill="{BRAND_DARK_PANEL}"/>'
    )
    parts.append(
        f'<rect x="0" y="{layout.subtitle_top}" width="{layout.width}" height="2" '
        f'fill="{BRAND_GREEN}" opacity="0.5"/>'
    )
    line_height = font_size * 1.36
    for offset, line in enumerate(lines):
        parts.append(
            f'<text x="{layout.subtitle_left}" y="{layout.subtitle_text_top + offset * line_height:.0f}" '
            f'font-family="{FONT_STACK}" font-size="{font_size:.0f}" '
            f'fill="{BRAND_TEXT}">{html.escape(line)}</text>'
        )

    parts.append("</svg>")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    _rasterize_custom(
        "\n".join(parts),
        output_path,
        width=layout.width,
        height=layout.height,
    )
    return output_path


def _rasterize_custom(
    svg_text: str,
    output_path: Path,
    *,
    width: int = VIDEO_W,
    height: int = VIDEO_H,
) -> None:
    """按给定尺寸渲染（illustration.rasterize_svg 会强制套用信息图的画布尺寸）。

    ⚠️ resvg 在这里的行为是**按宽度缩放、高度按比例推**，不是拉伸到给定尺寸：
    传 `width=936` 渲染一个 896×742 的 SVG，会得到 936×776（比例一样、但尺寸不对）。
    「整页/字幕带/强调行/进度条」都是 936 宽，正好蒙对；**图片卡是 896 宽，必须显式传尺寸**。
    """
    try:
        import resvg_py
    except ImportError as exc:  # pragma: no cover
        raise VideoError("缺少 resvg-py，无法渲染画面") from exc

    try:
        png = bytes(
            resvg_py.svg_to_bytes(
                svg_string=svg_text,
                width=width,
                height=height,
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


def asset_version(path: Any) -> str:
    """素材版本号：修改时间 + 大小。

    与路由层给 URL 加的 `?v=` 是同一套口径。这里存下来是为了能判断
    「视频生成之后配图有没有被改过」—— 改过就说明视频里还是旧画面，
    前端应当提示可以重新合成。
    """
    try:
        stat = Path(path).stat()
    except (OSError, TypeError):
        return "0"
    return f"{int(stat.st_mtime)}-{stat.st_size}"


def is_video_stale(stored_video: dict[str, Any] | None) -> bool:
    """视频是否已经跟不上素材了（配图被人工校正 / 重新提取过）。"""
    if not stored_video:
        return False
    recorded = stored_video.get("asset_versions") or {}
    assets = stored_video.get("assets") or {}
    if not recorded or not assets:
        return False
    for asset_id, version in recorded.items():
        path = assets.get(asset_id)
        if not path:
            continue
        if not Path(path).exists():
            return True
        if asset_version(path) != version:
            return True
    return False


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def beat_windows_aligned(
    beats: list[str],
    duration: float,
    *,
    start: float = 0.0,
    pauses: list[Pause] | None = None,
    fade: float = CAPTION_FADE_SEC,
) -> list[tuple[float, float]]:
    """字幕分句的时间窗：先按字数比例算，再**吸附到真实的说话停顿**。

    为什么要吸附：语速不均匀，按字数算出来的换句点经常落在句子中间，
    看起来就是「字幕换得莫名其妙」。音频里本来就有停顿，`silencedetect` 直接给出来
    （实测 250 秒的音频 0.14 秒跑完，82 个停顿）。

    `pauses` 为空（检测失败 / 没有停顿）时就是原来的比例切分 —— 这条路径必须永远可用。
    """
    windows = beat_windows(beats, duration, fade=fade)
    if not pauses or len(windows) < 2:
        return windows

    # 内部边界（不含首尾）的绝对时间，吸附后再还原成窗口
    boundaries = [start + end for _, end in windows[:-1]]
    aligned = align_boundaries(
        boundaries, pauses, start=start, end=start + duration
    )
    edges = [0.0, *[item - start for item in aligned], duration]
    return [(edges[i], edges[i + 1]) for i in range(len(edges) - 1)]


# --------------------------------------------------------------------------
# 动效：推镜 / 字幕分句 / 进度条
# --------------------------------------------------------------------------


def progress_bar_svg(*, layout: Layout = PORTRAIT) -> str:
    """进度条素材：一条**全宽**的色带。

    用法不是「把它截短」，而是整条叠上去、用 overlay 的逐帧 `x` 把它从左往右推进画面。

    为什么不用 `drawbox` 直接画：`drawbox` 的 `w` 只在初始化时求值一次，
    写 `w='iw*t/duration'` 实测每一帧都是满宽（`t` 在那个上下文里取不到值，被钳到了 iw）。
    overlay 的 `x`/`y` 是**逐帧**求值的，所以能拿到平滑增长的进度。
    这不是猜的：实测 2 秒进度条在 0.2s / 1.0s / 1.8s 处量到 96 / 468 / 842 像素
    （理论值 93 / 468 / 842）。
    """
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{layout.width}" height="{PROGRESS_BAR_H}">'
        f'<rect width="{layout.width}" height="{PROGRESS_BAR_H}" fill="{PROGRESS_BAR_COLOR}"/>'
        f"</svg>"
    )


def plan_transitions(
    scenes: list[Scene],
    image_ids: list[str] | None = None,
    *,
    style: str = TRANSITION_STYLE,
    seconds: float = TRANSITION_SEC,
    cards: list[Path | None] | None = None,
    slides: list[Path] | None = None,
) -> list[Transition]:
    """算出每一段开头该做什么转场。

    规则（都是「有理由才动」）：
    - 第一段 → `fade_in`（从白底淡入，比「啪」地出现自然）；
    - 与上一段**同一张图** → `none`：图没变就别动，无意义的溶解只会让人以为卡了一下；
    - 换图 → `dissolve`（`style="push"` 时改成横推）；
    - 片尾品牌卡 → `to_card`：上一整页淡出，露出底下的深色卡；
    - 片段太短（`seconds` 被压到 `TRANSITION_MIN_SEC` 以下）→ `none`。
    """
    ids = image_ids or [str(scene.image) for scene in scenes]
    plan: list[Transition] = []
    for index, scene in enumerate(scenes):
        if scene.brand == "outro":
            plan.append(
                Transition(
                    kind="to_card",
                    seconds=_clamp_transition(seconds, scene, minimum=0.0),
                    previous=(slides[index - 1] if slides and index else None),
                )
            )
            continue

        if index == 0:
            plan.append(
                Transition(kind="fade_in", seconds=_clamp_transition(FADE_IN_SEC, scene))
            )
            continue

        same_image = index < len(ids) and ids[index] == ids[index - 1]
        previous_card = cards[index - 1] if cards and index - 1 < len(cards) else None
        if same_image or style == "none" or previous_card is None:
            plan.append(Transition())
            continue

        allowed = _clamp_transition(seconds, scene)
        # 压到 0 就是「不做」：kind 也要跟着变成 none，否则日志和计划读起来自相矛盾
        plan.append(
            Transition(kind=style if allowed else "none", seconds=allowed, previous=previous_card)
        )
    return plan


def _clamp_transition(seconds: float, scene: Scene, *, minimum: float = TRANSITION_MIN_SEC) -> float:
    """过渡时长不能超过这一段的三分之一，太短就干脆不做。"""
    allowed = min(seconds, scene.duration * TRANSITION_MAX_RATIO)
    return allowed if allowed >= minimum else 0.0


def split_caption_beats(text: str, *, max_beats: int = CAPTION_BEAT_MAX) -> list[str]:
    """把一段字幕按句子切成最多 `max_beats` 句（字幕逐句出现）。

    不切的情况：太短（< `CAPTION_BEAT_MIN_CHARS`）就原样返回一句 ——
    二十来字再切成两半，每句剩几个字，闪来闪去比不切更难看。

    句末标点优先；没有标点（模型偶尔会写成一长串）就在中间最近的逗号处切；
    超过 `max_beats` 句时**按字数均衡合并**（不是丢弃后面的内容）。
    """
    clean = " ".join((text or "").split())
    if not clean:
        return []
    if len(clean) < CAPTION_BEAT_MIN_CHARS:
        return [clean]

    chunks: list[str] = []
    current = ""
    for char in clean:
        current += char
        if char in _SENTENCE_END:
            chunks.append(current.strip())
            current = ""
    if current.strip():
        chunks.append(current.strip())

    if len(chunks) <= 1:
        # 没有句末标点：优先在中点附近的逗号处切；连逗号都没有（模型偶尔会写成一长串）
        # 就按中点切一刀 —— 一整段糊上去比切得略生硬更糟。
        mid = len(clean) // 2
        candidates = [i for i, ch in enumerate(clean) if ch in "，,、"]
        if candidates:
            cut = min(candidates, key=lambda i: abs(i - mid))
        else:
            spaces = [i for i, ch in enumerate(clean) if ch == " "]
            cut = min(spaces, key=lambda i: abs(i - mid)) if spaces else mid
        left, right = clean[: cut + 1].strip(), clean[cut + 1 :].strip()
        if not left or not right:
            return [clean]
        return [left, right]

    if len(chunks) <= max_beats:
        return chunks

    # 合并到 max_beats 份：按总字数均分目标，逐句累加到接近目标就断一份
    total = sum(len(chunk) for chunk in chunks)
    target = total / max_beats
    merged: list[str] = []
    buffer = ""
    for chunk in chunks:
        buffer += chunk
        if len(buffer) >= target and len(merged) < max_beats - 1:
            merged.append(buffer.strip())
            buffer = ""
    if buffer.strip():
        merged.append(buffer.strip())
    return merged


def beat_windows(
    beats: list[str], duration: float, *, fade: float = CAPTION_FADE_SEC
) -> list[tuple[float, float]]:
    """按**字数占比**把这一段时长分给每句字幕，返回每句的 [起, 止)。

    为什么按字数：我们只有整段的音频时间轴（TTS 是按段合成的），没有逐句时间戳。
    按字数分是这里能做到的最好近似 —— 同一段里语速基本恒定。
    """
    if not beats:
        return []
    total = sum(len(beat) for beat in beats) or 1
    windows: list[tuple[float, float]] = []
    cursor = 0.0
    for beat in beats:
        span = duration * len(beat) / total
        windows.append((cursor, cursor + span))
        cursor += span
    return windows


def encode_video(
    scenes: list[Scene],
    slide_paths: list[Path],
    audio_path: Path,
    output_path: Path,
    *,
    target_duration: float | None = None,
    caption_bands: list[list[CaptionBand]] | None = None,
    point_rows: list[Path | None] | None = None,
    pauses: list[Pause] | None = None,
    image_cards: list[Path | None] | None = None,
    transitions: list[Transition] | None = None,
    focus_overlays: list[Path | None] | None = None,
    waveform: bool = True,
    layout: Layout = PORTRAIT,
) -> VideoResult:
    """把幻灯片序列和音频合成 MP4。

    ## 为什么要分两步，而不是一条 ffmpeg 命令搞定

    一条命令（concat 幻灯片 + 音频一起编码）**无法把视频长度对齐到音频**：

    | 写法 | 视频流时长 | 问题 |
    |---|---|---|
    | `-r 30 -shortest`（单步） | 209.23s | 比音频 206.86s 长 2.4 秒，结尾只有画面没声音 |
    | 不加 `-r`（单步） | 194.60s | 短 12 秒，结尾画面提前冻住 |
    | 输入端 `-r 30` | 0.80s | concat 会忽略每段 duration，直接崩掉 |

    拆成两步就干净了：先出纯视频轨（不关心它多长），再用 `-c:v copy` 只封装
    音频，此时 `-shortest` 能正确把总长裁到音频长度。实测视频 206.77s /
    音频 206.86s，误差 0.09 秒。

    注意：**不要**用 `-t` 去钳总长。实测 `-t 4.000` 会把 4 秒的片子砍成 2.03 秒。

    ## 动效怎么加进来的（同样不破坏上面那条长度对齐）

    现在是**每段先单独编成一个小片段**（`-frames:v` 精确控帧数），再 `-c copy`
    拼接，具体做法：

    - 每段的底图**静止**（不做推拉镜头：那种运动跟内容无关，观众看得出是凑的）；
    - 「本段要点」强调行单独成层，每段开头从左侧**滑入** 0.4 秒 —— 它标记内容换段；
    - 字幕分句后各做成一张图，用 `overlay` + `fade` 按时间叠上去；
    - 顶部进度条是一张全宽色带，用 overlay 的**逐帧 x 表达式**推进画面，
      段与段之间靠「全局起始时间」接续，所以拼起来是一条连续推进的进度。

    片段内部用 `-frames:v <帧数>` 边界，每段帧数是整数，所以拼接总长与原来
    按 `duration` 拼幻灯片时完全一致 —— 长度对齐这条不能动。
    """
    if not ffmpeg_available():
        raise VideoError("系统未安装 ffmpeg，无法合成视频")
    if len(scenes) != len(slide_paths):
        raise VideoError("画面数与幻灯片数不一致")
    if not slide_paths:
        raise VideoError("没有可用的画面")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    clips_dir = output_path.parent / f"{output_path.stem}-clips"
    list_path = output_path.parent / f"{output_path.stem}-clips.txt"
    silent_path = output_path.parent / f"{output_path.stem}-video-only.mp4"
    bar_path = clips_dir / "progress.png"
    clips_dir.mkdir(parents=True, exist_ok=True)
    _rasterize_custom(progress_bar_svg(layout=layout), bar_path, width=layout.width, height=PROGRESS_BAR_H)

    bands = caption_bands or [[] for _ in scenes]
    rows = point_rows or [None] * len(scenes)
    cards = image_cards or [None] * len(scenes)
    foci = focus_overlays or [None] * len(scenes)
    plan = transitions or [Transition() for _ in scenes]

    # 每段时长量化成整帧，避免 duration 落在帧边界之外被额外舍入
    frame_counts = [max(int(round(scene.duration * FPS)), 1) for scene in scenes]
    total_frames = sum(frame_counts)
    total_sec = total_frames / FPS

    def run(command: list[str], what: str) -> None:
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=1800)
        except subprocess.TimeoutExpired as exc:
            raise VideoError(f"{what}超时（超过 30 分钟）") from exc
        except OSError as exc:
            raise VideoError(f"无法调用 ffmpeg：{exc}") from exc
        if result.returncode != 0:
            raise VideoError(f"{what}失败：{(result.stderr or '')[-400:]}")

    transitions = plan or [Transition() for _ in scenes]
    clip_paths: list[Path] = []
    cursor_frames = 0
    try:
        for index, (scene, slide, frames) in enumerate(zip(scenes, slide_paths, frame_counts)):
            clip_path = clips_dir / f"clip-{index:04d}.mp4"
            start_sec = cursor_frames / FPS
            transition = transitions[index] if index < len(transitions) else Transition()

            # 输入序号是动态的（转场会多挂 1~2 路图），所以用计数器而不是写死 [0]/[1]/[2]
            inputs: list[str] = []
            counter = 0

            def add_image(path: Path) -> int:
                nonlocal counter
                inputs.extend(["-loop", "1", "-framerate", str(FPS), "-i", str(path)])
                position = counter
                counter += 1
                return position

            def add_source(expression: str) -> int:
                """挂一路 lavfi 源（比如纯色），返回输入序号。"""
                nonlocal counter
                inputs.extend(["-f", "lavfi", "-i", expression])
                position = counter
                counter += 1
                return position

            def add_audio(path: Path, start: float, duration: float) -> int:
                """把音频裁到这一段的区间挂进来（声波条只画本段的波形）。"""
                nonlocal counter
                inputs.extend(
                    ["-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(path)]
                )
                position = counter
                counter += 1
                return position

            chrome_in = add_image(slide)
            bar_in = add_image(bar_path)
            graph = [f"[{chrome_in}:v]scale={layout.width}:{layout.height}[v0]"]
            last, node = "v0", 0

            # ---- 转场：发生在这一段**内部**，所以总帧数一个都不变 ----
            card = cards[index] if cards and index < len(cards) else None
            if (
                transition.kind in ("dissolve", "push")
                and transition.seconds > 0
                and transition.previous is not None
                and card is not None
            ):
                prev_in = add_image(transition.previous)
                cur_in = add_image(card)
                span = f"min(1,t/{transition.seconds:.3f})"
                node += 1
                # 先把图片区从骨架里裁出来当「窗口」：这样两张卡在窗口内滑动/淡入淡出时
                # 会被自动裁切在图片框里，不会糊到标题或字幕上
                graph.append(
                    f"[{chrome_in}:v]crop={layout.image_box_w}:{layout.image_box_h}:{layout.image_box_left}:"
                    f"{layout.image_top}[win{node}]"
                )
                if transition.kind == "push":
                    # 横推：两张卡在窗口里左右滑动（窗口只有图片区那么大，超出部分自动裁掉）
                    graph.append(f"[{prev_in}:v]format=rgba[pa{node}]")
                    graph.append(f"[{cur_in}:v]format=rgba[ca{node}]")
                    graph.append(
                        f"[win{node}][pa{node}]overlay=x='-{layout.image_box_w}*{span}':y=0[wm{node}]"
                    )
                    graph.append(
                        f"[wm{node}][ca{node}]overlay=x='{layout.image_box_w}*(1-{span})':y=0[wc{node}]"
                    )
                else:
                    graph.append(
                        f"[{prev_in}:v]format=rgba,fade=t=out:st=0:d={transition.seconds:.3f}:"
                        f"alpha=1[po{node}]"
                    )
                    graph.append(
                        f"[{cur_in}:v]format=rgba,fade=t=in:st=0:d={transition.seconds:.3f}:"
                        f"alpha=1[co{node}]"
                    )
                    graph.append(f"[win{node}][po{node}]overlay=0:0[wm{node}]")
                    graph.append(f"[wm{node}][co{node}]overlay=0:0[wc{node}]")
                graph.append(
                    f"[{last}][wc{node}]overlay={layout.image_box_left}:{layout.image_top}[v{node}]"
                )
                last = f"v{node}"
            elif card is not None:
                cur_in = add_image(card)
                node += 1
                graph.append(f"[{cur_in}:v]format=rgba[ca{node}]")
                graph.append(
                    f"[{last}][ca{node}]overlay={layout.image_box_left}:{layout.image_top}[v{node}]"
                )
                last = f"v{node}"

            # ---- 图内聚光灯：讲到哪一块就把那一块框出来，其余压暗 ----
            #
            # 必须画在图片卡之后（它盖在图上），但要排在强调行/字幕/进度条之前 ——
            # 那些是画面上的独立信息，不该被压暗。
            focus_row = foci[index] if index < len(foci) else None
            if focus_row is not None:
                focus_in = add_image(focus_row)
                node += 1
                graph.append(
                    f"[{focus_in}:v]format=rgba,"
                    f"fade=t=in:st=0:d={FOCUS_FADE_SEC:.2f}:alpha=1[fo{node}]"
                )
                graph.append(
                    f"[{last}][fo{node}]overlay={layout.image_box_left}:{layout.image_top}[v{node}]"
                )
                last = f"v{node}"

            # ---- 首段：整页从白底淡入（「啪」地出现太硬） ----
            if transition.kind == "fade_in" and transition.seconds > 0:
                node += 1
                graph.append(
                    f"[{last}]fade=t=in:st=0:d={transition.seconds:.3f}:color=white[v{node}]"
                )
                last = f"v{node}"

            # ---- 进片尾品牌卡：上一整页淡出，露出底下的深色卡 ----
            if (
                transition.kind == "to_card"
                and transition.seconds > 0
                and transition.previous is not None
            ):
                prev_in = add_image(transition.previous)
                node += 1
                graph.append(
                    f"[{prev_in}:v]format=rgba,fade=t=out:st=0:d={transition.seconds:.3f}:"
                    f"alpha=1[po{node}]"
                )
                graph.append(f"[{last}][po{node}]overlay=0:0[v{node}]")
                last = f"v{node}"

            # ---- 本段要点：从左侧滑入（标记这一段的开始） ----
            row = rows[index] if index < len(rows) else None
            if row is not None:
                row_in = add_image(row)
                node += 1
                graph.append(f"[{row_in}:v]format=rgba[pt{node}]")
                graph.append(
                    f"[{last}][pt{node}]overlay="
                    f"x='-(W)+W*min(1,t/{POINT_SLIDE_SEC:.2f})':y={layout.point_top}[v{node}]"
                )
                last = f"v{node}"

            # ---- 进度条：整条色带从左推进，用全局时间算，跨段连续 ----
            node += 1
            graph.append(f"[{bar_in}:v]format=rgba[bar{node}]")
            graph.append(
                f"[{last}][bar{node}]overlay=x='min(0,-(W)+W*({start_sec:.3f}+t)/{total_sec:.3f})'"
                f":y=0[v{node}]"
            )
            last = f"v{node}"

            # ---- 字幕：第 0 句从一开始就在，后面的按窗口淡入 ----
            scene_bands = bands[index] if index < len(bands) else []
            windows = beat_windows_aligned(
                [band.text for band in scene_bands],
                scene.duration,
                start=scene.start,
                pauses=pauses,
            )
            for band, (beat_start, _beat_end) in zip(scene_bands, windows):
                band_in = add_image(band.image)
                node += 1
                graph.append(
                    f"[{band_in}:v]format=rgba,fade=t=in:st={beat_start:.3f}:"
                    f"d={CAPTION_FADE_SEC}:alpha=1[cap{node}]"
                )
                graph.append(
                    f"[{last}][cap{node}]overlay=x=0:y={layout.subtitle_top}:"
                    f"enable='gte(t,{beat_start:.3f})'[v{node}]"
                )
                last = f"v{node}"

            # ---- 声波条：跟着这一段的语音起伏（片尾品牌卡不加） ----
            #
            # **必须排在字幕带之后**：字幕带是一整条不透明的浅蓝底，先画声波条会被它整条盖住
            # （实测：画面上什么都看不到，而单独跑滤镜链时墨迹有 793 个像素 —— 就是顺序问题）。
            if waveform and scene.brand != "outro":
                color = SPEAKER_WAVE_COLORS.get(scene.speaker or "A", WAVEFORM_COLOR)
                color_in = add_source(f"color=c={color}:s={WAVEFORM_RENDER_SIZE}:r={FPS}")
                audio_in = add_audio(audio_path, scene.start, scene.duration)
                node += 1
                graph.append(
                    f"[{audio_in}:a]showwaves=s={WAVEFORM_RENDER_SIZE}:mode=cline:"
                    f"colors={color}:rate={FPS},format=gray[msk{node}]"
                )
                graph.append(
                    f"[{color_in}:v][msk{node}]alphamerge,"
                    f"scale={layout.waveform_width}:{WAVEFORM_HEIGHT},format=rgba[wave{node}]"
                )
                graph.append(
                    f"[{last}][wave{node}]overlay=x={layout.waveform_left}:y={layout.waveform_top}[v{node}]"
                )
                last = f"v{node}"

            graph.append(f"[{last}]format=yuv420p[out]")
            run(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    *inputs,
                    "-filter_complex", ";".join(graph),
                    "-map", "[out]", "-an",
                    "-frames:v", str(frames),
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", str(VIDEO_CRF),
                    "-pix_fmt", "yuv420p", "-r", str(FPS),
                    str(clip_path),
                ],
                f"第 {index + 1} 段画面编码",
            )
            clip_paths.append(clip_path)
            cursor_frames += frames

        # ---- 第一步：纯视频轨（片段拼接走 stream copy，不重编码） ----
        list_path.write_text(
            "\n".join(f"file '{clip.resolve()}'" for clip in clip_paths) + "\n",
            encoding="utf-8",
        )
        run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "concat", "-safe", "0", "-i", str(list_path),
                "-an",
                "-c:v", "copy",
                str(silent_path),
            ],
            "视频轨拼接",
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
        shutil.rmtree(clips_dir, ignore_errors=True)

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
    preset_scenes: list[dict[str, Any]] | None = None,
    preset_assets: dict[str, str] | None = None,
    preset_hook: str = "",
    allow_point_llm: bool | None = None,
    language: str = "zh",
    orientation: str = "portrait",
) -> VideoResult:
    """合成视频解读播客。任何一步失败都抛 VideoError，由调用方降级。

    preset_scenes / preset_assets 用于**重新合成**：上一次已经把「哪一段用哪张图」
    算好了并存了下来，重做时直接复用，不再问模型 —— 既省钱省时间，
    也保证重做前后画面选择一致（否则再问一次结果可能就不一样了）。

    `preset_hook` 是上次写的封面标题，`allow_point_llm` 同时管「要点文案」和
    「封面标题」这两件**可以单独补一次**的模型输出（见下面强调行那一段）。
    """
    pool = build_asset_pool(
        cover_path=cover_path, figures=figures, illustration_png=illustration_png
    )
    if not pool:
        raise VideoError("没有任何可用配图，无法生成视频")

    default_id = default_asset_id(pool)
    figure_ids = [i for i in pool if i not in ("cover", "illustration")]

    # 复用模式：把上次用到的素材（含现场生成的主题图）补回池子里
    if preset_scenes:
        for asset_id, asset_path in (preset_assets or {}).items():
            if asset_id in pool:
                continue
            if asset_path and Path(asset_path).exists():
                pool[asset_id] = ImageAsset(
                    id=asset_id,
                    path=Path(asset_path),
                    kind="illustration" if asset_id.startswith("topic") else "figure",
                    caption="",
                )
        default_id = default_asset_id(pool)
        figure_ids = [i for i in pool if i not in ("cover", "illustration")]

    # 逐段语义匹配：优先让模型判断「这一段在讲什么、哪张图正好在讲同一件事」
    image_for_segment: list[str] | None = None
    strategy = "heuristic"

    if preset_scenes:
        # 复用上次的选择，只做有效性校验（图可能被删了/文件丢了）
        picked: list[str] = []
        last_valid = default_id
        for index in range(len(segments)):
            entry = next(
                (sc for sc in preset_scenes if sc.get("index") == index), None
            )
            candidate = (entry or {}).get("image")
            if candidate and candidate in pool and pool[candidate].exists:
                last_valid = candidate
            picked.append(last_valid)
        image_for_segment = picked
        strategy = "reused"
        missing = sum(
            1
            for index in range(len(segments))
            if not (
                next((sc for sc in preset_scenes if sc.get("index") == index), {}) or {}
            ).get("image") in pool
        )
        logger.info("重新合成：复用上次的画面分配（%d 段）", len(picked))
        if missing:
            logger.info("其中有 %d 段引用的图已失效，回退到中性图", missing)

    elif llm is not None and not getattr(llm, "mock", True):
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

    # ---- 「本段要点」强调行 ----
    #
    # 三级来源，优先级从高到低：
    # 1. **上次存下来的**（`video.scenes[i].point`）—— 重新合成时必须复用，
    #    否则用户只是转了个配图，整段强调文案就全变了。
    # 2. **让模型写**（`POINTS_SYSTEM`）。这是唯一能给出「这一段最该记住什么」的来源：
    #    实测本地抽取够不着 —— 只有 30%~47% 的段落含阿拉伯数字，关键词命中接近 0。
    # 3. **本地兜底**（`local_point`，只抠数字），没有就留空、不显示强调行。
    #
    # **复用画面时默认不碰模型**（`tests/test_video.py::TestPresetReuse` 钉着这条），
    # 因为「用户只是转了个配图，画面和文案不该全变」。但旧视频里没有强调行文案，
    # 需要能**单独补一次**：调用方显式传 `allow_point_llm=True` 即可（见维护脚本）。
    if allow_point_llm is None:
        allow_point_llm = not preset_scenes
    point_limit = point_char_limit(language)

    points: list[str] = [""] * len(segments)
    focuses: list[dict[str, Any] | None] = [None] * len(segments)
    if preset_scenes:
        reused = 0
        reused_focus = 0
        for entry in preset_scenes:
            index = entry.get("index")
            point = str(entry.get("point") or "").strip()
            if isinstance(index, int) and 0 <= index < len(points):
                if point:
                    points[index] = point
                    reused += 1
                # 聚光灯也复用：重新合成时画面分配与「框哪里」都不该变
                focus = normalize_focus(entry.get("focus"))
                if focus:
                    focuses[index] = focus
                    reused_focus += 1
        if reused:
            logger.info("强调行：复用上次的 %d 段", reused)
        if reused_focus:
            logger.info("聚光灯：复用上次的 %d 段", reused_focus)

    if (
        allow_point_llm
        and any(not point for point in points)
        and llm is not None
        and not getattr(llm, "mock", True)
    ):
        # 这几张图有子图清单可给模型挑（几何已经由像素量好了，见 build_panel_catalog）
        panel_catalog = build_panel_catalog(figures, pool)
        panel_options = [
            panel_catalog[asset_id].options() if asset_id in panel_catalog else []
            for asset_id in image_for_segment
        ]
        panel_letters = [
            panel_catalog[asset_id].letters if asset_id in panel_catalog else []
            for asset_id in image_for_segment
        ]
        # 重试一次：这一路失败不会让视频整体失败（下面有本地兜底），
        # 但结果会从「每段都有要点」退化成「只有含数字的段有」——
        # 实测踩到过一次（中文那版整集没有要点，英文那版正常），所以值得多试一次。
        for attempt in (1, 2):
            try:
                data = llm._chat_json(
                    build_points_messages(
                        segments,
                        title=title,
                        language=language,
                        image_captions=[
                            pool[asset_id].caption if asset_id in pool else None
                            for asset_id in image_for_segment
                        ],
                        panel_options=panel_options,
                    ),
                    max_tokens=max(900, len(segments) * 60),
                    temperature=0.3,
                )
                written = normalize_point_items(
                    data.get("points"),
                    count=len(segments),
                    max_chars=point_limit,
                    language=language,
                    panel_letters=panel_letters,
                )
                filled = 0
                boxed = 0
                for index, item in enumerate(written):
                    if item["point"] and not points[index]:
                        points[index] = item["point"]
                        filled += 1
                    if focuses[index] is not None:
                        continue
                    focus = None
                    if item["panel"] and image_for_segment[index] in panel_catalog:
                        focus = panel_catalog[image_for_segment[index]].focus_for(item["panel"])
                    # 旧数据里存的「模型给的坐标框」还能读，但不能把空间让给它：
                    # 面板框是量出来的，坐标框是猜的（见 POINTS_SYSTEM 的说明）。
                    focus = focus or item["focus"]
                    if focus:
                        focuses[index] = focus
                        boxed += 1
                if filled:
                    logger.info("强调行：模型写了 %d 段", filled)
                if boxed:
                    logger.info("聚光灯：模型指了 %d 段", boxed)
                break
            except Exception as exc:  # noqa: BLE001
                if attempt == 1:
                    logger.warning("强调行生成失败，重试一次：%s", exc)
                else:
                    logger.warning("强调行生成失败，退回本地抽取：%s", exc)

    fallback_used = 0
    for index, segment in enumerate(segments):
        if not points[index]:
            local = local_point(
                str(segment.get("text") or ""), max_chars=point_limit, language=language
            )
            if local:
                points[index] = local
                fallback_used += 1
    logger.info(
        "强调行：共 %d 段有内容（其中本地兜底 %d 段）",
        sum(1 for point in points if point),
        fallback_used,
    )

    # ---- 封面标题（爆款标题）----
    #
    # 只出现在片头那一帧上。三级来源：
    # 1. **上次写的**（`video.hook`）—— 重新合成时必须复用（跟强调行同一个道理）；
    # 2. **让模型写一次**（`HOOK_SYSTEM`，挑风格、不许编数字）；
    # 3. **论文原题兜底** —— 没有标题的封面很怪，原题至少交代了这是什么。
    hook = normalize_hook(preset_hook, language=language)
    if not hook and allow_point_llm and llm is not None and not getattr(llm, "mock", True):
        try:
            raw = llm._chat(
                build_hook_messages(title=title, analysis=analysis, language=language),
                max_tokens=80,
                temperature=0.8,
                json_mode=False,  # 要的就是一行纯文本，塞进 JSON 反而更容易被引号包起来
            )
            hook = normalize_hook(raw, language=language)
        except Exception as exc:  # noqa: BLE001
            logger.warning("封面标题生成失败，改用论文原题：%s", exc)
    cover_headline = hook or _cover_fallback_headline(title)
    if hook:
        logger.info("封面标题：%s", hook)
    elif cover_headline:
        logger.info("封面标题：没有模型输出，用论文原题兜底（%s…）", cover_headline[:16])

    scenes = build_scenes(
        segments=segments,
        timings=timings,
        audio_duration=audio_duration,
        assets=pool,
        image_for_segment=image_for_segment,
        fallback_id=default_id,
        points=points,
        focuses=focuses,
        headline=cover_headline,
        cover_caption=title,
    )

    layout = layout_for(orientation)
    work_dir.mkdir(parents=True, exist_ok=True)
    slide_paths: list[Path] = []
    caption_bands: list[list[CaptionBand]] = []
    band_paths: list[Path] = []
    point_rows: list[Path | None] = []
    point_paths: list[Path] = []
    image_cards: list[Path | None] = []
    card_paths: list[Path] = []
    focus_rows: list[Path | None] = []
    focus_paths: list[Path] = []
    image_ids: list[str] = []
    endcard_count = 0
    beat_count = 0
    for index, scene in enumerate(scenes):
        slide_path = work_dir / f"slide-{index:04d}.png"
        beats = split_caption_beats(scene.text)
        bands: list[CaptionBand] = []
        card_path: Path | None = None

        if scene.brand == "outro":
            # 片尾用品牌卡（深色 + logo + 关注引导），正文用普通白底页。
            # 品牌卡上的字是大号引导语，不参与「字幕逐句出现」——那会把它切碎。
            # 它整页都是牌子，没有「图片层」，转场用 to_card（上一整页淡出）代替。
            render_endcard(scene, slide_path, layout=layout)
            endcard_count += 1
        else:
            # 正文：**骨架 + 图片卡** 两层。骨架（标题/图注/强调行/字幕带）全程不动，
            # 只有图片卡在换 —— 这就是「连贯」的来源（见 render_chrome 的说明）。
            render_chrome(
                scene,
                slide_path,
                include_subtitle=len(beats) <= 1,
                include_point=False,
                layout=layout,
            )
            card_path = work_dir / f"card-{index:04d}.png"
            render_image_card(scene, card_path, layout=layout)
            card_paths.append(card_path)
            if len(beats) > 1:
                for beat_index, beat in enumerate(beats):
                    band_path = work_dir / f"band-{index:04d}-{beat_index}.png"
                    render_caption_band(beat, band_path, layout=layout)
                    bands.append(CaptionBand(text=beat, image=band_path))
                    band_paths.append(band_path)
                beat_count += 1

        slide_paths.append(slide_path)
        caption_bands.append(bands)
        image_cards.append(card_path)
        # 「换了没有」按图片素材判断，而不是按渲染出来的文件：同一张图连着讲两段时
        # 不该做无意义的溶解（观众只会觉得画面卡了一下）
        image_ids.append(str(scene.image))

        # 聚光灯只框**论文原图**：我们生成的信息图/段落图是矢量示意图，
        # 下一步会给它们做「逐元素长出来」，两套动效叠在一起反而乱。
        if (
            scene.focus
            and scene.brand != "outro"
            and card_path is not None
            and scene.kind in ("figure", "table", "cover")
        ):
            focus_path = work_dir / f"focus-{index:04d}.png"
            render_focus_overlay(scene, focus_path, focus=scene.focus, layout=layout)
            focus_rows.append(focus_path)
            focus_paths.append(focus_path)
        else:
            focus_rows.append(None)

        if scene.point and scene.brand != "outro":
            point_path = work_dir / f"point-{index:04d}.png"
            render_point_row(scene.point, point_path, layout=layout)
            point_rows.append(point_path)
            point_paths.append(point_path)
        else:
            point_rows.append(None)

    transitions = plan_transitions(scenes, image_ids, cards=image_cards, slides=slide_paths)
    moved = sum(1 for item in transitions if item.kind not in ("", "none"))
    logger.info(
        "已渲染 %d 帧画面（其中片尾品牌卡 %d 帧，字幕分句 %d 段，转场 %d 处）",
        len(slide_paths), endcard_count, beat_count, moved,
    )

    # 说话停顿：用来把字幕换句对准真实的停顿（检测失败就是空列表 → 退回按字数）
    pauses = detect_pauses(audio_path)
    if pauses:
        logger.info("检测到 %d 处说话停顿，字幕换句将对齐到停顿", len(pauses))

    result = encode_video(
        scenes,
        slide_paths,
        audio_path,
        output_path,
        target_duration=audio_duration,
        caption_bands=caption_bands,
        point_rows=point_rows,
        pauses=pauses,
        image_cards=image_cards,
        transitions=transitions,
        focus_overlays=focus_rows,
        layout=layout,
    )
    result.assignment = strategy
    result.scenes = [
        {
            "index": index,
            "image": image_for_segment[index],
            # 强调文案与聚光灯都存下来，重新合成时复用（见上面三级来源的说明）
            "point": points[index] if index < len(points) else "",
            "focus": focuses[index] if index < len(focuses) else None,
        }
        for index in range(min(len(segments), len(image_for_segment)))
    ]
    result.assets = {asset_id: str(asset.path) for asset_id, asset in pool.items()}
    result.asset_versions = {
        asset_id: asset_version(asset.path) for asset_id, asset in pool.items()
    }
    result.hook = hook

    for temp_path in [*slide_paths, *band_paths, *point_paths, *card_paths, *focus_paths]:
        try:
            temp_path.unlink(missing_ok=True)
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
