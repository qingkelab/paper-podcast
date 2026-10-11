"""版式与配色令牌：视频画面的**设计常量**。

单独成一个模块，是因为这些数值现在有**两个消费者**：
`video.py` 的 SVG 渲染路径，和 `htmlpage.py` 的 HTML 渲染路径。
迁移完成（SVG 路径下线）之后它仍然该留在这里 —— 它描述的是「画面长什么样」，
跟「用什么渲染器画出来」无关。

**这里只放数值和注释，不放逻辑**（唯一一个函数是 `layout_for`，
它只是按朝向挑一份数值）。动手改版式时看清每段注释 —— 那些数字几乎都是**量出来的**
（模糊半径、图片区高度、行数上限……），不是随手定的。
"""

from __future__ import annotations

from dataclasses import dataclass


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
# 字幕带里**数字胶囊**的底色。**故意和强调行不同**：强调行是一整条 856px 的宽底，
# 浅一点才对；胶囊只有 30×24px，用同一个色等于没画。
# 实测（亮度按 0.299R+0.587G+0.114B 算，字幕带底色 `SUBTITLE_BG` 是 246.4）：
# 旧值 #eef4fb = 243.0，**只差 3.4** —— 一屏里根本看不出那里有个底，
# 「数字被点出来」全靠数字变蓝撑着。现在 #cfe0f8 差 **24.8**，是「浅蓝高亮」而不是「几乎同色」。
# 再往深（#c3d8f6 = 33.3）在手机上会显得像一块色卡，所以停在这里。
NUMBER_BG = "#cfe0f8"
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
# ---- 逐句画面动效（见 docs/BEAT_MOTION.md）----
# 画面原来是按「段」换的（一段 2~4 句、10~20 秒），现在每句话都可以有自己的画面。
# 下面四个数都跟内容绑定：**换图是因为这一句讲的不是同一件事**，
# 不是为了让画面「动起来」（推拉镜头那一版就是因此被否掉的）。
BEAT_CARD_FADE = 0.25     # 句内换图的溶解时长（参考片的整页溶解是 0.25~0.5 秒）
BEAT_IMAGE_MIN_SEC = 1.6  # 短于这个时长的句子不切图：几个字一闪而过比不切更难看
CHIP_POP_SEC = 0.22       # 句子里那个数字/关键词的强调层淡入时长（退回 SVG 那条路时用）
CHIP_POP_DELAY = 0.12     # 比这一句的字幕晚一点出现：先读到句子，再看到强调
# ---- 弹簧缓动（公式与推导见 services/motion.py）----
# 「用弹簧和缓动曲线，不要线性运动」：下面这几处原本要么匀速、要么只有淡入。
# **过冲量分开定**，因为屏上距离差两个数量级（强调行滑入 936px、数字胶囊 30px）。
SPRING_PEAK_RATIO = 0.3        # 过冲峰值出现在总时长的 30%（默认形状）
# 强调行滑入：过冲 2%（936px × 2% ≈ 19px），峰值放到 **60%** 处。
# 峰值放得越早、上升越陡 —— 0.3 时实测 0.08 秒就走了 96%（等于「啪」一下贴上去），
# 而这一行滑入的用意是「标记新的一段开始了」，人得看得见这个过程。
SPRING_SLIDE_OVERSHOOT = 0.02
SPRING_SLIDE_PEAK_RATIO = 0.6
SPRING_POP_OVERSHOOT = 0.09    # 数字胶囊「弹一下」的过冲：30px × 9% ≈ 2.7px
# 胶囊的落定速度（1/秒）。**不用「峰值出现在百分之几」那套**：冲激响应的峰值
# 时刻上限就是 1/k，所以「0.06 秒弹到位、0.3 秒落定」对应的是 k≈12。
# 硬按比例调会得到一个「序列播完还没落定」的曲线（末帧停在过冲上）。
SPRING_POP_DECAY = 12.0
CHIP_SPRING_SEC = 0.30         # 弹簧「弹一下」本身要多久（要留出落定时间）
CHIP_POP_FPS = 30              # Tier B 抓帧帧率（过冲落在第 2~3 帧上，抓得住）
# 「弹一下」**什么时候才开始**：胶囊自己也是淡入的（跟着字幕带那条淡入走），
# 而弹簧的峰值只在 0.06 秒处 —— 两者一起就是「弹给谁看」。
# 实测（真抓帧 + 数胶囊宽度）：不延迟时第 2 帧不透明度才 0.44，等到完全不透明（t≈0.23s）
# 弹簧早就落定了，抓下来的 10 帧里**量不到任何过冲**。所以把鼓包整体推迟到这里 ——
# 那时不透明度已经是 0.91，观众看得见它弹。
CHIP_POP_BUMP_DELAY = 0.20
# ---- 字幕「按小句依次出现」（填掉「≥4 秒没有可见变化」那些空档，见 docs/VIDEO_QA.md）----
# 一句 30 字的话念 10 秒，整句在 0.35 秒就全出现了，中间什么都不变。
# 按小句依次显露，读到哪里字长到哪里。判据全是时间，跟字体无关。
CLAUSE_REVEAL_MIN_SEC = 3.0    # 短于这个时长的句子不拆：一闪而过比不动更难看
CLAUSE_REVEAL_MIN_GAP = 1.1    # 两次显露之间至少隔这么久，免得连着闪
CLAUSE_REVEAL_TAIL_HOLD = 1.2  # 最后一个小句至少留这么久：刚出现就结束等于白搭
CLAUSE_REVEAL_MAX = 3          # 一句最多分几次（每多一次就多渲染一张整条带子）
CLAUSE_FADE_SEC = 0.18         # 一次显露的淡入时长（比整句的 0.35 短：它是「续上」，不是「开始」）
CAPTION_BEAT_MAX = 2      # 一段最多拆成几句字幕（按句子合并时的上限，见 split_caption_beats）
CAPTION_BEAT_MIN_CHARS = 28   # 短于这个长度就别拆了：每句只剩几个字，闪得更难看
# **一句字幕最多渲染几行**。多于此就在逗号处再拆一刀（拆成两个连续的字母事件）。
#
# 为什么要有这条：`_fit_subtitle` 放不下时会**缩字号**，于是那句 164 字的稿子
# 会被排成「5 行 @26px」——比「2 行 @30px」难读得多。实测 487 句里有 35 句超过两行
# （最长 164 字），而按「一分为二」的处方能修掉其中 21 句。
# 这条规矩来自 Speclip 的字幕 skill（硬约束：最多两行；先拆句，再缩字号）。
CAPTION_MAX_LINES = 2
# 行数规则允许拆到的句数上限（防止把一句话拆成七八个碎片）
CAPTION_BEAT_HARD_MAX = 6
# 拆出来的碎片短于这个字数就不再拆（几个字一闪而过比三行更难读）
CAPTION_SPLIT_MIN_CHARS = 10
CAPTION_BAND_MAX_FONT = 38.0  # 字幕带单独渲染，字可以比整页大（观众主要在读它）
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


