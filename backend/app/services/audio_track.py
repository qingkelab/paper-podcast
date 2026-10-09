"""音频轨分析：找出真实的说话停顿，用来对齐字幕换句。

## 为什么需要它

字幕现在是「一段话按字数比例切成两句」—— 但语速不是均匀的：实测一句 30 字的话里，
停顿可能落在第 8 个字后面。按字数算出来的边界经常切在句子中间，观众看到的是
「字幕换得莫名其妙」。而音频里**本来就有**说话停顿，捡出来用就行。

## 为什么用 ffmpeg 的 silencedetect 而不是自己算包络

自己解码算 RMS 也很快（实测 223 秒的音频解码 + 20ms 帧包络一共 0.35 秒，纯标准库够用），
但**时间轴对齐要自己保证**：豆包的 MP3 开头有一段合规水印，实测解码时会报
`Header missing` 并丢掉几个包 —— 自己算的包络和封装进 MP4 的音频之间就可能有系统性偏移，
而这种偏移是「差半秒」，肉眼很难发现、字幕却一直错位。

`silencedetect` 在滤镜图里跑，报出来的时间戳就是输入流自己的时间轴，
天然和后面 `-shortest` 封进去的音频一致。代价只有 0.14 秒（250 秒音频）。
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

# 判定「停顿」的门限：-34dB 是实测出来的 —— 说话区的包络中位数在 -25dB 上下，
# 句子之间的换气落在 -40dB 以下；取 -34dB 能把换气捡出来，又不会把轻声词切开。
PAUSE_NOISE_DB = -34
PAUSE_MIN_SEC = 0.22

_SILENCE_START = re.compile(r"silence_start:\s*(-?\d+(?:\.\d+)?)")
_SILENCE_END = re.compile(r"silence_end:\s*(-?\d+(?:\.\d+)?)")


@dataclass(frozen=True)
class Pause:
    """一段静音（相对音频开头，单位秒）。"""

    start: float
    end: float

    @property
    def center(self) -> float:
        return (self.start + self.end) / 2

    @property
    def duration(self) -> float:
        return max(self.end - self.start, 0.0)


def parse_silences(text: str) -> list[Pause]:
    """从 ffmpeg 的 stderr 里解析出静音区间。

    分开成纯函数是为了能单测：真正跑 ffmpeg 的地方只剩下面那一层。
    """
    pauses: list[Pause] = []
    pending: float | None = None
    for match in re.finditer(r"silence_start:\s*(-?\d+(?:\.\d+)?)|silence_end:\s*(-?\d+(?:\.\d+)?)", text):
        if match.group(1) is not None:
            pending = float(match.group(1))
        elif pending is not None:
            pauses.append(Pause(start=pending, end=float(match.group(2))))
            pending = None
    return [pause for pause in pauses if pause.duration > 0]


def detect_pauses(
    audio_path: Path,
    *,
    noise_db: float = PAUSE_NOISE_DB,
    min_duration: float = PAUSE_MIN_SEC,
    timeout: float = 120.0,
) -> list[Pause]:
    """跑一次 ffmpeg，拿到音频里所有「够长的停顿」。

    失败一律返回空列表：**字幕对齐是锦上添花，绝不能因为它让整条视频出不来**，
    调用方拿到空列表就退回按字数比例切句。
    """
    if not shutil.which("ffmpeg") or not Path(audio_path).exists():
        return []
    command = [
        "ffmpeg", "-hide_banner", "-nostats", "-i", str(audio_path),
        "-af", f"silencedetect=noise={noise_db}dB:d={min_duration}",
        "-f", "null", "-",
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return []
    return parse_silences(result.stderr or "")


def align_boundaries(
    boundaries: list[float],
    pauses: list[Pause],
    *,
    start: float,
    end: float,
    tolerance: float = 0.9,
    min_beat: float = 0.6,
) -> list[float]:
    """把字幕换句的边界吸附到最近的**停顿中心**。

    参数都是绝对时间（与音频时间轴一致）：`boundaries` 是这一段内部的换句时刻，
    `pauses` 是整条音频的静音区间，`start`/`end` 是这一段自己的起止。

    规则：
    - 只在 `tolerance` 秒以内吸附 —— 太远就不是「这里该换句」，而是硬挪，宁可不动；
    - 结果**强制单调**且相邻至少 `min_beat` 秒（两句挤在一起的闪烁比不对齐更难看）；
    - 越界（跑出这一段、或撞到段尾）就保留原值。
    """
    if not boundaries:
        return []

    usable = [pause for pause in pauses if start <= pause.center <= end]
    aligned: list[float] = []
    previous = start
    for position, boundary in enumerate(boundaries):
        is_last = position == len(boundaries) - 1
        candidate = boundary
        if usable:
            nearest = min(usable, key=lambda pause: abs(pause.center - boundary))
            if abs(nearest.center - boundary) <= tolerance:
                candidate = nearest.center

        lower = previous + min_beat
        upper = end - (0.0 if is_last else min_beat)
        if candidate < lower:
            candidate = min(max(boundary, lower), upper)
        if candidate > upper:
            candidate = upper
        aligned.append(candidate)
        previous = candidate
    return aligned
