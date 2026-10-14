"""成片自查：**画面有没有长时间不动** + **响度够不够** + **音画长度是否对齐**。

为什么要有这一层：这三件事**只有成片里才看得出来**。分层的单帧测试能证明
「字幕会换、动效会动」，但证明不了「整条片子里有 10 秒什么也没发生」——
那是时间轴上的性质，不是某一帧的性质。

## 判据是**标定**出来的，不是拍脑袋定的

拿三条真实成片（`data/videos/*.mp4`）在 2fps、64×36 灰度（面积平均缩放）上逐对抽样，
再用**库里存的段起始时间**当基准（字幕是按段换的，所以段起点就是「该有新东西」的时刻）：

| 「变了多少像素」 | 出现对数（100dce22，447 对） | 这是什么 |
|---|---|---|
| 恰好 0 | 197 | 真的一个像素都没变 |
| < 0.001 | 68 | 进度条在爬 |
| 0.001 ~ 0.003 | 56 | 进度条 + 声波条 + 淡入过渡的中间帧 |
| 0.003 ~ 0.006 | 24 | 同上（这一档噪声和信号是混的） |
| **≥ 0.006** | 102 | **字幕换句 / 强调行 / 换图**（落到段起点 ±0.75 秒内的占 **59%**，随机基准只有 22%） |
| ≥ 0.02 | 65 | 整块换图、片尾卡这种画面级变化 |

所以「有变化」的门槛定在 **0.6% 的像素变了超过 12 个灰阶**：低于它的基本是
进度条与声波条的细碎运动 —— 观众感知不到「这里有新东西」（把门槛放到 0.003
会让事件数从 102 涨到 126，但新增的那些只有 40% 落在段起点附近，就是噪声）。

按这条量出来：三条片子里**分别有 52.5% / 58.0% / 67.7% 的时长落在
「≥4 秒没有任何变化」的空档里**，最长的一段 11.5 / 9.5 / 11.0 秒。
也就是说「每 2–4 秒画面上要有新东西」这条规矩，**我们目前是不达标的**。

空档本身不是 bug（一句 30 字的话本来就要念 10 秒），它是一条**可量的指标**：
先能量，才谈得上改不改、改哪里。报告里给的是「最长的那 3 段 + 时间点」。
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .loudness import Loudness, describe as describe_loudness, ffmpeg_available, measure_loudness
from .video_encode import probe_media_duration

__all__ = [
    "SAMPLE_FPS",
    "SAMPLE_SIZE",
    "CHANGE_DELTA",
    "CHANGE_RATIO",
    "MIN_GAP_SEC",
    "MAX_REPORTED_GAPS",
    "StaticGap",
    "VideoQA",
    "sample_frames",
    "change_ratios",
    "find_static_gaps",
    "qa_video",
]

# 抽样频率。2fps 够：字幕换句是**一次性**的变化（0.25 秒淡入），
# 2fps 一定会抽到它；再密只是让扫描更慢（实测 224 秒的片子扫一遍约 1.7 秒）。
SAMPLE_FPS = 2.0
# 抽样画幅。64×36 一把就能把「换了半行字」变成几千分之一的像素差，
# 而整块换图会直接到 0.02 以上 —— 分辨力够，且逐字节比较在纯 Python 里也够快。
SAMPLE_SIZE = (64, 36)
# 单个像素算「变了」的灰阶差门槛：压掉 H.264 的量化噪声（实测同一张静止画面
# 上相邻采样的差值是 0，不是 1~2，所以这个门槛很宽裕）。
CHANGE_DELTA = 12
# 「这一对采样之间确实发生了可见变化」的像素占比门槛（标定过程见模块开头）。
# 0.003 会把进度条/声波条的噪声也算成变化（事件从 102 涨到 126，新增的多半是噪声），
# 0.02 则会把「只换了一行字幕」漏掉 —— 真实字幕换句量到 0.006~0.02 这一档。
CHANGE_RATIO = 0.006
# 多久没变化算一段空档。取 4 秒是因为这是**上限**（「每 2–4 秒要有新东西」）。
MIN_GAP_SEC = 4.0
# 报告里最多列几段（对齐「最严重的 3 个问题」那条自查规矩）。
MAX_REPORTED_GAPS = 3


@dataclass(frozen=True)
class StaticGap:
    """一段「画面没有可见变化」的时间窗（秒）。"""

    start: float
    end: float

    @property
    def seconds(self) -> float:
        return max(0.0, self.end - self.start)

    def __str__(self) -> str:  # pragma: no cover - 只为日志好看
        return f"{self.start:.1f}~{self.end:.1f}s（{self.seconds:.1f} 秒）"


@dataclass(frozen=True)
class VideoQA:
    """一次成片自查的结果。**每一项都是量出来的**，缺哪项就是 None。"""

    duration_sec: float
    change_events: int = 0
    sampled_pairs: int = 0
    gaps: tuple[StaticGap, ...] = ()
    loudness: Loudness | None = None
    audio_duration_sec: float | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def longest_gap_sec(self) -> float:
        return max((gap.seconds for gap in self.gaps), default=0.0)

    @property
    def gap_share(self) -> float:
        """空档时长占全片的比例（0~1）。"""
        if self.duration_sec <= 0:
            return 0.0
        total = sum(gap.seconds for gap in self.gaps)
        return min(1.0, total / self.duration_sec)

    @property
    def audio_delta_sec(self) -> float | None:
        if self.audio_duration_sec is None:
            return None
        return self.duration_sec - self.audio_duration_sec

    def worst_gaps(self, count: int = MAX_REPORTED_GAPS) -> tuple[StaticGap, ...]:
        return tuple(sorted(self.gaps, key=lambda gap: gap.seconds, reverse=True)[:count])

    def issues(self, count: int = MAX_REPORTED_GAPS) -> list[str]:
        """最严重的几件事，带时间点 —— 照着它去改，不用自己找。"""
        found: list[str] = []
        for gap in self.worst_gaps(count):
            found.append(
                f"{gap.start:.1f}s 起有 {gap.seconds:.1f} 秒画面没有任何可见变化"
            )
        if self.loudness is not None and self.loudness.below_target_lu >= 1.0:
            found.append(
                f"响度 {self.loudness.integrated_lufs:.1f} LUFS，"
                f"离目标还差 {self.loudness.below_target_lu:.1f} LU"
                "（素材峰均差太大，再往上加就削波）"
            )
        delta = self.audio_delta_sec
        if delta is not None and abs(delta) > 0.3:
            found.append(f"音画长度差 {delta:+.2f} 秒（视频 {self.duration_sec:.2f}s）")
        return found[:count]

    def to_dict(self) -> dict:
        payload: dict[str, object] = {
            "duration_sec": round(self.duration_sec, 2),
            "change_events": self.change_events,
            "sampled_pairs": self.sampled_pairs,
            "longest_gap_sec": round(self.longest_gap_sec, 2),
            "gap_share": round(self.gap_share, 3),
            "gaps": [[round(gap.start, 2), round(gap.end, 2)] for gap in self.gaps],
        }
        if self.loudness is not None:
            payload["loudness"] = self.loudness.to_dict()
        if self.audio_duration_sec is not None:
            payload["audio_duration_sec"] = round(self.audio_duration_sec, 2)
        if self.notes:
            payload["notes"] = list(self.notes)
        return payload

    def summary(self) -> str:
        parts = [
            f"画面变化 {self.change_events} 次 / {self.sampled_pairs} 对采样",
            f"最长静止 {self.longest_gap_sec:.1f}s",
            f"静止占比 {self.gap_share * 100:.1f}%",
        ]
        delta = self.audio_delta_sec
        if delta is not None:
            parts.append(f"音画长度差 {delta:+.2f}s")
        return "；".join(parts) + "。" + describe_loudness(self.loudness)


def sample_frames(
    path: Path | str,
    *,
    fps: float = SAMPLE_FPS,
    size: tuple[int, int] = SAMPLE_SIZE,
) -> list[bytes]:
    """把片子解码成一串**缩到 64×36 的灰度帧**（每帧 `w*h` 字节）。

    用 `flags=area` 而不是默认的 bicubic：面积平均对「一条细进度条」的贡献是
    确定的（按面积摊），缩放算法的选择不该影响判据。
    """
    width, height = size
    if not ffmpeg_available():
        return []
    command = [
        "ffmpeg", "-v", "error", "-i", str(path),
        "-vf", f"fps={fps},scale={width}:{height}:flags=area,format=gray",
        "-f", "rawvideo", "-",
    ]
    try:
        result = subprocess.run(command, capture_output=True, timeout=600.0)
    except (OSError, subprocess.SubprocessError):
        return []
    data = result.stdout
    stride = width * height
    count = len(data) // stride
    return [data[index * stride:(index + 1) * stride] for index in range(count)]


def change_ratios(
    frames: list[bytes],
    *,
    delta: int = CHANGE_DELTA,
) -> list[float]:
    """相邻采样之间「变了多少像素」的比例（0~1）。长度为 `len(frames) - 1`。"""
    ratios: list[float] = []
    for previous, current in zip(frames, frames[1:]):
        stride = len(previous)
        if not stride:
            continue
        changed = 0
        for before, after in zip(previous, current):
            if abs(before - after) > delta:
                changed += 1
        ratios.append(changed / stride)
    return ratios


def find_static_gaps(
    ratios: list[float],
    *,
    fps: float = SAMPLE_FPS,
    min_gap: float = MIN_GAP_SEC,
    threshold: float = CHANGE_RATIO,
) -> list[StaticGap]:
    """把「连续多对采样都没变化」的时间窗挑出来。

    时间轴约定：第 i 对采样代表 `(i+1)/fps` 这一刻（它与前一帧比出来的变化）。
    所以时间点的精度是 `1/fps`（0.5 秒）—— 报告里保留一位小数就够，
    别拿它当帧级证据用。
    """
    gaps: list[StaticGap] = []
    step = 1.0 / fps if fps > 0 else 0.0
    run_start: float | None = None
    last_change = 0.0
    for index, ratio in enumerate(ratios):
        moment = (index + 1) * step
        if ratio >= threshold:
            if run_start is not None and moment - run_start >= min_gap:
                gaps.append(StaticGap(run_start, moment))
            run_start = None
            last_change = moment
        elif run_start is None:
            run_start = last_change
    if run_start is not None and ratios:
        end = len(ratios) * step
        if end - run_start >= min_gap:
            gaps.append(StaticGap(run_start, end))
    return gaps


def qa_video(
    video_path: Path | str,
    *,
    audio_path: Path | str | None = None,
) -> VideoQA:
    """跑一次完整自查。**绝不抛异常** —— 自查失败不该让出片失败。

    `audio_path` 只用来量**长度**（「视频长度 == 音频长度」那条不变量）。
    **响度一律量成片本身**：成片里的音轨是过了两遍 `loudnorm` 的，和源音频不是一个数 ——
    实测 `100dce22` 源音频 −18.1 LUFS、成片 −15.7 LUFS，量源音频就会把
    「归一化有没有生效」这件事整个报错方向（数字好看，片子没动过一样看不出来）。
    """
    path = Path(video_path)
    notes: list[str] = []
    if not path.exists():
        return VideoQA(duration_sec=0.0, notes=("视频文件不存在",))
    frames = sample_frames(path)
    if not frames:
        notes.append("抽不到帧（ffmpeg 不可用或文件损坏），静止空档没测")
    ratios = change_ratios(frames)
    duration = probe_media_duration(path) or (len(frames) / SAMPLE_FPS)
    # 见 docstring：量成片，不量源音频
    loudness = measure_loudness(path)
    if loudness is None:
        notes.append("响度测不出来（无音轨或数字静音）")
    audio_duration = None
    if audio_path is not None:
        audio_duration = probe_media_duration(Path(audio_path))
    return VideoQA(
        duration_sec=duration or len(frames) / SAMPLE_FPS,
        change_events=sum(1 for ratio in ratios if ratio >= CHANGE_RATIO),
        sampled_pairs=len(ratios),
        gaps=tuple(find_static_gaps(ratios)),
        loudness=loudness,
        audio_duration_sec=audio_duration,
        notes=tuple(notes),
    )


def main(argv: list[str] | None = None) -> int:
    """命令行自查：`python -m app.services.video_qa <mp4> [音频文件]`。"""
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("用法：python -m app.services.video_qa <视频.mp4> [音频]")
        return 2
    report = qa_video(args[0], audio_path=args[1] if len(args) > 1 else None)
    print(report.summary())
    for index, issue in enumerate(report.issues(), start=1):
        print(f"  {index}. {issue}")
    for note in report.notes:
        print(f"  · {note}")
    return 0


if __name__ == "__main__":  # pragma: no cover - 维护脚本入口
    raise SystemExit(main())