# ---------------------------------------------------------------------------
# 文字折行：**两套渲染（HTML / SVG）必须共用同一套断行结果**
# ---------------------------------------------------------------------------
#
# 为什么要把折行放到这里、而不是各自写在渲染函数里：
# 画面有两条渲染路径（HTML 走 Chrome，SVG 走 resvg 兜底），两边都排同一行大字标题。
# 各排一份的话，同一个标题在两条路径上会断在不同位置 —— 而这种不一致平时看不出来
# （谁也不会同时跑两条路径对比），只有「同一篇论文两台机器出的画面不一样」时才暴露。


def wrap_units(text: str, max_units: float) -> list[str]:
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


def text_units(text: str) -> float:
    """这段文字占多少个全角单位（一个汉字 = 1，西文字母/数字 = 0.55）。"""
    from .illustration import _char_width

    return sum(_char_width(c) for c in text)


# 可以在**它后面**断行的标点。断在这里，标点自己留在上一行 ——
# 中文排版的「避头尾」：句号、逗号、右括号这些不能跑到下一行开头。
_HOOK_BREAK_AFTER = "，。、；：！？…,.;:!?）】》」』”’)"
# 换行后**不能**出现在行首的字符（万一按宽度断，也要把它们拉回上一行）
_HOOK_NO_LINE_START = "，。、；：！？…,.;:!?）】》」』”’)"


