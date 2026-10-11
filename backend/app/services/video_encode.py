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

import logging
import shutil
import subprocess
from pathlib import Path

from .audio_track import Pause

# 版式/配色/节奏令牌的**老家**在 design.py（video.py 只是把它们再导出给老调用点）
from .design import (
    CAPTION_FADE_SEC,
    FOCUS_FADE_SEC,
    FPS,
    POINT_SLIDE_SEC,
    PORTRAIT,
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
    # Tier B：某些段的聚光灯是**逐帧抓下来的帧序列**（而不是一张静态图）。
    # 与 foci 互斥 —— 同一段要么叠静态图，要么叠序列。
    focus_anims = focus_animations or [[] for _ in scenes]
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

            def add_sequence(paths: list[Path]) -> int:
                """挂一路图片序列（Tier B 逐帧抓下来的动画帧）。

                序列是**有限**的：播完就该停在最后一帧，而 `overlay` 的
                `eof_action` 默认正是 `repeat` —— 所以「先动一段、之后定住」
                不用额外做什么，片段总长仍由 `-frames:v` 说了算（长度对齐不受影响）。
                """
                nonlocal counter
                first = paths[0]
                pattern = first.parent / f"{first.stem.rsplit('-', 1)[0]}-%04d.png"
                inputs.extend(["-framerate", str(FPS), "-i", str(pattern)])
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
