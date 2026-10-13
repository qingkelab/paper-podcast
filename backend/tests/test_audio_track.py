"""音频轨分析：停顿检测与字幕换句对齐。

这两件事都容易「看起来对、其实差半秒」——所以测的是时间戳本身，
以及「检测失败时必须能退回按字数切句」这条降级路径。
"""

from __future__ import annotations

import math
import struct
import subprocess
import wave
from pathlib import Path

import pytest

from app.services.audio_track import (
    Pause,
    align_boundaries,
    detect_pauses,
    parse_silences,
)
from app.services.video import beat_windows, beat_windows_aligned

FFMPEG = subprocess.run(["which", "ffmpeg"], capture_output=True, text=True).returncode == 0


class TestParseSilences:
    def test_parses_pairs(self):
        stderr = (
            "[silencedetect @ 0x1] silence_start: 1.069167\n"
            "[silencedetect @ 0x1] silence_end: 1.516667 | silence_duration: 0.4475\n"
            "[silencedetect @ 0x1] silence_start: 8.5\n"
        )
        pauses = parse_silences(stderr)
        # 只有 start 没有 end 的那段不算（音频在静音里结束的情况由 duration 兜底）
        assert len(pauses) == 1
        assert pauses[0].start == pytest.approx(1.069167)
        assert pauses[0].center == pytest.approx((1.069167 + 1.516667) / 2)

    def test_ignores_noise(self):
        assert parse_silences("") == []
        assert parse_silences("no silence here") == []


class TestAlignBoundaries:
    def test_snaps_to_nearest_pause_center(self):
        pauses = [Pause(3.0, 3.6)]      # 中心 3.3
        aligned = align_boundaries([3.1], pauses, start=0.0, end=8.0)
        assert aligned == [pytest.approx(3.3)]

    def test_a_far_pause_still_pulls_the_boundary(self):
        """**反过来了**：现在「总是吸到最近的停顿」，不再用「够近才吸」的门槛。

        实测依据（最新一集 2f359258）：旧门槛下 25 个换句边界只有 3 个落在停顿上，
        其余离最近停顿 1.0~3.2 秒 —— 字幕就在人还没停顿的地方换了，观众看到的是
        「字幕和语音对不上」。改成总是吸附之后：**24/25（96%）落在停顿上，中位数 0.00 秒**。

        代价是位置可能偏离「按字数比例」的估算值几秒 —— 但落在一个真实停顿上，
        比精确地落在句子中间要好得多。
        """
        from app.services.audio_track import Pause, align_boundaries

        pauses = [Pause(start=3.0, end=3.4)]  # 中心 3.2，离 10.5 很远
        aligned = align_boundaries([10.5], pauses, start=0.0, end=20.0)
        assert aligned == [3.2], f"应当吸到最近的停顿上：{aligned}"

    def test_no_pause_means_the_estimate_stands(self):
        """没有可用停顿（检测失败 / 这一段真的没停顿）→ 保留按字数比例的估算值。

        这条路径必须永远可用：字幕对齐是锦上添花，不能因为它让出片失败。
        """
        from app.services.audio_track import align_boundaries

        assert align_boundaries([10.5], [], start=0.0, end=20.0) == [10.5]