def _hook_segments(text: str) -> list[str]:
    """按标点把标题切成「语义段」：标点跟着**前一段**走。"""
    segments: list[str] = []
    buffer = ""
    for char in text:
        buffer += char
        if char in _HOOK_BREAK_AFTER:
            segments.append(buffer)
            buffer = ""
    if buffer.strip():
        segments.append(buffer)
    return segments


def _no_orphan_punctuation(lines: list[str]) -> list[str]:
    """行首的标点拉回上一行（避头尾）。"""
    fixed: list[str] = []
    for line in lines:
        moved = ""
        while line and line[0] in _HOOK_NO_LINE_START:
            moved += line[0]
            line = line[1:]
        if moved and fixed:
            fixed[-1] += moved
        elif moved:
            line = moved + line
        if line.strip() or not fixed:
            fixed.append(line.strip())
    return [line for line in fixed if line]


def hook_lines(text: str, max_units: float) -> list[str]:
    """封面大字标题折行：**优先在标点处断**，没有标点才退回按宽度断。

    为什么不能只按宽度折（`wrap_units` 的做法）：那是「塞到塞不下为止」，
    于是断点经常落在词中间，或者像实测里的这条 ——

        显存直降 68.7%！Qwen/Kimi
        同款                       ← 第二行只剩一个词，读起来像话被截断了

    宽度上它是合理的（两行都塞满了），但句子的断点明明就在那个「！」上。
    爆款标题是**一句话**，标点就是它自带的断句：顺着标点断，每行都是一个完整的短语。

    规则：
    - 尽量把整句放在一行；一行放不下时，退到**最近的一个标点**处断开；
    - 一个标点都没有的标题（或某个标点之间太长）只能按宽度断 —— 这是明面上的退路，
      不是静默出错（那条超长的段会走 `wrap_units`，结果照样能画出来）；
    - 返回**全部**行，不在这里截断：字号与行数由 `cover_title_layout` 决定（它会缩字号
      直到行数放得下，实在放不下才截断）。
    """
    text = " ".join((text or "").split())
    if not text or max_units <= 0:
        return []

    lines: list[str] = []
    current = ""
    width = 0.0

    for segment in _hook_segments(text):
        segment_width = text_units(segment)
        if segment_width > max_units:
            # 这一段自己就塞不下一行（两个标点之间太长了）：只能按宽度断。
            # 先把手上的行收掉，再把它的前半截各行独立成行，末尾那截继续和后面拼。
            if current.strip():
                lines.append(current.strip())
            current, width = "", 0.0
            pieces = wrap_units(segment, max_units)
            lines.extend(pieces[:-1])
            current = pieces[-1] if pieces else ""
            width = text_units(current)
            continue
        if current and width + segment_width > max_units:
            lines.append(current.strip())
            current, width = "", 0.0
        current += segment
        width += segment_width

    if current.strip():
        lines.append(current.strip())
    return _no_orphan_punctuation(lines)


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
        # `hook_lines` 的单位宽度就是字号（一个全角字 ≈ 1 个字号宽），所以像素宽 ÷ 字号 = 单位数
        wrapped = hook_lines(text, inner / candidate)[:COVER_HEADLINE_MAX_LINES + 1]
        if len(wrapped) <= COVER_HEADLINE_MAX_LINES:
            size, lines = float(candidate), wrapped
            break
    else:
        size = float(COVER_HEADLINE_MIN_FONT)
        lines = hook_lines(text, inner / size)[:COVER_HEADLINE_MAX_LINES]
        if lines:
            lines[-1] = lines[-1].rstrip("，。、,.;") + "…"

    block_h = len(lines) * size * COVER_HEADLINE_LEADING
    sub_all = wrap_units(caption, inner / COVER_SUBTITLE_FONT)
    sub_lines = sub_all[:COVER_SUBTITLE_MAX_LINES]
    if len(sub_all) > COVER_SUBTITLE_MAX_LINES and sub_lines:
        # 截断要**看得出来**：默默少掉一行，观众会以为那就是论文的全名。
        sub_lines[-1] = sub_lines[-1].rstrip("，。、,.;") + "…"
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
