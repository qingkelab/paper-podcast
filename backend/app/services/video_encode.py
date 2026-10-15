"""ffmpeg 编码：把渲染好的画面层 + 音频合成 MP4，并抽一帧当封面静帧。

这里是「编码」这一件事的全部：每段滤镜链怎么拼、每段多少帧、进度条怎么推进、
音频怎么封进去、失败怎么报错 —— 改编码不必读版式，改版式也不必读滤镜链。
画面本身怎么画（坐标、SVG/HTML）在 `video_render.py`，版式与配色令牌在 `design.py`。
ffmpeg / ffprobe 的探测（`ffmpeg_available` / `probe_media_duration`）也只被这一条路径用，
所以一起放在这里。

分层：`design → video_core → video_render → video_encode → video`（本层是第 4 层）。
本模块**只**从 `video_core`（类型）与 `video_render`（画面）取东西，**不 import `video.py`** ——
所以 `import app.services.video_encode` 能独立成立，这正是这一轮拆分修掉的东西；
门面 `video.py` 反过来 import 本模块，再由它统一对外。
想改画面 → `video_render.py`；想改场景编排 / 转场 / 素材池 → `video.py`。
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import subprocess
from pathlib import Path

from .audio_track import Pause
from . import loudness
from .motion import ffmpeg_ease as spring_ease
from .motion import spring_kwargs

# 版式/配色/节奏令牌的**老家**在 design.py（video.py 只是把它们再导出给老调用点）
from .design import (
    BEAT_CARD_FADE,
    DIAGRAM_FADE_SEC,
    ENTRANCE_OVERSHOOT,
    ENTRANCE_RISE_PX,
    ENTRANCE_SEC,
    BEAT_IMAGE_MIN_SEC,
    CAPTION_FADE_SEC,
    CHIP_POP_DELAY,
    CLAUSE_FADE_SEC,
    CHIP_POP_FPS,
    CHIP_POP_SEC,
    FOCUS_FADE_SEC,
    FPS,
    POINT_SLIDE_SEC,
    PORTRAIT,
    SPRING_SLIDE_OVERSHOOT,
    SPRING_SLIDE_PEAK_RATIO,
    PROGRESS_BAR_H,
    SPEAKER_WAVE_COLORS,
    VIDEO_CRF,
    WAVEFORM_COLOR,
    WAVEFORM_HEIGHT,
    WAVEFORM_RENDER_SIZE,
    Layout,
)
from .video_core import (
    CaptionBand,
    Scene,
    Transition,
    VideoError,
    VideoResult,
)
from .video_render import (
    _rasterize_custom,
    beat_windows_aligned,
    progress_bar_svg,
)

# 沿用原来的 logger 名：main.py 的日志 format 里带 %(name)s，搬家不该换掉日志通道。
logger = logging.getLogger("app.services.video")

__all__ = [
    "capture_poster",
    "encode_video",
    "ffmpeg_available",
    "probe_media_duration",
]


def ffmpeg_available() -> bool:
    """系统里有没有 ffmpeg。调用方（流水线）靠它决定「这一步要不要做」。"""
    return shutil.which("ffmpeg") is not None


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



def _file_signature(path: Path | None) -> str | None:
    """图层文件的指纹，用来判「这一句的卡和上一句是不是同一张」。

    为什么按**内容**而不是路径：同一张素材渲染出来的卡是确定性的（同样的图、同样的
    排版 → 同样的字节），换了个文件名其实是同一张。拿路径比就会多做一次
    「看起来像卡了一下」的无意义溶解 —— 正是守卫要拦的那种。
    """
    if path is None:
        return None
    try:
        stat = path.stat()
    except OSError:
        return None
    key = f"{path}:{stat.st_size}:{stat.st_mtime_ns}"
    cached = _SIGNATURE_CACHE.get(key)
    if cached is None:
        try:
            cached = hashlib.blake2b(path.read_bytes(), digest_size=16).hexdigest()
        except OSError:
            return None
        _SIGNATURE_CACHE[key] = cached
    return cached


_SIGNATURE_CACHE: dict[str, str] = {}


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
    focus_animations: list[list[Path]] | None = None,
    beat_cards: list[list[Path | None]] | None = None,
    beat_chips: list[list[Path | None]] | None = None,
    beat_chip_frames: list[list[list[Path]]] | None = None,
    # **动态图解镜头的状态**：第 i 段第 k 个状态（相对本段起点的秒数 + 图层路径）。
    # 与「逐句图片卡」分开，是因为图解的状态数由内容决定（几个节点就几步），
    # 与字幕的句数无关 —— 硬套逐句卡会让「4 个节点」被压成「最多 2 句」。
    diagram_states: list[list[tuple[Path, float]]] | None = None,
    beat_focus_overlays: list[list[Path | None]] | None = None,
    waveform: bool = True,
    layout: Layout = PORTRAIT,
    # **每一段的版式**（`design.content_layout`）：图注/强调行跟着图的下沿走。
    # 只有「强调行的 y」会变（图片区与字幕带不动），所以这里只需要它一个数。
    # 空列表 = 全部用 `layout`（老调用点不受影响）。
    scene_layouts: list[Layout] | None = None,
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
    # Tier B：某些段的聚光灯是**逐帧抓下来的帧序列**（而不是一张静态图）。
    # 与 foci 互斥 —— 同一段要么叠静态图，要么叠序列。
    focus_anims = focus_animations or [[] for _ in scenes]
    # **逐句**的两层（见 docs/BEAT_MOTION.md）：第 i 段第 j 句的一张卡 / 一个数字强调层。
    # 空列表 / None 一律表示「这一句不换画面、不弹强调」—— 所以老数据（不给这两个参数）
    # 走的滤镜链和以前完全一样。
    beat_rows = beat_cards or [[] for _ in scenes]
    chip_rows = beat_chips or [[] for _ in scenes]
    # 弹簧版胶囊：**帧序列**（Tier B）。与静态图互斥（同一句只会给其中一种）。
    chip_frame_rows = beat_chip_frames or [[] for _ in scenes]
    diagram_rows = diagram_states or [[] for _ in scenes]
    # ⚠️ **行数与段数必须 1:1**：下面按**段号**取 `diagram_rows[index]`。少一行就整体错位，
    # 而错位的表现是「图解叠到了别的段上」（实测：第 11 段的图解出现在第 1 段，
    # 用户看到的是「第二个画面有问题、又跳回封面」）。错位这种事不该静默发生。
    if diagram_states and len(diagram_rows) != len(scenes):
        logger.warning(
            "图解状态行数（%d）与段数（%d）对不上：**不许按段号取**，本集不叠图解状态",
            len(diagram_rows),
            len(scenes),
        )
        diagram_rows = [[] for _ in scenes]
    beat_focus_rows = beat_focus_overlays or [[] for _ in scenes]
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

            def add_sequence(paths: list[Path], *, fps: int = FPS) -> int:
                """挂一路图片序列（Tier B 逐帧抓下来的动画帧）。

                序列是**有限**的：播完就该停在最后一帧，而 `overlay` 的
                `eof_action` 默认正是 `repeat` —— 所以「先动一段、之后定住」
                不用额外做什么，片段总长仍由 `-frames:v` 说了算（长度对齐不受影响）。
                """
                nonlocal counter
                first = paths[0]
                pattern = first.parent / f"{first.stem.rsplit('-', 1)[0]}-%04d.png"
                inputs.extend(["-framerate", str(fps), "-i", str(pattern)])
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

            # 这一段的版式：强调行跟着图的下沿走（渲染各层用的是同一个对象）
            scene_layout = (
                scene_layouts[index]
                if scene_layouts and index < len(scene_layouts)
                else layout
            )

            chrome_in = add_image(slide)
            bar_in = add_image(bar_path)
            graph = [f"[{chrome_in}:v]scale={layout.width}:{layout.height}[v0]"]
            last, node = "v0", 0

            # 这一段的字幕分句 + 时间窗。**先算出来**：逐句图片卡和逐句数字强调层
            # 用的就是这一组窗口（`beat_windows_aligned` 对齐真实停顿），
            # 两边各算一遍必然错位 —— 这也是「同一组窗口」在这里只算一次的原因。
            scene_bands = bands[index] if index < len(bands) else []
            windows = beat_windows_aligned(
                [band.text for band in scene_bands],
                scene.duration,
                start=scene.start,
                pauses=pauses,
            )

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

            # ---- 逐句图片卡：这一句讲了另一件事 → 在**图片窗口里**溶解过去 ----
            #
            # 为什么是「窗口里」而不是整页：整页换掉会连标题/图注/字幕/进度条一起闪，
            # 而这一层是**不透明整卡**在图片框内换 —— 新卡淡入盖住旧卡，就是那段溶解。
            # 淡入的起点是**这一句的起点**（`windows`，和字幕带同一组），
            # 所以「话讲到这一句」和「画面换过去」是同一个时刻。
            # 第 0 句不叠：它就是这一段原本的那张卡。
            beat_row = beat_rows[index] if index < len(beat_rows) else []
            # 守卫①：封面段（headline 非空）永远要是「论文首页 + 标题」，片尾是品牌卡 ——
            # 数据侧已经拦了一道，这里再兜一层（「第一帧必须是论文首页」是产品要求）
            if beat_row and not (scene.headline or "").strip() and scene.brand != "outro":
                showing = _file_signature(card)
                for beat_index, beat_card in enumerate(beat_row):
                    if beat_index == 0 or beat_card is None or beat_index >= len(windows):
                        continue
                    beat_start, beat_end = windows[beat_index]
                    # 守卫③：这一句太短（按**实际窗口时长**判，不是按字数估）
                    if (beat_end - beat_start) < BEAT_IMAGE_MIN_SEC:
                        continue
                    # 守卫②：和上一句是同一张图 —— 图没变就别动
                    signature = _file_signature(beat_card)
                    if signature is not None and signature == showing:
                        continue
                    beat_in = add_image(beat_card)
                    node += 1
                    graph.append(
                        f"[{beat_in}:v]format=rgba,fade=t=in:st={beat_start:.3f}:"
                        f"d={BEAT_CARD_FADE:.2f}:alpha=1[bk{node}]"
                    )
                    graph.append(
                        f"[{last}][bk{node}]overlay={layout.image_box_left}:{layout.image_top}:"
                        f"enable='gte(t,{beat_start:.3f})'[v{node}]"
                    )
                    last = f"v{node}"
                    showing = signature

            # 每句可有自己的、由论文图注确认的子图聚焦框。按句子的实际窗口显示，
            # 不依赖换图；无效或缺失的子图映射不会产生图层。
            beat_focus_row = beat_focus_rows[index] if index < len(beat_focus_rows) else []
            for beat_index, focus_path in enumerate(beat_focus_row):
                if focus_path is None or beat_index >= len(windows):
                    continue
                beat_start, beat_end = windows[beat_index]
                focus_in = add_image(focus_path)
                node += 1
                graph.append(f"[{focus_in}:v]format=rgba[bf{node}]")
                graph.append(
                    f"[{last}][bf{node}]overlay={layout.image_box_left}:{layout.image_top}:"
                    f"enable='between(t,{beat_start:.3f},{beat_end:.3f})'[v{node}]"
                )
                last = f"v{node}"

            # ---- 动态图解镜头的后续状态：节点一个个出现 / 连线一段段画 / 数字递增 ----
            #
            # 每一张都是**整幅图片卡**的同一个位置（与图片卡同尺寸），按时间点叠上去。
            # 用 `enable='gte(t,…)'` + 一次淡入：状态之间只多一件事（多一个节点/一段连线/
            # 一个数字），所以看起来是「图在长」而不是「画面在切」。
            state_row = diagram_rows[index] if index < len(diagram_rows) else []
            for state_index, (state_path, moment) in enumerate(state_row):
                state_in = add_image(state_path)
                node += 1
                at = max(0.0, float(moment))
                graph.append(
                    f"[{state_in}:v]format=rgba,fade=t=in:st={at:.3f}:"
                    f"d={DIAGRAM_FADE_SEC}:alpha=1[dg{node}]"
                )
                # 同一条规矩：图解的下一个状态也是「淡入 + 上升」
                rise = spring_ease(
                    ENTRANCE_SEC, variable=f"(t-{at:.3f})", **spring_kwargs("entrance")
                )
                graph.append(
                    f"[{last}][dg{node}]overlay=x={layout.image_box_left}:"
                    f"y='{layout.image_top}+{ENTRANCE_RISE_PX}*(1-{rise})':"
                    f"enable='gte(t,{at:.3f})'[v{node}]"
                )
                last = f"v{node}"

            # ---- 图内聚光灯：讲到哪一块就把那一块框出来，其余压暗 ----
            #
            # 必须画在图片卡之后（它盖在图上），但要排在强调行/字幕/进度条之前 ——
            # 那些是画面上的独立信息，不该被压暗。
            focus_row = foci[index] if index < len(foci) else None
            focus_frames = focus_anims[index] if index < len(focus_anims) else []
            if focus_frames:
                # Tier B：整层是逐帧重画出来的（压暗蒙版 + 框线 + 标签贴在一起，
                # 整层平移会让「洞」跟着跑出画外，所以只能用帧序列）。
                # 序列自带过程，**不再叠 fade** —— 叠了会把「移过去」糊成一团。
                focus_in = add_sequence(focus_frames)
                node += 1
                graph.append(f"[{focus_in}:v]format=rgba[fo{node}]")
                graph.append(
                    f"[{last}][fo{node}]overlay={layout.image_box_left}:{layout.image_top}[v{node}]"
                )
                last = f"v{node}"
            elif focus_row is not None:
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
                # 具名预设：滑入的过冲与峰值位置在 `motion.SPRING_PRESETS["slide"]` 里
                ease = spring_ease(POINT_SLIDE_SEC, **spring_kwargs("slide"))
                # 滑入用**弹簧**而不是 `min(1, t/T)`：后者是匀速（实测就是「机器在滑」），
                # 而 936px 的匀速位移最能看出「没有缓动」。过冲 2%（≈19px）后就地落定。
                graph.append(
                    f"[{last}][pt{node}]overlay="
                    f"x='-(W)+W*{ease}':y={scene_layout.point_top}[v{node}]"
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
            # （`scene_bands` / `windows` 在上面就算好了 —— 逐句卡和 chip 用的就是同一组）
            chip_row = chip_rows[index] if index < len(chip_rows) else []
            for beat_index, (band, (beat_start, _beat_end)) in enumerate(
                zip(scene_bands, windows)
            ):
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

                # ---- 这一句按小句依次出现：把后续状态的整条带子按时序叠上去 ----
                #
                # 每一张都是「同一套排版、只把还没轮到的小句变透明」，所以叠上去看到的是
                # 「读到哪里，字长到哪里」。**必须排在强调层之前**：这些带子是不透明的，
                # 排在后面会把刚点亮的胶囊盖掉（那正是「叠上去等于没叠」那条老坑的变种）。
                for reveal_index, (reveal, moment) in enumerate(band.reveals):
                    reveal_in = add_image(reveal)
                    node += 1
                    at = beat_start + moment
                    graph.append(
                        f"[{reveal_in}:v]format=rgba,fade=t=in:st={at:.3f}:"
                        f"d={CLAUSE_FADE_SEC}:alpha=1[rv{node}]"
                    )
                    # **淡入 + 上升**（参考做法：绝不用单一的淡入）。
                    # 上升用弹簧表达式：`variable="(t-{at})"` 把它变成「相对这一刻」，
                    # 到位时轻微回弹 —— 与强调行滑入同一套手感，零渲染成本。
                    rise = spring_ease(
                        ENTRANCE_SEC, variable=f"(t-{at:.3f})", **spring_kwargs("entrance")
                    )
                    graph.append(
                        f"[{last}][rv{node}]overlay=x=0:"
                        f"y='{layout.subtitle_top}+{ENTRANCE_RISE_PX}*(1-{rise})':"
                        f"enable='gte(t,{at:.3f})'[v{node}]"
                    )
                    last = f"v{node}"

                # ---- 逐句数字强调：紧跟**它自己那一句**的字幕带 ----
                #
                # 比这一句的字幕晚 `CHIP_POP_DELAY` 出现（先读到句子，再看到那个数字被点出来），
                # 而且必须排在条带之后 —— 它是叠在那条带子上的（和声波条同一类顺序问题）。
                # 带子里那个数字**在第几个小句**决定它什么时候能点亮：整句按小句出现时，
                # 还没轮到的小句是透明的，这时候弹一颗空胶囊等于「框里没字」
                # —— 所以 `chip_delay` 由渲染侧按小句时间表算好（见 plan_reveals）。
                chip_start = beat_start + (
                    CHIP_POP_DELAY if band.chip_delay is None else band.chip_delay
                )
                frame_row = (
                    chip_frame_rows[index] if index < len(chip_frame_rows) else []
                )
                # ⚠️ 名字不能叫 `frames`：那个名字在这一层是**本片段的帧数**（下面 -frames:v 要用），
                # 覆盖掉会变成「Expected number for frames:v but found: []」。
                pop_frames = frame_row[beat_index] if beat_index < len(frame_row) else []
                chip = chip_row[beat_index] if beat_index < len(chip_row) else None
                if pop_frames:
                    # 弹簧版：序列自带「弹一下 → 落定」，**不能再叠淡入**（叠了就把
                    # 弹簧糊掉了）。序列自己的时间轴从 0 开始，所以要把它推到
                    # `chip_start` 那一刻：`setpts` 平移 PTS + `enable` 门控。
                    # 序列有限，`overlay` 的 `eof_action` 默认 repeat，
                    # 所以播完就停在末帧（末帧是 scale=1，和静止状态重合）。
                    chip_in = add_sequence(pop_frames, fps=CHIP_POP_FPS)
                    node += 1
                    graph.append(
                        f"[{chip_in}:v]setpts=PTS+{chip_start:.3f}/TB,format=rgba[chp{node}]"
                    )
                    graph.append(
                        f"[{last}][chp{node}]overlay=x=0:y={layout.subtitle_top}:"
                        f"enable='gte(t,{chip_start:.3f})'[v{node}]"
                    )
                    last = f"v{node}"
                    continue
                if chip is None:
                    continue
                chip_in = add_image(chip)
                node += 1
                graph.append(
                    f"[{chip_in}:v]format=rgba,fade=t=in:st={chip_start:.3f}:"
                    f"d={CHIP_POP_SEC:.2f}:alpha=1[chp{node}]"
                )
                graph.append(
                    f"[{last}][chp{node}]overlay=x=0:y={layout.subtitle_top}:"
                    f"enable='gte(t,{chip_start:.3f})'[v{node}]"
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
        # 顺带做**响度归一化**（这一遍是唯一处理音频的地方，不用再多一次转码）。
        # 链条由 `loudness.normalization_chain` 按**实测值**给；测不出来
        # （无音轨 / 数字静音 / 没装 ffmpeg）就整段跳过，退回原来的行为。
        chain = loudness.normalization_chain(audio_path)
        mux = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(silent_path),
            "-i", str(audio_path),
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",   # 让浏览器能边下边播、可拖动
            *(["-af", chain, "-ar", str(loudness.OUTPUT_SAMPLE_RATE)] if chain else []),
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


def capture_poster(video_path: Path, poster_path: Path, *, at: float = 1.2) -> Path | None:
    """把视频某一时刻的画面存成 PNG，用来当 `<video poster>`（封面静帧）。

    **为什么要专门存一张**：视频第一帧上有封面大字标题（那是画进画面的），
    而页面上的 `<video>` 在没播之前显示的是 `poster`。不填 poster 时前端只能拿
    论文首页顶替 —— 那是一张**没有标题的原始 PDF 页**，于是「封面标题」这个东西
    在页面上等于不存在。（用户打开首页说「没有看到」，就是这么来的。）

    抽帧失败不影响出片（它只是张预览图），返回 None。
    """
    if not shutil.which("ffmpeg"):
        return None
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-v", "error", "-y",
                "-ss", f"{max(at, 0.0):.2f}",
                "-i", str(video_path),
                "-frames:v", "1",
                str(poster_path),
            ],
            capture_output=True,
            text=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("封面静帧抽取失败（不影响视频）：%s", exc)
        return None
    if result.returncode != 0 or not poster_path.exists():
        logger.warning("封面静帧抽取失败（不影响视频）：%s", (result.stderr or "").strip()[:200])
        return None
    return poster_path
